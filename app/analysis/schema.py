"""The structured shape of an incident analysis (V2-M3).

`AnalysisOutput` is what the MODEL must return (sent to OpenAI as a strict JSON schema).
`VerifiedAnalysis` is what FlowOps STORES after app.analysis.verify has checked it.

Design rules (they are tested, see tests/test_analysis_verify.py):

* There is NO field that says a cause is verified, confirmed or the root cause. A cause can
  only ever be a HYPOTHESIS. "Verified" exists in one place in the product: a human
  confirming a hypothesis (V2-M4).
* Confidence is a category (low / medium / high), never a percentage.
* The schema offers the model no action to take, no tool, no command: recommended actions
  are suggestions for a human.
* OpenAI strict mode: every field is required, no defaults; "nothing" is an empty list.
  No length constraints here -- the server enforces limits (verify.py), not the model.
"""
from typing import Literal

from pydantic import BaseModel, ConfigDict

Category = Literal[
    "auth_credential",
    "rate_limit",
    "timeout",
    "upstream_error",
    "data_schema",
    "configuration",
    "infrastructure_crash",
    "unknown",
]
Level = Literal["low", "medium", "high"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(_Strict):
    ref: str  # dotted path into the context, e.g. "untrusted.error.message"
    quote: str  # text copied from the value at that path


class Fact(_Strict):
    statement: str  # something directly visible in the context
    evidence: list[Evidence]


class Hypothesis(_Strict):
    cause: str
    confidence: Level
    supporting_evidence: list[Evidence]
    would_confirm: list[str]  # what a human could check to confirm it
    verification_steps: list[str]


class RecommendedAction(_Strict):
    step: str
    risk: Level  # all of them need a human; nothing is executed by FlowOps


class AnalysisOutput(_Strict):
    summary: str
    category: Category
    facts: list[Fact]
    hypotheses: list[Hypothesis]  # at most 3, most likely first
    insufficient_evidence: bool
    recommended_actions: list[RecommendedAction]


class VerifiedHypothesis(Hypothesis):
    # False = none of its evidence resolved to a real context field with a matching quote.
    supported: bool


class VerifiedAnalysis(_Strict):
    summary: str
    category: Category
    facts: list[Fact]
    hypotheses: list[VerifiedHypothesis]
    insufficient_evidence: bool
    recommended_actions: list[RecommendedAction]
