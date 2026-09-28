"""Direct capacity check endpoint outside the Temporal workflow."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter
from pydantic import BaseModel, model_validator

from bessible.cable_route import with_cable_route
from bessible.models import CapacityOutput, Position
from bessible.stages.capacity import propose, propose_live
from bessible.ukpn.snapshot import get_snapshot

if TYPE_CHECKING:
    from typing import Any

router = APIRouter(tags=["capacity"])


class CapacityCheckRequest(BaseModel):
    """Payload for fast capacity check on map pin movement."""

    position: Position
    flexible: bool = False
    requested_mw: float | None = None
    battery_mw: float | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_input(cls, data: Any) -> Any:  # ruff: ignore[any-type]
        """Normalize flexible_connection alias to flexible and battery_mw to requested_mw."""
        if isinstance(data, dict):
            data = dict(data)
            if "flexible_connection" in data and "flexible" not in data:
                data["flexible"] = data["flexible_connection"]
            if "battery_mw" in data and "requested_mw" not in data:
                data["requested_mw"] = data["battery_mw"]
            elif "target_mw" in data and "requested_mw" not in data:
                data["requested_mw"] = data["target_mw"]
        return data


async def capacity_at(
    position: Position, *, flexible: bool = False, requested_mw: float | None = None, run_id: str = "check"
) -> CapacityOutput:
    """The capacity proposal and cable run at a coordinate: the UKPN snapshot, else the other operators' live data."""
    out = propose(position, get_snapshot(), run_id=run_id, flexible=flexible, requested_mw=requested_mw)
    if out.out_of_area:
        out = await propose_live(position, run_id, fallback=out)
    return with_cable_route(position, out, run_id)


@router.post("/capacity/check", response_model=CapacityOutput)
async def check_capacity(req: CapacityCheckRequest) -> CapacityOutput:
    """Run direct grid capacity proposal for a coordinate under 1 second."""
    mw = req.requested_mw if req.requested_mw is not None else req.battery_mw
    return await capacity_at(req.position, flexible=req.flexible, requested_mw=mw)
