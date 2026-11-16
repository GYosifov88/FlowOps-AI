"""Authentication for the server-rendered pages (V2-M5). No JS, no new dependency.

The JSON API authenticates with the `X-FlowOps-Token` header. A plain HTML form cannot send a
header, so the pages use a short session instead:

* `POST /unlock` takes the ingest token plus a name ("actor"), compares the token with
  `secrets.compare_digest`, and sets a SIGNED cookie. The token itself is never stored in the
  cookie or any page. The signing key is derived from the token, so rotating the token logs
  every browser out.
* Cookie = `base64url(json{a: actor, iat, n: nonce}) . base64url(HMAC-SHA256(key, payload))`.
  HttpOnly, SameSite=Strict, 12 h. Not `Secure`: V1 is plain http on localhost (must change
  before any deployment, together with the rest of the V1 auth story).
* Every state-changing form POST must pass, in this order: (1) the Origin/Host check, (2) a
  valid session (no cookie at all = redirect to /unlock; a cookie that is present but tampered,
  expired or made under another token = 403), (3) the per-session CSRF token. SameSite is not
  enough on localhost: browsers treat every port on localhost as the same site, so a page served
  from another local port could otherwise post with our cookie.
* Reads stay open (V1). Stateless sessions: a stolen cookie works until it expires or the token
  is rotated.
"""
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import Depends, HTTPException, Request, status

from app.config import Settings, get_settings

SESSION_COOKIE = "flowops_ui"
SESSION_MAX_AGE = 12 * 3600
CLOCK_SKEW = 60  # a cookie "from the future" by more than this is refused
LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})
ACTOR_RE = re.compile(r"[A-Za-z0-9 ._@-]{1,100}")
MAX_FORM_BYTES = 16 * 1024
MAX_FORM_FIELDS = 20
CSRF_FIELD = "csrf"


@dataclass(frozen=True)
class UiSession:
    actor: str
    nonce: str
    csrf: str  # the token this session's forms must carry


# ------------------------------------------------------------------ signing


def _key(settings: Settings) -> bytes:
    return hmac.new(settings.ingest_token.encode("utf-8"), b"flowops-ui-session-v1", hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(settings: Settings, data: bytes) -> str:
    return _b64(hmac.new(_key(settings), data, hashlib.sha256).digest())


def csrf_token(settings: Settings, nonce: str) -> str:
    return hmac.new(_key(settings), b"csrf|" + nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def clean_actor(value: str) -> str | None:
    """The stripped name if it is acceptable (same rule as the API's `actor`), else None."""
    value = value.strip()
    return value if ACTOR_RE.fullmatch(value) else None


def issue_cookie(settings: Settings, actor: str, now: float | None = None) -> str:
    payload = {"a": actor, "iat": int(_now() if now is None else now), "n": secrets.token_hex(16)}
    body = _b64(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{body}.{_sign(settings, body.encode('ascii'))}"


def read_session(settings: Settings, cookie: str | None, now: float | None = None) -> UiSession | None:
    """The session a cookie proves, or None (missing, malformed, tampered, expired, other token)."""
    if not cookie or not settings.ingest_token:
        return None
    try:
        body, signature = cookie.split(".")
        expected = _sign(settings, body.encode("ascii"))
        if not secrets.compare_digest(signature.encode("utf-8"), expected.encode("ascii")):
            return None
        payload = json.loads(_unb64(body))
        actor, issued, nonce = payload["a"], int(payload["iat"]), payload["n"]
    except (ValueError, KeyError, TypeError, binascii.Error):
        return None
    if not (isinstance(actor, str) and ACTOR_RE.fullmatch(actor) and isinstance(nonce, str) and len(nonce) == 32):
        return None
    current = _now() if now is None else now
    if issued > current + CLOCK_SKEW or current - issued > SESSION_MAX_AGE:
        return None
    return UiSession(actor, nonce, csrf_token(settings, nonce))


def csrf_ok(session: UiSession, supplied: str) -> bool:
    return secrets.compare_digest(supplied.encode("utf-8"), session.csrf.encode("utf-8"))


def token_ok(settings: Settings, supplied: str) -> bool:
    expected = settings.ingest_token
    return bool(expected) and secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


def safe_next(value: str | None) -> str:
    """Only a plain site-relative path may be redirected to (no open redirect)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return "/"
    parts = urlsplit(value)
    return value if not parts.scheme and not parts.netloc else "/"


def origin_ok(request: Request) -> bool:
    """Host must be a local name, and Origin must be exactly this site (a missing or `null` Origin fails)."""
    host_header = request.headers.get("host", "")
    try:
        hostname = urlsplit("//" + host_header).hostname
    except ValueError:
        return False
    if hostname not in LOCAL_HOSTNAMES:
        return False
    origin = request.headers.get("origin")
    return origin is not None and origin.lower() == f"{request.url.scheme}://{host_header}".lower()


def clear_cookie_header() -> dict[str, str]:
    return {"Set-Cookie": f"{SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict"}


# ------------------------------------------------------------------ dependencies


def get_ui_session(request: Request, settings: Settings = Depends(get_settings)) -> UiSession | None:
    """The current session for a page render. Never raises: no valid session just means 'locked'."""
    return read_session(settings, request.cookies.get(SESSION_COOKIE))


def require_local_origin(request: Request) -> None:
    if not origin_ok(request):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad Origin or Host")


async def read_form(request: Request) -> dict[str, str]:
    """A small urlencoded form as {name: first value}. Stdlib only (no python-multipart); capped."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Expected an urlencoded form")
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_FORM_BYTES:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, "Form too large")
    try:
        parsed = parse_qs(
            body.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=MAX_FORM_FIELDS
        )
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Too many form fields")
    return {name: values[0] for name, values in parsed.items()}


@dataclass(frozen=True)
class UiAction:
    session: UiSession
    form: dict[str, str]


async def ui_action(request: Request, settings: Settings = Depends(get_settings)) -> UiAction:
    """Gate for every state-changing form POST: Origin/Host, then session, then CSRF."""
    require_local_origin(request)
    form = await read_form(request)
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie is None:
        incident_id = str(request.path_params.get("incident_id", ""))
        target = f"/incidents/{incident_id}" if incident_id.isdigit() else "/"
        raise HTTPException(
            status.HTTP_303_SEE_OTHER, "Unlock first", headers={"Location": f"/unlock?next={quote(target, safe='')}"}
        )
    session = read_session(settings, cookie)
    if session is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Invalid or expired session; unlock again", headers=clear_cookie_header()
        )
    if not csrf_ok(session, form.get(CSRF_FIELD, "")):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Bad CSRF token")
    return UiAction(session, form)
