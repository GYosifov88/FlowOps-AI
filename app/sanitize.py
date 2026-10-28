"""Turn a raw error payload into a short summary that is safe to render.

Best-effort redaction of obvious secrets only. It does not catch URL credentials
(https://user:pass@host), bare JWTs or personal data in free text. The real protection is
that error_raw is stored but never returned or rendered.
"""
import re

MAX_LENGTH = 500
TRUNCATION_MARKER = "… [truncated]"
REDACTED = "[REDACTED]"

# "Authorization: Bearer abc", "authorization=Basic abc", "Authorization: abc"
_AUTHORIZATION = re.compile(
    r"(authorization\\?[\"']?\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s,;&\"'}]+",
    re.IGNORECASE,
)
# A bare "Bearer abc" outside a header.
_BEARER = re.compile(r"(\bbearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
# key: value / key=value as it appears in headers, URL query strings and JSON bodies
# (including JSON that is itself escaped inside a string).
_KEYED_SECRET = re.compile(
    r"""((?:x-api-key|api[_-]?key|apikey|access[_-]?token|refresh[_-]?token|token
         |password|passwd|client[_-]?secret|secret)\\?["']?\s*[:=]\s*)
        (\\?"[^"]*?\\?"|'[^']*'|[^\s&,;"'}]+)""",
    re.IGNORECASE | re.VERBOSE,
)
_WHITESPACE = re.compile(r"\s+")


def sanitize_error(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    # Redact first, so truncation can never leave the front half of a secret behind.
    text = _AUTHORIZATION.sub(rf"\1{REDACTED}", raw)
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    text = _KEYED_SECRET.sub(rf"\1{REDACTED}", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > MAX_LENGTH:
        text = text[: MAX_LENGTH - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return text
