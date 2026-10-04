#!/usr/bin/env python3
"""A42: Compare run metrics between two traces, snapshots, or saved ``metrics`` output.

Each argument is a character ``.toml`` (last run in its trace), a ``.jsonl`` trace,
a ``.json`` metrics snapshot, or a text file with one ``metrics`` CLI line
(``name: {"deaths": ...}``).

Example workflow across commits:

  git checkout <baseline-commit>
  cd python && python3 -m agentrealm_agent run characters/wren.toml
  python3 -m agentrealm_agent metrics characters/wren.toml > /tmp/wren.baseline.metrics

  git checkout <candidate-commit>
  python3 -m agentrealm_agent run characters/wren.toml
  python3 -m agentrealm_agent metrics characters/wren.toml > /tmp/wren.candidate.metrics

  python3 scripts/compare_run_metrics.py /tmp/wren.baseline.metrics /tmp/wren.candidate.metrics

Positive ``deaths`` or ``time_per_level`` deltas mean the candidate run did worse;
negative ``kills`` or ``levels_cleared`` deltas mean the same.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "python"))

from agentrealm_agent import __main__ as cli  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Compare two run metric sources (A42).")
    ap.add_argument("baseline", help="baseline trace, metrics file, or character .toml")
    ap.add_argument("candidate", help="candidate trace, metrics file, or character .toml")
    args = ap.parse_args(argv)
    return cli.compare_metrics_cmd(args.baseline, args.candidate)


if __name__ == "__main__":
    sys.exit(main())
