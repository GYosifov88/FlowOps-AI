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
- Pull n8n executions (needs N8N_BASE_URL + N8N_API_KEY in `.env`): `python -m scripts.sync_n8n` (`--loop 300` to repeat)
- Recompute incident grouping after a signature-rule change: `python -m scripts.rebuild_incidents` (idempotent)
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
- `Workflow` has UNIQUE (`source`, `external_id`) (M8; NULL external_ids never collide). A duplicate on
  POST/PATCH `/api/workflows` is a 409 (`DuplicateWorkflow`). Adding it needed a DB reset.
- `error_raw` is stored as received and NEVER rendered or returned; only the sanitized
  `error_summary` may be shown. ONE exception (V2-M2): `app/services/incident_context.py::error_projection` may expose
  an allow-listed, scrubbed projection of it (message, description, httpCode, <=15 stack lines) inside the incident
  context/preview only. The string itself, and every other key in it, still never leaves.
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

## Observability (M6.5)
- `app/logging_config.py`: `configure_logging(level, fmt)` runs first in `lifespan`. One tagged root handler
  (idempotent; never `dictConfig` on root, which would wipe pytest's caplog). `LOG_LEVEL` and
  `LOG_FORMAT` (text|json) are validated: a bad value stops startup. Log times are UTC with `Z`.
- Every record gets `request_id` via a LogRecord factory (from `request_id_var`, "-" outside requests).
  Use `logging.getLogger(__name__)`; never log tokens, headers, bodies or `error_raw`.
- `app/middleware.py` (pure ASGI): `X-Request-ID` reused if `[A-Za-z0-9._-]{1,64}`, else a new uuid4 hex;
  returned on every response; one `app.access` line per request (method, path, status, ms; no query string).
- Unhandled exceptions: caught in that middleware, traceback logged once (`app.errors`), client gets
  `500 {"detail": "Internal Server Error", "request_id": ...}`. Do not add `exception_handler(Exception)`.
- Text format escapes newlines in messages (log injection); uvicorn's access log is replaced by ours.
- Engine uses `hide_parameters=True` so DB errors don't print stored values into the log.
- Tests: autouse `restore_logging` fixture in `tests/conftest.py` resets global logging after each test.

## n8n pull sync (M8)
- Pull only (M7 decision: push deferred). `app/n8n_client.py` is GET-only (no write methods; `https` required
  unless localhost; no redirects; errors never carry the key or response bodies). `app/services/n8n_sync.py`
  does the work and writes through `ingest_event(..., summary_source=...)`; `scripts/sync_n8n.py` is the CLI
  (exit 0 ok / 1 n8n problem / 2 config). Run it as its own process; the web app does not schedule anything.
- Registered = `source == "n8n"`, active, `external_id` = the n8n workflow id (a string). Unregistered active
  n8n workflows are only REPORTED. `external_event_id` is `n8n:<execution id>`.
- Per workflow: list executions newest-first, ingest OLDEST first (one commit each), stop the walk at a known
  finished execution older than 24 h before FlowOps's newest (overlap window, so late finishers are seen), then
  re-check FlowOps's own non-finished rows (24 h give-up; 30 days for waiting). Pruned history -> "possible gap".
- Status: success; error/crashed -> failed; canceled; waiting; running/new -> running; unknown; null -> running
  (no stoppedAt) else unknown. `finished_at` is set only for terminal statuses.
- `node_name` comes ONLY from `resultData.error.node.name`; never from the last-node-executed field. Absent -> NULL.
- Data minimisation: the list call never asks for data; `includeData=true` only for executions that map to
  `failed`, one at a time; `ignoreDataSizeLimit`/`redactExecutionData` are never sent. `extract_error` is an
  ALLOW-LIST (name, message, description, httpCode, stack, node NAME); the rest of `error.node` (it carries
  credentials and parameters), runData, pinData, workflowData and customData are never kept or logged.
- Tests never touch a network: `tests/n8n_helpers.py` has `FakeN8n`; `tests/fixtures/n8n/` holds SCRUBBED real
  responses (fake ids/domains; `tests/test_fixtures_clean.py` guards them). Never commit unscrubbed captures.
- The assistant must NOT run the live sync against the real n8n instance: error text can hold customer data.
  The user runs it and reports counts.

## Incident grouping (V2-M1)
- `Incident` (`app/models.py`) groups FAILED events with the same signature; `ExecutionEvent.incident_id` is nullable
  (FK RESTRICT, indexed). Only `failed` events ever get one. `IncidentStatus` has only `open` until V2-M4.
  Adding the table/column needed a DB reset (then re-register the workflow and re-run the sync).
- `app/signature.py` (PURE, no DB): `signature = sha256("v1|workflow_id|node or <unattributed>|error_name|normalized_message")[:16]`.
  `|` and `%` inside a field are escaped. `error_name`/message come from the `error_raw` JSON (`name`, `message`); anything
  else (free text from the API, no error) uses `error_summary` and has no error name. `normalize_message`: `redact()` ->
  lowercase + whitespace -> `"x"`->`"<q>"` (single quotes kept), URL->`scheme://host/<path>`, e-mail, UUID, ISO date->`<ts>`,
  12+ char ids with a digit (or hex)->`<id>`, numbers of 2+ digits->`<n>` except standalone 4xx/5xx codes -> trailing
  punctuation off -> cap 300 (cap is LAST, never splits a placeholder). `tests/test_signature.py` GOLDEN table is the spec.
- ANY rule change = bump `SIGNATURE_VERSION` + run `python -m scripts.rebuild_incidents` (and update the golden tests/corpus).
  Rebuild reassigns all failed events, detaches non-failed ones, and DELETES incidents left with no events (derived data;
  execution events are never touched).
- `app/sanitize.py::redact()` is the shared secret scrubber (sanitize_error uses it too).
- `assign_incident` runs inside `ingest_event` (events.py) in the SAME transaction: ingest commits once at the end and, on an
  `IntegrityError` (lost race on external_event_id OR on a new incident signature), rolls back and retries once.
- Counters (`occurrence_count`, `first_seen`, `last_seen`) are NEVER stored; computed by query. "Seen" = `started_at`,
  else `created_at`.
- `GET /api/incidents` (`workflow_id`, `status`, `limit` 1..200, `offset`; last_seen DESC, id DESC) and
  `GET /api/incidents/{id}` (+ 10 newest events). Unauthenticated reads (V1). `EventRead` now has `incident_id`.
- `tests/corpus/incidents.json` (30 labelled failed events; NEVER loaded into the real DB). Same `group` => one incident,
  different `group` => never. Loaded by `tests/corpus_helpers.py` through the real sync mapping. n8n-shaped cases must match
  `tests/fixtures/n8n/F3_error_with_data.json`. Fake secrets in it must start with `FAKE` (guarded in `test_fixtures_clean.py`).
- No LLM, no API key, no new dependency (V2-M2 builds the context, V2-M3 adds the model).

## Incident context (V2-M2)
- `app/services/incident_context.py::build_context(db, incident_id)` = EXACTLY what V2-M3 will send to the model.
  `GET /api/incidents/{id}/context` previews it: `{context_version, context_hash, context}` (404 unknown id). M3 must call
  the same function and store the same hash. No LLM, no key, no new dependency, no schema change.
- Two sections: `trusted` (computed by FlowOps: ids, enums, counts, ISO-Z times; NO free text, `mode` is limited to n8n's
  known modes else `"other"`) and `untrusted` (workflow name/source, node, error name, normalized message, and the `error`
  projection from the LATEST occurrence; every string = `scrub` = `redact()` + `scrub_pii()`, capped). Third-party text goes
  ONLY in `untrusted`; keep it that way when adding fields. The tests pin the exact key sets: adding a field is a deliberate edit.
- Facts: last success before the first failure, successes after the last failure (count + first), and the window
  [first_seen, last_seen]: workflow successes, ALL workflow failures, this incident's failures. Only status `success` counts.
- `app/sanitize.py::scrub_pii`: e-mail, URL userinfo + query string, card/IBAN/9+ digit runs, phone numbers; ISO timestamps,
  line numbers and short ids are left alone. Best-effort: personal NAMES and street addresses are NOT caught; minimisation
  (allow-list, no run data) is the real control.
- Determinism: no clock/randomness; same events -> same context and `context_hash` (sha256 of the canonical JSON).
- `frame_context(ctx)` wraps the canonical JSON in `<untrusted_incident_data>...</untrusted_incident_data>` with `<`, `>`, `&`
  written as `\uXXXX`, so error text can never close or open the tag. V2-M3's system prompt must state that the block is
  untrusted data and the output schema must give the model no action to take.

## Other conventions
- Dependencies are pinned in `requirements.txt`. Pre-authorized: fastapi, uvicorn,
  pydantic, python-dotenv, pytest, httpx2, sqlalchemy, jinja2. Ask before adding anything else.
- Secrets live in `.env` (git-ignored); keep `.env.example` current. Never hardcode secrets
  or use real production data in tests.
- Keep business logic out of route functions and templates.
- V1 is localhost-only; the dashboard has no auth by design (revisit before any deployment).
- If a milestone spec is wrong or inconsistent, say so and STOP — don't silently improve it.
- Update this file whenever commands or conventions change.
