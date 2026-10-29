# FlowOps AI

Personal automation-monitoring app (n8n first). Learning/portfolio project; built
incrementally, one milestone at a time. Implement only the milestone asked for, then stop.

## Stack
Python 3.12, FastAPI, Uvicorn, Pydantic, python-dotenv, pytest, httpx2, SQLAlchemy 2.0 (SQLite).
Jinja2 for the server-rendered dashboard (M6).
Do NOT add React, Docker, Redis, Celery, vector DBs, LangChain or LangGraph.

## Commands (PowerShell, from repo root, venv active: `.\venv\Scripts\Activate.ps1`)
- Install:  `pip install -r requirements.txt`
- Run app:  `python -m uvicorn app.main:app --reload`  (http://localhost:8000, docs at /docs)
- Run tests: `python -m pytest`
- Seed synthetic data (app must be running): `python -m scripts.seed`  (safe to repeat)
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
   delete and re-seed (`python -m scripts.reset_db --yes`, then `python -m scripts.seed`).

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
- Startup (`lifespan` in `app/main.py`) runs `init_db()` after the token check: creates MISSING tables only.
  It never drops, alters or seeds, so a changed schema still needs `scripts.reset_db`. A fresh checkout needs no manual step.
- `DATABASE_URL` (default: `flowops.db` in the repo root). Importing `app.database` builds the engine.
- Tests: an autouse fixture in `tests/conftest.py` points `app.database.engine` at a tmp file so starting
  the app can never create `flowops.db`. Every test gets a fresh SQLite file in `tmp_path` (`engine`, `db_session`,
  `override_get_db` fixtures in `tests/conftest.py`); dispose the engine on teardown or
  Windows cannot delete the file.

## Workflow API (M3)
- Layers: `app/routers/` (thin `def` routes) -> `app/services/` (all DB work, commits) ->
  `app/models.py`. `app/schemas.py` holds separate input/output Pydantic models; never return ORM objects.
- `/api/workflows`: POST 201, GET list (active only; `?include_inactive=true`), GET/PATCH `/{id}`,
  DELETE `/{id}` = soft delete, 204, idempotent. 404 unknown id, 422 bad input. No hard delete.
- GET/PATCH by id also work on inactive workflows; re-activate with PATCH `{"is_active": true}`.
- Input schemas use `extra="forbid"` and strip whitespace; PATCH applies only fields sent
  (`exclude_unset`) and rejects null for name/source/is_active.
- API datetimes are serialized as aware UTC (`...Z`); the DB stays naive UTC.
- Tests: `client` fixture in `tests/conftest.py` = TestClient on the real app + per-test DB.

## Event ingestion (M4)
- `POST /api/events` (`app/routers/events.py` -> `app/services/events.py`). Requires header
  `X-FlowOps-Token` == `FLOWOPS_INGEST_TOKEN` (`app/auth.py`, `secrets.compare_digest` on bytes).
  The app refuses to start without the token (lifespan check in `app/main.py`); an empty configured
  token also rejects every request. Auth is checked before body validation (401 beats 422).
- Upsert on `external_event_id`: created 201; NON_TERMINAL -> TERMINAL updates the row in place
  (keeps id/created_at/started_at) 200 `updated`; anything else on an existing row is 200
  `unchanged` (TERMINAL is never overwritten; running -> waiting is also "unchanged"). No external
  id = always insert. Same id under another workflow = 409. Unknown workflow = 404; inactive accepted.
- `app/sanitize.py::sanitize_error` builds `error_summary` (redact -> collapse whitespace -> cap 500).
  Best-effort only; `error_raw` is stored unmodified and has no field in any response schema.
- Event `status` must already be a canonical FlowOps value; n8n's words (`error`, `crashed`...) are
  mapped in M7. Malformed timestamps are 422.
- Tests: `client` fixture sets the token to a known value; use the `auth_headers` fixture.
- `scripts/seed.py` runs against a live server (FLOWOPS_BASE_URL, default http://localhost:8000).

## Event queries (M5)
- `GET /api/events` (unauthenticated read, V1) -> `{items, total, limit, offset}`; `service.query_events`.
  Filters (AND): `workflow_id`, `status` (canonical enum), `started_from` (inclusive), `started_to` (exclusive).
- Bounds are UTC: naive = UTC, aware converted; a bare `YYYY-MM-DD` is 00:00 UTC, but for
  `started_to` it means 00:00 of the NEXT day (`app.timeutil.parse_query_bound`). Bad date = 422.
- Either bound excludes events whose `started_at` is NULL. Without bounds they list last
  (explicit `nulls_last()`: SQLite and PostgreSQL order NULLs differently).
- Order: `started_at` DESC, `id` DESC. `limit` 1..200 (default 50; above 200 is 422, never clamped), `offset` >= 0.
- Unknown `workflow_id` or from > to = empty page, not an error. `error_raw` is never in a response.

## Dashboard (M6)
- HTML pages: `GET /` (dashboard), `/workflows` (`?include_inactive=true`), `/events` (filters from the
  query string + `offset`, 25 per page). `app/routers/pages.py` (hidden from OpenAPI) -> services -> `app/templates/`.
- Templates only render what the route hands them; no queries or logic in them. They receive `EventRead`
  views (`dashboard.to_views`), which have NO `error_raw` field, so it cannot be rendered. Only `error_summary` is shown.
- `/events` page reuses `events_service.query_events` (same filter/date semantics as the API; the `to` date
  includes that whole day). Bad filter input shows a message, never a JSON error. Autoescaping is on; keep it.
- Dashboard status counts cover ALL events (zeros included); "recent" lists are the latest 10 events / 5 failures.
- Times: stored/passed as UTC, shown in the server's local zone via the `localtime` filter (`app/web.py`).
- Styles are inline in `base.html` (no static files, no JS, no CDN).

## Other conventions
- Dependencies are pinned in `requirements.txt`. Pre-authorized: fastapi, uvicorn,
  pydantic, python-dotenv, pytest, httpx2, sqlalchemy, jinja2. Ask before adding anything else.
- Secrets live in `.env` (git-ignored); keep `.env.example` current. Never hardcode secrets
  or use real production data in tests.
- Keep business logic out of route functions and templates.
- V1 is localhost-only; the dashboard has no auth by design (revisit before any deployment).
- If a milestone spec is wrong or inconsistent, say so and STOP — don't silently improve it.
- Update this file whenever commands or conventions change.
