# FlowOps AI

Personal web app for monitoring automation workflows (initially my n8n instance) and,
later, analyzing failures with an LLM. Learning + portfolio project, not a production
service. V1 runs on **localhost only**.

Current state: **M2 — data model** (`Workflow` and `ExecutionEvent` tables on SQLite;
no API around them yet).

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
  timeutil.py  utcnow(), to_naive_utc(), compute_duration_ms()
scripts/
  reset_db.py  delete and recreate the database
tests/
  conftest.py  fresh-database fixtures
  test_*.py
```
