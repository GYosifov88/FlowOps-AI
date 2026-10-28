import pytest

from app.sanitize import MAX_LENGTH, TRUNCATION_MARKER, sanitize_error

# (case id, raw input, secret values that must not survive)
SECRET_CASES = [
    (
        "bearer-header",
        "Request failed: Authorization: Bearer eyJhbGciOi.SECRETPAYLOAD.sig123 -> 401",
        ["eyJhbGciOi.SECRETPAYLOAD.sig123", "SECRETPAYLOAD"],
    ),
    ("basic-auth-header", "authorization: Basic dXNlcjpodW50ZXIy", ["dXNlcjpodW50ZXIy"]),
    ("bare-bearer", "token rejected: Bearer abc123SECRET.def", ["abc123SECRET"]),
    (
        "api-key-in-url-query",
        "GET https://api.example.com/v1/items?page=2&api_key=sk_live_ABCDEF123&limit=5 failed",
        ["sk_live_ABCDEF123"],
    ),
    ("x-api-key-header", "headers: X-Api-Key: hunter2-key-value", ["hunter2-key-value"]),
    ("apikey-equals", "apikey=zzTOPSECRETzz", ["zzTOPSECRETzz"]),
    (
        "json-password-field",
        '{"user": "bob", "password": "correct-horse-battery", "retries": 3}',
        ["correct-horse-battery"],
    ),
    (
        "json-password-with-spaces",
        '{"password": "has some spaces in it", "ok": true}',
        ["has some spaces in it", "spaces"],
    ),
    (
        "json-escaped-inside-a-string",
        'NodeApiError: bad body "{\\"client_secret\\": \\"ESCAPEDSECRET\\", \\"a\\": 1}"',
        ["ESCAPEDSECRET"],
    ),
    ("single-quoted", "config = {'secret': 'single-quoted-secret'}", ["single-quoted-secret"]),
    ("access-token", "access_token: ya29.GOOGLETOKEN refresh_token=1//REFRESHME", ["GOOGLETOKEN", "REFRESHME"]),
    ("upper-case-keys", "PASSWORD=UPPERSECRET TOKEN: UPPERTOKEN", ["UPPERSECRET", "UPPERTOKEN"]),
]


@pytest.mark.parametrize(
    "raw, secrets", [c[1:] for c in SECRET_CASES], ids=[c[0] for c in SECRET_CASES]
)
def test_secret_values_never_appear_in_the_output(raw, secrets):
    out = sanitize_error(raw)

    assert out is not None
    for secret in secrets:
        assert secret not in out
    assert "[REDACTED]" in out
    assert len(out) <= MAX_LENGTH


def test_redaction_keeps_the_surrounding_message_readable():
    out = sanitize_error(
        "HTTP 401 calling https://api.example.com/items?api_key=SECRETVALUE&limit=5"
    )

    assert out == "HTTP 401 calling https://api.example.com/items?api_key=[REDACTED]&limit=5"


def test_a_long_stack_trace_is_capped_and_a_deep_secret_is_removed():
    frames = "\n".join(
        f"    at Object.run (/app/node_modules/pkg/lib/file{i}.js:{i}:13)" for i in range(150)
    )
    raw = (
        "Error: Request failed with status code 500\n"
        + frames
        + '\n    body: {"password": "DEEPSECRET"}\n'
        + frames
    )
    assert len(raw) > 10_000

    out = sanitize_error(raw)

    assert len(out) == MAX_LENGTH
    assert out.endswith(TRUNCATION_MARKER)
    assert out.startswith("Error: Request failed with status code 500 at Object.run")
    assert "\n" not in out
    assert "DEEPSECRET" not in out


def test_a_secret_straddling_the_cap_is_not_half_leaked():
    secret = "STRADDLESECRET1234567890"
    for padding in range(MAX_LENGTH - 60, MAX_LENGTH + 5):
        out = sanitize_error("x" * padding + f" password={secret} tail")
        assert "STRADDLE" not in out, padding
        assert len(out) <= MAX_LENGTH


def test_short_input_is_not_marked_truncated():
    out = sanitize_error("Timeout after 30s")

    assert out == "Timeout after 30s"


def test_input_exactly_at_the_cap_is_unchanged():
    raw = "a" * MAX_LENGTH

    assert sanitize_error(raw) == raw


def test_whitespace_is_collapsed():
    assert sanitize_error("  line one\n\n\tline   two  ") == "line one line two"


@pytest.mark.parametrize("raw", [None, "", "   ", "\n\t "])
def test_nothing_to_summarize_gives_none(raw):
    assert sanitize_error(raw) is None


def test_sanitizing_twice_changes_nothing():
    once = sanitize_error("password=abc Authorization: Bearer xyz " + "pad " * 300)

    assert sanitize_error(once) == once


def test_a_word_that_merely_contains_a_key_name_is_left_alone():
    assert sanitize_error("tokens: 5 remaining, secrets manager unavailable") == (
        "tokens: 5 remaining, secrets manager unavailable"
    )


def test_a_very_large_hostile_input_is_handled_quickly():
    raw = ("password=" * 20_000) + ("\\" * 50_000) + ('"token":' * 5_000)

    out = sanitize_error(raw)

    assert len(out) <= MAX_LENGTH
