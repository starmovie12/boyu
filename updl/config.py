"""Manifest loading and run settings. No instrument or date is hard-coded in Python: all of it comes from data_manifest.yaml."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .timeutil import to_date

PLANS = ("A", "A_PARTIAL", "B", "C")
RIGHTS = ("OK", "MINIMAL", "NOT_ALLOWED", "UNVERIFIED")
ITEM_ORDER = ["D12", "D01", "D02", "D03", "D04", "D09", "D07", "D05", "D06", "D08", "D10", "D11"]


def load_yaml(path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class Settings:
    manifest: dict
    root: Path = field(default_factory=lambda: Path("."))
    workdir: Path = field(default_factory=lambda: Path("updl_data"))
    reference_dir: Optional[Path] = None
    external_dir: Optional[Path] = None
    plan_override: Optional[str] = None
    items_filter: Optional[list] = None
    dry_run: bool = False
    stop_at: Optional[str] = None
    max_calls: Optional[int] = None
    log_level: str = "INFO"
    refetch_last_day: bool = True
    workers: int = 1                      # download worker threads (1 = sequential, the old behaviour)
    progress: Optional[object] = None     # updl.progress.Progress or None (no live line)
    warmup_items: Optional[set] = None    # None = warm-up for every item; a set = only these item ids (week mode)
    skip_items: set = field(default_factory=set)          # items switched off for this run (fetch: D11)
    only_index: Optional[str] = None      # fetch: the one index this run works on (restrict_to)
    stock_groups: tuple = ("HEAVY", "BANK")               # D09 stock groups (all indices: the old behaviour)
    wide_stock_groups: tuple = ("NIFTY50", "SENSEX")      # D10 stock groups

    def __post_init__(self):
        self.root = Path(self.root)
        self.workdir = Path(self.workdir)
        self.reference_dir = Path(self.reference_dir) if self.reference_dir else self.root / "reference"
        self.external_dir = Path(self.external_dir) if self.external_dir else self.root / "external"
        self.params = dict(self.manifest.get("params", {}))
        self.instruments = dict(self.manifest.get("instruments", {}))
        self.items = {it["id"]: it for it in self.manifest.get("items", [])}

    # ---- parameter access -------------------------------------------------
    def set_param(self, name, value):
        self.params[name] = value

    @property
    def test_start(self) -> dt.date:
        return to_date(self.params["TEST_START"])

    @property
    def test_end(self) -> dt.date:
        return to_date(self.params["TEST_END"])

    @property
    def holdout_start(self) -> dt.date:
        return to_date(self.params["HOLDOUT_START"])

    @property
    def rate_per_min(self) -> float:
        return float(self.params["RATE_CALLS_PER_MIN"])

    @property
    def max_per_30min(self) -> int:
        return int(self.params["RATE_MAX_PER_30MIN"])

    def warmup_for(self, item_id: str, include_warmup: bool) -> bool:
        return bool(include_warmup and (self.warmup_items is None or item_id in self.warmup_items))

    @property
    def data_rights(self) -> str:
        value = str(self.params.get("DATA_RIGHTS", "UNVERIFIED")).upper()
        return value if value in RIGHTS else "UNVERIFIED"

    @property
    def warmup_1m_start(self) -> dt.date:
        return self.test_start - dt.timedelta(days=int(self.params["WARMUP_SPOT_1M_CALENDAR_DAYS"]))

    @property
    def warmup_daily_start(self) -> dt.date:
        return self.test_start - dt.timedelta(days=int(self.params["WARMUP_DAILY_CALENDAR_DAYS"]))

    def option_warmup_days(self, underlying: str) -> int:
        return int(self.params["WARMUP_OPTION_TRADING_DAYS_BY_INDEX"].get(underlying, 7))

    def instrument_names(self):
        return [k for k in self.instruments if k != "INDIAVIX"]

    def menu_indices(self) -> list:
        """Indices offered by the `fetch` menu, sorted by the manifest's menu_no."""
        rows = [(int(v["menu_no"]), k) for k, v in self.instruments.items() if v.get("menu_no") is not None]
        return [k for _n, k in sorted(rows)]

    def index_label(self, underlying: str) -> str:
        return str(self.instruments.get(underlying, {}).get("label") or underlying)

    def resolve_index(self, text: str) -> Optional[str]:
        """'banknifty', 'BANK NIFTY', 'nifty 50' -> the manifest name, or None."""
        want = str(text or "").replace(" ", "").replace("_", "").upper()
        for k in self.menu_indices():
            if want in (k.upper(), self.index_label(k).replace(" ", "").upper()):
                return k
        return None

    def restrict_to(self, index: str, with_globals: bool = False) -> str:
        """fetch: work on ONE index (+ India VIX, which the strike band needs). Every loop over
        settings.instruments / instrument_names() then sees only that index. D09/D10 stock groups come from the
        index's manifest row; D11 (globals) is switched off unless with_globals."""
        name = self.resolve_index(index)
        if name is None:
            raise ValueError(f"unknown index {index!r}")
        cfg = self.instruments[name]
        self.instruments = {k: v for k, v in self.instruments.items() if k in (name, "INDIAVIX")}
        self.only_index = name
        self.stock_groups = tuple(cfg.get("stock_groups") or ())
        self.wide_stock_groups = tuple(cfg.get("wide_stock_groups") or ())
        if not with_globals:
            self.skip_items = set(self.skip_items) | {"D11"}
        return name

    def is_monthly(self, underlying: str) -> bool:
        return str(self.instruments[underlying].get("expiry", "")).startswith("monthly")

    def exchange_of(self, underlying: str) -> str:
        return self.instruments[underlying]["exchange"]

    def spot_key(self, underlying: str) -> str:
        return self.instruments[underlying]["spot_key"]

    def item_selected(self, item_id: str) -> bool:
        return (self.items_filter is None or item_id in self.items_filter) and item_id not in self.skip_items

    # ---- folder layout ----------------------------------------------------
    @property
    def raw_dir(self):
        return self.workdir / "raw"

    @property
    def state_dir(self):
        return self.workdir / "state"

    @property
    def store_dir(self):
        return self.workdir / "store"

    @property
    def logs_dir(self):
        return self.workdir / "logs"

    @property
    def reports_dir(self):
        return self.workdir / "reports"

    @property
    def handoff_dir(self):
        return self.workdir / "handoff"

    @property
    def probe_json(self):
        return self.workdir / "probe_report.json"

    @property
    def probe_txt(self):
        return self.workdir / "probe_report.txt"

    def ensure_dirs(self):
        for p in (self.raw_dir, self.state_dir, self.store_dir, self.logs_dir, self.reports_dir):
            p.mkdir(parents=True, exist_ok=True)


def load_settings(manifest_path="data_manifest.yaml", workdir="./updl_data", test_start=None, test_end=None,
                  holdout_start=None, plan=None, items=None, dry_run=False, rate=None, stop_at=None,
                  max_calls=None, log_level="INFO", reference_dir=None, external_dir=None) -> Settings:
    manifest_path = Path(manifest_path)
    manifest = load_yaml(manifest_path)
    s = Settings(manifest=manifest, root=manifest_path.resolve().parent, workdir=Path(workdir),
                 reference_dir=reference_dir, external_dir=external_dir, plan_override=plan,
                 items_filter=[x.strip() for x in items.split(",")] if isinstance(items, str) and items else items,
                 dry_run=dry_run, stop_at=stop_at, max_calls=max_calls, log_level=log_level)
    if test_start:
        s.set_param("TEST_START", test_start)
    if test_end:
        s.set_param("TEST_END", test_end)
    if holdout_start:
        s.set_param("HOLDOUT_START", holdout_start)
    if rate is not None:
        s.set_param("RATE_CALLS_PER_MIN", float(rate))
    if plan and plan not in PLANS:
        raise ValueError(f"unknown plan {plan}")
    return s
