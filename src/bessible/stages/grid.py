"""Grid connection assessment stage: National Grid transmission impact, Gate 2 queue, and timescales."""

from __future__ import annotations

import asyncio
import os

from pydantic import HttpUrl
from temporalio import activity

from bessible.models import Artifact, DataGap, GridOutput, NodeInput
from bessible.ukpn.snapshot import GSP_DATASET_ID, GSP_DATASET_URL, get_snapshot
from bessible.ukpn.timescales import gsp_queue, timescales

_demo_failed_runs: set[str] = set()
CONFIDENCE = 0.88
MODEL_USED = "ukpn-snapshot"


async def grid_connection(inp: NodeInput) -> GridOutput:
    """Assess National Grid transmission impact and Gate 2 connection timescales."""
    await asyncio.sleep(0)

    # Demo failure injection to demonstrate Temporal activity retries live
    if os.environ.get("BESSIBLE_DEMO_FAIL_ONCE") == "1":
        should_fail = False
        try:
            attempt = activity.info().attempt
            if attempt == 1:
                should_fail = True
        except RuntimeError:
            if inp.run_id not in _demo_failed_runs:
                _demo_failed_runs.add(inp.run_id)
                should_fail = True

        if should_fail:
            msg = "Simulated transient network failure (BESSIBLE_DEMO_FAIL_ONCE=1)"
            raise RuntimeError(msg)

    snapshot = await asyncio.to_thread(get_snapshot)

    # Resolve parent GSP from serving substation
    sub_name = inp.capacity.substation
    gsp: str | None = None
    if sub_name:
        for s in snapshot.substations:
            if s.name == sub_name and s.gsp:
                gsp = s.gsp
                break
        if not gsp:
            for g in snapshot.grid_substations:
                if g.name == sub_name and g.gsp:
                    gsp = g.gsp
                    break

    queue = gsp_queue(gsp, snapshot) if gsp else None
    times = timescales(gsp, snapshot) if gsp else None

    artifacts: list[Artifact] = []

    if queue is not None:
        queue_pos = queue.next_position
        artifacts.append(
            Artifact(
                id=f"grid-queue-{inp.run_id[:8]}",
                stage="grid",
                claim=(
                    f"Parent GSP {gsp} has {queue.projects} queued projects ({queue.total_mw:.1f} MW); "
                    f"new application estimated position {queue_pos} "
                    f"[{GSP_DATASET_ID}, snapshot {snapshot.fetched_at.isoformat()}]"
                ),
                source_url=HttpUrl(GSP_DATASET_URL),
                confidence=CONFIDENCE,
                model_used=MODEL_USED,
            )
        )
    else:
        queue_pos = None
        artifacts.append(
            Artifact(
                id=f"grid-queue-{inp.run_id[:8]}",
                stage="grid",
                claim=(
                    f"Parent GSP queue data unavailable for {sub_name or 'serving substation'} "
                    f"[{GSP_DATASET_ID}, snapshot {snapshot.fetched_at.isoformat()}]"
                ),
                source_url=HttpUrl(GSP_DATASET_URL),
                confidence=0.5,
                model_used=MODEL_USED,
            )
        )

    if times is not None:
        median_months = round(times.median_months)
        conf_note = "low confidence" if times.low_confidence else "standard confidence"
        artifacts.append(
            Artifact(
                id=f"grid-timescale-{inp.run_id[:8]}",
                stage="grid",
                claim=(
                    f"Indicative connection timescale at {gsp}: median {times.median_months:g} months "
                    f"(interquartile range {times.p25_months:g}-{times.p75_months:g} months across {times.records} records, {conf_note}) "
                    f"[{GSP_DATASET_ID}, snapshot {snapshot.fetched_at.isoformat()}]"
                ),
                source_url=HttpUrl(GSP_DATASET_URL),
                confidence=0.65 if times.low_confidence else CONFIDENCE,
                model_used=MODEL_USED,
            )
        )
    else:
        median_months = None
        artifacts.append(
            Artifact(
                id=f"grid-timescale-{inp.run_id[:8]}",
                stage="grid",
                claim=(
                    f"Indicative connection timescale unavailable for {sub_name or 'serving substation'} "
                    f"[{GSP_DATASET_ID}, snapshot {snapshot.fetched_at.isoformat()}]"
                ),
                source_url=HttpUrl(GSP_DATASET_URL),
                confidence=0.5,
                model_used=MODEL_USED,
            )
        )

    where = sub_name or "the serving substation"
    gaps = [
        DataGap(stage="grid", what=what, reason=f"No grid supply point data for {where} in the UKPN snapshot.")
        for what, missing in (("gate2_queue_position", queue is None), ("connection_timescale", times is None))
        if missing
    ]
    return GridOutput(
        gate2_queue_position=queue_pos,
        indicative_connection_months=median_months,
        artifacts=artifacts,
        gaps=gaps,
    )
