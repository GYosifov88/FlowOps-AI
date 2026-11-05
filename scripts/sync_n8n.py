"""Pull executions of your registered n8n workflows into FlowOps (read-only towards n8n).

    python -m scripts.sync_n8n                 one run, then exit
    python -m scripts.sync_n8n --loop 300      repeat every 300 seconds (Ctrl+C to stop)

Needs N8N_BASE_URL and N8N_API_KEY in .env. A workflow is synced when it is registered in
FlowOps with source "n8n", is active, and has its n8n id as external_id. Exit codes:
0 ok, 1 n8n unreachable / rejected the key / unexpected response, 2 configuration problem.
"""
import argparse
import logging
import sys
import time
import uuid

from dotenv import load_dotenv

from app import database
from app.config import get_settings
from app.logging_config import configure_logging, request_id_var
from app.n8n_client import N8nAuthError, N8nClient, N8nConfigError, N8nError
from app.services.n8n_sync import SyncReport, sync_all

logger = logging.getLogger("scripts.sync_n8n")

MIN_LOOP_SECONDS = 30


def run_once(client: N8nClient) -> SyncReport:
    """One sync run in its own session, on whichever engine app.database currently uses."""
    from sqlalchemy.orm import sessionmaker

    with sessionmaker(bind=database.engine)() as db:
        return sync_all(db, client)


def print_report(report: SyncReport) -> None:
    if not report.workflows:
        print("No registered n8n workflows to sync. Register one with source 'n8n' and its n8n id as external_id.")
    for w in report.workflows:
        print(
            f"workflow {w.workflow_id} (n8n {w.external_id}): created {w.created}, "
            f"updated {w.updated}, unchanged {w.unchanged}, skipped {w.skipped}"
        )
        for message in w.warnings:
            print(f"  WARNING: {message}")
    if report.unregistered:
        print("Active in n8n but not registered in FlowOps (not synced):")
        for item in report.unregistered:
            print(f"  {item['id']}  {item['name']}")
    totals = report.totals
    print(
        "Total: " + ", ".join(f"{totals[k]} {k}" for k in ("created", "updated", "unchanged", "skipped"))
    )


def _parse_args(argv):
    parser = argparse.ArgumentParser(prog="python -m scripts.sync_n8n", description=__doc__.split("\n")[0])
    parser.add_argument("--loop", type=int, metavar="SECONDS",
                        help=f"repeat every SECONDS (at least {MIN_LOOP_SECONDS})")
    args = parser.parse_args(argv)
    if args.loop is not None and args.loop < MIN_LOOP_SECONDS:
        parser.error(f"--loop must be at least {MIN_LOOP_SECONDS} seconds")
    return args


def main(argv=None) -> int:
    args = _parse_args(argv)
    load_dotenv()
    settings = get_settings()
    try:
        configure_logging(settings.log_level, settings.log_format)
    except ValueError as exc:
        print(exc)
        return 2
    logging.getLogger("httpx2").setLevel(logging.WARNING)  # its INFO lines repeat every URL

    try:
        client = N8nClient(settings.n8n_base_url, settings.n8n_api_key)
    except N8nConfigError as exc:
        print(f"Configuration problem: {exc}. See .env.example.")
        return 2

    database.init_db()  # creates MISSING tables only, like app startup
    try:
        with client:
            while True:
                # Every log line of this run carries "sync-<id>"; reset so nothing leaks.
                run_id = request_id_var.set(f"sync-{uuid.uuid4().hex[:8]}")
                try:
                    print_report(run_once(client))
                except N8nAuthError as exc:
                    print(f"n8n sync failed: {exc}")
                    return 1
                except N8nError as exc:
                    print(f"n8n sync failed: {exc}")
                    if args.loop is None:
                        return 1
                    logger.error("n8n sync failed (will retry): %s", exc)
                finally:
                    request_id_var.reset(run_id)
                if args.loop is None:
                    return 0
                time.sleep(args.loop)
    except KeyboardInterrupt:
        print("Stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
