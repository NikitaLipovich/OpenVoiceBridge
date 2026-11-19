"""Run a part of a unittest module: a shard of a check of run_tests.py (goal 110).

  python shard.py MODULE --only Class,Class.test_x      only these
  python shard.py MODULE --exclude Class.test_y         everything else, in the module's order
  python shard.py MODULE --only Class --minus Class.test_y   a class without the tests other shards run

A shard of "everything except" keeps a test added later in the run without a change of run_tests.py. The output is the
one of "python -m unittest -v" (one line per test, "Ran N tests", "OK (skipped=K)"), the exit code too. The module is
imported from the current folder first, then from this one. Class fixtures (setUpClass) run once per shard.
"""
import argparse
import os
import sys
import unittest


def flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from flatten(t)
        else:
            yield t


def matches(test, names):
    """Class or Class.test of the module, against the test's id module.Class.test."""
    parts = test.id().split(".")
    cls_test = ".".join(parts[-2:])
    return any(n == parts[-2] or n == cls_test for n in names)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("module")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--only")
    g.add_argument("--exclude")
    ap.add_argument("--minus", default="")
    args = ap.parse_args()
    sys.path.insert(0, os.getcwd())
    names = [n.strip() for n in (args.only or args.exclude).split(",") if n.strip()]
    tests = list(flatten(unittest.defaultTestLoader.loadTestsFromName(args.module)))
    errors = [t for t in tests if type(t).__name__ == "_FailedTest"]
    if args.only:
        minus = [n.strip() for n in args.minus.split(",") if n.strip()]
        picked = [t for t in tests if matches(t, names) and not matches(t, minus)]
        unknown = [n for n in names if not any(matches(t, [n]) for t in tests)]
        if unknown:
            print("shard.py: no test %s in %s" % (", ".join(unknown), args.module), file=sys.stderr)
            return 2
    else:
        picked = [t for t in tests if not matches(t, names)]
    result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(errors + [t for t in picked if t not in errors]))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
