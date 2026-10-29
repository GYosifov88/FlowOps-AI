from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.database import init_db
from app.routers import events, pages, workflows

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast: never run with an unauthenticated ingest endpoint.
    if not get_settings().ingest_token:
        raise RuntimeError(
            "FLOWOPS_INGEST_TOKEN is not set. Add it to .env (see .env.example)."
        )
    # Create any missing tables. Idempotent: never drops, alters or seeds anything, so an
    # existing database is untouched (a changed schema still needs scripts.reset_db).
    init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(workflows.router)
app.include_router(events.router)
app.include_router(pages.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
