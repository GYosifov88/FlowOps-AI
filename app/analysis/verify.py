"""Server-side checks on a model's analysis (V2-M3). Pure: no database, no network, no clock.

The model is not trusted to be right about its own evidence, so FlowOps checks it against the
exact context that was sent:

1. A `ref` is a path from the context root ("untrusted.error.message",
   "trusted.occurrences.recent[0].mode"). It must resolve to a SCALAR value in that context.
2. The `quote` must appear in that value (case- and whitespace-insensitive), and be either at
   least MIN_QUOTE_CHARS long or the whole value -- so "a" cannot "prove" anything.
3. A FACT without a single valid evidence item is REMOVED. A HYPOTHESIS without one is KEPT but
   marked `supported=False`. Invalid evidence items are dropped from both.
4. Confidence ceiling: `high` needs at least two valid evidence items with DISTINCT refs,
   otherwise it becomes `medium`.
5. At most MAX_HYPOTHESES hypotheses are kept (the first ones; the prompt asks for most likely
   first). Long texts and lists are capped.

Everything that was changed is explained in `notes`, which are stored with the analysis.
Nothing here decides whether the model's conclusion is TRUE -- only whether what it cites is
really in the data. `insufficient_evidence` is passed through exactly as the model gave it
(the eval measures the model on it; forcing it here would hide a weak model).
"""
import re
from dataclasses import dataclass

from app.analysis.schema import (
    AnalysisOutput,
    Evidence,
    Fact,
    RecommendedAction,
    CheckedAnalysis,
    CheckedHypothesis,
)

MAX_HYPOTHESES = 3
MIN_QUOTE_CHARS = 8
HIGH_NEEDS_DISTINCT_REFS = 2
TEXT_CAP = 1000
QUOTE_CAP = 300
LIST_CAP = 10

_REF = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(\[\d{1,3}\])*(\.[A-Za-z_][A-Za-z0-9_]*(\[\d{1,3}\])*)*$"
)
_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


def resolve_ref(context: dict, ref: object) -> tuple[bool, object]:
    """(True, value) when `ref` is a well-formed path to a scalar in `context`, else (False, None).

    Only dict keys and list indexes are followed (never attributes), so no ref can reach
    anything outside the context. A path ending at a dict, a list or null is not evidence.
    """
    if not isinstance(ref, str) or len(ref) > 200 or not _REF.match(ref):
        return False, None
    node: object = context
    for match in _TOKEN.finditer(ref):
        key, index = match.group(1), match.group(2)
        if key is not None:
            if not isinstance(node, dict) or key not in node:
                return False, None
            node = node[key]
        else:
            if not isinstance(node, list) or int(index) >= len(node):
                return False, None
            node = node[int(index)]
    if node is None or isinstance(node, (dict, list)):
        return False, None
    return True, node


def _norm(value: object) -> str:
    return " ".join(str(value).split()).lower()


def check_evidence(context: dict, evidence: Evidence) -> str | None:
    """None when the evidence is valid, otherwise a short reason."""
    found, value = resolve_ref(context, evidence.ref)
    if not found:
        return "ref does not point to a value in the context"
    quote, target = _norm(evidence.quote), _norm(value)
    if not quote:
        return "empty quote"
    if quote not in target:
        return "quote not found in the value"
    if len(quote) < MIN_QUOTE_CHARS and quote != target:
        return f"quote shorter than {MIN_QUOTE_CHARS} characters and not the whole value"
    return None


def _clip(text: str, cap: int = TEXT_CAP) -> str:
    return text.strip()[:cap]


def _clip_list(items: list[str], where: str, notes: list[str]) -> list[str]:
    if len(items) > LIST_CAP:
        notes.append(f"{where}: kept the first {LIST_CAP} of {len(items)} items")
    return [_clip(item) for item in items[:LIST_CAP]]


def _valid_evidence(context: dict, items: list[Evidence], where: str, notes: list[str]) -> list[Evidence]:
    valid: list[Evidence] = []
    for j, item in enumerate(items):
        reason = check_evidence(context, item)
        if reason is not None:
            notes.append(f"{where} evidence[{j}] dropped ({reason}): ref={item.ref[:80]!r}")
            continue
        cleaned = Evidence(ref=item.ref.strip(), quote=_clip(item.quote, QUOTE_CAP))
        if cleaned not in valid:
            valid.append(cleaned)
    return valid


@dataclass(frozen=True)
class VerifyStats:
    facts_in: int
    facts_kept: int
    hypotheses_in: int
    hypotheses_kept: int
    hypotheses_unsupported: int


@dataclass(frozen=True)
class VerifyResult:
    analysis: CheckedAnalysis
    notes: list[str]
    stats: VerifyStats


def verify_analysis(output: AnalysisOutput, context: dict) -> VerifyResult:
    notes: list[str] = []

    facts: list[Fact] = []
    for i, fact in enumerate(output.facts):
        evidence = _valid_evidence(context, fact.evidence, f"fact[{i}]", notes)
        if not evidence:
            notes.append(f"fact[{i}] removed: no valid evidence")
            continue
        facts.append(Fact(statement=_clip(fact.statement), evidence=evidence))

    if len(output.hypotheses) > MAX_HYPOTHESES:
        notes.append(f"{len(output.hypotheses)} hypotheses returned; kept the first {MAX_HYPOTHESES}")
    hypotheses: list[CheckedHypothesis] = []
    for i, hypothesis in enumerate(output.hypotheses[:MAX_HYPOTHESES]):
        evidence = _valid_evidence(context, hypothesis.supporting_evidence, f"hypothesis[{i}]", notes)
        supported = bool(evidence)
        if not supported:
            notes.append(f"hypothesis[{i}] has no valid evidence: kept, marked unsupported")
        confidence = hypothesis.confidence
        if confidence == "high" and len({e.ref for e in evidence}) < HIGH_NEEDS_DISTINCT_REFS:
            confidence = "medium"
            notes.append(
                f"hypothesis[{i}] confidence lowered from high to medium: "
                f"high needs at least {HIGH_NEEDS_DISTINCT_REFS} valid evidence items with distinct refs"
            )
        hypotheses.append(
            CheckedHypothesis(
                cause=_clip(hypothesis.cause),
                confidence=confidence,
                supporting_evidence=evidence,
                would_confirm=_clip_list(hypothesis.would_confirm, f"hypothesis[{i}].would_confirm", notes),
                verification_steps=_clip_list(
                    hypothesis.verification_steps, f"hypothesis[{i}].verification_steps", notes
                ),
                supported=supported,
            )
        )

    if len(output.recommended_actions) > LIST_CAP:
        notes.append(f"recommended_actions: kept the first {LIST_CAP} of {len(output.recommended_actions)}")
    actions = [
        RecommendedAction(step=_clip(a.step), risk=a.risk) for a in output.recommended_actions[:LIST_CAP]
    ]

    analysis = CheckedAnalysis(
        summary=_clip(output.summary),
        category=output.category,
        facts=facts,
        hypotheses=hypotheses,
        insufficient_evidence=output.insufficient_evidence,
        recommended_actions=actions,
    )
    stats = VerifyStats(
        facts_in=len(output.facts),
        facts_kept=len(facts),
        hypotheses_in=len(output.hypotheses),
        hypotheses_kept=len(hypotheses),
        hypotheses_unsupported=sum(1 for h in hypotheses if not h.supported),
    )
    return VerifyResult(analysis, notes, stats)
