import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

import app.database as database
import scripts.sync_n8n as cli
from app.config import Settings
from app.models import ExecutionEvent, Workflow
from app.n8n_client import N8nAuthError, N8nUnavailable
from tests.n8n_helpers import WF, FakeN8n

KEY = "KEY-SECRET-CLI-1"
REPO_ROOT = Path(__file__).resolve().parent.parent


def settings(**overrides):
    values = dict(app_name="t", log_level="INFO", database_url="unused",
                  n8n_base_url="https://n8n.example.com", n8n_api_key=KEY)
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def cli_env(engine, db_session, monkeypatch):
    """Point the CLI at the per-test database and a fake n8n; returns the fake."""
    monkeypatch.setattr(database, "engine", engine)
    fake = FakeN8n()
    monkeypatch.setattr(cli, "get_settings", lambda: settings())
    monkeypatch.setattr(cli, "N8nClient", lambda base_url, api_key: fake)
    return fake


@pytest.fixture
def registered(db_session):
    db_session.add(Workflow(name="Example Workflow", source="n8n", external_id=WF))
    db_session.commit()


def event_count(db_session) -> int:
    db_session.expire_all()
    return db_session.scalar(select(func.count()).select_from(ExecutionEvent))


def test_a_run_prints_counts_warnings_and_unregistered_workflows(cli_env, registered, db_session, capsys):
    assert cli.main([]) == 0

    out = capsys.readouterr().out
    assert f"workflow 1 (n8n {WF}): created 9, updated 0, unchanged 0, skipped 0" in out
    assert "Active in n8n but not registered in FlowOps" in out and "wf_fixture_004" in out
    assert "Total: 9 created, 0 updated, 0 unchanged, 0 skipped" in out
    assert event_count(db_session) == 9


def test_a_second_run_reports_nothing_new(cli_env, registered, db_session, capsys):
    cli.main([])
    capsys.readouterr()

    assert cli.main([]) == 0

    assert "Total: 0 created, 0 updated, 0 unchanged, 0 skipped" in capsys.readouterr().out
    assert event_count(db_session) == 9


def test_each_run_logs_under_its_own_id_and_leaves_no_id_behind(cli_env, registered, caplog):
    from app.logging_config import request_id_var

    with caplog.at_level("INFO", logger="app.services.n8n_sync"):
        cli.main([])

    summary = [r for r in caplog.records if r.name == "app.services.n8n_sync"]
    assert summary and all(r.request_id.startswith("sync-") for r in summary)
    assert request_id_var.get() == "-"


def test_with_no_registered_workflow_it_says_what_to_do(cli_env, capsys):
    assert cli.main([]) == 0

    out = capsys.readouterr().out
    assert "No registered n8n workflows" in out and "external_id" in out


def test_a_rejected_key_exits_1_without_printing_the_key(cli_env, registered, monkeypatch, capsys, caplog):
    def rejected(*args, **kwargs):
        raise N8nAuthError("n8n rejected the API key (HTTP 401) for GET /executions")

    monkeypatch.setattr(cli_env, "list_executions", rejected)

    assert cli.main([]) == 1

    out = capsys.readouterr().out
    assert "HTTP 401" in out and KEY not in out and KEY not in caplog.text


def test_an_unreachable_n8n_exits_1(cli_env, registered, monkeypatch, capsys):
    def down(*args, **kwargs):
        raise N8nUnavailable("GET /workflows/x failed: ConnectError")

    monkeypatch.setattr(cli_env, "get_workflow", down)

    assert cli.main([]) == 1
    assert "ConnectError" in capsys.readouterr().out


@pytest.mark.parametrize("overrides, hint", [
    ({"n8n_base_url": ""}, "N8N_BASE_URL"),
    ({"n8n_api_key": ""}, "N8N_API_KEY"),
    ({"n8n_base_url": "http://n8n.example.com"}, "https"),
    ({"log_level": "LOUD"}, "LOG_LEVEL"),
])
def test_configuration_problems_exit_2_and_say_what_is_wrong(engine, monkeypatch, capsys, overrides, hint):
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(cli, "get_settings", lambda: settings(**overrides))

    assert cli.main([]) == 2

    out = capsys.readouterr().out
    assert hint in out and KEY not in out


def test_loop_interval_has_a_floor():
    with pytest.raises(SystemExit) as caught:
        cli.main(["--loop", "5"])

    assert caught.value.code == 2


def test_loop_mode_keeps_going_after_a_failed_run_until_interrupted(cli_env, registered, monkeypatch, capsys, db_session):
    real = cli_env.list_executions
    state = {"calls": 0, "sleeps": 0}

    def flaky(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise N8nUnavailable("GET /executions failed: ReadTimeout")
        return real(*args, **kwargs)

    def sleep(seconds):
        state["sleeps"] += 1
        assert seconds == 60
        if state["sleeps"] == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli_env, "list_executions", flaky)
    monkeypatch.setattr(cli.time, "sleep", sleep)

    assert cli.main(["--loop", "60"]) == 0

    out = capsys.readouterr().out
    assert "ReadTimeout" in out and "Total: 9 created" in out and "Stopped." in out
    assert event_count(db_session) == 9


def test_a_rejected_key_stops_loop_mode_instead_of_retrying_forever(cli_env, registered, monkeypatch):
    def rejected(*args, **kwargs):
        raise N8nAuthError("n8n rejected the API key (HTTP 401) for GET /executions")

    monkeypatch.setattr(cli_env, "list_executions", rejected)
    monkeypatch.setattr(cli.time, "sleep", lambda s: pytest.fail("must not sleep and retry"))

    assert cli.main(["--loop", "60"]) == 1


def test_running_it_as_a_module_without_configuration_exits_2(tmp_path):
    env = {**os.environ, "N8N_BASE_URL": "", "N8N_API_KEY": "",
           "DATABASE_URL": f"sqlite:///{(tmp_path / 'x.db').as_posix()}"}

    result = subprocess.run([sys.executable, "-m", "scripts.sync_n8n"], cwd=REPO_ROOT, env=env,
                            capture_output=True, text=True)

    assert result.returncode == 2
    assert "is not set" in result.stdout and ".env.example" in result.stdout
    assert "Traceback" not in result.stderr
    assert not (tmp_path / "x.db").exists()  # failed before touching any database
