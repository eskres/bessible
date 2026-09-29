"""Grid capacity proposal from the UKPN snapshot: headroom per direction, voltage cap, range, 5 MW floor."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import UTC, datetime
from operator import itemgetter
from typing import TYPE_CHECKING, Literal

import httpx
from pydantic import HttpUrl

from bessible.api import opendatasoft
from bessible.api import ukpn as ukpn_api
from bessible.cable_route import with_cable_route
from bessible.config import settings
from bessible.models import AlternateOption, Artifact, CapacityInput, CapacityOutput, Position
from bessible.ukpn.competition import competition
from bessible.ukpn.export_ceiling import export_ceiling
from bessible.ukpn.snapshot import (
    DATASET_ID,
    DATASET_URL,
    GRID_DATASET_ID,
    GRID_DATASET_URL,
    TABLE2A_DATASET_ID,
    TABLE2A_DATASET_URL,
    TABLE6_DATASET_ID,
    TABLE6_DATASET_URL,
    Snapshot,
    get_snapshot,
)

if TYPE_CHECKING:
    from bessible.api.ukpn import CapacityHeatmapSite
    from bessible.location.models import Headroom, Substation
    from bessible.ukpn.models import Competition

MODEL_USED = "ukpn-snapshot"
LIVE_MODEL_USED = "live-dno-headroom"
LIVE_TIMEOUT_S = 45
log = logging.getLogger(__name__)
CONFIDENCE = 0.9
FLOOR_MW = 5.0
SEARCH_RADIUS_KM = 5.0  # serving substation and alternates must be within this
MARGINAL_KM = 1.0
MAX_ALTERNATES = 4
SAME_SITE_KM = 0.1  # UKPN lists each busbar voltage of a site as its own row; rows this close are one site
LOW_VOLTAGE_CAP_MW = 8.0  # 22 kV and below
HIGH_VOLTAGE_CAP_MW = 50.0  # 33 kV and 66 kV
GRID_VOLTAGE_CAP_MW = 100.0  # 132 kV
EARTH_RADIUS_KM = 6371.0088
LIVE_CHECK_TIMEOUT_S = 3.0  # per-run live re-check of the snapshot; slower than this -> keep the snapshot
LIVE_CHECK_MODEL_USED = "live-ukpn-check"
HEADROOM_TOLERANCE_MW = 0.05
ASSUMED_CONNECTION_KV = 11.0  # no voltage published: assume a primary's 11 kV busbar (the lower, 8 MW cap)
POWER_FACTOR = 0.95  # MVA -> MW, for operators that publish import headroom in MVA (SSEN)
CHECK_LOG = "capacity_checks.jsonl"

_VOLTAGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kv", re.IGNORECASE)
_TIA_RE = re.compile(r"\(TIA\).*?=\s*(\d+)\s*MW", re.IGNORECASE)


def haversine_km(a: Position, lat: float, lon: float) -> float:
    """Great-circle distance in km from `a` to a WGS84 point."""
    p1, p2 = math.radians(a.lat), math.radians(lat)
    dphi, dlmb = p2 - p1, math.radians(lon - a.lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(h))


def connection_voltage_kv(row: CapacityHeatmapSite) -> float | None:
    """Proposed connection voltage; falls back to the row's own voltage, then the name ("Dorking Town 11kV")."""
    if row.voltage:
        return row.voltage
    if row.voltages:
        return row.voltages
    match = _VOLTAGE_RE.search(row.name or "")
    return float(match.group(1)) if match else None


def voltage_cap_mw(voltage_kv: float) -> float:
    """What the connection voltage can carry, independent of headroom."""
    return LOW_VOLTAGE_CAP_MW if voltage_kv <= 22 else HIGH_VOLTAGE_CAP_MW


def tia_threshold_mw(row: CapacityHeatmapSite) -> Literal[1, 5] | None:
    """Transmission Impact Assessment threshold (1 or 5 MW) parsed from the row description."""
    match = _TIA_RE.search(row.description or "")
    value = match.group(1) if match else None
    if value == "1":
        return 1
    return 5 if value == "5" else None


def distance_weight(distance_km: float) -> float:
    """1 up to 1 km, then falling steeply: exp(-2 * (d - 1))."""
    return 1.0 if distance_km <= MARGINAL_KM else math.exp(-2 * (distance_km - MARGINAL_KM))


class Headroom:
    """Effective headroom of one primary substation. All values in MW."""

    def __init__(
        self,
        row: CapacityHeatmapSite,
        *,
        flexible: bool,
        export_ceiling_mw: float | None = None,
    ) -> None:
        """Compute import/export headroom net of accepted offers, then firm, ceiling and size."""
        voltage = connection_voltage_kv(row)
        if voltage is None:
            msg = f"Cannot determine connection voltage for substation '{row.name}'"
            raise ValueError(msg)
        self.row = row
        self.voltage_kv = voltage
        self.cap_mw = voltage_cap_mw(voltage)
        self.export_ceiling_mw = export_ceiling_mw
        load_accepted = row.loadconnectionoffersacceptedcapacity or 0.0
        gen_accepted = row.generationconnectionoffersacceptedcapacity or 0.0
        reverse = (row.reversepowerflowavailablecapacity or 0.0) if (row.demandminimum or 0.0) < 0 else 0.0
        self.import_mw = max(0.0, (row.demandavailablecapacity or 0.0) - load_accepted)
        self.export_mw = max(0.0, (row.generationavailablecapacity or 0.0) - gen_accepted + reverse)
        self.binding_direction: Literal["import", "export"] = "import" if self.import_mw <= self.export_mw else "export"
        self.firm_mw = min(self.import_mw, self.export_mw, self.cap_mw)
        self.import_ceiling = (row.demandfirmcapacity or 0.0) - (row.demandminimum or 0.0) - load_accepted
        if export_ceiling_mw is not None:
            self.ceiling_mw = max(self.firm_mw, min(self.import_ceiling, export_ceiling_mw, self.cap_mw))
        else:
            self.ceiling_mw = max(self.firm_mw, min(self.import_ceiling, self.cap_mw))
        self.size_mw = self.ceiling_mw if flexible else self.firm_mw


def _viable_message(head: Headroom, *, flexible: bool) -> str | None:
    """Explain why a site fails the 5 MW floor, or None if it passes."""
    if head.size_mw >= FLOOR_MW:
        return None
    name = head.row.name
    if not flexible and head.ceiling_mw >= FLOOR_MW:
        return (
            f"Firm capacity at {name} is {head.firm_mw:.1f} MW (below the {FLOOR_MW:g} MW floor); "
            f"ceiling is {head.ceiling_mw:.1f} MW. Enable flexible connection (--flexible) to use the ceiling."
        )
    return (
        f"Ceiling capacity at {name} is {head.ceiling_mw:.1f} MW, below the {FLOOR_MW:g} MW floor; "
        f"firm capacity is {head.firm_mw:.1f} MW."
    )


def _artifact(
    run_id: str,
    suffix: str,
    claim: str,
    snapshot: Snapshot,
    confidence: float = CONFIDENCE,
    dataset_id: str = DATASET_ID,
    dataset_url: str = DATASET_URL,
) -> Artifact:
    return Artifact(
        id=f"capacity-{suffix}-{run_id[:8]}",
        stage="capacity",
        claim=f"{claim} [{dataset_id}, snapshot {snapshot.fetched_at.isoformat()}]",
        source_url=HttpUrl(dataset_url),
        confidence=confidence,
        model_used=MODEL_USED,
    )


def _same_site(a: CapacityHeatmapSite, b: CapacityHeatmapSite) -> bool:
    """True when two rows are busbars of one site (UKPN gives "Histon Grid 33kV" and "Histon Primary 11kV" one spot)."""
    if a.latitude is None or a.longitude is None or b.latitude is None or b.longitude is None:
        return False
    return haversine_km(Position(lat=a.latitude, lon=a.longitude), b.latitude, b.longitude) <= SAME_SITE_KM


def _alternates(
    nearby: list[tuple[float, CapacityHeatmapSite]],
    snapshot: Snapshot,
    *,
    flexible: bool,
) -> list[AlternateOption]:
    """Up to four other sites within range, best distance-weighted size first. Far ones are flagged, not hidden.

    One option per site: the largest of its busbars. A busbar at the serving site is listed only if it beats the
    serving connection, so a smaller voltage on the same spot never shows up as a separate choice.
    """
    serving = nearby[0][1]
    serving_mw = Headroom(serving, flexible=flexible, export_ceiling_mw=export_ceiling(serving, snapshot)).size_mw
    sites: list[tuple[float, Headroom]] = []
    for d, row in nearby[1:]:
        alt = Headroom(row, flexible=flexible, export_ceiling_mw=export_ceiling(row, snapshot))
        if _same_site(row, serving) and alt.size_mw <= serving_mw:
            continue
        i = next((i for i, (_, h) in enumerate(sites) if _same_site(row, h.row)), None)
        if i is None and len(sites) < MAX_ALTERNATES:
            sites.append((d, alt))
        elif i is not None and alt.size_mw > sites[i][1].size_mw:
            sites[i] = (d, alt)
    scored: list[tuple[float, AlternateOption]] = []
    for d, alt in sites:
        row = alt.row
        option = AlternateOption(
            substation=row.name or "",
            distance_km=round(d, 2),
            size_mw=round(alt.size_mw, 2),
            marginal=d > MARGINAL_KM,
            position=Position(lat=row.latitude, lon=row.longitude)
            if row.latitude is not None and row.longitude is not None
            else None,
        )
        scored.append((alt.size_mw * distance_weight(d), option))
    return [opt for _, opt in sorted(scored, key=lambda t: -t[0])]


def _artifacts(
    run_id: str,
    snapshot: Snapshot,
    head: Headroom,
    dist: float,
    comp: Competition | None = None,
) -> list[Artifact]:
    """One artifact per key figure, plus context artifacts for RAG, parent GSP and TIA, competition, and caveat."""
    row = head.row
    tia = tia_threshold_mw(row)
    marginal_note = f"; {dist:.1f} km away, marginal beyond {MARGINAL_KM:g} km" if dist > MARGINAL_KM else ""
    arts = [
        _artifact(
            run_id,
            "substation",
            f"Predicted point of connection: {row.name} ({head.voltage_kv:g} kV), "
            f"{dist:.2f} km away{marginal_note}. "
            "Nearest primary substation by distance (distribution-area polygons not in the snapshot)",
            snapshot,
        ),
        _artifact(
            run_id,
            "headroom",
            f"Effective headroom at {row.name}: import {head.import_mw:.1f} MW, export {head.export_mw:.1f} MW "
            "(available capacity minus accepted offers; export adds reverse power flow "
            "when minimum demand is negative)",
            snapshot,
        ),
        _artifact(
            run_id,
            "range",
            f"Firm {head.firm_mw:.1f} MW, ceiling {head.ceiling_mw:.1f} MW, limited by {head.binding_direction}; "
            f"{head.voltage_kv:g} kV caps size at {head.cap_mw:g} MW. Ceiling = "
            + (
                f"lower of import-side formula and Table 2a export ceiling ({head.export_ceiling_mw:g} MW)"
                if head.export_ceiling_mw is not None
                else "import-side formula (demand firm capacity - minimum demand - accepted load offers), an assumption"
            )
            + ". No seasonal split in this dataset",
            snapshot,
        ),
        _artifact(
            run_id,
            "context",
            f"Context only, not used in the verdict: RAG demand {row.demandconstraint}, "
            f"generation {row.generationconstraint}; parent GSP {row.gsp or 'unknown'}; "
            f"TIA threshold {tia if tia is not None else 'unknown'} MW",
            snapshot,
        ),
    ]

    if head.export_ceiling_mw is None:
        arts.append(
            _artifact(
                run_id,
                "export-ceiling",
                f"Export ceiling is unavailable for {row.name}; overall ceiling uses import side only",
                snapshot,
                dataset_id=TABLE2A_DATASET_ID,
                dataset_url=TABLE2A_DATASET_URL,
            )
        )

    if comp is not None:
        if comp.weighted_mw > 0:
            comp_claim = (
                f"Connection competition at {row.name}: {comp.offers_not_accepted_mw:.1f} MW offers not accepted, "
                f"{comp.budget_estimates_mw:.1f} MW budget estimates, {comp.enquiries_mw:.1f} MW enquiries; "
                f"weighted total {comp.weighted_mw:.1f} MW (pressure: {comp.pressure}). "
                "Context only; not subtracted from effective headroom"
            )
        else:
            comp_claim = (
                f"Connection competition at {row.name}: no competing connection records found. "
                "Weighted total 0.0 MW (pressure: low). Context only; not subtracted from effective headroom"
            )
        arts.append(
            _artifact(
                run_id,
                "competition",
                comp_claim,
                snapshot,
                dataset_id=TABLE6_DATASET_ID,
                dataset_url=TABLE6_DATASET_URL,
            )
        )

    arts.append(
        _artifact(
            run_id,
            "caveat",
            "Effective headroom subtracts the current accepted connection queue; "
            "this may be pessimistic if speculative projects leave the queue under proposed Ofgem reforms",
            snapshot,
        )
    )

    return arts


def _propose_grid_level(
    position: Position,
    snapshot: Snapshot,
    run_id: str,
    *,
    flexible: bool,
    requested_mw: float,
) -> CapacityOutput:
    """Proposal logic when requested size exceeds the primary voltage cap (>50 MW)."""
    if requested_mw > GRID_VOLTAGE_CAP_MW:
        msg = (
            f"Requested battery size ({requested_mw:g} MW) exceeds maximum grid-level capacity ({GRID_VOLTAGE_CAP_MW:g} MW); "
            "sites above 100 MW are out of scope"
        )
        return CapacityOutput(
            viable=False,
            message=msg,
            out_of_area=False,
            snapshot_date=snapshot.fetched_at,
            artifacts=[
                _artifact(
                    run_id,
                    "scope",
                    f"Out of scope: {msg}",
                    snapshot,
                    confidence=1.0,
                    dataset_id=GRID_DATASET_ID,
                    dataset_url=GRID_DATASET_URL,
                )
            ],
        )

    grid_subs = [
        g
        for g in snapshot.grid_substations
        if g.position is not None and g.position.lat is not None and g.position.lon is not None
    ]
    ranked = sorted(
        ((haversine_km(position, g.position.lat, g.position.lon), g) for g in grid_subs),
        key=itemgetter(0),
    )
    nearby = [(d, g) for d, g in ranked if d <= SEARCH_RADIUS_KM]
    if not nearby:
        msg = (
            f"No UK Power Networks grid-level substation within {SEARCH_RADIUS_KM:g} km in the snapshot; "
            "no grid-level data covers the site"
        )
        return CapacityOutput(
            viable=False,
            message=msg,
            out_of_area=True,
            snapshot_date=snapshot.fetched_at,
            artifacts=[
                _artifact(
                    run_id,
                    "area",
                    f"No grid-level coverage: {msg}",
                    snapshot,
                    confidence=0.8,
                    dataset_id=GRID_DATASET_ID,
                    dataset_url=GRID_DATASET_URL,
                )
            ],
        )

    dist, serving = nearby[0]
    import_mw = serving.headroom_import_mw
    export_mw = serving.headroom_export_mw
    binding_direction: Literal["import", "export"] = "import" if import_mw <= export_mw else "export"
    firm_mw = min(import_mw, export_mw, GRID_VOLTAGE_CAP_MW)
    ceiling_mw = max(firm_mw, min(import_mw, GRID_VOLTAGE_CAP_MW))
    size_mw = ceiling_mw if flexible else firm_mw

    comp = competition(serving, snapshot, effective_headroom=firm_mw)

    if size_mw < FLOOR_MW:
        if not flexible and ceiling_mw >= FLOOR_MW:
            msg = (
                f"Firm capacity at {serving.name} is {firm_mw:.1f} MW (below the {FLOOR_MW:g} MW floor); "
                f"ceiling is {ceiling_mw:.1f} MW. Enable flexible connection (--flexible) to use the ceiling."
            )
        else:
            msg = (
                f"Ceiling capacity at {serving.name} is {ceiling_mw:.1f} MW, below the {FLOOR_MW:g} MW floor; "
                f"firm capacity is {firm_mw:.1f} MW."
            )
        viable = False
    else:
        viable = True
        msg = None

    scored_alts: list[tuple[float, AlternateOption]] = []
    for d, g in nearby[1 : MAX_ALTERNATES + 1]:
        alt_firm = min(g.headroom_import_mw, g.headroom_export_mw, GRID_VOLTAGE_CAP_MW)
        alt_ceiling = max(alt_firm, min(g.headroom_import_mw, GRID_VOLTAGE_CAP_MW))
        alt_size = alt_ceiling if flexible else alt_firm
        opt = AlternateOption(
            substation=g.name,
            distance_km=round(d, 2),
            size_mw=round(alt_size, 2),
            marginal=d > MARGINAL_KM,
            position=Position(lat=g.position.lat, lon=g.position.lon),
        )
        scored_alts.append((alt_size * distance_weight(d), opt))
    alternates = [opt for _, opt in sorted(scored_alts, key=lambda t: -t[0])]

    marginal_note = f"; {dist:.1f} km away, marginal beyond {MARGINAL_KM:g} km" if dist > MARGINAL_KM else ""
    artifacts = [
        _artifact(
            run_id,
            "substation",
            f"Predicted point of connection: {serving.name} (132 kV), "
            f"{dist:.2f} km away{marginal_note}. "
            "Grid-level substation connection for large request (>50 MW)",
            snapshot,
            dataset_id=GRID_DATASET_ID,
            dataset_url=GRID_DATASET_URL,
        ),
        _artifact(
            run_id,
            "headroom",
            f"Effective headroom at {serving.name}: import {import_mw:.1f} MW, export {export_mw:.1f} MW",
            snapshot,
            dataset_id=GRID_DATASET_ID,
            dataset_url=GRID_DATASET_URL,
        ),
        _artifact(
            run_id,
            "range",
            f"Firm {firm_mw:.1f} MW, ceiling {ceiling_mw:.1f} MW, limited by {binding_direction}; "
            f"132 kV connection capped at {GRID_VOLTAGE_CAP_MW:g} MW",
            snapshot,
            dataset_id=GRID_DATASET_ID,
            dataset_url=GRID_DATASET_URL,
        ),
        _artifact(
            run_id,
            "context",
            f"Context only, not used in the verdict: licence area {serving.licence_area or 'unknown'}; "
            f"parent GSP {serving.gsp or 'unknown'}; Bulk Supply Point {serving.bsp or 'unknown'}",
            snapshot,
            dataset_id=GRID_DATASET_ID,
            dataset_url=GRID_DATASET_URL,
        ),
    ]

    if comp.weighted_mw > 0:
        comp_claim = (
            f"Connection competition at {serving.name}: {comp.offers_not_accepted_mw:.1f} MW offers not accepted, "
            f"{comp.budget_estimates_mw:.1f} MW budget estimates, {comp.enquiries_mw:.1f} MW enquiries; "
            f"weighted total {comp.weighted_mw:.1f} MW (pressure: {comp.pressure}). "
            "Context only; not subtracted from effective headroom"
        )
    else:
        comp_claim = (
            f"Connection competition at {serving.name}: no competing connection records found. "
            "Weighted total 0.0 MW (pressure: low). Context only; not subtracted from effective headroom"
        )
    artifacts.extend([
        _artifact(
            run_id,
            "competition",
            comp_claim,
            snapshot,
            dataset_id=TABLE6_DATASET_ID,
            dataset_url=TABLE6_DATASET_URL,
        ),
        _artifact(
            run_id,
            "caveat",
            "Effective headroom subtracts the current accepted connection queue; "
            "this may be pessimistic if speculative projects leave the queue under proposed Ofgem reforms",
            snapshot,
        ),
    ])

    return CapacityOutput(
        viable=viable,
        message=msg,
        out_of_area=False,
        substation=serving.name,
        connection_voltage_kv=132.0,
        firm_mw=firm_mw,
        ceiling_mw=ceiling_mw,
        recommended_mw=size_mw,
        binding_direction=binding_direction,
        binding_season=None,
        distance_km=round(dist, 2),
        substation_position=Position(lat=serving.position.lat, lon=serving.position.lon),
        alternates=alternates,
        tia_threshold_mw=None,
        snapshot_date=snapshot.fetched_at,
        competition=comp,
        gsp=serving.gsp,
        artifacts=artifacts,
    )


def propose(
    position: Position,
    snapshot: Snapshot,
    run_id: str,
    *,
    flexible: bool,
    requested_mw: float | None = None,
) -> CapacityOutput:
    """Deterministic capacity proposal for a position. Pure: no I/O, no clock."""
    if requested_mw is not None and requested_mw > HIGH_VOLTAGE_CAP_MW:
        return _propose_grid_level(position, snapshot, run_id, flexible=flexible, requested_mw=requested_mw)

    primaries = [
        r for r in snapshot.substations if r.type == "Primary" and r.latitude is not None and r.longitude is not None
    ]
    ranked = sorted(
        ((haversine_km(position, r.latitude, r.longitude), r) for r in primaries),  # type: ignore[arg-type]
        key=itemgetter(0),
    )
    nearby = [(d, r) for d, r in ranked if d <= SEARCH_RADIUS_KM]
    if not nearby:
        coverage = "partial demo snapshot" if snapshot.partial else "snapshot"
        msg = (
            f"No UK Power Networks primary substation within {SEARCH_RADIUS_KM:g} km in the {coverage} "
            f"({snapshot.fetched_at.isoformat()}); this tool covers UKPN areas "
            "(London, South East, East of England) only"
        )
        return CapacityOutput(
            viable=False,
            message=msg,
            out_of_area=True,
            snapshot_date=snapshot.fetched_at,
            artifacts=[_artifact(run_id, "area", f"Outside UKPN coverage: {msg}", snapshot, confidence=0.8)],
        )

    dist, serving_row = nearby[0]  # nearest primary; distribution-area polygons are not in the snapshot yet
    exp_ceil = export_ceiling(serving_row, snapshot)
    head = Headroom(serving_row, flexible=flexible, export_ceiling_mw=exp_ceil)
    message = _viable_message(head, flexible=flexible)

    comp = competition(serving_row, snapshot, effective_headroom=head.firm_mw)

    alternates = _alternates(nearby, snapshot, flexible=flexible)

    return CapacityOutput(
        viable=message is None,
        message=message,
        out_of_area=False,
        substation=serving_row.name,
        connection_voltage_kv=head.voltage_kv,
        firm_mw=head.firm_mw,
        ceiling_mw=head.ceiling_mw,
        export_ceiling_mw=head.export_ceiling_mw,
        recommended_mw=head.size_mw,
        binding_direction=head.binding_direction,
        binding_season=None,
        distance_km=round(dist, 2),
        substation_position=Position(lat=serving_row.latitude, lon=serving_row.longitude),
        alternates=alternates,
        tia_threshold_mw=tia_threshold_mw(serving_row),
        snapshot_date=snapshot.fetched_at,
        competition=comp,
        gsp=serving_row.gsp,
        artifacts=_artifacts(run_id, snapshot, head, dist, comp=comp),
    )


def _append_check_log(inp: CapacityInput, out: CapacityOutput) -> None:
    """Append one JSON line per check to ``out/capacity_checks.jsonl`` (write-only; never read in a run)."""
    record = {
        "run_id": inp.run_id,
        "postcode": inp.location.postcode,
        "position": inp.location.position.model_dump(),
        "flexible": inp.request.flexible_connection,
        "snapshot_date": out.snapshot_date.isoformat() if out.snapshot_date else None,
        "result": out.model_dump(mode="json", exclude={"artifacts"}),
    }
    path = settings.data_dir.parent / "out" / CHECK_LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


LIVE_OPERATORS = "UK Power Networks, NGED, SSEN, SP Energy Networks and Northern Powergrid areas"


async def propose_live(position: Position, run_id: str, *, fallback: CapacityOutput) -> CapacityOutput:
    """Outside the snapshot: live DNO headroom (UKPN, NGED, SSEN, SPEN, Northern Powergrid) via `location.collate`.

    Any failure -> fallback.
    """
    if not settings.live_capacity:
        return fallback
    try:
        live = await asyncio.wait_for(_propose_live(position, run_id), LIVE_TIMEOUT_S)
    except Exception:
        log.exception("live capacity lookup failed; keeping snapshot result")
        return fallback
    if live is not None:
        return live
    msg = (
        f"No primary substation with published headroom within {SEARCH_RADIUS_KM:g} km. Live data covers "
        f"{LIVE_OPERATORS}; other network operators (e.g. Electricity North West) are not supported yet"
    )
    return fallback.model_copy(update={"message": msg})


def live_connection_kv(sub: Substation) -> tuple[float, bool]:
    """The substation's connection voltage, and whether it is assumed because the operator publishes none."""
    if sub.connection_voltage_kv:
        return sub.connection_voltage_kv, False
    return ASSUMED_CONNECTION_KV, True


def live_demand_mw(head: Headroom) -> float | None:
    """Import headroom in MW; MVA figures are scaled by POWER_FACTOR."""
    if head.demand is None:
        return None
    return head.demand * POWER_FACTOR if head.demand_unit == "MVA" else head.demand


def live_firm_mw(sub: Substation) -> float:
    """A battery imports and exports, so the smaller headroom binds, capped by the connection voltage."""
    head = sub.headroom
    if head is None:
        return 0.0
    cap = voltage_cap_mw(live_connection_kv(sub)[0])
    return max(0.0, min(head.generation_mw or 0.0, live_demand_mw(head) or 0.0, cap))


async def _propose_live(position: Position, run_id: str) -> CapacityOutput | None:
    from bessible.location import Coordinates, collate  # ruff: ignore[import-outside-top-level]

    location = await collate(Coordinates(lat=position.lat, lon=position.lon))
    with_headroom = [
        s for s in location.deterministic.grid.substations if s.headroom and s.distance_km <= SEARCH_RADIUS_KM
    ]
    if not with_headroom:
        return None
    primaries = [s for s in with_headroom if s.kind == "primary"] or with_headroom

    # Best connection option in reach: headroom weighted down by distance (same weighting as the alternates).
    primaries = sorted(primaries, key=lambda sub: -live_firm_mw(sub) * distance_weight(sub.distance_km))
    serving = primaries[0]
    head = serving.headroom
    assert head is not None  # ruff: ignore[assert]
    firm_mw = round(live_firm_mw(serving), 2)
    kv, kv_assumed = live_connection_kv(serving)
    import_mw = live_demand_mw(head)
    fetched_at = _utc_now()
    unpublished = [d for d, v in (("import", head.demand), ("export", head.generation_mw)) if v is None]
    message: str | None = (
        None
        if firm_mw >= FLOOR_MW
        else f"Firm capacity at {serving.name} is {firm_mw:.1f} MW, below the {FLOOR_MW:g} MW floor."
    )
    urls = [
        s.url
        for s in location.sources
        if s.status == "ok"
        and s.url.startswith("http")
        and s.name.startswith(("UKPN", "NGED", "SSEN", "SP Energy Networks", "Northern Powergrid"))
    ]
    source = HttpUrl(urls[0]) if urls else HttpUrl(DATASET_URL)

    def art(suffix: str, claim: str) -> Artifact:
        return Artifact(
            id=f"capacity-{suffix}-{run_id[:8]}",
            stage="capacity",
            claim=f"{claim} [{serving.operator} live open data]",
            source_url=source,
            confidence=0.85,
            model_used=LIVE_MODEL_USED,
        )

    return CapacityOutput(
        viable=message is None,
        message=message,
        out_of_area=False,
        substation=serving.name,
        connection_voltage_kv=kv,
        firm_mw=firm_mw,
        ceiling_mw=firm_mw,
        recommended_mw=firm_mw,
        binding_direction="import" if (import_mw or 0.0) <= (head.generation_mw or 0.0) else "export",
        distance_km=round(serving.distance_km, 2),
        substation_position=Position(lat=serving.coords.lat, lon=serving.coords.lon),
        alternates=[
            AlternateOption(
                substation=s.name,
                distance_km=round(s.distance_km, 2),
                size_mw=round(live_firm_mw(s), 2),
                marginal=s.distance_km > MARGINAL_KM,
                position=Position(lat=s.coords.lat, lon=s.coords.lon),
            )
            for s in primaries[1 : MAX_ALTERNATES + 1]
        ],
        tia_threshold_mw=1 if serving.tia_threshold_mw == 1 else 5 if serving.tia_threshold_mw == 5 else None,
        artifacts=[
            art(
                "substation",
                f"Predicted point of connection: {serving.name} ({serving.operator}), connecting at {kv:g} kV "
                f"(cap {voltage_cap_mw(kv):g} MW), {serving.distance_km:.2f} km away"
                + (
                    f"; {serving.operator} publishes no voltage for this substation, so {kv:g} kV is assumed"
                    if kv_assumed
                    else ""
                ),
            ),
            art(
                "headroom",
                f"Published headroom at {serving.name}: import {head.demand} {head.demand_unit}"
                + (
                    f" (= {import_mw:.1f} MW at power factor {POWER_FACTOR:g})"
                    if head.demand_unit == "MVA" and import_mw is not None
                    else ""
                )
                + f", export {head.generation_mw} MW ({head.basis}); firm = smaller of the two, capped by the "
                f"connection voltage = {firm_mw:g} MW; fetched live at {fetched_at}"
                + (
                    f"; {serving.operator} publishes no {' or '.join(unpublished)} headroom here, so it counts as 0 MW"
                    if unpublished
                    else ""
                ),
            ),
        ],
    )


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


async def fetch_live_heatmap(position: Position, radius_km: float = SEARCH_RADIUS_KM) -> list[CapacityHeatmapSite]:
    """UKPN capacity heatmap rows within `radius_km` of the site, fetched live in one request."""
    if settings.ukpn_api_key is None:
        msg = "UKPN_API_KEY is not set"
        raise RuntimeError(msg)
    spec = ukpn_api.DATASETS["capacity_heatmap"]
    req = spec.near(position.lat, position.lon, radius_km * 1000)
    headers = opendatasoft.auth_headers(settings.ukpn_api_key.get_secret_value())
    async with httpx.AsyncClient(timeout=LIVE_CHECK_TIMEOUT_S, headers=headers) as client:
        res = await client.get(req.url(), params=req.params())
        res.raise_for_status()
    return spec.parse(res.json()).results


def _row_key(row: CapacityHeatmapSite) -> str:
    return row.mrid or row.name or ""


def headroom_changes(
    snapshot_rows: list[CapacityHeatmapSite], live_rows: list[CapacityHeatmapSite]
) -> list[tuple[CapacityHeatmapSite, CapacityHeatmapSite]]:
    """(snapshot, live) pairs whose import / export headroom or connection voltage changed. Matched on UKPN's mrid."""
    live = {_row_key(r): r for r in live_rows}
    changed = []
    for old in snapshot_rows:
        new = live.get(_row_key(old))
        if new is None:
            continue
        moved = any(
            abs((getattr(new, f) or 0.0) - (getattr(old, f) or 0.0)) > HEADROOM_TOLERANCE_MW
            for f in ("demandavailablecapacity", "generationavailablecapacity")
        )
        if moved or new.voltage != old.voltage:
            changed.append((old, new))
    return changed


def _live_check_artifact(run_id: str, claim: str, confidence: float) -> Artifact:
    return Artifact(
        id=f"capacity-live-check-{run_id[:8]}",
        stage="capacity",
        claim=f"{claim} [{DATASET_ID}, live]",
        source_url=HttpUrl(DATASET_URL),
        confidence=confidence,
        model_used=LIVE_CHECK_MODEL_USED,
    )


async def verify_live(
    position: Position,
    snapshot: Snapshot,
    out: CapacityOutput,
    run_id: str,
    *,
    flexible: bool,
    requested_mw: float | None = None,
) -> CapacityOutput:
    """Re-check the snapshot rows near the site against live UKPN data (one request per run).

    Unchanged: say so. Changed: propose again on the live rows and show both values. Unreachable: keep the snapshot
    and say it is unverified. Never fails the run.
    """
    checked_at = _utc_now()
    try:
        live_rows = await fetch_live_heatmap(position)
    except Exception as exc:  # any failure keeps the snapshot result
        log.warning("live UKPN check failed; keeping the snapshot result: %s", exc)
        art = _live_check_artifact(
            run_id,
            f"Not verified against live UKPN data ({type(exc).__name__}); figures are from the snapshot dated "
            f"{snapshot.fetched_at.isoformat()}",
            confidence=0.7,
        )
        return out.model_copy(update={"artifacts": [*out.artifacts, art]})

    nearby = [
        r
        for r in snapshot.substations
        if r.latitude is not None
        and r.longitude is not None
        and haversine_km(position, r.latitude, r.longitude) <= SEARCH_RADIUS_KM
    ]
    changes = headroom_changes(nearby, live_rows)
    if not changes:
        art = _live_check_artifact(
            run_id,
            f"Checked against live UKPN data at {checked_at}: headroom at {len(nearby)} substation(s) within "
            f"{SEARCH_RADIUS_KM:g} km matches the snapshot dated {snapshot.fetched_at.isoformat()}",
            confidence=0.95,
        )
        return out.model_copy(update={"artifacts": [*out.artifacts, art]})

    live_by_key = {_row_key(r): r for r in live_rows if r.name and r.latitude is not None and r.longitude is not None}
    patched = snapshot.model_copy(
        update={"substations": [live_by_key.get(_row_key(r), r) for r in snapshot.substations]}
    )
    fresh = propose(position, patched, run_id, flexible=flexible, requested_mw=requested_mw)
    detail = "; ".join(
        f"{old.name}: import {old.demandavailablecapacity} -> {new.demandavailablecapacity} MW, "
        f"export {old.generationavailablecapacity} -> {new.generationavailablecapacity} MW"
        for old, new in changes[:3]
    )
    art = _live_check_artifact(
        run_id,
        f"Live UKPN data at {checked_at} differs from the snapshot dated {snapshot.fetched_at.isoformat()}, so these "
        f"figures use the live values ({len(changes)} substation(s) changed: {detail})",
        confidence=0.9,
    )
    return fresh.model_copy(update={"artifacts": [*fresh.artifacts, art]})


async def propose_capacity(inp: CapacityInput) -> CapacityOutput:
    """Assess available grid headroom at the located position and propose capacity limits."""
    snapshot = await asyncio.to_thread(get_snapshot)
    flexible, requested_mw = inp.request.flexible_connection, inp.request.battery_mw
    out = propose(inp.location.position, snapshot, inp.run_id, flexible=flexible, requested_mw=requested_mw)
    if out.out_of_area:
        out = await propose_live(inp.location.position, inp.run_id, fallback=out)
    elif settings.live_capacity and (requested_mw is None or requested_mw <= HIGH_VOLTAGE_CAP_MW):
        # Primary-level proposals come from heatmap rows: check those rows are still current.
        out = await verify_live(
            inp.location.position, snapshot, out, inp.run_id, flexible=flexible, requested_mw=requested_mw
        )
    out = with_cable_route(inp.location.position, out, inp.run_id)
    await asyncio.to_thread(_append_check_log, inp, out)
    return out
