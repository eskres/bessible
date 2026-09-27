"""What goes into and comes out of the possibility checks. Field descriptions double as the agents' briefing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from bessible.location import LocationData

Outcome = Literal["pass", "warn", "fail", "unknown"]
Fact = bool | int | float | str | None


class Limits(BaseModel):
    """Thresholds behind the hard checks: screening assumptions, not regulation. Change them per run."""

    acres_per_mwh_min: float = Field(default=0.05, description="Below this the battery does not fit on the title.")
    acres_per_mwh_comfortable: float = Field(default=0.075, description="At or above this it fits with room to spare.")
    max_flood_zone_3_pct: float = Field(default=80, description="Share of the title in Flood Zone 3 that blocks it.")
    max_protected_pct: float = Field(default=50, description="Share of the title under a protection that blocks it.")
    comfortable_median_slope_pct: float = Field(default=5, description="Steeper than this needs earthworks.")
    max_median_slope_pct: float = Field(default=10, description="Steeper than this is not a buildable platform.")
    max_substation_km: float = Field(default=5, description="Furthest substation worth a connection.")
    min_headroom_mw: float = Field(
        default=0,
        description="Published headroom below this blocks the site. 0 = never: no headroom means reinforcement or a "
        "curtailable connection (cost and delay, a suitability matter), not impossibility.",
    )


class Proposal(BaseModel):
    """A battery of a given size proposed on a location."""

    location: LocationData
    battery_mw: float = Field(gt=0)
    duration_h: Literal[2, 4, 8] = 4
    limits: Limits = Field(default_factory=Limits)

    @property
    def battery_mwh(self) -> float:
        """Energy capacity: power x duration."""
        return self.battery_mw * self.duration_h


class Check(BaseModel):
    """The result of one hard check."""

    name: str
    outcome: Outcome = Field(
        description="fail = cannot be built here; warn = possible with a caveat; unknown = no data."
    )
    reason: str = Field(description="One sentence a person can read, with the figures that decided it.")
    facts: dict[str, Fact] = Field(default_factory=dict, description="The measured values and limits compared.")
    source_urls: list[str] = Field(default_factory=list, description="The upstream requests the facts came from.")
    failed_sources: list[str] = Field(
        default_factory=list, description="Upstream sources this check needed that failed (a retry may fill them)."
    )
    produced_by: str = Field(default="deterministic rule", description="The rule or model that decided the outcome.")
    confidence: float = Field(default=0.95, ge=0, le=1, description="How far to trust the outcome.")


class PossibilityReport(BaseModel):
    """Every hard check on a proposal. `possible` is False as soon as one check fails."""

    possible: bool
    checks: list[Check]
    blockers: list[str] = Field(default_factory=list, description="Reasons of the failed checks.")
    caveats: list[str] = Field(default_factory=list, description="Reasons of the checks that warned.")
    unknowns: list[str] = Field(default_factory=list, description="Names of the checks that had no data.")
