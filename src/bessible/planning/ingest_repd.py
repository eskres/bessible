"""Load and ingest DESNZ Renewable Energy Planning Database (REPD) battery storage records.

The snapshot is built from the published REPD CSV. Every record keeps its REPD `Ref ID` and its row in that CSV
(spreadsheet numbering: the header is row 1), so a reader can open the file and find the exact line we used.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import math
import sys
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, HttpUrl

from bessible.config import settings
from bessible.models import NearbyProject, Position

log = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0


def haversine_km(a: Position, lat: float, lon: float) -> float:
    """Great-circle distance in km from `a` to a WGS84 point."""
    p1, p2 = math.radians(a.lat), math.radians(lat)
    dphi, dlmb = p2 - p1, math.radians(lon - a.lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


DATASET_NAME = "desnz-repd"
DATASET_URL = "https://www.gov.uk/government/publications/renewable-energy-planning-database-quarterly-extract"
CONTENT_API_URL = (
    "https://www.gov.uk/api/content/government/publications/renewable-energy-planning-database-quarterly-extract"
)
REPD_FILE = "repd.json"
MANIFEST_FILE = "manifest.json"
TECHNOLOGY = "Battery"

# Planning milestones in the CSV, dd/mm/yyyy. The latest one dates the current status.
MILESTONE_COLUMNS = (
    "Planning Application Submitted",
    "Planning Application Withdrawn",
    "Planning Permission Refused",
    "Appeal Lodged",
    "Appeal Withdrawn",
    "Appeal Refused",
    "Appeal Granted",
    "Planning Permission  Granted",  # two spaces in the published header
    "Secretary of State - Intervened",
    "Secretary of State - Refusal",
    "Secretary of State - Granted",
    "Planning Permission Expired",
    "Under Construction",
    "Operational",
)


class RepdProject(BaseModel):
    """One REPD battery record, traceable to its row in the published CSV."""

    id: str  # "repd-<Ref ID>"
    ref_id: str  # REPD "Ref ID" column
    csv_row: int  # row in the CSV as a spreadsheet shows it (header = 1)
    name: str
    mw: float | None = None
    status: str
    status_date: date
    latitude: float
    longitude: float
    postcode: str | None = None
    planning_authority: str | None = None
    planning_ref: str | None = None
    technology_type: str = TECHNOLOGY


class RepdSnapshot(BaseModel):
    """Committed snapshot of REPD projects."""

    fetched_at: date
    dataset: str = DATASET_NAME
    dataset_url: HttpUrl = HttpUrl(DATASET_URL)
    csv_url: HttpUrl | None = None  # the exact CSV file the rows were read from
    csv_title: str | None = None  # e.g. "Renewable Energy Planning Database (REPD): July 2026 (CSV)"
    records_count: int = 0
    projects: list[RepdProject] = Field(default_factory=list)
    partial: bool = False
    note: str | None = None

    @property
    def csv_name(self) -> str | None:
        """File name of the source CSV, e.g. `REPD_Publication_Q2_2026.csv`."""
        return str(self.csv_url).rsplit("/", 1)[-1] if self.csv_url else None

    def row_url(self, project: RepdProject) -> HttpUrl:
        """Link to the project's CSV row (RFC 7111 fragment), or the publication page without a CSV."""
        if self.csv_url is None:
            return self.dataset_url
        return HttpUrl(f"{self.csv_url}#row={project.csv_row}")


# Alias for compatibility with design.md interface
Snapshot = RepdSnapshot


class RepdSnapshotNotFoundError(Exception):
    """No REPD snapshot found on disk."""


def load_repd_snapshot(directory: Path | None = None) -> RepdSnapshot:
    """Load and validate the committed REPD snapshot from data/repd/."""
    root = directory or (settings.data_dir / "repd")
    data_path = root / REPD_FILE
    manifest_path = root / MANIFEST_FILE

    if not data_path.exists() or not manifest_path.exists():
        msg = f"No REPD snapshot in {root}. Run `uv run python -m bessible.planning.ingest_repd`."
        raise RepdSnapshotNotFoundError(msg)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_data = json.loads(data_path.read_text(encoding="utf-8"))
    results = raw_data.get("results", raw_data if isinstance(raw_data, list) else [])

    projects: list[RepdProject] = []
    for item in results:
        try:
            projects.append(RepdProject.model_validate(item))
        except Exception:
            log.warning("Skipping invalid REPD item %s", item, exc_info=True)

    fetched_at_raw = manifest.get("fetched_at")
    fetched_at = date.fromisoformat(fetched_at_raw) if fetched_at_raw else datetime.now(UTC).date()

    return RepdSnapshot(
        fetched_at=fetched_at,
        dataset=manifest.get("dataset", DATASET_NAME),
        dataset_url=HttpUrl(manifest.get("dataset_url", DATASET_URL)),
        csv_url=manifest.get("csv_url"),
        csv_title=manifest.get("csv_title"),
        records_count=len(projects),
        projects=projects,
        partial=bool(manifest.get("partial", False)),
        note=manifest.get("note"),
    )


@lru_cache(maxsize=1)
def get_repd_snapshot() -> RepdSnapshot:
    """Cached snapshot from default data directory."""
    return load_repd_snapshot()


def nearby_batteries(
    pos: Position,
    snap: RepdSnapshot | None = None,
    radius_km: float = 5.0,
) -> list[NearbyProject]:
    """Find battery storage projects within radius_km of position from REPD snapshot."""
    if snap is None:
        try:
            snap = get_repd_snapshot()
        except RepdSnapshotNotFoundError:
            log.warning("REPD snapshot not found; returning empty nearby list")
            return []

    nearby: list[NearbyProject] = []
    for project in snap.projects:
        dist = haversine_km(pos, project.latitude, project.longitude)
        if dist <= radius_km:
            nearby.append(
                NearbyProject(
                    id=project.id,
                    ref_id=project.ref_id,
                    csv_row=project.csv_row,
                    name=project.name,
                    mw=project.mw,
                    status=project.status,
                    status_date=project.status_date,
                    distance_km=round(dist, 2),
                    planning_authority=project.planning_authority,
                    planning_ref=project.planning_ref,
                    source_url=snap.row_url(project),
                )
            )

    nearby.sort(key=lambda p: p.distance_km)
    return nearby


# --- ingest --------------------------------------------------------------------------------------------------------


def _dmy(value: str) -> date | None:
    try:
        return datetime.strptime(value.strip(), "%d/%m/%Y").date()  # ruff: ignore[call-datetime-strptime-without-zone] (a calendar date)
    except ValueError:
        return None


def parse_repd_csv(text: str) -> list[RepdProject]:
    """Battery records from the REPD CSV text, with BNG coordinates converted to WGS84.

    `csv_row` counts records the way a spreadsheet does (header = row 1); some address cells hold line breaks,
    so it can differ from the raw line number.
    """
    from pyproj import Transformer  # ruff: ignore[import-outside-top-level] (ingest-only dependency, dev group)

    to_wgs84 = Transformer.from_crs("EPSG:27700", "EPSG:4326", always_xy=True)
    projects: list[RepdProject] = []
    for row_number, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        if row.get("Technology Type", "").strip() != TECHNOLOGY:
            continue
        try:
            x, y = float(row["X-coordinate"]), float(row["Y-coordinate"])
        except (KeyError, ValueError):
            log.warning("Skipping REPD row %d without coordinates", row_number)
            continue
        try:
            mw: float | None = float(row["Installed Capacity (MWelec)"])
        except ValueError:
            mw = None  # about a fifth of battery rows have no capacity yet
        milestones = [d for col in MILESTONE_COLUMNS if (d := _dmy(row.get(col, "")))]
        status_date = max(milestones) if milestones else _dmy(row["Record Last Updated (dd/mm/yyyy)"])
        if status_date is None:
            log.warning("Skipping REPD row %d without any date", row_number)
            continue
        lon, lat = to_wgs84.transform(x, y)
        ref_id = row["Ref ID"].strip()
        projects.append(
            RepdProject(
                id=f"repd-{ref_id}",
                ref_id=ref_id,
                csv_row=row_number,
                name=row["Site Name"].strip(),
                mw=mw,
                status=row["Development Status (short)"].strip() or row["Development Status"].strip(),
                status_date=status_date,
                latitude=round(lat, 6),
                longitude=round(lon, 6),
                postcode=row.get("Post Code", "").strip() or None,
                planning_authority=row.get("Planning Authority", "").strip() or None,
                planning_ref=row.get("Planning Application Reference", "").strip() or None,
            )
        )
    return projects


def latest_csv_attachment() -> tuple[str, str]:
    """(title, url) of the newest REPD CSV on the gov.uk publication page."""
    resp = httpx.get(CONTENT_API_URL, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    for att in resp.json()["details"]["attachments"]:
        if att.get("content_type") == "text/csv":
            return att["title"], att["url"]
    msg = f"No CSV attachment on {DATASET_URL}"
    raise RuntimeError(msg)


def ingest_from_records(  # ruff: ignore[too-many-arguments]
    records: list[RepdProject] | list[dict[str, object]],
    out_dir: Path,
    *,
    csv_url: str | None = None,
    csv_title: str | None = None,
    partial: bool = False,
    note: str | None = None,
) -> int:
    """Validate records and write snapshot files."""
    validated = [RepdProject.model_validate(r) for r in records]
    out_dir.mkdir(parents=True, exist_ok=True)

    dumped = [p.model_dump(mode="json") for p in validated]
    (out_dir / REPD_FILE).write_text(
        json.dumps({"total_count": len(dumped), "results": dumped}, indent=1),
        encoding="utf-8",
    )

    manifest = {
        "fetched_at": datetime.now(UTC).date().isoformat(),
        "dataset": DATASET_NAME,
        "dataset_url": DATASET_URL,
        "csv_url": csv_url,
        "csv_title": csv_title,
        "records_count": len(dumped),
        "file": REPD_FILE,
        "partial": partial,
        "note": note or f"REPD {TECHNOLOGY} records; csv_row is the row in csv_url (header = row 1).",
    }
    (out_dir / MANIFEST_FILE).write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(f"Wrote {len(dumped)} REPD records to {out_dir}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Ingest command-line entrypoint: download the latest REPD CSV (or read `--csv`) into data/repd/."""
    parser = argparse.ArgumentParser(description="Ingest REPD battery data into data/repd/")
    parser.add_argument("--csv", type=Path, help="Local copy of the REPD CSV (default: download the latest)")
    parser.add_argument("--csv-url", help="Published URL of the --csv file, cited in artifacts")
    parser.add_argument("--out", type=Path, default=settings.data_dir / "repd", help="Output directory")
    args = parser.parse_args(argv)

    if args.csv:
        if not args.csv_url:
            parser.error("--csv needs --csv-url so artifacts can link to the published file")
        title, url = None, args.csv_url
        raw = args.csv.read_bytes()
    else:
        title, url = latest_csv_attachment()
        sys.stdout.write(f"Downloading {title}: {url}\n")
        resp = httpx.get(url, timeout=120, follow_redirects=True)
        resp.raise_for_status()
        raw = resp.content
    text = raw.decode("cp1252", errors="replace")  # DESNZ publishes the CSV in Windows-1252
    return ingest_from_records(parse_repd_csv(text), args.out, csv_url=url, csv_title=title)


if __name__ == "__main__":
    sys.exit(main())
