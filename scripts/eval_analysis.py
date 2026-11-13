"""Evaluate the incident analysis against the labelled corpus. OPT-IN, and it costs money.

    python -m scripts.eval_analysis                     quote only: what it WOULD cost; sends nothing
    python -m scripts.eval_analysis --yes               run it for real (needs OPENAI_API_KEY)
    python -m scripts.eval_analysis --yes --model gpt-6-sol     compare a bigger model

The corpus (tests/corpus/incidents.json, synthetic or scrubbed) is loaded into a TEMPORARY
database, never flowops.db, and each incident's context is built exactly as the real analysis
builds it (scrubbed). Only after --yes does anything leave your machine.

Per case it reports the status, whether the category matches the label, how many facts the
server's evidence check removed, unsupported hypotheses, and tokens / cost. TRAP cases (the
data cannot support a cause) pass only if the model sets insufficient_evidence AND gives no
`high` confidence hypothesis.

"Start cheap, move up only on evidence": a bigger model is suggested when any trap fails,
category accuracy is under 80%, or more than 15% of the facts failed verification.
Exit codes: 0 ok, 1 a trap case failed, 2 configuration problem.
"""
import argparse
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from app.analysis.analyzer import Analyzer, AnalyzerResult, OpenAIAnalyzer
from app.analysis.prompt import SYSTEM_PROMPT
from app.analysis.verify import verify_analysis
from app.config import OPENAI_REASONING_EFFORTS, get_settings
from app.database import create_db_engine, init_db
from app.models import ExecutionEvent
from app.services.incident_context import build_context, frame_context

# USD per 1M tokens (input, output) -- OpenAI changelog, 22 Sep 2026. Pass --price-input and
# --price-output for any other model; check https://developers.openai.com/api/docs/pricing first.
PRICES = {"gpt-6-luna": (0.10, 0.50), "gpt-6-sol": (2.00, 10.00)}
ASSUMED_OUTPUT_TOKENS = 3000  # reasoning + the answer; real runs report the actual numbers
CHARS_PER_TOKEN = 4  # rough; the quote is an estimate, not a promise

MIN_CATEGORY_ACCURACY = 0.80
MAX_FACTS_REMOVED = 0.15


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    group: str
    category: str  # the label
    trap: bool
    context: dict


@dataclass(frozen=True)
class CaseScore:
    case: EvalCase
    status: str
    category_ok: bool
    trap_pass: bool | None  # None for non-trap cases
    facts_in: int
    facts_kept: int
    hypotheses_unsupported: int
    input_tokens: int | None
    output_tokens: int | None
    cost: float | None


@dataclass(frozen=True)
class EvalReport:
    scores: list[CaseScore]
    cost: float | None

    @property
    def traps_failed(self) -> list[CaseScore]:
        return [s for s in self.scores if s.trap_pass is False]

    @property
    def category_accuracy(self) -> float:
        return sum(s.category_ok for s in self.scores) / len(self.scores) if self.scores else 0.0

    @property
    def facts_removed_ratio(self) -> float:
        total = sum(s.facts_in for s in self.scores)
        return 1 - sum(s.facts_kept for s in self.scores) / total if total else 0.0

    def step_up_reasons(self) -> list[str]:
        reasons = []
        if self.traps_failed:
            reasons.append(f"{len(self.traps_failed)} trap case(s) failed")
        if self.category_accuracy < MIN_CATEGORY_ACCURACY:
            reasons.append(f"category accuracy {self.category_accuracy:.0%} < {MIN_CATEGORY_ACCURACY:.0%}")
        if self.facts_removed_ratio > MAX_FACTS_REMOVED:
            reasons.append(f"{self.facts_removed_ratio:.0%} of facts failed verification (> {MAX_FACTS_REMOVED:.0%})")
        return reasons


# ------------------------------------------------------------------ building the cases


def build_eval_cases() -> list[EvalCase]:
    """One case per corpus group, with its context built from a temporary database."""
    # The corpus loader lives with the tests; the eval deliberately uses the same code path.
    from tests.corpus_helpers import ingest_corpus, load_corpus

    corpus = load_corpus()
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_db_engine(f"sqlite:///{(Path(tmp) / 'eval.db').as_posix()}")
        try:
            init_db(engine)
            with sessionmaker(bind=engine)() as session:
                _, event_ids = ingest_corpus(session, corpus)
                cases, seen = [], set()
                for case in corpus["cases"]:
                    incident_id = session.get(ExecutionEvent, event_ids[case["id"]]).incident_id
                    if incident_id in seen:
                        continue
                    seen.add(incident_id)
                    cases.append(
                        EvalCase(
                            case_id=case["id"],
                            group=case["group"],
                            category=case["category"],
                            trap=case["trap"],
                            context=build_context(session, incident_id),
                        )
                    )
        finally:
            engine.dispose()  # Windows cannot delete the file otherwise
    return cases


# ------------------------------------------------------------------ cost


def estimated_input_tokens(context: dict) -> int:
    return (len(SYSTEM_PROMPT) + len(frame_context(context))) // CHARS_PER_TOKEN


def cost_of(price: tuple[float, float] | None, input_tokens: int | None, output_tokens: int | None) -> float | None:
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def quote(cases: list[EvalCase], price: tuple[float, float] | None) -> tuple[int, float | None]:
    """(estimated input tokens in total, estimated cost or None when the price is unknown)."""
    tokens_in = sum(estimated_input_tokens(c.context) for c in cases)
    return tokens_in, cost_of(price, tokens_in, ASSUMED_OUTPUT_TOKENS * len(cases))


# ------------------------------------------------------------------ scoring


def score_case(case: EvalCase, result: AnalyzerResult, price: tuple[float, float] | None) -> CaseScore:
    facts_in = facts_kept = unsupported = 0
    category_ok = False
    trap_pass: bool | None = None
    if result.status == "ok" and result.output is not None:
        verified = verify_analysis(result.output, case.context)
        facts_in, facts_kept = verified.stats.facts_in, verified.stats.facts_kept
        unsupported = verified.stats.hypotheses_unsupported
        category_ok = result.output.category == case.category
        if case.trap:
            claimed_high = any(h.confidence == "high" for h in result.output.hypotheses)
            trap_pass = result.output.insufficient_evidence is True and not claimed_high
    elif case.trap:
        trap_pass = False  # no usable answer is not a pass
    return CaseScore(
        case=case,
        status=result.status,
        category_ok=category_ok,
        trap_pass=trap_pass,
        facts_in=facts_in,
        facts_kept=facts_kept,
        hypotheses_unsupported=unsupported,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost=cost_of(price, result.input_tokens, result.output_tokens),
    )


def run_eval(cases: list[EvalCase], analyzer: Analyzer, price: tuple[float, float] | None) -> EvalReport:
    scores = [score_case(case, analyzer.analyze(case.context), price) for case in cases]
    costs = [s.cost for s in scores]
    return EvalReport(scores, sum(costs) if scores and all(c is not None for c in costs) else None)


# ------------------------------------------------------------------ output (ASCII only: Windows consoles)


def _money(value: float | None) -> str:
    return "unknown" if value is None else f"${value:.4f}"


def print_quote(cases: list[EvalCase], model: str, price) -> None:
    tokens_in, cost = quote(cases, price)
    print(f"Eval quote for model {model}: {len(cases)} incidents (one per corpus group)")
    print(f"  estimated input : ~{tokens_in:,} tokens (about {CHARS_PER_TOKEN} characters per token)")
    print(f"  assumed output  : {ASSUMED_OUTPUT_TOKENS:,} tokens per incident, reasoning included")
    if price:
        print(f"  price           : ${price[0]:.2f}/1M input, ${price[1]:.2f}/1M output")
    else:
        print("  price           : unknown model; pass --price-input and --price-output (USD per 1M tokens)")
    print(f"  estimated cost  : ~{_money(cost)}")
    print("Nothing was sent. Run again with --yes to spend it.")


def print_report(report: EvalReport, model: str) -> None:
    print(f"Eval results for model {model}")
    print(f"{'case':<6}{'group':<24}{'status':<11}{'category':<10}{'trap':<6}{'facts':<8}{'unsup':<6}{'tokens in/out':<16}cost")
    for s in report.scores:
        trap = "-" if s.trap_pass is None else ("PASS" if s.trap_pass else "FAIL")
        category = "ok" if s.category_ok else "WRONG"
        tokens = f"{s.input_tokens}/{s.output_tokens}"
        print(
            f"{s.case.case_id:<6}{s.case.group[:23]:<24}{s.status:<11}{category:<10}{trap:<6}"
            f"{s.facts_kept}/{s.facts_in:<6}{s.hypotheses_unsupported:<6}{tokens:<16}{_money(s.cost)}"
        )
    print(
        f"Category accuracy {report.category_accuracy:.0%}; facts removed by verification "
        f"{report.facts_removed_ratio:.0%}; traps failed {len(report.traps_failed)}; total cost {_money(report.cost)}"
    )
    reasons = report.step_up_reasons()
    if reasons:
        print("Consider a bigger model (--model gpt-6-sol): " + "; ".join(reasons))
    else:
        print("No quality gap against the thresholds; the current model is enough.")


# ------------------------------------------------------------------ CLI


def _default_factory(settings, effort: str, max_output_tokens: int):
    def make(model: str) -> Analyzer:
        return OpenAIAnalyzer(
            api_key=settings.openai_api_key,
            model=model,
            reasoning_effort=effort,
            max_output_tokens=max_output_tokens,
            timeout=settings.openai_timeout_seconds,
        )

    return make


def main(argv=None, *, analyzer_factory=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.eval_analysis", description=__doc__.split("\n")[0])
    parser.add_argument("--yes", action="store_true", help="really call the model (costs money)")
    parser.add_argument("--model", help="default: OPENAI_MODEL")
    parser.add_argument("--reasoning-effort", help="none|low|medium|high|xhigh|max (default: OPENAI_REASONING_EFFORT)")
    parser.add_argument("--max-output-tokens", type=int, help="default: OPENAI_MAX_OUTPUT_TOKENS")
    parser.add_argument("--price-input", type=float, help="USD per 1M input tokens (for models not in the table)")
    parser.add_argument("--price-output", type=float, help="USD per 1M output tokens")
    args = parser.parse_args(argv)

    settings = get_settings()
    model = args.model or settings.openai_model
    effort = (args.reasoning_effort if args.reasoning_effort is not None else settings.openai_reasoning_effort).lower()
    max_tokens = args.max_output_tokens or settings.openai_max_output_tokens
    if effort and effort not in OPENAI_REASONING_EFFORTS:
        print(f"Configuration problem: reasoning effort must be one of {sorted(OPENAI_REASONING_EFFORTS)}")
        return 2
    if (args.price_input is None) != (args.price_output is None):
        print("Configuration problem: give both --price-input and --price-output, or neither")
        return 2
    price = (args.price_input, args.price_output) if args.price_input is not None else PRICES.get(model)

    cases = build_eval_cases()
    if not args.yes:
        print_quote(cases, model, price)
        return 0

    factory = analyzer_factory or _default_factory(settings, effort, max_tokens)
    if analyzer_factory is None and not settings.openai_api_key:
        print("Configuration problem: OPENAI_API_KEY is not set (see .env.example)")
        return 2
    print_quote(cases, model, price)
    print("Running...")
    report = run_eval(cases, factory(model), price)
    print_report(report, model)
    return 1 if report.traps_failed else 0


if __name__ == "__main__":
    sys.exit(main())
