# FlowOps AI

Personal automation-monitoring app (n8n first). Learning/portfolio project; built
incrementally, one milestone at a time. Implement only the milestone asked for, then stop.

## Stack
Python 3.12, FastAPI, Uvicorn, Pydantic, python-dotenv, pytest, httpx2, SQLAlchemy 2.0 (SQLite).
Coming in later milestones: Jinja2.
Do NOT add React, Docker, Redis, Celery, vector DBs, LangChain or LangGraph.

## Commands (PowerShell, from repo root, venv active: `.\venv\Scripts\Activate.ps1`)
- Install:  `pip install -r requirements.txt`
- Run app:  `python -m uvicorn app.main:app --reload`  (http://localhost:8000, docs at /docs)
- Run tests: `python -m pytest`
- Reset the DB (DESTRUCTIVE, deletes all data): `python -m scripts.reset_db --yes`
  (run from the repo root; SQLite only; stop the app first)

## Hard conventions
1. **Timestamps are naive UTC** in the DB. Produce with `datetime.now(timezone.utc)`,
   never `datetime.utcnow()`. Convert to local time for display only.
2. **Sync SQLAlchemy sessions and plain `def` routes.** No async engine, no `async def`
   routes that touch the database.
3. **DB portability:** no SQLite-specific SQL, no `sqlite3` import, all date/time logic
   in Python — keeps the PostgreSQL path open.
4. **Throwaway DB through V1:** no migrations (Alembic is Phase 2). Schema changes =
   delete and re-seed (`python -m scripts.reset_db --yes`, then `scripts/seed.py`).

## Data model (M2)
- `app/models.py`: `Workflow` 1—* `ExecutionEvent`. Statuses: `ExecutionStatus` =
  success | failed | running | waiting | canceled | unknown, stored as lowercase text with a
  DB CHECK constraint (`create_constraint=True` -- without it SQLAlchemy creates NO check).
  `TERMINAL` / `NON_TERMINAL` frozensets drive the M4 upsert.
- Timestamps: columns use `NaiveUTCDateTime` (aware in -> converted to UTC and stripped;
  naive in -> assumed UTC; always naive out). Use `app.timeutil.utcnow()` for "now" and
  `compute_duration_ms()` for durations (None, never 0, when it cannot be known).
- Soft delete only: `is_active=False`. FK is `ON DELETE RESTRICT`, no cascade, no hard-delete code.
- `error_raw` is stored as received and NEVER rendered or returned; only the sanitized
  `error_summary` may be shown.
- SQLite pragmas (WAL, busy_timeout=5000, foreign_keys=ON) are set by a connect listener
  in `app/database.py`, registered per engine, SQLite only. WAL creates `*.db-wal` / `*.db-shm`.
- `DATABASE_URL` (default: `flowops.db` in the repo root). Importing `app.database` builds the engine.
- Tests: every test gets a fresh SQLite file in `tmp_path` (`engine`, `db_session`,
  `override_get_db` fixtures in `tests/conftest.py`); dispose the engine on teardown or
  Windows cannot delete the file.

## Other conventions
- Dependencies are pinned in `requirements.txt`. Pre-authorized: fastapi, uvicorn,
  pydantic, python-dotenv, pytest, httpx2, sqlalchemy, jinja2. Ask before adding anything else.
- Secrets live in `.env` (git-ignored); keep `.env.example` current. Never hardcode secrets
  or use real production data in tests.
- Keep business logic out of route functions and templates.
- V1 is localhost-only; the dashboard has no auth by design (revisit before any deployment).
- If a milestone spec is wrong or inconsistent, say so and STOP — don't silently improve it.
- Update this file whenever commands or conventions change.
