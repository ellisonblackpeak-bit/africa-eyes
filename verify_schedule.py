# -*- coding: utf-8 -*-
"""Schedule self-check for cloud_app.py.

Answers one question with evidence instead of assertion: does the deployed
service actually rebuild on the 07:00-anchored 3-hour grid, and does it still
catch up when the sandbox was asleep and the background thread died?

Run:  python verify_schedule.py
"""
import io
import sys
from datetime import datetime, timedelta

import cloud_app as ca

CST = ca.CST


def dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=CST)


def main():
    ok = True
    print("=" * 62)
    print("1) slot grid (Beijing time)")
    grid = ca._slot_hours()
    print("   slots:", grid)
    if grid != [1, 4, 7, 10, 13, 16, 19, 22]:
        print("   FAIL unexpected grid")
        ok = False
    else:
        print("   PASS 8 slots, 3h apart, anchored at 07:00")

    print("=" * 62)
    print("2) next_slot / last_slot at sample times")
    cases = [
        ("2026-10-19 06:00", "07:00", "04:00"),
        ("2026-10-19 07:00", "10:00", "07:00"),
        ("2026-10-19 07:00", "10:00", "07:00"),
        ("2026-10-19 14:26", "16:00", "13:00"),
        ("2026-10-19 22:30", "2026-10-20 01:00", "22:00"),
        ("2026-10-20 00:10", "01:00", "2026-10-19 22:00"),
        ("2026-10-20 04:00", "07:00", "04:00"),
    ]
    for raw, exp_next, exp_last in cases:
        now = dt(raw)
        n = ca.next_slot(now).strftime("%Y-%m-%d %H:%M")
        l = ca.last_slot(now).strftime("%Y-%m-%d %H:%M")
        en = exp_next if "-" in exp_next else raw[:10] + " " + exp_next
        el = exp_last if "-" in exp_last else raw[:10] + " " + exp_last
        good = (n == en and l == el)
        ok = ok and good
        print("   %s  now=%s -> next=%s (%s) | last=%s (%s)"
              % ("PASS" if good else "FAIL", raw, n, "ok" if n == en else "want " + en,
                 l, "ok" if l == el else "want " + el))

    print("=" * 62)
    print("3) slot_missed(): is the stored data older than the last slot?")
    real_gen = ca.data_generated_at
    now = datetime.now(CST)
    for label, gen, expect in (
            ("just rebuilt", now - timedelta(minutes=8), False),
            ("missed one slot", now - timedelta(hours=4), True),
            ("no data at all", None, True),
    ):
        ca.data_generated_at = lambda g=gen: g
        got = ca.slot_missed()
        good = (got == expect)
        ok = ok and good
        print("   %s  %-16s -> slot_missed=%s" % ("PASS" if good else "FAIL", label, got))
    ca.data_generated_at = real_gen

    print("=" * 62)
    print("4) maybe_refresh(): does an incoming request trigger a catch-up?")
    real_age, real_missed, real_safe = ca.data_age_min, ca.slot_missed, ca._safe_refresh
    fired = []
    ca._safe_refresh = lambda tag, fast=False: fired.append((tag, fast)) or True
    for label, age, missed, expect in (
            ("fresh data, on time", 10, False, False),
            ("stale data (5h old)", 300, False, True),
            ("missed a slot", 10, True, True),
    ):
        fired[:] = []
        ca.data_age_min = lambda a=age: a
        ca.slot_missed = lambda m=missed: m
        ca._last_kick[0] = 0.0
        started = ca.maybe_refresh("selftest")
        import time as _t
        _t.sleep(0.4)
        good = (started == expect) and (bool(fired) == expect)
        ok = ok and good
        print("   %s  %-20s -> started=%s cycle=%s"
              % ("PASS" if good else "FAIL", label, started,
                 "fast" if fired and fired[0][1] else ("full" if fired else "none")))
    ca.data_age_min, ca.slot_missed, ca._safe_refresh = real_age, real_missed, real_safe

    print("=" * 62)
    print("RESULT:", "ALL PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
