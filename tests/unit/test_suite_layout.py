"""The suite's own layout: test modules are collected by basename (the test
directories are not packages), so two modules with one name stop the
collection of the whole suite, not only of themselves."""

from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_no_two_test_modules_share_a_name():
    names = Counter(p.name for d in ("tests", "demo") for p in (ROOT / d).rglob("test_*.py")
                    if "__pycache__" not in p.parts)
    assert [n for n, count in names.items() if count > 1] == []
