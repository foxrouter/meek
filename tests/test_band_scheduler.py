#!/usr/bin/env python3
"""
tests/test_band_scheduler.py — Unit tests for BandScheduler logic.

Mirrors the C++ BandScheduler::from_env() parsing and the
dwell_elapsed / advance / reset_dwell state-machine in Python so that
regressions in the scheduler behaviour are caught without needing a
full hardware build.

Run with:
    python3 tests/test_band_scheduler.py [-v]

Requires: no external dependencies
"""

import datetime
import os
import sys
import unittest
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# Python mirror of BandScheduler::from_env() parsing
# ---------------------------------------------------------------------------

_WHITESPACE = " \t\r\n\f\v"
_DEFAULT_DWELL_MS = 10_000


def _parse_rf_sched_bands(
    bands_env: Optional[str],
    dwell_ms: int = _DEFAULT_DWELL_MS,
) -> List[Tuple[float, int]]:
    """Parse RF_SCHED_BANDS string into a list of (center_hz, dwell_ms) pairs.

    Mirrors the C++ BandScheduler::from_env() token loop:
    - Splits on commas.
    - Strips leading/trailing whitespace from each token.
    - Rejects tokens with trailing non-numeric characters.
    - Rejects non-positive frequencies.
    - Returns an empty list (scheduler disabled) when fewer than 2 valid
      entries remain.
    """
    if not bands_env:
        return []

    slots: List[Tuple[float, int]] = []
    for token in bands_env.split(","):
        trimmed = token.strip(_WHITESPACE)
        if not trimmed:
            continue
        try:
            hz = float(trimmed)
        except ValueError:
            continue
        # Reject tokens with trailing non-whitespace (e.g. "433920000oops").
        # float() already does this for Python, but we replicate the C++ guard
        # explicitly: after parsing, any remaining chars must be whitespace.
        # Since we already stripped, if float() succeeded, the full string was
        # consumed — guard passes.
        if hz <= 0.0:
            continue
        slots.append((hz, dwell_ms))

    return slots if len(slots) >= 2 else []


# ---------------------------------------------------------------------------
# Python mirror of the BandScheduler dwell state-machine
# ---------------------------------------------------------------------------

class BandSchedulerSim:
    """Pure-Python simulation of the BandScheduler dwell state-machine.

    Mirrors:
      dwell_elapsed(now)  — anchor on first call (return False), then
                            return True when dwell_ms has elapsed.
      advance(now)        — move to next slot, reset dwell_start.
      reset_dwell(now)    — reset dwell_start without changing slot.
      current()           — current (hz, dwell_ms) slot.
    """

    def __init__(self, slots: List[Tuple[float, int]]) -> None:
        assert len(slots) >= 2, "BandSchedulerSim requires >= 2 slots"
        self._slots = slots
        self._idx = 0
        self._dwell_start: Optional[datetime.datetime] = None
        self._enabled = True

    @property
    def enabled(self) -> bool:
        return self._enabled

    def current(self) -> Tuple[float, int]:
        return self._slots[self._idx]

    def peek_next(self) -> Tuple[float, int]:
        return self._slots[(self._idx + 1) % len(self._slots)]

    def dwell_elapsed(self, now: datetime.datetime) -> bool:
        if not self._enabled:
            return False
        if self._dwell_start is None:
            self._dwell_start = now
            return False
        elapsed_ms = (now - self._dwell_start).total_seconds() * 1000
        return elapsed_ms >= self._slots[self._idx][1]

    def advance(self, now: datetime.datetime) -> None:
        self._idx = (self._idx + 1) % len(self._slots)
        self._dwell_start = now

    def reset_dwell(self, now: datetime.datetime) -> None:
        self._dwell_start = now


def _t(ms: int) -> datetime.datetime:
    """Helper: return a datetime offset by *ms* milliseconds from epoch."""
    return datetime.datetime(2000, 1, 1) + datetime.timedelta(milliseconds=ms)


# ---------------------------------------------------------------------------
# Tests: parsing
# ---------------------------------------------------------------------------

class TestFromEnvParsing(unittest.TestCase):
    """Mirror of BandScheduler::from_env() parsing rules."""

    def test_none_env_returns_empty(self):
        self.assertEqual(_parse_rf_sched_bands(None), [])

    def test_empty_string_returns_empty(self):
        self.assertEqual(_parse_rf_sched_bands(""), [])

    def test_single_entry_disabled(self):
        # Only 1 valid slot → disabled
        self.assertEqual(_parse_rf_sched_bands("433920000"), [])

    def test_two_valid_entries_enabled(self):
        slots = _parse_rf_sched_bands("433920000,868100000")
        self.assertEqual(len(slots), 2)
        self.assertAlmostEqual(slots[0][0], 433_920_000.0)
        self.assertAlmostEqual(slots[1][0], 868_100_000.0)

    def test_three_valid_entries(self):
        slots = _parse_rf_sched_bands("433920000,868100000,144800000")
        self.assertEqual(len(slots), 3)

    def test_whitespace_around_tokens_accepted(self):
        slots = _parse_rf_sched_bands(" 433920000 , 868100000 ")
        self.assertEqual(len(slots), 2)

    def test_malformed_token_skipped(self):
        # "oops" is not a valid float → skipped; 2 valid entries remain
        slots = _parse_rf_sched_bands("433920000,oops,868100000")
        self.assertEqual(len(slots), 2)
        self.assertAlmostEqual(slots[0][0], 433_920_000.0)
        self.assertAlmostEqual(slots[1][0], 868_100_000.0)

    def test_only_malformed_disabled(self):
        slots = _parse_rf_sched_bands("abc,def")
        self.assertEqual(slots, [])

    def test_non_positive_hz_rejected(self):
        # 0 and negative values are invalid
        slots = _parse_rf_sched_bands("0,433920000,868100000")
        self.assertEqual(len(slots), 2)
        slots_neg = _parse_rf_sched_bands("-1,433920000,868100000")
        self.assertEqual(len(slots_neg), 2)

    def test_custom_dwell_applied(self):
        slots = _parse_rf_sched_bands("433920000,868100000", dwell_ms=5000)
        self.assertTrue(all(dwell == 5000 for _, dwell in slots))

    def test_empty_tokens_between_commas_ignored(self):
        # ",,433920000,,868100000,," — extra commas produce empty tokens
        slots = _parse_rf_sched_bands(",,433920000,,868100000,,")
        self.assertEqual(len(slots), 2)

    def test_float_hz_accepted(self):
        # Fractional Hz should be accepted
        slots = _parse_rf_sched_bands("433920000.5,868100000.0")
        self.assertEqual(len(slots), 2)
        self.assertAlmostEqual(slots[0][0], 433_920_000.5)


# ---------------------------------------------------------------------------
# Tests: dwell state-machine
# ---------------------------------------------------------------------------

class TestDwellStateMachine(unittest.TestCase):
    """Mirror of BandScheduler dwell_elapsed / advance / reset_dwell."""

    def _make_sched(self, dwell_ms: int = 10_000) -> BandSchedulerSim:
        slots = [
            (433_920_000.0, dwell_ms),
            (868_100_000.0, dwell_ms),
            (144_800_000.0, dwell_ms),
        ]
        return BandSchedulerSim(slots)

    def test_first_call_returns_false_and_anchors(self):
        sched = self._make_sched(10_000)
        # First call must return False (anchor, not trigger)
        self.assertFalse(sched.dwell_elapsed(_t(0)))

    def test_no_transition_before_dwell_expires(self):
        sched = self._make_sched(10_000)
        sched.dwell_elapsed(_t(0))          # anchor
        self.assertFalse(sched.dwell_elapsed(_t(9_999)))

    def test_transition_after_dwell_expires(self):
        sched = self._make_sched(10_000)
        sched.dwell_elapsed(_t(0))          # anchor
        self.assertTrue(sched.dwell_elapsed(_t(10_000)))

    def test_advance_moves_to_next_slot(self):
        sched = self._make_sched(10_000)
        initial_hz = sched.current()[0]
        sched.dwell_elapsed(_t(0))          # anchor
        sched.advance(_t(10_000))
        self.assertNotEqual(sched.current()[0], initial_hz)
        self.assertAlmostEqual(sched.current()[0], 868_100_000.0)

    def test_advance_wraps_around(self):
        sched = self._make_sched(1_000)
        # Cycle through all three slots
        sched.dwell_elapsed(_t(0))          # anchor slot 0
        sched.advance(_t(1_000))            # → slot 1
        sched.advance(_t(2_000))            # → slot 2
        sched.advance(_t(3_000))            # → slot 0 (wrap)
        self.assertAlmostEqual(sched.current()[0], 433_920_000.0)

    def test_reset_dwell_restarts_timer(self):
        sched = self._make_sched(10_000)
        sched.dwell_elapsed(_t(0))          # anchor at t=0
        # Simulate failed retune at t=10_000 — reset dwell
        sched.reset_dwell(_t(10_000))
        # Dwell should not have elapsed immediately after reset
        self.assertFalse(sched.dwell_elapsed(_t(10_001)))
        # Only after another full dwell
        self.assertTrue(sched.dwell_elapsed(_t(20_000)))

    def test_reset_dwell_does_not_change_slot(self):
        sched = self._make_sched(10_000)
        hz_before = sched.current()[0]
        sched.dwell_elapsed(_t(0))
        sched.reset_dwell(_t(10_000))
        self.assertAlmostEqual(sched.current()[0], hz_before)

    def test_peek_next_does_not_advance(self):
        sched = self._make_sched(10_000)
        hz_before = sched.current()[0]
        next_hz = sched.peek_next()[0]
        # peek_next must not change current
        self.assertAlmostEqual(sched.current()[0], hz_before)
        self.assertAlmostEqual(next_hz, 868_100_000.0)

    def test_slot_0_gets_full_dwell(self):
        """dwell_elapsed() anchors on first call (returns False) so slot 0
        always gets its full configured dwell before the first transition."""
        sched = self._make_sched(10_000)
        # Immediately after construction, dwell_elapsed must return False even
        # if we pass a time far in the future — anchor semantics.
        self.assertFalse(sched.dwell_elapsed(_t(1_000_000)))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
