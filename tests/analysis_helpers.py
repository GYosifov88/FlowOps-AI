"""Test doubles for the incident analysis. Nothing here can reach the network."""
from app.analysis.analyzer import AnalyzerResult
from app.analysis.schema import (
    AnalysisOutput,
    Evidence,
    Fact,
    Hypothesis,
    RecommendedAction,
)


class FakeAnalyzer:
    """Implements the Analyzer protocol with scripted results; records every context it is given.

    `results` is a list consumed in order (the last one repeats), or a callable(context) -> result.
    """

    provider = "fake"

    def __init__(self, results=None, model="fake-model-1"):
        self.model = model
        self._results = results if results is not None else [ok_result(good_output())]
        self.calls: list[dict] = []

    def analyze(self, context: dict) -> AnalyzerResult:
        self.calls.append(context)
        if callable(self._results):
            return self._results(context)
        index = min(len(self.calls) - 1, len(self._results) - 1)
        result = self._results[index]
        if isinstance(result, Exception):
            raise result
        return result


def ev(ref: str, quote: str) -> Evidence:
    return Evidence(ref=ref, quote=quote)


def good_output(**overrides) -> AnalysisOutput:
    """An analysis of the corpus credential incident whose evidence is all real."""
    data = dict(
        summary="The Google Sheets credential needs to be reconnected.",
        category="auth_credential",
        facts=[
            Fact(
                statement="The failing node is SHEETS — Load Settings.",
                evidence=[ev("untrusted.node_name", "SHEETS — Load Settings")],
            ),
            Fact(
                statement="n8n says the credential needs to be reconnected.",
                evidence=[ev("untrusted.error.message", "needs to be reconnected")],
            ),
        ],
        hypotheses=[
            Hypothesis(
                cause="The OAuth connection of the credential expired or was revoked.",
                confidence="medium",
                supporting_evidence=[ev("untrusted.error.message", "needs to be reconnected")],
                would_confirm=["Open the credential in n8n and look at its connection status."],
                verification_steps=["Reconnect the credential and run the workflow once."],
            )
        ],
        insufficient_evidence=False,
        recommended_actions=[RecommendedAction(step="Reconnect the credential in n8n.", risk="low")],
    )
    data.update(overrides)
    return AnalysisOutput(**data)


def ok_result(output: AnalysisOutput | None = None, **tokens) -> AnalyzerResult:
    return AnalyzerResult(
        "ok",
        output=output or good_output(),
        input_tokens=tokens.get("input_tokens", 1500),
        output_tokens=tokens.get("output_tokens", 2500),
        reasoning_tokens=tokens.get("reasoning_tokens", 2000),
    )


def trap_output(**overrides) -> AnalysisOutput:
    """What a model should say when the data supports no cause."""
    data = dict(
        summary="The data does not show why the execution failed.",
        category="unknown",
        facts=[],
        hypotheses=[],
        insufficient_evidence=True,
        recommended_actions=[RecommendedAction(step="Open the execution in n8n and read its log.", risk="low")],
    )
    data.update(overrides)
    return AnalysisOutput(**data)
