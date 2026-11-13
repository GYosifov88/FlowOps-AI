"""The model-agnostic anti-hallucination layers: the schema contract and server-side verification."""
import pytest

from app.analysis.schema import (
    AnalysisOutput,
    Fact,
    Hypothesis,
    RecommendedAction,
    CheckedAnalysis,
)
from app.analysis.verify import (
    MAX_HYPOTHESES,
    check_evidence,
    resolve_ref,
    verify_analysis,
)
from tests.analysis_helpers import ev, good_output, trap_output

CONTEXT = {
    "context_version": 1,
    "trusted": {
        "occurrences": {"count": 3, "recent": [{"mode": "webhook"}, {"mode": "trigger"}]},
        "facts": {"last_success_before_first_failure": "2026-11-01T08:00:00Z", "successes_after_last_failure": 0},
        "error_detail_available": True,
    },
    "untrusted": {
        "node_name": "SHEETS — Load Settings",
        "error": {
            "message": 'The credential "Example-marketing" needs   to be reconnected.',
            "description": None,
            "stack": ["NodeApiError: x", "at apiRequest (index.ts:80:9)"],
        },
    },
}
MESSAGE = "untrusted.error.message"


def fact(statement, *evidence):
    return Fact(statement=statement, evidence=list(evidence))


def hyp(confidence="medium", *evidence, cause="Some cause."):
    return Hypothesis(cause=cause, confidence=confidence, supporting_evidence=list(evidence),
                      would_confirm=["check x"], verification_steps=["do y"])


def run(**overrides):
    return verify_analysis(good_output(**overrides), CONTEXT)


# ====================================================================== the schema contract


def property_names(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "properties":
                yield from value
            yield from property_names(value)
    elif isinstance(node, list):
        for item in node:
            yield from property_names(item)


FORBIDDEN = {"verified", "root_cause", "rootcause", "confirmed", "proven", "certain", "action", "tool",
             "tools", "command", "execute", "run", "fix", "apply", "probability", "percent", "score"}


@pytest.mark.parametrize("model", [AnalysisOutput, CheckedAnalysis], ids=lambda m: m.__name__)
def test_no_field_can_claim_a_verified_cause_or_ask_for_an_action(model):
    names = set(property_names(model.model_json_schema()))
    assert names and not (names & FORBIDDEN), names & FORBIDDEN


def test_the_only_free_form_action_field_is_a_suggestion_for_a_human():
    action = RecommendedAction.model_json_schema()
    assert set(action["properties"]) == {"step", "risk"}


def test_confidence_is_a_three_value_category_not_a_number():
    schema = Hypothesis.model_json_schema()["properties"]["confidence"]
    assert schema["enum"] == ["low", "medium", "high"]


def test_the_schema_is_acceptable_to_openai_strict_mode():
    from openai.lib._pydantic import to_strict_json_schema

    def walk(node, path="$"):
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                assert node.get("additionalProperties") is False, path
                assert sorted(node["properties"]) == sorted(node["required"]), path
            for key, value in node.items():
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{path}[{i}]")

    walk(to_strict_json_schema(AnalysisOutput))


def test_unknown_fields_are_rejected():
    with pytest.raises(ValueError):
        AnalysisOutput.model_validate({**good_output().model_dump(), "verified": True})


# ====================================================================== references


@pytest.mark.parametrize("ref, expected", [
    ("untrusted.node_name", "SHEETS — Load Settings"),
    ("trusted.occurrences.count", 3),
    ("trusted.occurrences.recent[0].mode", "webhook"),
    ("trusted.occurrences.recent[1].mode", "trigger"),
    ("untrusted.error.stack[1]", "at apiRequest (index.ts:80:9)"),
    ("trusted.facts.successes_after_last_failure", 0),
    ("trusted.error_detail_available", True),
])
def test_valid_refs_resolve_to_their_value(ref, expected):
    assert resolve_ref(CONTEXT, ref) == (True, expected)


@pytest.mark.parametrize("ref", [
    "", "nothing", "trusted.nothing", "trusted.occurrences.recent[5].mode", "trusted.occurrences.recent[x]",
    "trusted.occurrences.count.deeper",                  # through a scalar
    "trusted.occurrences",                               # ends at an object
    "trusted.occurrences.recent",                        # ends at a list
    "untrusted.error.description",                       # null is not evidence
    "untrusted..node_name", ".untrusted", "untrusted.", "untrusted.node_name[0]", "untrusted[0]",
    "__class__", "untrusted.__class__", "untrusted.__dict__", "trusted.occurrences.count.__class__",
    "untrusted.node name", "untrusted.node_name; DROP", "a" * 300, None, 5, ["untrusted.node_name"],
])
def test_bad_refs_never_resolve_and_never_raise(ref):
    assert resolve_ref(CONTEXT, ref) == (False, None)


# ====================================================================== quotes


def test_a_quote_must_appear_in_the_value_ignoring_case_and_whitespace():
    assert check_evidence(CONTEXT, ev(MESSAGE, "NEEDS TO BE RECONNECTED")) is None
    assert check_evidence(CONTEXT, ev(MESSAGE, 'credential "Example-marketing" needs to')) is None  # runs of spaces collapse


def test_a_quote_that_is_not_in_the_value_is_rejected():
    assert check_evidence(CONTEXT, ev(MESSAGE, "the refresh token expired")) is not None
    assert check_evidence(CONTEXT, ev("untrusted.node_name", "HTTP — Fetch Orders")) is not None


def test_empty_and_tiny_quotes_prove_nothing_unless_they_are_the_whole_value():
    assert check_evidence(CONTEXT, ev(MESSAGE, "")) is not None
    assert check_evidence(CONTEXT, ev(MESSAGE, "   ")) is not None
    assert check_evidence(CONTEXT, ev(MESSAGE, "a")) is not None              # in the text, but meaningless
    assert check_evidence(CONTEXT, ev(MESSAGE, "the")) is not None
    assert check_evidence(CONTEXT, ev("trusted.occurrences.count", "3")) is None  # the whole value
    assert check_evidence(CONTEXT, ev("trusted.occurrences.recent[0].mode", "webhook")) is None
    assert check_evidence(CONTEXT, ev("trusted.occurrences.recent[0].mode", "web")) is not None


# ====================================================================== facts


def test_a_fact_with_real_evidence_is_kept():
    result = run(facts=[fact("Node.", ev("untrusted.node_name", "SHEETS — Load Settings"))])
    assert [f.statement for f in result.analysis.facts] == ["Node."]
    assert result.notes == []


def test_a_fact_citing_an_invented_ref_is_removed_with_a_note():
    result = run(facts=[fact("Made up.", ev("untrusted.error.cause", "needs to be reconnected"))])
    assert result.analysis.facts == []
    assert any("fact[0] removed" in n for n in result.notes)
    assert any("ref does not point" in n for n in result.notes)


def test_a_fact_whose_quote_is_not_in_the_field_is_removed():
    result = run(facts=[fact("Wrong.", ev(MESSAGE, "the refresh token expired yesterday"))])
    assert result.analysis.facts == []
    assert any("quote not found" in n for n in result.notes)


def test_only_the_valid_evidence_of_a_fact_survives():
    result = run(facts=[fact("Mixed.", ev(MESSAGE, "invented words here"), ev(MESSAGE, "needs to be reconnected"))])
    (kept,) = result.analysis.facts
    assert [e.quote for e in kept.evidence] == ["needs to be reconnected"]
    assert any("evidence[0] dropped" in n for n in result.notes)


def test_a_fact_with_no_evidence_at_all_is_removed():
    result = run(facts=[fact("Unsupported claim.")])
    assert result.analysis.facts == []


def test_duplicate_evidence_counts_once():
    result = run(facts=[fact("Dup.", ev(MESSAGE, "needs to be reconnected"), ev(MESSAGE, "needs to be reconnected"))])
    assert len(result.analysis.facts[0].evidence) == 1


# ====================================================================== hypotheses and confidence


def test_a_hypothesis_without_valid_evidence_is_kept_but_unsupported():
    result = run(hypotheses=[hyp("medium", ev(MESSAGE, "invented words here"))])
    (h,) = result.analysis.hypotheses
    assert h.supported is False and h.supporting_evidence == []
    assert h.cause == "Some cause." and h.would_confirm == ["check x"]
    assert any("marked unsupported" in n for n in result.notes)


def test_a_hypothesis_with_valid_evidence_is_supported():
    (h,) = run(hypotheses=[hyp("low", ev(MESSAGE, "needs to be reconnected"))]).analysis.hypotheses
    assert h.supported is True and h.confidence == "low"


def test_high_with_one_valid_evidence_item_becomes_medium():
    result = run(hypotheses=[hyp("high", ev(MESSAGE, "needs to be reconnected"))])
    assert result.analysis.hypotheses[0].confidence == "medium"
    assert any("lowered from high to medium" in n for n in result.notes)


def test_high_with_two_items_on_the_same_ref_still_becomes_medium():
    evidence = [ev(MESSAGE, "needs to be reconnected"), ev(MESSAGE, "credential \"Example-marketing\"")]
    assert run(hypotheses=[hyp("high", *evidence)]).analysis.hypotheses[0].confidence == "medium"


def test_high_with_one_valid_and_one_invalid_item_becomes_medium():
    evidence = [ev(MESSAGE, "needs to be reconnected"), ev("untrusted.node_name", "invented node")]
    assert run(hypotheses=[hyp("high", *evidence)]).analysis.hypotheses[0].confidence == "medium"


def test_high_with_two_distinct_valid_refs_stays_high():
    evidence = [ev(MESSAGE, "needs to be reconnected"), ev("untrusted.node_name", "SHEETS — Load Settings")]
    result = run(hypotheses=[hyp("high", *evidence)])
    assert result.analysis.hypotheses[0].confidence == "high"
    assert result.notes == []


def test_an_unsupported_high_is_also_capped():
    assert run(hypotheses=[hyp("high")]).analysis.hypotheses[0].confidence == "medium"


def test_low_and_medium_are_never_changed():
    for level in ("low", "medium"):
        assert run(hypotheses=[hyp(level, ev(MESSAGE, "needs to be reconnected"))]).analysis.hypotheses[0].confidence == level


def test_only_the_first_three_hypotheses_are_kept():
    many = [hyp("low", ev(MESSAGE, "needs to be reconnected"), cause=f"cause {i}") for i in range(5)]
    result = run(hypotheses=many)
    assert [h.cause for h in result.analysis.hypotheses] == ["cause 0", "cause 1", "cause 2"]
    assert len(result.analysis.hypotheses) == MAX_HYPOTHESES
    assert any("5 hypotheses returned" in n for n in result.notes)
    assert result.stats.hypotheses_in == 5 and result.stats.hypotheses_kept == 3


# ====================================================================== pass-through, caps, stats


def test_insufficient_evidence_and_category_are_passed_through_unchanged():
    result = verify_analysis(trap_output(), CONTEXT)
    assert result.analysis.insufficient_evidence is True
    assert result.analysis.category == "unknown"
    assert result.analysis.facts == [] and result.analysis.hypotheses == []


def test_a_model_that_wrongly_claims_sufficient_evidence_is_not_corrected_here():
    """The eval measures the model on this flag; forcing it server-side would hide a weak model."""
    result = verify_analysis(trap_output(insufficient_evidence=False), CONTEXT)
    assert result.analysis.insufficient_evidence is False


def test_long_text_and_long_lists_are_capped_with_notes():
    long = "x" * 5000
    steps = [f"step {i}" for i in range(15)]
    output = good_output(
        summary=long,
        facts=[fact(long, ev(MESSAGE, "needs to be reconnected"))],
        hypotheses=[Hypothesis(cause=long, confidence="low", supporting_evidence=[ev(MESSAGE, "needs to be reconnected")],
                               would_confirm=steps, verification_steps=steps)],
        recommended_actions=[RecommendedAction(step=f"a{i}", risk="low") for i in range(15)],
    )
    result = verify_analysis(output, CONTEXT).analysis
    assert len(result.summary) == 1000 and len(result.facts[0].statement) == 1000
    assert len(result.hypotheses[0].cause) == 1000
    assert len(result.hypotheses[0].would_confirm) == 10 and len(result.recommended_actions) == 10


def test_stats_count_what_was_dropped():
    result = run(
        facts=[fact("ok", ev(MESSAGE, "needs to be reconnected")), fact("bad", ev("x.y", "nothing at all"))],
        hypotheses=[hyp("low", ev(MESSAGE, "needs to be reconnected")), hyp("low", ev(MESSAGE, "invented words here"))],
    )
    stats = result.stats
    assert (stats.facts_in, stats.facts_kept) == (2, 1)
    assert (stats.hypotheses_in, stats.hypotheses_kept, stats.hypotheses_unsupported) == (2, 2, 1)


def test_verification_is_deterministic_and_does_not_mutate_its_input():
    output = good_output()
    before = output.model_dump()
    first = verify_analysis(output, CONTEXT)
    second = verify_analysis(output, CONTEXT)
    assert first.analysis == second.analysis and first.notes == second.notes
    assert output.model_dump() == before
