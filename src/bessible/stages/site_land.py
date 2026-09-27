"""Site and land constraints assessment stage using LocationData and possibility checks."""

from __future__ import annotations

from bessible.location import Coordinates, collate
from bessible.models import NodeInput, SiteLandOutput
from bessible.possibility import Proposal, assess
from bessible.possibility.pipeline import site_land_output


async def site_land(inp: NodeInput) -> SiteLandOutput:
    """Assess land classification, topography, and environmental designations using real LocationData.

    A data source that fails does not raise: `collate` records it and the checks that needed it come back
    "unknown", listed in `not_assessed`. Anything that does raise is a bug, left to the activity's retries.
    """
    coords = Coordinates(lat=inp.site.position.lat, lon=inp.site.position.lon)
    location = await collate(coords)
    proposal = Proposal(location=location, battery_mw=inp.site.capacity_mw)
    return site_land_output(proposal, assess(proposal), inp.run_id)
