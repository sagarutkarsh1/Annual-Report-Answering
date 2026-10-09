"""Spend protection and rate limits for a publicly reachable deployment (all of it is off with the default settings).

* `UsageBudget` - a running ESTIMATE of what the owner's OpenAI key has been charged, rebuilt from the Store:
  stored answer costs (`Message.usage.cost_usd`) + a fixed estimate per indexed document + a fixed estimate per completed
  evaluation.  No schema change.  Deleting a chat would erase its rows and so refund the budget; to stop "upload, ask, delete,
  repeat" from being free, a deleted chat's spend is moved into a small ledger file (`budget_ledger.json`) first.
  The estimate is per container lifetime: when the host wipes its disk the store and the ledger go with it, which is why the
  OpenAI-side spending limit stays the real backstop (docs/DEPLOY.md).
* `SlidingWindowLimiter` - at most N events per key (client IP) in a time window.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from .config import Settings
from .models import ServiceError
from .store import Store

log = logging.getLogger("reportlens.limits")

LEDGER_NAME = "budget_ledger.json"
ANSWER_FALLBACK_USD = 0.50          # an answered question whose model is not in the price table (cost unknown)
FAILED_ANSWER_FALLBACK_USD = 0.10   # an assistant message that ended in an error before any usage was recorded
QUESTION_RESERVE_USD = 0.60         # held back for every question that is still running (its cost is not stored yet)
STATUS_CACHE_S = 5.0

BUDGET_EXHAUSTED_MESSAGE = "The demo's usage budget has been used up. Please contact the owner."


def budget_exhausted() -> ServiceError:
    return ServiceError("budget_exhausted", BUDGET_EXHAUSTED_MESSAGE, 402)


# --------------------------------------------------------------------------------------------- spend estimate
def _answer_spend(snapshot) -> float:
    return (snapshot.answer_cost_usd + snapshot.unpriced_answers * ANSWER_FALLBACK_USD
            + snapshot.unpriced_failed * FAILED_ANSWER_FALLBACK_USD)


class UsageBudget:
    """Thread-safe.  `Settings.budget_usd_total == 0` disables every check (they cost nothing then)."""

    def __init__(self, settings: Settings, store: Store):
        self._settings = settings
        self._store = store
        self._lock = threading.RLock()
        self._ledger_path = settings.data_dir / LEDGER_NAME
        self._retired_usd = 0.0
        self._retired_keys: set[str] = set()
        self._cached: Optional[tuple[float, float]] = None          # (monotonic time, spent)
        self._load_ledger()

    # ----- ledger
    def _load_ledger(self) -> None:
        try:
            raw = json.loads(self._ledger_path.read_text(encoding="utf-8"))
            self._retired_usd = max(0.0, float(raw.get("retired_usd", 0.0)))
            self._retired_keys = {str(k) for k in raw.get("retired_keys", [])}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError):
            log.warning("could not read %s; starting the spend ledger from zero", self._ledger_path, exc_info=True)

    def _save_ledger(self) -> None:
        tmp = self._ledger_path.with_name(self._ledger_path.name + ".tmp")
        try:
            tmp.write_text(json.dumps({"retired_usd": round(self._retired_usd, 6), "retired_keys": sorted(self._retired_keys)}), encoding="utf-8")
            os.replace(tmp, self._ledger_path)
        except OSError:
            log.warning("could not write the spend ledger %s (the in-memory total is still enforced)", self._ledger_path, exc_info=True)

    # ----- public API
    @property
    def enabled(self) -> bool:
        return self._settings.budget_usd_total > 0

    def spent_usd(self) -> float:
        """Estimated spend so far (fresh, not cached)."""
        with self._lock:
            snap = self._store.usage_snapshot(exclude_index_keys=self._retired_keys)
            spent = (self._retired_usd + _answer_spend(snap) + len(snap.index_keys) * self._settings.index_cost_estimate_usd
                     + snap.evaluations * self._settings.eval_cost_estimate_usd)
            self._cached = (time.monotonic(), spent)
            return spent

    def used_fraction(self, *, fresh: bool = False) -> float:
        if not self.enabled:
            return 0.0
        with self._lock:
            cached = self._cached
            if fresh or cached is None or time.monotonic() - cached[0] > STATUS_CACHE_S:
                spent = self.spent_usd()
            else:
                spent = cached[1]
        return max(0.0, min(1.0, spent / self._settings.budget_usd_total))

    def status(self) -> dict:
        """What GET /api/config exposes: a fraction only, never dollar amounts."""
        return {"enabled": self.enabled, "used_fraction": round(self.used_fraction(fresh=True), 3)}

    def check(self, *, reserve_usd: float = 0.0) -> None:
        """Raise the 402 error when the budget (plus what running operations will still cost) is used up."""
        if not self.enabled:
            return
        spent = self.spent_usd() + max(0.0, reserve_usd)
        if spent >= self._settings.budget_usd_total:
            log.warning("usage budget exhausted (estimated %.2f of %.2f USD); refusing new work", spent, self._settings.budget_usd_total)
            raise budget_exhausted()

    def invalidate(self) -> None:
        with self._lock:
            self._cached = None

    def charge(self, usd: float) -> None:
        """Add spend that no stored row will show (for example a repeated evaluation of the same answer)."""
        if not self.enabled or usd <= 0:
            return
        with self._lock:
            self._retired_usd += usd
            self._cached = None
            self._save_ledger()

    def retire_session(self, sid: str) -> None:
        """Move a chat's spend into the ledger just before the chat is deleted."""
        if not self.enabled:
            return
        with self._lock:
            snap = self._store.usage_snapshot(sid, exclude_index_keys=self._retired_keys)
            amount = (_answer_spend(snap) + len(snap.index_keys) * self._settings.index_cost_estimate_usd
                      + snap.evaluations * self._settings.eval_cost_estimate_usd)
            self._retired_usd += amount
            self._retired_keys.update(snap.index_keys)
            self._cached = None
            self._save_ledger()

    def retire_document(self, key: str, *, charge_index: bool) -> None:
        """A failed document is about to be replaced by a new upload: keep its indexing cost if it got as far as spending any."""
        if not self.enabled:
            return
        with self._lock:
            if key in self._retired_keys:
                return
            self._retired_keys.add(key)
            if charge_index:
                self._retired_usd += self._settings.index_cost_estimate_usd
            self._cached = None
            self._save_ledger()


# --------------------------------------------------------------------------------------------- rate limiting
class SlidingWindowLimiter:
    """At most `limit` events per key within `window_s`.  `limit <= 0` means unlimited.  Thread-safe, memory-bounded."""

    MAX_KEYS = 10_000

    def __init__(self, limit: int, window_s: float, *, clock=time.monotonic):
        self.limit = limit
        self.window_s = window_s
        self._clock = clock
        self._events: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._events.get(key)
        if q is None:
            q = self._events[key] = deque()
        while q and now - q[0] >= self.window_s:
            q.popleft()
        return q

    def _sweep(self, now: float) -> None:
        if len(self._events) <= self.MAX_KEYS:
            return
        for key in [k for k, q in self._events.items() if not q or now - q[-1] >= self.window_s]:
            del self._events[key]
        if len(self._events) > self.MAX_KEYS:            # still too many live keys: forget the oldest half
            for key in sorted(self._events, key=lambda k: self._events[k][-1])[: len(self._events) // 2]:
                del self._events[key]

    def retry_after(self, key: str) -> int:
        """Seconds until `key` may act again (0 when it may now)."""
        if self.limit <= 0:
            return 0
        with self._lock:
            now = self._clock()
            q = self._prune(key, now)
            if len(q) < self.limit:
                return 0
            return max(1, math.ceil(self.window_s - (now - q[0])))

    def hit(self, key: str) -> int:
        """Record one event.  Returns 0 when it is allowed, otherwise the seconds to wait (the event is NOT recorded)."""
        if self.limit <= 0:
            return 0
        with self._lock:
            now = self._clock()
            q = self._prune(key, now)
            if len(q) >= self.limit:
                return max(1, math.ceil(self.window_s - (now - q[0])))
            q.append(now)
            self._sweep(now)
            return 0

    def hit_many(self, key: str, n: int) -> int:
        """Record `n` events at once, all or nothing (a batch of questions).  Returns 0 when all `n` fit, otherwise the seconds
        until they would (nothing is recorded).  A batch larger than the whole allowance never fits: it waits a full window."""
        if self.limit <= 0 or n <= 0:
            return 0
        with self._lock:
            now = self._clock()
            q = self._prune(key, now)
            free = self.limit - len(q)
            if n <= free:
                q.extend([now] * n)
                self._sweep(now)
                return 0
            if n > self.limit:
                return max(1, math.ceil(self.window_s))
            must_leave = n - free                          # this many of the oldest events have to leave the window first
            return max(1, math.ceil(self.window_s - (now - q[must_leave - 1])))

    def refund_many(self, key: str, n: int) -> None:
        """Take back the `n` most recent events of `key`."""
        with self._lock:
            q = self._events.get(key)
            for _ in range(min(n, len(q) if q else 0)):
                q.pop()

    def record(self, key: str) -> None:
        """Record an event unconditionally (used for failed logins)."""
        if self.limit <= 0:
            return
        with self._lock:
            now = self._clock()
            self._prune(key, now).append(now)
            self._sweep(now)

    def refund(self, key: str) -> None:
        """Take back the most recent event of `key` (the action it paid for was refused before it did any work)."""
        with self._lock:
            q = self._events.get(key)
            if q:
                q.pop()

    def reset(self, key: str) -> None:
        with self._lock:
            self._events.pop(key, None)
