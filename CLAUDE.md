# FlowOps AI

Personal automation-monitoring app (n8n first). Learning/portfolio project; built
incrementally, one milestone at a time. Implement only the milestone asked for, then stop.

## Stack
Python 3.12, FastAPI, Uvicorn, Pydantic, python-dotenv, pytest, httpx2.
Coming in later milestones: SQLAlchemy 2.x (SQLite), Jinja2.
Do NOT add React, Docker, Redis, Celery, vector DBs, LangChain or LangGraph.

## Commands (PowerShell, from repo root, venv active: `.\venv\Scripts\Activate.ps1`)
- Install:  `pip install -r requirements.txt`
- Run app:  `python -m uvicorn app.main:app --reload`  (http://localhost:8000, docs at /docs)
- Run tests: `python -m pytest`

## Hard conventions
1. **Timestamps are naive UTC** in the DB. Produce with `datetime.now(timezone.utc)`,
   never `datetime.utcnow()`. Convert to local time for display only.
2. **Sync SQLAlchemy sessions and plain `def` routes.** No async engine, no `async def`
   routes that touch the database.
3. **DB portability:** no SQLite-specific SQL, no `sqlite3` import, all date/time logic
   in Python — keeps the PostgreSQL path open.
4. **Throwaway DB through V1:** no migrations (Alembic is Phase 2). Schema changes =
   delete and re-seed (`scripts/reset_db.py`, then `scripts/seed.py`).

## Other conventions
- Dependencies are pinned in `requirements.txt`. Pre-authorized: fastapi, uvicorn,
  pydantic, python-dotenv, pytest, httpx2, sqlalchemy, jinja2. Ask before adding anything else.
- Secrets live in `.env` (git-ignored); keep `.env.example` current. Never hardcode secrets
  or use real production data in tests.
- Keep business logic out of route functions and templates.
- V1 is localhost-only; the dashboard has no auth by design (revisit before any deployment).
- If a milestone spec is wrong or inconsistent, say so and STOP — don't silently improve it.
- Update this file whenever commands or conventions change.
