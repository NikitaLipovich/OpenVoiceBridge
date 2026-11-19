"""The two levels of the PC tests (goal 110): fast for every change during work, full before a push or a release.

  BENCH_TEST_TIER=fast     set by run_tests.py --fast; unset or "full" otherwise (a test run alone is full)
  full_only(why)           decorator: the case runs only in the full run. Only for a long variant of a function whose
                           short case is in the fast level too (lyrat_resend/docs/TESTS.md, goal 110 DECISIONS D1)
  wait_for(cond, timeout)  poll cond until it is true or the time is up; returns its last value. Instead of a fixed
                           sleep that waits for something the simulator does (it answers in milliseconds)

run_tests.py counts a skip whose reason starts with FULL_ONLY as left to the full run, not as a failure, and only in
the fast level; every other skip is still a failure.
"""
import os
import time
import unittest

FULL_ONLY = "full run only"
FAST = os.environ.get("BENCH_TEST_TIER", "full") == "fast"


def full_only(why):
    return unittest.skipIf(FAST, "%s: %s" % (FULL_ONLY, why))


def wait_for(cond, timeout, step=0.05):
    end = time.monotonic() + timeout
    while True:
        value = cond()
        if value or time.monotonic() >= end:
            return value
        time.sleep(step)
