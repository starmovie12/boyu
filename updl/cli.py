"""Command-line entry point (`python -m updl <command> [options]`).

Commands:
  probe | plan | download | continue | month | compact | validate | derive | report | handoff | verify | ingest
  continue = one WEEK per run (newest first, then one week further back each time); `--window-days 0` = old month mode
  export (phone CSV zip, option chain at time T) | record (live option-chain Recorder)
  repair (queue failed chunks again; good chunks are never downloaded twice)
"""
from __future__ import annotations

import argparse
from typing import Optional

from .client import HttpClient, RateLimiter
from .config import load_settings
from .derive import run_derive
from .errors import (EXIT_OK, EXIT_OTHER_ERROR, EXIT_SOME_NOT_OK, UpdlError)
from .export import DEFAULT_EVERY_MIN, run_export
from .handoff import run_handoff, run_report, run_verify
from .ingest import run_ingest
from .messages import DATA_RIGHTS_WARNING, RIGHTS_NOT_ALLOWED
from .calsync import cached_calendar
from .monthly import run_monthly_continue
from .plan import run_download, run_plan_command
from .probe import run_probe
from .progress import Progress
from .recorder import run_record
from .repair import run_repair
from .runlock import RunLock, startup_checks
from .runlog import RunLog
from .sessions import load_calendar
from .store import compact
from .tokens import load_tokens
from .validate import run_validate
from .weekly import DEFAULT_WINDOW_DAYS, run_week_batches

COMMANDS = ("probe", "plan", "download", "continue", "month", "compact",
            "validate", "derive", "report", "handoff", "verify", "ingest", "export", "record", "repair")
LOCKED_COMMANDS = ("download", "continue", "month", "record", "repair")     # one writer per workdir


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m updl",
        description="Read-only Upstox historical market-data downloader, validator & derived-table builder (Termux: one week per `continue`)."
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
                   help="download: run one `continue` batch instead of the whole window")
    p.add_argument("--today", default=None, help="Override today's date in IST (YYYY-MM-DD) for continue mode")
    p.add_argument("--phone-dir", default=None,
                   help="Phone output folder (default: Download/BOYU_Data, or env UPDL_PHONE_DIR)")
    p.add_argument("--with-warmup", action="store_true", help="Old month mode only: include the option warm-up window")
    p.add_argument("--window-days", type=int, default=None,
                   help=f"continue: calendar days per batch (default {DEFAULT_WINDOW_DAYS}; 0 = old calendar-month mode)")
    p.add_argument("--weeks", type=int, default=1, help="continue: run this many batches in one go (default 1)")
    p.add_argument("--reset-cursor", action="store_true",
                   help="continue: forget the batch cursor and start again at today (covered days are not re-downloaded)")
    p.add_argument("--back-to", default=None, help="continue: oldest date to walk back to (default 2024-10-01)")
    p.add_argument("--workers", type=int, default=None, help="Parallel download workers (default from manifest, 1..8)")
    p.add_argument("--max-per-30min", type=int, default=None, help="Override RATE_MAX_PER_30MIN")
    p.add_argument("--profile", action="store_true", help="continue: print seconds per stage at the end")
    p.add_argument("--synthetic-fixture", action="store_true", help="Label output as SYNTHETIC_FIXTURE")
    p.add_argument("--every", type=int, default=None,
                   help="export: minutes between chain snapshots (default 15); record: seconds between snapshots (default 60)")
    p.add_argument("--underlyings", default=None, help="record: comma-separated, e.g. NIFTY,SENSEX (default all)")
    p.add_argument("--until", default=None, help="record: stop at HH:MM IST (default: session close)")
    p.add_argument("--once", action="store_true", help="record: take one snapshot now and stop")
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
        if args.max_per_30min is not None:
            settings.set_param("RATE_MAX_PER_30MIN", int(args.max_per_30min))
        workers = args.workers if args.workers is not None else int(settings.params.get("WORKERS", 1))
        settings.workers = max(1, min(8, int(workers)))

        if settings.data_rights == "NOT_ALLOWED":
            print(RIGHTS_NOT_ALLOWED)
            return 40

        tokens = load_tokens(environ=environ)
        secrets = tokens.secret_values()
        rlog = RunLog(logs_dir=None if settings.dry_run else settings.logs_dir, secrets=secrets)
        calendar = cached_calendar(settings, load_calendar(settings.reference_dir))   # + API facts already cached

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
        if cmd in LOCKED_COMMANDS and not settings.dry_run:
            with RunLock(settings.state_dir, cmd):
                startup_checks(settings)
                return _dispatch(cmd, args, settings, client, limiter, calendar, rlog, secrets, sleep)
        return _dispatch(cmd, args, settings, client, limiter, calendar, rlog, secrets, sleep)

    except UpdlError as err:
        if err.message:
            print(err.message)
        return err.exit_code
    except Exception as exc:
        print(f"Error: {type(exc).__name__}: {exc}")
        return EXIT_OTHER_ERROR


def _dispatch(cmd, args, settings, client, limiter, calendar, rlog, secrets, sleep) -> int:
    """Run one command (inside the run lock for commands that write the state)."""
    if True:
        if cmd == "repair":
            run_repair(settings, calendar=calendar)
            return EXIT_OK

        if cmd == "probe":
            run_probe(settings, client, calendar=calendar, probe_today=args.today)
            return EXIT_OK

        if cmd == "plan":
            run_plan_command(settings, calendar=calendar)
            return EXIT_OK

        if cmd in ("continue", "month") or (cmd == "download" and args.month_step):
            window_days = args.window_days if args.window_days is not None else int(
                settings.params.get("WINDOW_DAYS", DEFAULT_WINDOW_DAYS))
            if window_days <= 0:      # the old calendar-month mode, unchanged
                run_monthly_continue(
                    settings, client, calendar=calendar,
                    today_override=args.today, with_warmup=args.with_warmup,
                    phone_dir=args.phone_dir, run_id=rlog.run_id,
                    log=rlog, secrets=secrets
                )
                return EXIT_OK
            progress = None
            if not settings.dry_run:
                progress = Progress(limiter=limiter).start()
                if sleep is None:          # never sleep silently: waits are sliced and the line keeps moving
                    client.sleep = progress.sleep
                    limiter.sleep = progress.sleep
            try:
                res = run_week_batches(
                    settings, client, calendar=load_calendar(settings.reference_dir),
                    today_override=args.today, window_days=window_days, weeks=args.weeks,
                    phone_dir=args.phone_dir, run_id=rlog.run_id, log=rlog, secrets=secrets,
                    reset_cursor=args.reset_cursor, until=args.back_to, progress=progress
                )
            finally:
                if progress is not None:
                    progress.stop()
            if args.profile and res.get("stage_seconds"):
                print("PROFILE (seconds): " + ", ".join(f"{k}={v:.1f}" for k, v in res["stage_seconds"].items())
                      + f" | total={res.get('seconds')} | calls={res.get('calls')}")
            return EXIT_OK

        if cmd == "download":
            run_download(settings, client, calendar=calendar, run_id=rlog.run_id, log=rlog, secrets=secrets)
            return EXIT_OK

        if cmd == "record":
            if settings.data_rights == "UNVERIFIED":
                print(DATA_RIGHTS_WARNING)
            if settings.dry_run:
                print("record --dry-run: Recorder network ke bina kuch nahi kar sakta. Kuch nahi kiya.")
                return EXIT_OK
            unds = [u.strip() for u in args.underlyings.split(",")] if args.underlyings else None
            run_record(settings, client, calendar=calendar, underlyings=unds,
                       every_sec=args.every or 60, until=args.until, once=args.once, log=rlog)
            return EXIT_OK

        if cmd == "export":
            run_export(settings, calendar=calendar, every_min=args.every or DEFAULT_EVERY_MIN,
                       phone_dir=args.phone_dir)
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
