# FlowOps AI

Personal web app for monitoring automation workflows (initially my n8n instance) and,
later, analyzing failures with an LLM. Learning + portfolio project, not a production
service. V1 runs on **localhost only**.

Current state: **M6 — dashboard** (HTML pages on top of the workflow CRUD, token-protected event
ingestion and the event query API).

## Setup (Windows PowerShell)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# then set FLOWOPS_INGEST_TOKEN in .env, e.g. the output of:
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Requires Python 3.12.

## Run

```powershell
python -m uvicorn app.main:app --reload
```

- Health check: http://localhost:8000/health -> `{"status": "ok"}`
- Interactive API docs: http://localhost:8000/docs
- Dashboard: http://localhost:8000/ (also `/workflows` and `/events`). No login in V1, localhost only.
  Run `python -m scripts.seed` for sample data.

## Test

```powershell
python -m pytest
```

## Workflow API

| Method & path | Does |
|---|---|
| `POST /api/workflows` | create (201) |
| `GET /api/workflows` | list active; `?include_inactive=true` for all |
| `GET /api/workflows/{id}` | one workflow (inactive too) |
| `PATCH /api/workflows/{id}` | partial update; `{"is_active": true}` re-activates |
| `DELETE /api/workflows/{id}` | soft delete (204); the row and its events are kept |

Try it in the interactive docs at http://localhost:8000/docs.

## Event ingestion

`POST /api/events` records one execution event. It needs the header `X-FlowOps-Token` (401
otherwise). Events are upserted on `external_event_id`: a `running` event later sent as
`success`/`failed`/`canceled` updates the same row; finished events are never overwritten.
The response is `{"result": "created" | "updated" | "unchanged", "event": {...}}`. The raw error
text is stored but never returned; only a redacted, length-capped `error_summary` is.

With the app running, load synthetic data (safe to repeat):

```powershell
python -m scripts.seed
```

## Querying events

`GET /api/events` (no token needed in V1) returns `{"items": [...], "total": N, "limit": 50, "offset": 0}`,
newest first.

| Parameter | Meaning |
|---|---|
| `workflow_id`, `status` | exact match (`success`, `failed`, `running`, `waiting`, `canceled`, `unknown`) |
| `started_from` | `started_at >=` this, inclusive |
| `started_to` | `started_at <` this, exclusive; a bare date like `2026-10-27` covers all of that day |
| `limit`, `offset` | paging; `limit` 1-200 (default 50), above 200 is a 422 |

Dates are UTC (a bare date is 00:00 UTC; URL-encode a `+` offset as `%2B`). Events with no
`started_at` are left out whenever `started_from` or `started_to` is used.

## Configuration

Settings come from environment variables, loaded from a local `.env` file via
python-dotenv. `.env` is git-ignored; `.env.example` is the committed template.

| Variable       | Default                      | Purpose            |
|----------------|------------------------------|--------------------|
| `APP_NAME`     | `FlowOps AI`                 | App title in /docs |
| `LOG_LEVEL`    | `INFO`                       | Log verbosity      |
| `DATABASE_URL` | `flowops.db` in the repo root | SQLite database    |
| `FLOWOPS_INGEST_TOKEN` | *(required)*        | Shared secret for `POST /api/events`; the app will not start without it |
| `FLOWOPS_BASE_URL` | `http://localhost:8000`     | Only used by `scripts.seed` |

## Database

The app creates any missing tables when it starts, so a fresh checkout works without setup
(it never drops or alters existing tables, and never adds data).

The database is **throwaway during V1**: there are no migrations. After changing the
schema, delete and recreate it (this erases all data):

```powershell
python -m scripts.reset_db --yes
```

Run it from the repo root with the app stopped. It only works on SQLite. SQLite runs in WAL
mode, so you will also see `flowops.db-wal` and `flowops.db-shm` next to the database
file; both are git-ignored.

## Layout

```
app/
  main.py      FastAPI app and routes
  config.py    environment-based settings
  database.py  engine, sessions, SQLite pragmas, UTC datetime column type
  models.py    Workflow, ExecutionEvent, ExecutionStatus
  schemas.py   Pydantic input/output models
  routers/     HTTP routes (thin); pages.py renders the HTML pages
  templates/   Jinja2 templates
  web.py       Jinja2 setup and display filters
  services/    database logic
  auth.py      ingest token check
  sanitize.py  error text redaction
  timeutil.py  utcnow(), to_naive_utc(), compute_duration_ms()
scripts/
  reset_db.py  delete and recreate the database
  seed.py      post synthetic data to a running app
tests/
  conftest.py  fresh-database fixtures
  test_*.py
```
