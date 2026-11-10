"""Recompute every incident with the CURRENT signature rules.

Run from the repo root:  python -m scripts.rebuild_incidents

Needed after changing a rule in app/signature.py (and bumping SIGNATURE_VERSION). Safe to
repeat: a second run changes nothing. Failed events are re-attached to the incident their
current signature gives; incidents left with no events are removed (they are derived data;
no execution event is ever touched or deleted). Stop the app first to be safe.
"""
import sys

from sqlalchemy.orm import sessionmaker

from app import database
from app.services.incidents import rebuild_all
from app.signature import SIGNATURE_VERSION


def main() -> int:
    with sessionmaker(bind=database.engine)() as db:
        result = rebuild_all(db)
    print(
        f"Signature version {SIGNATURE_VERSION}: {result.failed_events} failed events -> "
        f"{result.incidents_total} incidents "
        f"({result.incidents_created} created, {result.incidents_removed} removed)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
