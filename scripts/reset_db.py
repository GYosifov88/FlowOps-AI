"""Delete the SQLite database and recreate an empty schema.

Run from the repo root:  python -m scripts.reset_db --yes

DESTRUCTIVE: all data is lost. That is the V1 workflow -- there are no migrations, so a
schema change means reset, then re-seed.
"""
import argparse
import sys
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import get_settings


def reset_database(url: str) -> None:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        raise ValueError("reset_db only works on SQLite databases.")

    # Imported only after the guard above: app.database builds the app's engine at import
    # time, which would crash on a non-SQLite URL whose driver is not installed, before
    # we could refuse politely.
    from app.database import create_db_engine, init_db

    if parsed.database and parsed.database != ":memory:":
        path = Path(parsed.database)
        # WAL mode keeps -wal/-shm files next to the database; stale ones must go too.
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{path}{suffix}").unlink(missing_ok=True)

    engine = create_db_engine(url)
    try:
        init_db(engine)
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete the SQLite database and recreate an empty schema."
    )
    parser.add_argument(
        "--yes", action="store_true", help="confirm that ALL data should be deleted"
    )
    args = parser.parse_args(argv)

    if not args.yes:
        print("Refusing to run without --yes: this deletes the database.")
        return 1

    url = get_settings().database_url
    try:
        reset_database(url)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    except PermissionError:
        print("Error: the database file is in use. Stop the app and try again.")
        return 1

    print(f"Database reset: {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
