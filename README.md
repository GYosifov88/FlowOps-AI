# FlowOps AI

Personal web app for monitoring automation workflows (initially my n8n instance) and,
later, analyzing failures with an LLM. Learning + portfolio project, not a production
service. V1 runs on **localhost only**.

Current state: **M1 — project skeleton** (a `/health` endpoint and one test).

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

| Variable    | Default      | Purpose            |
|-------------|--------------|--------------------|
| `APP_NAME`  | `FlowOps AI` | App title in /docs |
| `LOG_LEVEL` | `INFO`       | Log verbosity      |

## Layout

```
app/
  main.py      FastAPI app and routes
  config.py    environment-based settings
tests/
  test_health.py
```
