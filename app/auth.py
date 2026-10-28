"""Shared-secret authentication for the ingest endpoint."""
import secrets

from fastapi import Depends, Header, HTTPException, status

from app.config import Settings, get_settings


def require_ingest_token(
    x_flowops_token: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.ingest_token
    # compare_digest("", "") is True, so an unset secret must never reach the comparison.
    # Bytes, because compare_digest raises TypeError on non-ASCII str.
    if (
        not expected
        or x_flowops_token is None
        or not secrets.compare_digest(
            x_flowops_token.encode("utf-8"), expected.encode("utf-8")
        )
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or missing X-FlowOps-Token",
        )
