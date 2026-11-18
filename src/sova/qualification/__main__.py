"""Offline measurement report; exit zero never means operational qualification."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from sova.qualification.requests import RequestJournal, reconcile


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate observation key")
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Order-result measurement, not a production qualification")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("reconcile")
    report.add_argument("--journal", type=Path, required=True)
    report.add_argument("--observation", type=Path, required=True)
    args = parser.parse_args(argv)
    journal = None
    try:
        with args.observation.open("rb") as f:
            raw = f.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("Observation too large")
        observed = json.loads(raw, object_pairs_hook=_unique)
        journal = RequestJournal(args.journal)
        result = reconcile(journal.snapshot(), observed)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))
        if result["data_integrity"] == "VIOLATION":
            return 2
        if (result["data_integrity"] != "SATISFIED" or not result["requests"]
                or any(r["resolution"] != "CONFIRMED" for r in result["requests"])):
            return 3
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
        # Local file/SQL errors can contain private paths or injected values.
        print("Qualification measurement input is invalid or unavailable", file=sys.stderr)
        return 2
    finally:
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
