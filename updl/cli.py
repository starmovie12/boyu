"""Command-line entry point (`python -m updl <command> [options]`).

Commands:
  probe | plan | download | continue | month | compact | validate | derive | report | handoff | verify | ingest
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from .client import HttpClient, RateLimiter
from .config import load_settings
from .derive import run_derive
from .errors import (EXIT_OK, EXIT_OTHER_ERROR, EXIT_SOME_NOT_OK, UpdlError)
from .handoff import run_handoff, run_report, run_verify
from .ingest import run_ingest
from .messages import DATA_RIGHTS_WARNING, RIGHTS_NOT_ALLOWED
from .monthly import run_monthly_continue
from .plan import run_download, run_plan_command
from .probe import run_probe
from .runlog import RunLog
from .sessions import load_calendar
from .store import compact
from .tokens import load_tokens
from .validate import run_validate

COMMANDS = ("probe", "plan", "download", "continue", "month", "compact",
            "validate", "derive", "report", "handoff", "verify", "ingest")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m updl",
        description="Read-only Upstox historical market-data downloader, validator & derived-table builder (with Termux 1-month continue mode)."
    )
    p.add_argument("command", choices=COMMANDS, help="Stage command to execute")
    p.add_argument("target", nargs="?", default=None, help="File or directory path for `ingest`")
    p.add_argument("--manifest", default="data_manifest.yaml", help="Path to data_manifest.yaml")
    p.add_argument("--workdir", default="./updl_data", help="Working data folder")
    p.add_argument("--test-start", default=None, help="Override TEST_START (YYYY-MM-DD)")
    p.add_argument("--test-end", default=None, help="Override TEST_END (YYYY-MM-DD)")
    p.add_argument("--holdout-start", default=None, help="Override HOLDOUT_START (YYYY-MM-DD)")
    p.add_argument("--plan", default=None, choices=("A", "A_PARTIAL", "B", "C"), help="Override selected plan")
    p.add_argument("--items", default=None, help="Comma-separated item IDs (e.g. D01,D05)")
    p.add_argument("--dry-run", action="store_true", help="Zero network calls; print selection plan & counts")
    p.add_argument("--rate", type=float, default=None, help="Override RATE_CALLS_PER_MIN")
    p.add_argument("--stop-at", default=None, help="Stop cleanly at HH:MM IST")
    p.add_argument("--max-calls", type=int, default=None, help="Stop after N API calls")
    p.add_argument("--log-level", default="INFO", help="Logging level")
    p.add_argument("--no-refetch-last-day", action="store_true", help="Disable re-fetching last day on next run")
    p.add_argument("--month-step", action="store_true",
                   help="Download 1 month at a time going backwards (same as `continue` command)")
    p.add_argument("--today", default=None, help="Override today's date in IST (YYYY-MM-DD) for month continue mode")
    p.add_argument("--phone-dir", default=None, help="Optional phone output folder for monthly zip packages")
    p.add_argument("--with-warmup", action="store_true", help="Include warmup window during 1-month continue mode")
    p.add_argument("--synthetic-fixture", action="store_true", help="Label output as SYNTHETIC_FIXTURE")
    return p


class _NoNetworkTransport:
    def get(self, *args, **kwargs):
        raise RuntimeError("Network calls are disabled in dry-run mode")


def main(argv: Optional[list] = None, transport=None, environ=None, sleep=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        settings = load_settings(
            manifest_path=args.manifest,
            workdir=args.workdir,
            test_start=args.test_start,
            test_end=args.test_end,
            holdout_start=args.holdout_start,
            plan=args.plan,
            items=args.items,
            dry_run=args.dry_run,
            rate=args.rate,
            stop_at=args.stop_at,
            max_calls=args.max_calls,
            log_level=args.log_level,
        )
        if args.no_refetch_last_day:
            settings.refetch_last_day = False

        if settings.data_rights == "NOT_ALLOWED":
            print(RIGHTS_NOT_ALLOWED)
            return 40

        tokens = load_tokens(environ=environ)
        secrets = tokens.secret_values()
        rlog = RunLog(logs_dir=None if settings.dry_run else settings.logs_dir, secrets=secrets)
        calendar = load_calendar(settings.reference_dir)

        kw_lim = {"calls_per_min": settings.rate_per_min, "max_per_30min": settings.max_per_30min}
        if sleep is not None:
            kw_lim["sleep"] = sleep
        limiter = RateLimiter(**kw_lim)

        eff_transport = (_NoNetworkTransport() if settings.dry_run and transport is None else transport)
        kw_cli = {"tokens": tokens, "transport": eff_transport, "limiter": limiter, "logger": rlog}
        if sleep is not None:
            kw_cli["sleep"] = sleep
        client = HttpClient(**kw_cli)

        cmd = args.command
        if cmd == "probe":
            run_probe(settings, client, calendar=calendar, probe_today=args.today)
            return EXIT_OK

        if cmd == "plan":
            run_plan_command(settings, calendar=calendar)
            return EXIT_OK

        if cmd in ("continue", "month") or (cmd == "download" and args.month_step):
            run_monthly_continue(
                settings, client, calendar=calendar,
                today_override=args.today, with_warmup=args.with_warmup,
                phone_dir=args.phone_dir, run_id=rlog.run_id,
                log=rlog, secrets=secrets
            )
            return EXIT_OK

        if cmd == "download":
            run_download(settings, client, calendar=calendar, run_id=rlog.run_id, log=rlog, secrets=secrets)
            return EXIT_OK

        if settings.data_rights == "UNVERIFIED":
            print(DATA_RIGHTS_WARNING)

        if cmd == "compact":
            compact(settings, calendar, log=rlog)
            return EXIT_OK

        if cmd == "validate":
            res = run_validate(settings, calendar=calendar, log=rlog)
            if res["counts"].get("NOT OK", 0) > 0:
                return EXIT_SOME_NOT_OK
            return EXIT_OK

        if cmd == "derive":
            run_derive(settings, calendar=calendar)
            return EXIT_OK

        if cmd == "report":
            run_report(settings, calendar=calendar)
            return EXIT_OK

        if cmd == "handoff":
            run_handoff(settings, synthetic_fixture=args.synthetic_fixture)
            return EXIT_OK

        if cmd == "verify":
            run_verify(settings)
            return EXIT_OK

        if cmd == "ingest":
            if not args.target:
                print("Usage: python -m updl ingest <file_or_dir>")
                return EXIT_OTHER_ERROR
            run_ingest(settings, args.target, calendar=calendar)
            return EXIT_OK

        return EXIT_OK

    except UpdlError as err:
        if err.message:
            print(err.message)
        return err.exit_code
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}")
        return EXIT_OTHER_ERROR
