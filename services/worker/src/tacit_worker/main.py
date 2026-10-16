"""Run durable maintenance without silently invoking paid models."""

import argparse
import logging
import signal
import threading

from sqlalchemy import create_engine
from tacit_api.access import AccessService
from tacit_api.app import Settings
from tacit_api.github import GitHub
from tacit_api.worker import cycle


def main() -> None:
    parser = argparse.ArgumentParser(description="Tacit transactional relay and retention worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=15)
    args = parser.parse_args()
    if not 1 <= args.interval <= 60:
        parser.error("interval must be 1–60 seconds")
    settings = Settings()
    if (
        not all(
            (
                settings.database_url,
                settings.github_client_id,
                settings.github_app_id,
                settings.credential_encryption_key,
            )
        )
        or settings.demo
    ):
        raise SystemExit("Worker requires migrated PostgreSQL and production GitHub configuration")
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    if engine.dialect.name != "postgresql":
        raise SystemExit("Worker requires PostgreSQL")
    access = AccessService(
        engine,
        GitHub(settings.github_client_id, settings.github_app_id),
        settings.credential_encryption_key,
    )
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    logging.basicConfig(level=logging.INFO)
    try:
        while not stop.is_set():
            try:
                logging.info("Maintenance cycle: %s", cycle(access, settings))
            except Exception:
                logging.error(
                    "Maintenance cycle failed; retrying without acknowledging unfinished work"
                )
                if args.once:
                    raise SystemExit(1) from None
            if args.once:
                break
            stop.wait(args.interval)
    finally:
        engine.dispose()
