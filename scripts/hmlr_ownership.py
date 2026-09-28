"""Download HMLR CCOD / OCOD (free company ownership files) and index them by postcode under `out/cache/hmlr/`.

    uv run python scripts/hmlr_ownership.py                 # both datasets
    uv run python scripts/hmlr_ownership.py ocod            # one dataset
    uv run python scripts/hmlr_ownership.py --lookup "AB1 2CD"

Needs HMLR_API_KEY and the dataset licences accepted once at use-land-property-data.service.gov.uk.
Only `ccod` and `ocod` can be requested; the paid National Polygon Service is refused.
"""

from __future__ import annotations

import asyncio
import sys
import time

from bessible.titles import hmlr


async def _refresh(dataset: str) -> None:
    started = time.monotonic()
    path = await hmlr.download(dataset)
    if hmlr.indexed_file(dataset) == path.name:
        print(f"{dataset}: index already built from {path.name}")  # ruff: ignore[print]
        return
    print(f"{dataset}: indexing {path.name} ...")  # ruff: ignore[print]
    index = await asyncio.to_thread(hmlr.build_index, dataset, path)
    print(f"{dataset}: {index} ({time.monotonic() - started:.0f} s)")  # ruff: ignore[print]


def main() -> None:
    """Refresh the indexes, or look postcodes up in them."""
    args = sys.argv[1:]
    if args[:1] == ["--lookup"]:
        for dataset in hmlr.DATASETS:
            for row in hmlr.lookup(dataset, args[1:]):
                print(f"{dataset}\t{row.title_number}\t{row.tenure}\t{row.postcode}\t{row.address}\t{row.proprietor}")  # ruff: ignore[print]
        return
    for dataset in args or hmlr.DATASETS:
        asyncio.run(_refresh(dataset))


if __name__ == "__main__":
    main()
