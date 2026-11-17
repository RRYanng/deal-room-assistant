"""Public read-only demo: DEMO_MODE=true turns this on; without it nothing here runs.

- Data: every start, and every day at 00:00 UTC, the working database (DB_PATH) is overwritten with a clean copy
  of DEMO_SOURCE_DB (default data/demo_v2.db). The source file is opened read-only and never changed.
- Turned off: upload, new companies, delete, re-extract, Run deck check and Run monitoring answer 403.
- Limited: questions and briefs call the model, so each IP gets DEMO_ASKS_PER_IP questions (5) and
  DEMO_BRIEFS_PER_IP briefs (1) a day, and the whole site DEMO_MODEL_CALLS_PER_DAY of both together (100).
  A question or brief counts once when it is sent, however many model calls it makes inside. Counts are kept in
  memory by UTC day, so a restart or redeploy starts them again.
"""

import asyncio
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app.config import PROJECT_ROOT

OFF_MESSAGE = "Turned off in the demo"
LIMIT_MESSAGE = "Demo limit reached. Watch the walkthrough video instead."

# (method, path pattern) of the requests the demo refuses: they store files, delete them or start paid runs.
TURNED_OFF = [
    ("POST", r"/api/companies"),
    ("POST", r"/api/companies/\d+/documents"),
    ("POST", r"/api/uploads/.*"),
    ("POST", r"/api/documents/\d+/extract"),
    ("DELETE", r"/api/documents/\d+"),
    ("POST", r"/api/companies/\d+/claim-check"),
    ("POST", r"/api/companies/\d+/monitor"),
]
# (method, path pattern, kind) of the requests that call the model and are counted.
LIMITED = [
    ("POST", r"/api/ask", "ask"),
    ("POST", r"/api/companies/\d+/ask", "ask"),
    ("POST", r"/api/companies/\d+/brief", "brief"),
]


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def demo_mode() -> bool:
    return os.getenv("DEMO_MODE", "").strip().lower() in ("1", "true", "yes", "on")


def source_db() -> Path:
    path = Path(os.getenv("DEMO_SOURCE_DB", "data/demo_v2.db"))
    return path if path.is_absolute() else PROJECT_ROOT / path


def turned_off(method: str, path: str) -> bool:
    return any(method == m and re.fullmatch(p, path) for m, p in TURNED_OFF)


def limited_kind(method: str, path: str) -> str | None:
    return next((kind for m, p, kind in LIMITED if method == m and re.fullmatch(p, path)), None)


def client_ip(headers, peer: str | None) -> str:
    """The visitor's address. Railway's edge sets X-Real-IP and replaces any copy the visitor sends. Elsewhere,
    the last X-Forwarded-For entry is the one the nearest proxy added; earlier entries could be made up."""
    real = (headers.get("x-real-ip") or "").strip()
    if real:
        return real
    forwarded = [part.strip() for part in (headers.get("x-forwarded-for") or "").split(",") if part.strip()]
    return forwarded[-1] if forwarded else (peer or "unknown")


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


@dataclass
class Limits:
    asks_per_ip: int = 5
    briefs_per_ip: int = 1
    calls_per_day: int = 100

    @classmethod
    def from_env(cls) -> "Limits":
        return cls(_env_int("DEMO_ASKS_PER_IP", 5), _env_int("DEMO_BRIEFS_PER_IP", 1),
                   _env_int("DEMO_MODEL_CALLS_PER_DAY", 100))


@dataclass
class Limiter:
    """Counts questions and briefs per IP and for the whole site, per UTC day."""
    limits: Limits
    today: Callable[[], str] = utc_today
    _day: str = ""
    _per_ip: dict = field(default_factory=dict)
    _total: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def take(self, ip: str, kind: str) -> bool:
        """Counts one request if it is within the limits; False when it is over a limit (nothing is counted)."""
        per_ip_limit = self.limits.asks_per_ip if kind == "ask" else self.limits.briefs_per_ip
        with self._lock:
            day = self.today()
            if day != self._day:
                self._day, self._per_ip, self._total = day, {}, 0
            used = self._per_ip.get((ip, kind), 0)
            if used >= per_ip_limit or self._total >= self.limits.calls_per_day:
                return False
            self._per_ip[(ip, kind)] = used + 1
            self._total += 1
            return True


def restore(source: Path, target: Path) -> None:
    """Overwrite the working database with the clean source, in one transaction (open connections see either
    the old or the new data). The source is opened read-only."""
    if source.resolve() == target.resolve():
        raise RuntimeError("DB_PATH must not be the demo source database: the demo overwrites DB_PATH.")
    if not source.is_file():
        raise RuntimeError(f"Demo source database not found: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    dst = sqlite3.connect(target, timeout=30)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def seconds_to_midnight_utc(now: datetime | None = None) -> float:
    now = now or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (tomorrow - now).total_seconds()


async def reset_daily(source: Path, target: Path) -> None:
    """Restore the clean data every day at 00:00 UTC, until the server stops."""
    while True:
        await asyncio.sleep(seconds_to_midnight_utc() + 1)
        await asyncio.to_thread(restore, source, target)
