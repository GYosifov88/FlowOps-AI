"""The analyzer interface and its OpenAI implementation (V2-M3).

`Analyzer` is the only thing the rest of FlowOps knows about a model. Tests use a fake that
implements it, so no test ever calls the API. THIS is the only module that imports `openai`.

The OpenAI call (Responses API, verified against the OpenAI docs and the installed SDK):

    client.responses.parse(
        model=..., input=[system, user], text_format=AnalysisOutput,
        max_output_tokens=..., store=False, [reasoning={"effort": ...}],
    )

* `text_format` makes the SDK send a strict JSON schema and parse the answer into the Pydantic model.
* `store=False`: the Responses API stores a response for 30 days by default; this analysis
  must not be retained at OpenAI.
* `max_output_tokens` includes the model's reasoning tokens.
* The result is one of five statuses and `analyze` never raises: ok, refused (the model declined),
  incomplete (cut off, usually out of tokens), invalid (no usable structured output), error
  (network, auth or API error). Only the exception CLASS NAME and HTTP status are kept: never a
  message or a body, which could echo the request.
"""
import logging
from dataclasses import dataclass
from typing import Literal, Protocol

import openai
from pydantic import ValidationError

from app.analysis.prompt import build_input
from app.analysis.schema import AnalysisOutput
from app.sanitize import scrub

logger = logging.getLogger(__name__)

ResultStatus = Literal["ok", "refused", "incomplete", "invalid", "error"]
REFUSAL_CAP = 500


@dataclass(frozen=True)
class AnalyzerResult:
    status: ResultStatus
    output: AnalysisOutput | None = None  # only for "ok"
    refusal: str | None = None  # scrubbed and capped
    error_type: str | None = None  # exception class (+ HTTP status), or the incomplete reason
    input_tokens: int | None = None
    output_tokens: int | None = None  # includes reasoning tokens
    reasoning_tokens: int | None = None


class Analyzer(Protocol):
    provider: str
    model: str

    def analyze(self, context: dict) -> AnalyzerResult:
        """Analyse the context. Must not raise: failures are returned as a status."""
        ...


class OpenAIAnalyzer:
    provider = "openai"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        reasoning_effort: str = "",
        max_output_tokens: int = 16000,
        timeout: float = 120.0,
        client=None,
    ) -> None:
        self.model = model
        self._reasoning_effort = reasoning_effort
        self._max_output_tokens = max_output_tokens
        # max_retries: the SDK itself retries 429 / 5xx / connection errors with backoff.
        self._client = client or openai.OpenAI(api_key=api_key, timeout=timeout, max_retries=2)

    def _request(self, context: dict) -> dict:
        request = {
            "model": self.model,
            "input": build_input(context),
            "text_format": AnalysisOutput,
            "max_output_tokens": self._max_output_tokens,
            "store": False,
        }
        if self._reasoning_effort:
            request["reasoning"] = {"effort": self._reasoning_effort}
        return request

    def analyze(self, context: dict) -> AnalyzerResult:
        try:
            response = self._client.responses.parse(**self._request(context))
        except ValidationError:
            return AnalyzerResult("invalid", error_type="ValidationError")
        except openai.OpenAIError as exc:
            return AnalyzerResult("error", error_type=_error_type(exc))
        return map_response(response)


def _error_type(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    return f"{name} {status}" if isinstance(status, int) else name


def _tokens(response) -> dict:
    usage = getattr(response, "usage", None)
    details = getattr(usage, "output_tokens_details", None)
    return {
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None),
    }


def _refusal_text(response) -> str | None:
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", None) or []:
            if getattr(content, "type", None) == "refusal":
                return str(getattr(content, "refusal", "") or "")
    return None


def map_response(response) -> AnalyzerResult:
    """A parsed Responses API response -> AnalyzerResult. Pure, so it is tested without a network."""
    tokens = _tokens(response)
    status = getattr(response, "status", None)

    if status == "incomplete":
        reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
        return AnalyzerResult("incomplete", error_type=str(reason or "incomplete")[:100], **tokens)
    if status in ("failed", "cancelled"):
        return AnalyzerResult("error", error_type=f"response_{status}", **tokens)

    refusal = _refusal_text(response)
    if refusal is not None:
        return AnalyzerResult("refused", refusal=scrub(refusal)[:REFUSAL_CAP], **tokens)

    parsed = getattr(response, "output_parsed", None)
    if not isinstance(parsed, AnalysisOutput):
        return AnalyzerResult("invalid", error_type="no_parsed_output", **tokens)
    return AnalyzerResult("ok", output=parsed, **tokens)
