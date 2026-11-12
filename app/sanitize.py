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


def redact(text: str) -> str:
    """Replace obvious secret VALUES (Authorization / Bearer / keyed secrets) with [REDACTED].

    Same best-effort limits as the module docstring. Shared by sanitize_error and by the
    incident signature code, so nothing secret reaches either.
    """
    text = _AUTHORIZATION.sub(rf"\1{REDACTED}", text)
    text = _BEARER.sub(rf"\1{REDACTED}", text)
    return _KEYED_SECRET.sub(rf"\1{REDACTED}", text)


def sanitize_error(raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    # Redact first, so truncation can never leave the front half of a secret behind.
    text = redact(raw)
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > MAX_LENGTH:
        text = text[: MAX_LENGTH - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER
    return text


# ---------------------------------------------------------------- PII scrub (V2-M2)
#
# redact() removes secrets. scrub_pii() removes what would identify a PERSON or an account
# before text is put in a context that may later leave the machine. Best-effort, like
# redact(): personal NAMES and street addresses in free text are NOT caught. The real
# control is minimisation (an allow-list of fields, no run data); this is the safety net.

PLACEHOLDER_EMAIL = "<email>"
PLACEHOLDER_NUMBER = "<number>"
PLACEHOLDER_PHONE = "<phone>"
PLACEHOLDER_QUERY = "?<query>"

# authority drops "user:password@"; the rest keeps the path but not the query string.
# "?<query>" is accepted as input so scrubbing twice changes nothing.
_PII_URL = re.compile(r"\b(https?://)([^\s/?#\"'<>]*)((?:\?<query>|[^\s\"'<>])*)")
_PII_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_ISO_TIME = r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
# One pass, leftmost match wins and alternatives are tried in this order, so a timestamp is
# consumed (and left alone) before the digit rules could mistake it for a phone number.
_PII_NUMBERS = re.compile(
    rf"(?P<ts>{_ISO_TIME})"
    r"|(?P<iban>\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,3})?\b|\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b)"
    r"|(?P<card>(?<![\w.])\d(?:[ -]?\d){12,18}(?![\w]))"
    r"|(?P<digits>(?<![\w.])\d{9,}(?!\w))"
    r"|(?P<phone>(?<![\w.])\+?\(?\d[\d\s().\-]{6,}\d(?!\w))"
)


def _pii_url(match: re.Match) -> str:
    scheme, authority, rest = match.group(1), match.group(2), match.group(3)
    host = authority.rsplit("@", 1)[-1]
    if "?" in rest:
        rest = rest.partition("?")[0] + PLACEHOLDER_QUERY
    return f"{scheme}{host}{rest}"


def _pii_number(match: re.Match) -> str:
    kind = match.lastgroup
    if kind == "ts":
        return match.group()
    if kind == "phone":
        digits = sum(c.isdigit() for c in match.group())
        return PLACEHOLDER_PHONE if digits >= 9 else match.group()
    return PLACEHOLDER_NUMBER


def scrub_pii(text: str) -> str:
    text = _PII_URL.sub(_pii_url, text)
    text = _PII_EMAIL.sub(PLACEHOLDER_EMAIL, text)
    return _PII_NUMBERS.sub(_pii_number, text)


def scrub(text: str) -> str:
    """redact() then scrub_pii(): the full treatment for anything put in an LLM context."""
    return scrub_pii(redact(text))
