from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.routers import events, workflows

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast: never run with an unauthenticated ingest endpoint.
    if not get_settings().ingest_token:
        raise RuntimeError(
            "FLOWOPS_INGEST_TOKEN is not set. Add it to .env (see .env.example)."
        )
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(workflows.router)
app.include_router(events.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
