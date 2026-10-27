import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect
from sqlalchemy.orm import Session

import scripts.reset_db as reset_db
from app.config import Settings
from app.database import create_db_engine
from app.models import Workflow

REPO_ROOT = Path(__file__).resolve().parent.parent


def sqlite_url(path) -> str:
    return f"sqlite:///{path.as_posix()}"


def run_cli(database_url: str, *args: str) -> subprocess.CompletedProcess:
    """Run `python -m scripts.reset_db` for real, in a fresh process."""
    return subprocess.run(
        [sys.executable, "-m", "scripts.reset_db", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
    )


def use_database(monkeypatch, url: str) -> None:
    monkeypatch.setattr(
        reset_db,
        "get_settings",
        lambda: Settings(app_name="test", log_level="INFO", database_url=url),
    )


def test_reset_replaces_existing_data_and_stale_wal_files(tmp_path):
    db_file = tmp_path / "reset.db"
    url = sqlite_url(db_file)
    reset_db.reset_database(url)
    engine = create_db_engine(url)
    with Session(engine) as session:
        session.add(Workflow(name="old", source="n8n"))
        session.commit()
    engine.dispose()
    # Leftovers a crashed run could have left behind.
    (tmp_path / "reset.db-wal").write_bytes(b"stale")
    (tmp_path / "reset.db-shm").write_bytes(b"stale")

    reset_db.reset_database(url)

    engine = create_db_engine(url)
    try:
        assert "workflows" in inspect(engine).get_table_names()
        with Session(engine) as session:
            assert session.query(Workflow).count() == 0
    finally:
        engine.dispose()
    assert not (tmp_path / "reset.db-wal").exists()
    assert not (tmp_path / "reset.db-shm").exists()


def test_reset_refuses_a_non_sqlite_database():
    with pytest.raises(ValueError, match="SQLite"):
        reset_db.reset_database("postgresql://user:secret@localhost/flowops")


def test_main_without_yes_does_nothing(tmp_path, monkeypatch, capsys):
    db_file = tmp_path / "keep.db"
    db_file.write_bytes(b"precious")
    use_database(monkeypatch, sqlite_url(db_file))

    assert reset_db.main([]) == 1

    assert db_file.read_bytes() == b"precious"
    assert "--yes" in capsys.readouterr().out


def test_main_with_yes_creates_a_fresh_schema(tmp_path, monkeypatch):
    db_file = tmp_path / "fresh.db"
    use_database(monkeypatch, sqlite_url(db_file))

    assert reset_db.main(["--yes"]) == 0

    engine = create_db_engine(sqlite_url(db_file))
    try:
        assert {"workflows", "execution_events"} <= set(
            inspect(engine).get_table_names()
        )
    finally:
        engine.dispose()


def test_cli_refuses_non_sqlite_cleanly_even_without_its_driver():
    """Regression: used to die with a psycopg2 ImportError traceback instead of refusing."""
    result = run_cli("postgresql://user:secret@localhost/flowops", "--yes")

    assert result.returncode == 1
    assert "SQLite" in result.stdout
    assert "Traceback" not in result.stderr
    assert "secret" not in result.stdout + result.stderr


def test_cli_runs_as_a_module_and_creates_the_schema(tmp_path):
    db_file = tmp_path / "cli.db"

    result = run_cli(sqlite_url(db_file), "--yes")

    assert result.returncode == 0, result.stderr
    engine = create_db_engine(sqlite_url(db_file))
    try:
        assert "workflows" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_main_with_yes_still_refuses_non_sqlite(monkeypatch, capsys):
    use_database(monkeypatch, "postgresql://user:secret@localhost/flowops")

    assert reset_db.main(["--yes"]) == 1

    out = capsys.readouterr().out
    assert "SQLite" in out
    assert "secret" not in out  # never echo credentials
