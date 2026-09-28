"""`uv run python -m bessible.possibility <lat> <lon> [mw=20] [hours=4] [--policy] [--json]`: checks on a live location.

`--policy` adds the agent that reads the local plan (uses GOOGLE_API_KEY from .env).
"""

from __future__ import annotations

import asyncio
import sys

from bessible.llm import developer_model
from bessible.location import Coordinates, collate

from . import Proposal, assess
from .policy import assess_with_policy

USAGE = "usage: python -m bessible.possibility <lat> <lon> [mw=20] [hours=4] [--policy] [--json]"
FLAGS = {"--json", "--policy"}
MIN_ARGS = 2
MARKS = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "unknown": " ?  "}


def main() -> None:
    """Collate one coordinate, run the hard checks and print them."""
    args = [a.strip(",") for a in sys.argv[1:] if a not in FLAGS]
    if len(args) < MIN_ARGS:
        sys.exit(USAGE)
    lat, lon, mw, hours = (*map(float, args), 20.0, 4.0)[:4] if len(args) == MIN_ARGS else (*map(float, args), 4.0)[:4]
    location = asyncio.run(collate(Coordinates(lat=lat, lon=lon)))
    proposal = Proposal.model_validate({"location": location, "battery_mw": mw, "duration_h": int(hours)})
    report = (
        asyncio.run(assess_with_policy(proposal, developer_model())) if "--policy" in sys.argv else assess(proposal)
    )
    if "--json" in sys.argv:
        sys.stdout.write(report.model_dump_json(indent=2) + "\n")
        return
    for check in report.checks:
        sys.stdout.write(f"{MARKS[check.outcome]}  {check.label:22} {check.reason}\n")
    sys.stdout.write(f"\n{'POSSIBLE' if report.possible else 'BLOCKED'}: {mw:g} MW / {hours:g} h at {lat}, {lon}\n")


if __name__ == "__main__":
    main()
