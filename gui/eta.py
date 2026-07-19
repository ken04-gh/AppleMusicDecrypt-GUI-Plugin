"""Professional countdown ETA: sample-aware, unit floor, never false-zero."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


def format_duration(seconds: float | None) -> str:
    if seconds is None or seconds != seconds:
        return "--:--"
    s = int(max(0, round(seconds)))
    if s >= 3600:
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}"
    m, sec = divmod(s, 60)
    return f"{m:02d}:{sec:02d}"


@dataclass
class EtaTracker:
    """
    Installer-style ETA:
    - Warm-up 「估算中…」 until enough real wall time + samples
    - Rate from cumulative average only (resists burst UI updates)
    - Floor: remaining_units * min_sec_per_unit — never slam to 00:00 early
    - Wall-clock countdown, re-anchors when progress stalls or speeds up
    - Never show 00:00 until done >= total
    """

    # Before locking a numeric ETA
    warmup_sec: float = 1.4
    min_done_for_lock: float = 0.5
    min_samples_for_lock: int = 1
    # Per-unit lower bound (query: ~0.6s/song, download unit: set higher via set_min_unit_sec)
    min_sec_per_unit: float = 0.55
    # Soft clamps on re-anchor
    reanchor_down_slack: float = 3.0
    reanchor_up_slack: float = 12.0
    stall_sec: float = 4.0

    _t0: float = field(default=0.0, init=False)
    _last_prog_t: float = field(default=0.0, init=False)
    _sample_count: int = field(default=0, init=False)
    _sec_per_unit: float | None = field(default=None, init=False)
    _total: float = field(default=0.0, init=False)
    _done: float = field(default=0.0, init=False)

    _locked: bool = field(default=False, init=False)
    _anchor_eta: float = field(default=0.0, init=False)
    _anchor_t: float = field(default=0.0, init=False)
    _last_text: str = field(default="", init=False)
    _last_shown_sec: int | None = field(default=None, init=False)

    def reset(self, total: float = 0.0, min_sec_per_unit: float | None = None):
        now = time.monotonic()
        self._t0 = now
        self._last_prog_t = now
        self._sample_count = 0
        self._sec_per_unit = None
        self._total = max(0.0, float(total))
        self._done = 0.0
        self._locked = False
        self._anchor_eta = 0.0
        self._anchor_t = now
        self._last_text = "估算中…"
        self._last_shown_sec = None
        if min_sec_per_unit is not None and min_sec_per_unit > 0:
            self.min_sec_per_unit = float(min_sec_per_unit)

    def set_total(self, total: float):
        self._total = max(0.0, float(total))

    def set_min_unit_sec(self, sec: float):
        if sec > 0:
            self.min_sec_per_unit = float(sec)

    def _finished(self) -> bool:
        return self._total > 0 and self._done >= self._total - 1e-9

    def _remaining_units(self) -> float:
        return max(self._total - self._done, 0.0)

    def _floor_eta(self) -> float:
        """Hard lower bound while work remains — prevents early 00:00."""
        rem = self._remaining_units()
        if rem <= 0:
            return 0.0
        # At least 1s, and scale with unfinished units
        return max(1.0, rem * self.min_sec_per_unit)

    def _calc_eta(self) -> float | None:
        if self._finished():
            return 0.0
        rem = self._remaining_units()
        if rem <= 0:
            return 0.0
        spu = self._sec_per_unit
        if spu is None or spu <= 0:
            return None
        # Blend observed rate with min floor so optimistic bursts cannot collapse ETA
        effective = max(spu, self.min_sec_per_unit * 0.85)
        return rem * effective

    def update(self, done: float, total: float | None = None) -> tuple[float | None, str, bool]:
        now = time.monotonic()
        if total is not None and total > 0:
            self._total = float(total)
        done = max(0.0, float(done))
        if self._total > 0:
            done = min(done, self._total)
        if self._t0 <= 0:
            self.reset(self._total)

        if done + 1e-9 < self._done:
            # Progress went backwards — soft restart keeping total
            saved_min = self.min_sec_per_unit
            self.reset(self._total, min_sec_per_unit=saved_min)
            self._done = done
            return self.tick()

        prev = self._done
        d_done = done - prev
        self._done = done
        elapsed = max(now - self._t0, 1e-3)

        if d_done > 1e-9:
            self._sample_count += 1
            self._last_prog_t = now
            # Cumulative average only — immune to UI burst of many updates in 1 frame
            if done > 0 and elapsed >= 0.25:
                overall_spu = elapsed / done
                if self._sec_per_unit is None:
                    self._sec_per_unit = overall_spu
                else:
                    # Prefer overall; slight EMA toward recent overall
                    self._sec_per_unit = 0.65 * self._sec_per_unit + 0.35 * overall_spu
            elif done > 0 and self._sec_per_unit is None:
                # Too early for a trustworthy rate — seed conservatively
                self._sec_per_unit = max(elapsed / max(done, 1e-6), self.min_sec_per_unit)

        return self.tick()

    def tick(self) -> tuple[float | None, str, bool]:
        now = time.monotonic()
        if self._t0 <= 0:
            self.reset(self._total)

        if self._finished():
            text = "剩余 00:00"
            changed = self._last_text != text
            self._last_text = text
            self._last_shown_sec = 0
            return 0.0, text, changed

        elapsed = now - self._t0
        calc = self._calc_eta()
        floor = self._floor_eta()

        # Warm-up: no numeric countdown yet
        if not self._locked:
            ready = (
                calc is not None
                and elapsed >= self.warmup_sec
                and self._done >= self.min_done_for_lock
                and self._sample_count >= self.min_samples_for_lock
            )
            if ready:
                # Conservative seed: never lock below floor * 1.1
                seed = max(calc * 1.12, calc + 1.0, floor * 1.1, 2.0)
                self._locked = True
                self._anchor_eta = seed
                self._anchor_t = now
            else:
                text = "估算中…"
                changed = self._last_text != text
                self._last_text = text
                return None, text, changed

        # Wall-clock countdown from anchor
        displayed = max(0.0, self._anchor_eta - (now - self._anchor_t))

        # Stall: no progress for a while → gently lift toward calc/floor
        stalled = (now - self._last_prog_t) >= self.stall_sec
        if stalled and calc is not None:
            target = max(calc, floor, displayed)
            if target > displayed + 0.5:
                displayed = displayed * 0.82 + target * 0.18
                self._anchor_eta = displayed
                self._anchor_t = now

        if calc is not None:
            if calc + self.reanchor_down_slack < displayed and not stalled:
                # Faster than expected — ease down but never under floor
                target = max(calc, floor)
                displayed = max(target, displayed * 0.80 + target * 0.20)
                self._anchor_eta = displayed
                self._anchor_t = now
            elif calc > displayed + self.reanchor_up_slack and self._done > 0:
                displayed = displayed * 0.85 + calc * 0.15
                displayed = max(displayed, floor)
                self._anchor_eta = displayed
                self._anchor_t = now

        # Critical: never display 00:00 while work remains
        displayed = max(displayed, floor, 1.0)

        sec = int(round(displayed))
        sec = max(sec, 1)
        text = f"剩余 {format_duration(float(sec))}"
        changed = sec != self._last_shown_sec or self._last_text != text
        if changed:
            self._last_shown_sec = sec
            self._last_text = text
        return float(sec), text, changed

    def elapsed_text(self) -> str:
        if self._t0 <= 0:
            return "00:00"
        return format_duration(time.monotonic() - self._t0)

    @property
    def active(self) -> bool:
        return self._t0 > 0 and self._total > 0 and not self._finished()
