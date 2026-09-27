"""The hard checks: can a battery of this size be built here at all? Each is `Proposal -> Check`, no I/O."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from .models import Check, Fact, Limits, Outcome, PossibilityReport, Proposal

if TYPE_CHECKING:
    from collections.abc import Callable

    from bessible.location.models import Designation, Substation

HA_PER_ACRE = 0.40468564
UNKNOWN_CONFIDENCE = 0.2  # an "unknown" check is a gap in the evidence, not a finding

PROTECTED_ECOLOGY = ("sssi", "sac", "spa", "ramsar", "national_nature_reserve", "ancient_woodland")
PROTECTED_HERITAGE = (
    "scheduled_monument",
    "registered_park_or_garden",
    "registered_battlefield",
    "world_heritage_site",
)
PROTECTED_LANDSCAPE = ("national_park", "national_landscape")

# Names of the `LocationData.sources` each check's facts come from (matched by prefix).
TITLE_SOURCE = ("Planning Data: title boundary",)
TERRAIN_SOURCES = ("EA LIDAR", "Open-Meteo")
FLOOD_SOURCE = ("EA: flood zones",)
LAND_SOURCES = ("Natural England: alc", "Planning Data: designations on the title")
DESIGNATION_SOURCES = ("Natural England", "Planning Data: designations")
GRID_SOURCES = ("UKPN", "NGED", "SSEN", "SP Energy Networks")


# ------------------------------------------- the land ------------------------------------------- #


def title_found(proposal: Proposal) -> Check:
    result = partial(_check, "title_found", proposal, TITLE_SOURCE)
    title = proposal.location.title
    if title is None:
        return result("unknown", "No registered title boundary at this point.")
    return result("pass", f"Title {title.inspire_id} covers {title.area_ha:.2f} ha.", inspire_id=title.inspire_id)


def enough_area(proposal: Proposal) -> Check:
    result = partial(_check, "enough_area", proposal, TITLE_SOURCE)
    title = proposal.location.title
    if title is None:
        return result("unknown", "No title boundary to measure.")
    min_ha = proposal.battery_mwh * proposal.limits.acres_per_mwh_min * HA_PER_ACRE
    comfortable_ha = proposal.battery_mwh * proposal.limits.acres_per_mwh_comfortable * HA_PER_ACRE
    outcome = _lower_is_worse(title.area_ha, fail_below=min_ha, warn_below=comfortable_ha)
    reason = (
        f"{proposal.battery_mwh:g} MWh needs {min_ha:.2f}-{comfortable_ha:.2f} ha; the title has {title.area_ha:.2f}."
    )
    return result(outcome, reason, area_ha=title.area_ha, min_ha=min_ha, comfortable_ha=comfortable_ha)


def buildable_slope(proposal: Proposal) -> Check:
    result = partial(_check, "buildable_slope", proposal, TERRAIN_SOURCES)
    terrain, limits = proposal.location.deterministic.terrain, proposal.limits
    if terrain is None or terrain.slope_median_pct is None:
        return result("unknown", "No 1 m terrain model inside the title.")
    slope = terrain.slope_median_pct
    outcome = _higher_is_worse(
        slope, warn_above=limits.comfortable_median_slope_pct, fail_above=limits.max_median_slope_pct
    )
    reason = f"Median slope {slope}% (limit {limits.max_median_slope_pct:g}%), relief {terrain.relief_m} m."
    return result(outcome, reason, slope_median_pct=slope, relief_m=terrain.relief_m)


def outside_flood_zone_3(proposal: Proposal) -> Check:
    result = partial(_check, "outside_flood_zone_3", proposal, FLOOD_SOURCE)
    flood, limit = proposal.location.deterministic.flood, proposal.limits.max_flood_zone_3_pct
    if flood is None:
        return result("unknown", _not_assessed("Flood zones", proposal))
    outcome: Outcome = "fail" if flood.zone_3_pct >= limit else "warn" if flood.zone > 1 else "pass"
    reason = f"{flood.zone_3_pct}% of the title is in Flood Zone 3, {flood.zone_2_pct}% in Zone 2 (limit {limit:g}%)."
    return result(outcome, reason, zone=flood.zone, zone_3_pct=flood.zone_3_pct, zone_2_pct=flood.zone_2_pct)


def outside_green_belt(proposal: Proposal) -> Check:
    result = partial(_check, "outside_green_belt", proposal, LAND_SOURCES)
    land = proposal.location.deterministic.land
    if land is None:
        return result("unknown", _not_assessed("Green belt", proposal))
    if not land.green_belt:
        return result("pass", "Not in the green belt.")
    return result("warn", f"In the green belt ({land.green_belt_name or 'unnamed'}): needs very special circumstances.")


def avoids_best_farmland(proposal: Proposal) -> Check:
    result = partial(_check, "avoids_best_farmland", proposal, LAND_SOURCES)
    land = proposal.location.deterministic.land
    if land is None:
        return result("unknown", _not_assessed("Farmland grade", proposal))
    if land.best_and_most_versatile is None:
        return result("unknown", "Farmland grade unknown: provisional Grade 3 is not split into 3a / 3b here.")
    grades = ", ".join(f"{g.grade} {g.overlap_pct:g}%" for g in land.alc)
    if not land.best_and_most_versatile:
        return result("pass", f"No best and most versatile farmland ({grades}).", grades=grades)
    return result(
        "warn", f"Best and most versatile farmland ({grades}): policy steers development away.", grades=grades
    )


# ----------------------------------------- designations ----------------------------------------- #


def clear_of_protected_ecology(proposal: Proposal) -> Check:
    return _clear_of(PROTECTED_ECOLOGY, "clear_of_protected_ecology", proposal)


def clear_of_protected_heritage(proposal: Proposal) -> Check:
    return _clear_of(PROTECTED_HERITAGE, "clear_of_protected_heritage", proposal)


def clear_of_protected_landscape(proposal: Proposal) -> Check:
    return _clear_of(PROTECTED_LANDSCAPE, "clear_of_protected_landscape", proposal)


def _clear_of(kinds: tuple[str, ...], name: str, proposal: Proposal) -> Check:
    result = partial(_check, name, proposal, DESIGNATION_SOURCES)
    if not _designations_cover(proposal):
        return result("unknown", _not_assessed("Designations", proposal))
    on_site = [d for d in proposal.location.deterministic.designations if d.kind in kinds and d.on_site]
    if not on_site and _failed(proposal, DESIGNATION_SOURCES):
        return result("unknown", f"None found on the title ({', '.join(kinds)}), but not every layer answered.")
    if not on_site:
        return result("pass", f"None on the title ({', '.join(kinds)}).")
    covered_pct, limit = max(map(_covered_pct, on_site)), proposal.limits.max_protected_pct
    names = ", ".join(sorted({d.name or d.kind for d in on_site}))
    reason = f"{names} covers up to {covered_pct:g}% of the title (limit {limit:g}%)."
    return result("fail" if covered_pct >= limit else "warn", reason, designations=names, covered_pct=covered_pct)


def _designations_cover(proposal: Proposal) -> bool:
    in_england = proposal.location.deterministic.locality.country == "England"
    return in_england and bool(_source_urls(proposal, DESIGNATION_SOURCES))


def _covered_pct(designation: Designation) -> float:
    unmeasured_counts_as_the_whole_title = 100.0
    return unmeasured_counts_as_the_whole_title if designation.overlap_pct is None else designation.overlap_pct


# --------------------------------------------- grid --------------------------------------------- #


def substation_within_reach(proposal: Proposal) -> Check:
    result = partial(_check, "substation_within_reach", proposal, GRID_SOURCES)
    if not proposal.location.deterministic.grid.operators:
        return result("unknown", "No open grid data for this network operator's area.")
    reachable = _reachable_substations(proposal)
    if not reachable:
        return result("fail", f"No substation with published headroom within {proposal.limits.max_substation_km:g} km.")
    nearest = reachable[0]
    reason = f"{nearest.name} ({nearest.operator}, {nearest.voltage_kv or '?'} kV) is {nearest.distance_km} km away."
    return result("pass", reason, substation=nearest.name, distance_km=nearest.distance_km)


def grid_headroom(proposal: Proposal) -> Check:
    result = partial(_check, "grid_headroom", proposal, GRID_SOURCES)
    reachable = _reachable_substations(proposal)
    if not reachable:
        return result("unknown", "No substation with published headroom in reach.")
    best = max(reachable, key=_two_way_headroom_mw)
    headroom_mw = _two_way_headroom_mw(best)
    outcome = _lower_is_worse(headroom_mw, fail_below=proposal.limits.min_headroom_mw, warn_below=proposal.battery_mw)
    reason = (
        f"Best two-way headroom in reach: {headroom_mw:g} MW at {best.name}, for a {proposal.battery_mw:g} MW battery."
    )
    return result(outcome, reason, substation=best.name, headroom_mw=headroom_mw)


def clear_of_overhead_lines(proposal: Proposal) -> Check:
    result = partial(_check, "clear_of_overhead_lines", proposal, GRID_SOURCES)
    if not proposal.location.deterministic.grid.operators:
        return result("unknown", "No open grid data for this network operator's area.")
    crossing = [line for line in proposal.location.deterministic.grid.lines if line.crosses_site]
    if not crossing:
        return result("pass", "No overhead line of 22 kV or above crosses the title.")
    voltages = ", ".join(sorted({f"{line.voltage_kv:g} kV" for line in crossing if line.voltage_kv}))
    reason = f"{len(crossing)} overhead line section(s) cross the title ({voltages}): keep the safety clearance."
    return result("warn", reason, lines_crossing=len(crossing))


def _reachable_substations(proposal: Proposal) -> list[Substation]:
    substations = proposal.location.deterministic.grid.substations  # nearest first
    return [s for s in substations if s.headroom and s.distance_km <= proposal.limits.max_substation_km]


def _two_way_headroom_mw(substation: Substation) -> float:
    """A battery imports and exports, so the smaller headroom binds (MVA read as MW)."""
    headroom = substation.headroom
    if headroom is None:
        return 0.0
    return max(0.0, min(headroom.generation_mw or 0.0, headroom.demand or 0.0))


# -------------------------------------------- running ------------------------------------------- #

HARD_CHECKS: tuple[Callable[[Proposal], Check], ...] = (
    title_found,
    enough_area,
    buildable_slope,
    outside_flood_zone_3,
    outside_green_belt,
    avoids_best_farmland,
    clear_of_protected_ecology,
    clear_of_protected_heritage,
    clear_of_protected_landscape,
    substation_within_reach,
    grid_headroom,
    clear_of_overhead_lines,
)


# The checks that can fail, i.e. whose missing data could be hiding a blocker. The others can only warn.
CAN_BLOCK = frozenset({
    "enough_area",
    "buildable_slope",
    "outside_flood_zone_3",
    "clear_of_protected_ecology",
    "clear_of_protected_heritage",
    "clear_of_protected_landscape",
    "substation_within_reach",
})


def can_block(name: str, limits: Limits) -> bool:
    """Whether a check of this name could fail under these limits (headroom blocks only when asked to)."""
    return name in CAN_BLOCK or (name == "grid_headroom" and limits.min_headroom_mw > 0)


def assess(proposal: Proposal) -> PossibilityReport:
    checks = [check(proposal) for check in HARD_CHECKS]
    blockers = [c.reason for c in checks if c.outcome == "fail"]
    return PossibilityReport(
        possible=not blockers,
        checks=checks,
        blockers=blockers,
        caveats=[c.reason for c in checks if c.outcome == "warn"],
        unknowns=[c.name for c in checks if c.outcome == "unknown"],
    )


# -------------------------------------------- helpers ------------------------------------------- #


def _check(
    name: str, proposal: Proposal, sources: tuple[str, ...], outcome: Outcome, reason: str, **facts: Fact
) -> Check:
    rounded = {k: round(v, 2) if isinstance(v, float) else v for k, v in facts.items()}
    urls = _source_urls(proposal, sources)
    if outcome != "unknown":
        return Check(name=name, outcome=outcome, reason=reason, facts=rounded, source_urls=urls)
    # No data: say which source let us down, and point at the request we attempted.
    missed = [s for s in proposal.location.sources if s.status != "ok" and s.name.startswith(sources)]
    if missed:
        why = [f"{s.name} {s.status}" + (f" ({s.detail[:120]})" if s.detail else "") for s in missed]
        reason += f" {'; '.join(why)}."
    attempted = [s.url for s in missed if s.url.startswith("http")]
    return Check(
        name=name,
        outcome=outcome,
        reason=reason,
        facts=rounded,
        source_urls=urls + [u for u in attempted if u not in urls],
        failed_sources=_failed(proposal, sources),
        confidence=UNKNOWN_CONFIDENCE,
    )


def _not_assessed(what: str, proposal: Proposal) -> str:
    """Why a check has no data: outside the England-only sources, or (appended by `_check`) a failed source."""
    outside = proposal.location.deterministic.locality.country not in {None, "England"}
    return f"{what} not assessed (England only)." if outside else f"{what} not assessed."


def _failed(proposal: Proposal, prefixes: tuple[str, ...]) -> list[str]:
    return [s.name for s in proposal.location.sources if s.status == "failed" and s.name.startswith(prefixes)]


def _source_urls(proposal: Proposal, prefixes: tuple[str, ...]) -> list[str]:
    succeeded = (s for s in proposal.location.sources if s.status == "ok" and s.url.startswith("http"))
    return [s.url for s in succeeded if s.name.startswith(prefixes)]


def _lower_is_worse(value: float, *, fail_below: float, warn_below: float) -> Outcome:
    return "fail" if value < fail_below else "warn" if value < warn_below else "pass"


def _higher_is_worse(value: float, *, warn_above: float, fail_above: float) -> Outcome:
    return "fail" if value > fail_above else "warn" if value > warn_above else "pass"
