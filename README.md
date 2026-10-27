# FlowOps AI

Personal web app for monitoring automation workflows (initially my n8n instance) and,
later, analyzing failures with an LLM. Learning + portfolio project, not a production
service. V1 runs on **localhost only**.

Current state: **M3 — workflow registry** (`/api/workflows` CRUD with soft delete, on top
of the M2 `Workflow` / `ExecutionEvent` tables; no event API yet).

## Setup (Windows PowerShell)

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Requires Python 3.12.

## Run

```powershell
python -m uvicorn app.main:app --reload
```

- Health check: http://localhost:8000/health -> `{"status": "ok"}`
- Interactive API docs: http://localhost:8000/docs

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

## Configuration

Settings come from environment variables, loaded from a local `.env` file via
python-dotenv. `.env` is git-ignored; `.env.example` is the committed template.

| Variable       | Default                      | Purpose            |
|----------------|------------------------------|--------------------|
| `APP_NAME`     | `FlowOps AI`                 | App title in /docs |
| `LOG_LEVEL`    | `INFO`                       | Log verbosity      |
| `DATABASE_URL` | `flowops.db` in the repo root | SQLite database    |

## Database

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
  routers/     HTTP routes (thin)
  services/    database logic
  timeutil.py  utcnow(), to_naive_utc(), compute_duration_ms()
scripts/
  reset_db.py  delete and recreate the database
tests/
  conftest.py  fresh-database fixtures
  test_*.py
```
