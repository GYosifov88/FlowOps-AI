"""Incident signatures (V2-M1): which failed events are "the same problem".

    signature = sha256("v1|<workflow_id>|<node or <unattributed>>|<error name>|<normalized message>")[:16]

Everything here is a PURE function: no database, no clock, no randomness. The rules are
versioned (SIGNATURE_VERSION): changing ANY rule below means a new version and a run of
scripts/rebuild_incidents.py, otherwise old and new incidents of the same problem diverge.

normalize_message() turns the error text into a form that is the same on every run of the
same problem. In this order:

 1. redact secrets (app.sanitize.redact), so nothing secret ever reaches a signature
 2. lower-case, collapse whitespace
 3. values that change every run become placeholders:
        "double quoted" -> "<q>"       UUID -> <uuid>          ISO date/time -> <ts>
        URL -> scheme://host/<path>    e-mail -> <email>       12+ char id -> <id>
        numbers of 2+ digits -> <n>, EXCEPT 4xx/5xx status codes (429 and 401 differ!)
    'Single quoted' values are KEPT: in JS errors they are field names, e.g. (reading 'email').
 4. strip trailing punctuation, cap at 300 characters

The cap comes last (the design record lists it with step 2): capping before the
placeholders would cut a UUID or URL in half and leave a leftover that differs per run.
"""
import hashlib
import json
import re
from dataclasses import dataclass

from app.sanitize import redact

SIGNATURE_VERSION = "v1"
UNATTRIBUTED = "<unattributed>"
MESSAGE_CAP = 300
NAME_CAP = 255

_WHITESPACE = re.compile(r"\s+")
_DOUBLE_QUOTED = re.compile(r'"[^"]*"')
# After the host: our own "/<path>" placeholder (so normalizing twice changes nothing), or
# whatever follows up to the next space or quote.
_URL = re.compile(r"\b(https?)://([^\s/?#\"'<>]*)(/<path>|[^\s\"'<>]*)")
_EMAIL = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[t ]\d{2}:\d{2}(?::\d{2}(?:[.,]\d+)?)?(?:z|[+-]\d{2}:?\d{2})?)?(?![\d-])"
)
# 12+ chars of [a-z0-9] containing a digit (so ordinary long words are untouched), or 12+ hex.
_LONG_ID = re.compile(r"(?<![a-z0-9])(?:(?=[a-z0-9]*\d)[a-z0-9]{12,}|[0-9a-f]{12,})(?![a-z0-9])")
_NUMBER = re.compile(r"(?<!\d)\d{2,}(?!\d)")
_URL_TRAILING_PUNCTUATION = ".,;:!?"
_URL_CLOSERS = {")": "(", "]": "[", "}": "{"}
_TRAILING_PUNCTUATION = " .,;:!?"


def _strip_url_trailer(text: str) -> tuple[str, str]:
    """Split sentence punctuation off the end of a matched URL: "see https://x.com/a)." ->
    the ")." is not part of the URL. A closer is only stripped when the URL has no opener
    for it (the "]" of "api_key=[redacted]" belongs to the URL)."""
    trailer = ""
    while text:
        last = text[-1]
        if last in _URL_TRAILING_PUNCTUATION or (
            last in _URL_CLOSERS and _URL_CLOSERS[last] not in text
        ):
            trailer = last + trailer
            text = text[:-1]
        else:
            break
    return text, trailer


def _url_placeholder(match: re.Match) -> str:
    scheme, authority, rest = match.group(1), match.group(2), match.group(3)
    if rest:
        rest, trailer = _strip_url_trailer(rest)
    else:
        authority, trailer = _strip_url_trailer(authority)
    host = authority.rsplit("@", 1)[-1]  # drop user:password@ -- redact() does not catch it
    path = "/<path>" if rest not in ("", "/") else ""
    return f"{scheme}://{host}{path}{trailer}"


def _number_placeholder(match: re.Match) -> str:
    digits = match.group()
    if len(digits) == 3 and digits[0] in "45":
        text, start, end = match.string, match.start(), match.end()
        before = text[start - 1] if start > 0 else " "
        after = text[end] if end < len(text) else " "
        if not before.isalnum() and not after.isalnum():  # "status code 429", not "429ms"
            return digits
    return "<n>"


def normalize_message(message: str | None) -> str:
    if not message:
        return ""
    text = redact(message)
    text = _WHITESPACE.sub(" ", text.lower()).strip()
    text = _DOUBLE_QUOTED.sub('"<q>"', text)
    text = _URL.sub(_url_placeholder, text)
    text = _EMAIL.sub("<email>", text)
    text = _UUID.sub("<uuid>", text)
    text = _TIMESTAMP.sub("<ts>", text)
    text = _LONG_ID.sub("<id>", text)
    text = _NUMBER.sub(_number_placeholder, text)
    text = text.rstrip(_TRAILING_PUNCTUATION)
    if len(text) > MESSAGE_CAP:
        text = text[:MESSAGE_CAP]
        if text.rfind("<") > text.rfind(">"):  # never leave half a placeholder ("<uui")
            text = text[: text.rfind("<")]
    return text.rstrip(_TRAILING_PUNCTUATION)


@dataclass(frozen=True)
class SignatureParts:
    """The human-readable parts a signature is built from (stored on the incident)."""

    node_name: str | None  # None = unattributed
    error_name: str  # "" when unknown
    normalized_message: str


def build_parts(
    *, node_name: str | None, error_raw: str | None, error_summary: str | None
) -> SignatureParts:
    """Pick the signature inputs from an event's fields.

    n8n events carry a small JSON object in error_raw: its `name` is the error name and its
    `message` the text. Anything else (free text from POST /api/events, or no error at all)
    has no error name, and the text comes from the already-sanitized error_summary.
    """
    error_name = ""
    message = None
    parsed = _load_object(error_raw)
    if parsed is not None:
        name = parsed.get("name")
        if isinstance(name, str) and name.strip():
            error_name = name.strip()[:NAME_CAP]
        text = parsed.get("message")
        if isinstance(text, str) and text.strip():
            message = text
    if message is None:
        message = error_summary
    node = node_name.strip()[:NAME_CAP] if node_name and node_name.strip() else None
    return SignatureParts(node, error_name, normalize_message(message))


def _load_object(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _field(value: str) -> str:
    # "|" separates the fields, so a "|" inside a node name must not be able to move a
    # boundary and make two different problems hash alike.
    return value.replace("%", "%25").replace("|", "%7C")


def compute_signature(workflow_id: int, parts: SignatureParts) -> str:
    material = "|".join(
        (
            SIGNATURE_VERSION,
            str(workflow_id),
            _field(parts.node_name if parts.node_name else UNATTRIBUTED),
            _field(parts.error_name),
            _field(parts.normalized_message),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
