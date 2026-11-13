"""The OpenAI adapter, tested against hand-built response objects and a fake client: no network.

The tests/conftest.py guard makes constructing the real `openai.OpenAI` raise, so a regression
that reaches the API fails here instead of spending money.
"""
from types import SimpleNamespace

import httpx2
import openai
import pytest
from pydantic import ValidationError

from app.analysis.analyzer import OpenAIAnalyzer, map_response
from app.analysis.prompt import SYSTEM_PROMPT
from app.analysis.schema import AnalysisOutput
from app.services.incident_context import frame_context
from tests.analysis_helpers import good_output

CONTEXT = {"context_version": 1, "trusted": {"occurrences": {"count": 3}}, "untrusted": {"node_name": "N"}}
REQUEST = httpx2.Request("POST", "https://api.openai.com/v1/responses")


def response(output=None, *, status="completed", refusal=None, reason=None, usage=(100, 200, 150), parsed="__output__"):
    content = []
    if refusal is not None:
        content.append(SimpleNamespace(type="refusal", refusal=refusal))
    elif output is not None:
        content.append(SimpleNamespace(type="output_text", text="{}", parsed=output))
    return SimpleNamespace(
        status=status,
        incomplete_details=SimpleNamespace(reason=reason) if reason else None,
        output=[SimpleNamespace(type="message", content=content)],
        output_parsed=output if parsed == "__output__" else parsed,
        usage=SimpleNamespace(
            input_tokens=usage[0], output_tokens=usage[1],
            output_tokens_details=SimpleNamespace(reasoning_tokens=usage[2]),
        ),
    )


class FakeClient:
    """Stands in for openai.OpenAI: records the request, answers with a response or raises."""

    def __init__(self, outcome):
        self.outcome = outcome
        self.requests: list[dict] = []
        self.responses = SimpleNamespace(parse=self._parse)

    def _parse(self, **kwargs):
        self.requests.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def analyzer(outcome, **settings):
    client = FakeClient(outcome)
    return OpenAIAnalyzer(api_key="unused", model="gpt-6-luna", client=client, **settings), client


# ====================================================================== the exact call shape


def test_the_request_is_exactly_the_documented_call():
    a, client = analyzer(response(good_output()), max_output_tokens=12345)
    a.analyze(CONTEXT)

    (request,) = client.requests
    assert set(request) == {"model", "input", "text_format", "max_output_tokens", "store"}
    assert request["model"] == "gpt-6-luna"
    assert request["text_format"] is AnalysisOutput
    assert request["max_output_tokens"] == 12345
    assert request["store"] is False  # OpenAI keeps responses for 30 days unless told not to
    system, user = request["input"]
    assert (system["role"], system["content"]) == ("system", SYSTEM_PROMPT)
    assert (user["role"], user["content"]) == ("user", frame_context(CONTEXT))
    assert set(user) == {"role", "content"}


def test_reasoning_effort_is_sent_only_when_configured():
    a, client = analyzer(response(good_output()))
    a.analyze(CONTEXT)
    assert "reasoning" not in client.requests[0]

    a, client = analyzer(response(good_output()), reasoning_effort="low")
    a.analyze(CONTEXT)
    assert client.requests[0]["reasoning"] == {"effort": "low"}


def test_constructing_the_real_client_is_blocked_in_tests():
    with pytest.raises(AssertionError, match="openai.OpenAI"):
        OpenAIAnalyzer(api_key="k", model="m")
    with pytest.raises(AssertionError, match="openai.OpenAI"):
        openai.OpenAI(api_key="k")


def test_the_provider_and_model_are_reported():
    a, _ = analyzer(response(good_output()))
    assert (a.provider, a.model) == ("openai", "gpt-6-luna")


# ====================================================================== response mapping


def test_ok():
    output = good_output()
    result = analyzer(response(output))[0].analyze(CONTEXT)
    assert result.status == "ok" and result.output is output
    assert (result.input_tokens, result.output_tokens, result.reasoning_tokens) == (100, 200, 150)
    assert result.refusal is None and result.error_type is None


def test_a_refusal_is_scrubbed_and_capped():
    result = map_response(response(refusal="I won't. Contact jane@example.com " + "x" * 2000, parsed=None))
    assert result.status == "refused" and result.output is None
    assert "<email>" in result.refusal and "jane@" not in result.refusal and len(result.refusal) <= 500


def test_a_refusal_wins_over_missing_output():
    assert map_response(response(refusal="No.", parsed=None)).status == "refused"


def test_incomplete_because_of_the_token_budget():
    result = map_response(response(status="incomplete", reason="max_output_tokens", usage=(100, 16000, 15900), parsed=None))
    assert (result.status, result.error_type, result.output) == ("incomplete", "max_output_tokens", None)
    assert result.output_tokens == 16000


def test_incomplete_because_of_the_content_filter():
    assert map_response(response(status="incomplete", reason="content_filter", parsed=None)).error_type == "content_filter"


def test_incomplete_without_a_reason():
    assert map_response(response(status="incomplete", parsed=None)).error_type == "incomplete"


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_a_failed_response_is_an_error(status):
    result = map_response(response(status=status, parsed=None))
    assert (result.status, result.error_type) == ("error", f"response_{status}")


def test_nothing_parsed_is_invalid():
    result = map_response(response(None, parsed=None))
    assert (result.status, result.error_type) == ("invalid", "no_parsed_output")


def test_a_parsed_value_of_the_wrong_type_is_invalid():
    assert map_response(response(None, parsed={"summary": "x"})).status == "invalid"


def test_missing_usage_is_tolerated():
    bare = SimpleNamespace(status="completed", output=[], output_parsed=good_output())
    result = map_response(bare)
    assert result.status == "ok" and result.input_tokens is None


# ====================================================================== exceptions


def validation_error():
    with pytest.raises(ValidationError) as caught:
        AnalysisOutput.model_validate({})
    return caught.value


def test_a_schema_validation_error_is_invalid():
    result = analyzer(validation_error())[0].analyze(CONTEXT)
    assert (result.status, result.error_type) == ("invalid", "ValidationError")


SECRET = "sk-SECRET-KEY-VALUE-123"


def status_error(cls, code):
    return cls(f"Incorrect API key provided: {SECRET}", response=httpx2.Response(code, request=REQUEST), body={"error": SECRET})


@pytest.mark.parametrize("exc, expected", [
    (openai.APITimeoutError(request=REQUEST), "APITimeoutError"),
    (openai.APIConnectionError(request=REQUEST), "APIConnectionError"),
    (status_error(openai.AuthenticationError, 401), "AuthenticationError 401"),
    (status_error(openai.RateLimitError, 429), "RateLimitError 429"),
    (status_error(openai.BadRequestError, 400), "BadRequestError 400"),
    (status_error(openai.InternalServerError, 500), "InternalServerError 500"),
], ids=lambda v: v if isinstance(v, str) else type(v).__name__)
def test_api_errors_keep_the_class_and_status_only(exc, expected):
    result = analyzer(exc)[0].analyze(CONTEXT)
    assert (result.status, result.error_type) == ("error", expected)
    assert SECRET not in repr(result)  # neither the message nor the body can reach what is stored


def test_other_exceptions_are_not_swallowed_by_the_adapter():
    """The service wraps the call; the adapter only handles what it understands."""
    with pytest.raises(RuntimeError):
        analyzer(RuntimeError("bug"))[0].analyze(CONTEXT)
