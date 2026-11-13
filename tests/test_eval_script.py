"""scripts/eval_analysis: the dry-run quote, trap scoring, cost and exit codes. Offline."""
import pytest

import scripts.eval_analysis as eval_script
from app.analysis.analyzer import AnalyzerResult
from app.analysis.schema import Fact, Hypothesis
from scripts.eval_analysis import (
    ASSUMED_OUTPUT_TOKENS,
    PRICES,
    build_eval_cases,
    cost_of,
    quote,
    run_eval,
    score_case,
)
from tests.analysis_helpers import FakeAnalyzer, ev, good_output, ok_result, trap_output
from tests.corpus_helpers import load_corpus

LUNA = PRICES["gpt-6-luna"]


@pytest.fixture(scope="module")
def cases():
    return build_eval_cases()


def label_of(cases):
    return {c.context["trusted"]["incident"]["signature"]: c for c in cases}


def perfect(cases):
    """A model that answers every case correctly, citing the real normalized message."""
    labels = label_of(cases)

    def answer(context):
        case = labels[context["trusted"]["incident"]["signature"]]
        if case.trap:
            return ok_result(trap_output(category=case.category))
        message = context["untrusted"]["normalized_message"]
        return ok_result(good_output(
            category=case.category,
            facts=[Fact(statement="The error text.", evidence=[ev("untrusted.normalized_message", message)])],
            hypotheses=[Hypothesis(cause="A cause.", confidence="low",
                                   supporting_evidence=[ev("untrusted.normalized_message", message)],
                                   would_confirm=["x"], verification_steps=["y"])],
        ))

    return answer


# ====================================================================== the cases


def test_one_case_per_corpus_group_with_a_context(cases):
    corpus = load_corpus()
    assert {c.group for c in cases} == {c["group"] for c in corpus["cases"]}
    assert len(cases) == len({c["group"] for c in corpus["cases"]})
    assert any(c.trap for c in cases) and not all(c.trap for c in cases)
    assert all(set(c.context) == {"context_version", "trusted", "untrusted"} for c in cases)


# ====================================================================== dry run: nothing is sent


def test_the_default_run_only_quotes(cases, capsys):
    def must_not_be_called(model):
        raise AssertionError("the dry run must not create an analyzer")

    assert eval_script.main([], analyzer_factory=must_not_be_called) == 0

    out = capsys.readouterr().out
    assert "Eval quote for model gpt-6-luna" in out and "Nothing was sent" in out
    assert f"{len(cases)} incidents" in out and "estimated cost" in out and "$0.10/1M input" in out
    assert out.isascii()


def test_the_quote_is_a_fraction_of_a_cent_per_incident_on_luna(cases):
    tokens_in, cost = quote(cases, LUNA)
    assert 0 < cost < 0.1 and tokens_in > 0
    assert cost == pytest.approx((tokens_in * 0.10 + ASSUMED_OUTPUT_TOKENS * len(cases) * 0.50) / 1e6)
    assert quote(cases, PRICES["gpt-6-sol"])[1] > cost * 10


def test_an_unknown_model_needs_its_price(capsys):
    assert eval_script.main(["--model", "some-future-model"]) == 0
    out = capsys.readouterr().out
    assert "unknown model" in out and "estimated cost  : ~unknown" in out

    assert eval_script.main(["--model", "some-future-model", "--price-input", "1", "--price-output", "4"]) == 0
    assert "$1.00/1M input, $4.00/1M output" in capsys.readouterr().out


def test_bad_arguments_are_a_configuration_error(capsys):
    assert eval_script.main(["--price-input", "1"]) == 2
    assert eval_script.main(["--reasoning-effort", "turbo"]) == 2
    assert "Configuration problem" in capsys.readouterr().out


def test_running_for_real_without_a_key_is_refused_before_any_work(capsys):
    assert eval_script.main(["--yes"]) == 2
    assert "OPENAI_API_KEY is not set" in capsys.readouterr().out


# ====================================================================== scoring


def trap_case(cases):
    return next(c for c in cases if c.trap)


def plain_case(cases):
    return next(c for c in cases if not c.trap)


def test_a_trap_passes_only_with_insufficient_evidence_and_no_high_confidence(cases):
    case = trap_case(cases)
    passing = score_case(case, ok_result(trap_output()), LUNA)
    assert passing.trap_pass is True

    confident = score_case(case, ok_result(trap_output(insufficient_evidence=False)), LUNA)
    assert confident.trap_pass is False

    high = Hypothesis(cause="Surely X.", confidence="high", supporting_evidence=[], would_confirm=[], verification_steps=[])
    overconfident = score_case(case, ok_result(trap_output(hypotheses=[high])), LUNA)
    assert overconfident.trap_pass is False  # the model's own claim counts, even though the server would cap it

    low = Hypothesis(cause="Maybe X.", confidence="low", supporting_evidence=[], would_confirm=[], verification_steps=[])
    assert score_case(case, ok_result(trap_output(hypotheses=[low])), LUNA).trap_pass is True


@pytest.mark.parametrize("status", ["refused", "incomplete", "invalid", "error"])
def test_no_usable_answer_on_a_trap_is_a_failure(cases, status):
    assert score_case(trap_case(cases), AnalyzerResult(status), LUNA).trap_pass is False


def test_non_trap_cases_have_no_trap_verdict_and_wrong_answers_count(cases):
    case = plain_case(cases)
    right = score_case(case, ok_result(good_output(category=case.category)), LUNA)
    wrong = score_case(case, ok_result(good_output(category="unknown" if case.category != "unknown" else "timeout")), LUNA)
    failed = score_case(case, AnalyzerResult("error"), LUNA)
    assert (right.category_ok, wrong.category_ok, failed.category_ok) == (True, False, False)
    assert right.trap_pass is None


def test_cost_comes_from_the_tokens_actually_used(cases):
    score = score_case(plain_case(cases), ok_result(good_output(), input_tokens=1500, output_tokens=2500), LUNA)
    assert score.cost == pytest.approx(0.0014)
    assert cost_of(None, 1, 1) is None and cost_of(LUNA, None, 5) is None


# ====================================================================== whole runs


def test_a_perfect_model_passes_everything(cases, capsys):
    report = run_eval(cases, FakeAnalyzer(perfect(cases)), LUNA)

    assert report.traps_failed == [] and report.category_accuracy == 1.0
    assert report.facts_removed_ratio == 0 and report.step_up_reasons() == []
    assert report.cost == pytest.approx(len(cases) * 0.0014)
    eval_script.print_report(report, "gpt-6-luna")
    out = capsys.readouterr().out
    assert "No quality gap" in out and "FAIL" not in out and out.isascii()


def test_main_exits_1_when_a_trap_fails_and_recommends_a_bigger_model(cases, capsys):
    labels = label_of(cases)
    good = perfect(cases)

    def overconfident(context):
        if labels[context["trusted"]["incident"]["signature"]].trap:
            return ok_result(trap_output(insufficient_evidence=False))
        return good(context)

    code = eval_script.main(["--yes"], analyzer_factory=lambda model: FakeAnalyzer(overconfident))

    out = capsys.readouterr().out
    assert code == 1 and "FAIL" in out and "trap case(s) failed" in out and "gpt-6-sol" in out


def test_main_exits_0_for_a_perfect_run(capsys):
    cases = build_eval_cases()
    code = eval_script.main(["--yes"], analyzer_factory=lambda model: FakeAnalyzer(perfect(cases)))
    assert code == 0 and "Running..." in capsys.readouterr().out


def test_wrong_categories_and_invented_evidence_trigger_the_step_up_rule(cases):
    def sloppy(context):
        return ok_result(good_output(category="rate_limit"))  # same answer, real-looking but mostly wrong evidence

    report = run_eval(cases, FakeAnalyzer(sloppy), LUNA)
    reasons = " ".join(report.step_up_reasons())
    assert "category accuracy" in reasons and "failed verification" in reasons


def test_the_model_given_to_the_factory_is_the_requested_one(cases):
    seen = []
    eval_script.main(["--yes", "--model", "gpt-6-sol"],
                     analyzer_factory=lambda model: seen.append(model) or FakeAnalyzer(perfect(cases)))
    assert seen == ["gpt-6-sol"]
