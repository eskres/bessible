"""Public demo replay routes for Bessible.

Replays recorded runs keyless and offline without Temporal, LLM calls, or Firebase auth.
Demo run IDs are prefixed with `demo-` and never resolve to real runs.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import time
from typing import TYPE_CHECKING, Any
import uuid

from fastapi import APIRouter, HTTPException, Header, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from bessible.config import settings
from bessible.models import (
    AssessmentRequest,
    AssessmentResult,
    RunStatus,
    SiteDecision,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/demo/runs", tags=["demo"])

PRE_GATE_STAGES = {"location", "capacity", "title"}
EARLY_END_STATUSES = ("out_of_area", "not_viable")


class StartDemoRequest(BaseModel):
    """Optional configuration when initiating a demo replay run."""

    slug: str = Field(default="dorking", pattern=r"^[a-z0-9-]+$")  # a folder under data/demo
    pacing_multiplier: float = Field(default=1.0, ge=0.0, le=10.0)


class DemoReplaySession:
    """Manages the lifecycle and event streaming for an active demo replay."""

    def __init__(
        self,
        run_id: str,
        slug: str,
        request: AssessmentRequest,
        events: list[dict[str, Any]],
        snapshots: list[dict[str, Any]],
        decision: SiteDecision,
        result: AssessmentResult,
        pacing_multiplier: float = 1.0,
    ) -> None:
        self.run_id = run_id
        self.slug = slug
        self.request = request
        self.raw_events = events
        self.snapshots = snapshots
        self.recorded_decision = decision
        self.result = result
        self.pacing_multiplier = pacing_multiplier

        self.created_at = time.time()
        self.confirmed_event = asyncio.Event()
        self.new_event_cond = asyncio.Condition()

        self.emitted_events: list[dict[str, Any]] = []
        self.is_completed = False
        self.is_awaiting_confirmation = False

        # Initialize current status from first snapshot or default running
        first_status = self._find_snapshot_status(stage="location") or RunStatus(
            status="running", stages=["location"]
        )
        self.current_status: RunStatus = first_status.model_copy(update={"run_id": run_id})

        self._task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _find_snapshot_status(self, *, stage: str | None = None, status_str: str | None = None) -> RunStatus | None:
        """Find matching snapshot for a given stage or status."""
        for item in self.snapshots:
            snap_data = item.get("status", item)
            st_val = snap_data.get("status")
            stages_val = snap_data.get("stages", [])

            if status_str and st_val == status_str:
                return RunStatus.model_validate(snap_data)
            if stage and (stage in stages_val or item.get("stage") == stage):
                return RunStatus.model_validate(snap_data)

        return None

    def start(self) -> None:
        """Start background replay task."""
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run_replay())

    def get_status(self) -> RunStatus:
        """Get the current live status snapshot for this replay."""
        return self.current_status.model_copy(update={"run_id": self.run_id})

    def submit_decision(self, _decision: SiteDecision) -> None:
        """Accept visitor decision and unpause the gate."""
        self.is_awaiting_confirmation = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self.confirmed_event.set)
        else:
            self.confirmed_event.set()

    async def _emit_event(self, ev_data: dict[str, Any]) -> None:
        """Emit an event to the session's stream and notify subscribers."""
        ev = {
            "id": int(ev_data.get("id", len(self.emitted_events) + 1)),
            "t": datetime.now(UTC).isoformat(),
            "stage": ev_data.get("stage", ""),
            "msg": ev_data.get("msg", ""),
        }
        async with self.new_event_cond:
            self.emitted_events.append(ev)
            self.new_event_cond.notify_all()

    async def _run_replay(self) -> None:
        """Drive replay pacing, confirmation pause, and status progression."""
        try:
            # Separate events into pre-gate and post-gate
            pre_gate: list[dict[str, Any]] = []
            post_gate: list[dict[str, Any]] = []
            reached_post = False
            for ev in self.raw_events:
                stg = ev.get("stage", "")
                if not reached_post and stg in PRE_GATE_STAGES:
                    pre_gate.append(ev)
                else:
                    reached_post = True
                    post_gate.append(ev)

            # 1. Play pre-gate events
            prev_offset = 0.0
            for ev in pre_gate:
                curr_offset = float(ev.get("offset_s", 0.0))
                dt = max(0.0, curr_offset - prev_offset) * self.pacing_multiplier
                if dt > 0:
                    await asyncio.sleep(min(dt, 2.5))
                prev_offset = curr_offset

                # Update stage status
                stg = ev.get("stage", "")
                st = self._find_snapshot_status(stage=stg)
                if st:
                    self.current_status = st.model_copy(update={"run_id": self.run_id})

                await self._emit_event(ev)

            # A run that stopped before the gate (out of area, not viable) ends here with its recorded status.
            early_end = next(
                (st for s in EARLY_END_STATUSES if (st := self._find_snapshot_status(status_str=s)) is not None),
                None,
            )
            if early_end is not None:
                self.current_status = early_end.model_copy(update={"run_id": self.run_id})
                self.is_completed = True
                async with self.new_event_cond:
                    self.new_event_cond.notify_all()
                return

            # 2. Pause at site-confirmation gate
            gate_status = self._find_snapshot_status(status_str="awaiting_confirmation")
            if gate_status:
                self.current_status = gate_status.model_copy(update={"run_id": self.run_id})
            else:
                self.current_status = self.current_status.model_copy(
                    update={"status": "awaiting_confirmation", "stages": []}
                )

            self.is_awaiting_confirmation = True
            await self.confirmed_event.wait()

            # 3. Decision received -> resume post-gate events
            first_post_stage = post_gate[0].get("stage", "grid") if post_gate else "grid"
            post_status = self._find_snapshot_status(stage=first_post_stage)
            if post_status:
                self.current_status = post_status.model_copy(update={"run_id": self.run_id})
            else:
                self.current_status = self.current_status.model_copy(
                    update={"status": "running", "stages": [first_post_stage]}
                )

            prev_offset = float(post_gate[0].get("offset_s", 0.0)) if post_gate else 0.0
            for ev in post_gate:
                curr_offset = float(ev.get("offset_s", 0.0))
                dt = max(0.0, curr_offset - prev_offset) * self.pacing_multiplier
                if dt > 0:
                    await asyncio.sleep(min(dt, 2.5))
                prev_offset = curr_offset

                # Update stage status
                stg = ev.get("stage", "")
                st = self._find_snapshot_status(stage=stg)
                if st:
                    self.current_status = st.model_copy(update={"run_id": self.run_id})

                await self._emit_event(ev)

            # 4. Mark completed
            completed_status = self._find_snapshot_status(status_str="completed")
            if completed_status:
                self.current_status = completed_status.model_copy(update={"run_id": self.run_id})
            else:
                self.current_status = self.current_status.model_copy(
                    update={"status": "completed", "stages": []}
                )

            self.is_completed = True
            async with self.new_event_cond:
                self.new_event_cond.notify_all()

        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Error in demo replay for %s", self.run_id)
            self.current_status = self.current_status.model_copy(
                update={"status": "failed", "stages": []}
            )
            self.is_completed = True
            async with self.new_event_cond:
                self.new_event_cond.notify_all()

    async def stream_events(self, after_id: int = 0) -> AsyncGenerator[str, None]:
        """Stream SSE events with reconnect support and keep-alives during gate pause."""
        last_sent_id = after_id

        while True:
            # Send any events that are already emitted and newer than last_sent_id
            events_to_send: list[dict[str, Any]] = []
            async with self.new_event_cond:
                for ev in self.emitted_events:
                    if ev["id"] > last_sent_id:
                        events_to_send.append(ev)

            for ev in events_to_send:
                yield f"id: {ev['id']}\ndata: {json.dumps(ev)}\n\n"
                last_sent_id = ev["id"]

            if self.is_completed and last_sent_id >= (self.emitted_events[-1]["id"] if self.emitted_events else 0):
                break

            # Wait for next event or send keep-alive
            try:
                async with self.new_event_cond:
                    await asyncio.wait_for(self.new_event_cond.wait(), timeout=3.0)
            except TimeoutError:
                yield ": keep-alive\n\n"


_SESSIONS: dict[str, DemoReplaySession] = {}


def _cleanup_old_sessions() -> None:
    """Evict demo sessions older than 2 hours to avoid memory growth."""
    now = time.time()
    cutoff = now - 7200
    expired = [rid for rid, s in _SESSIONS.items() if s.created_at < cutoff]
    for rid in expired:
        _SESSIONS.pop(rid, None)


def _resolve_demo_dir(slug: str) -> Path:
    """Find the directory containing the demo artifacts for `slug`."""
    candidates = [
        settings.data_dir / "demo" / slug,
        Path("data/demo") / slug,
        settings.data_dir.parent / "data" / "demo" / slug,
    ]
    for p in candidates:
        if p.exists() and (p / "result.json").exists():
            return p

    # Fall back to any available slug directory if requested slug doesn't exist
    demo_root = settings.data_dir / "demo"
    if demo_root.exists():
        for sub in demo_root.iterdir():
            if sub.is_dir() and (sub / "result.json").exists():
                return sub

    raise HTTPException(status_code=503, detail="No recorded demo runs found")


@router.post("", status_code=200)
async def start_demo_run(req: StartDemoRequest | None = None) -> dict[str, str]:
    """Start a keyless replay run from recorded data and return its demo run id."""
    _cleanup_old_sessions()

    slug = req.slug if req else "dorking"
    pacing = req.pacing_multiplier if req else 1.0

    demo_dir = _resolve_demo_dir(slug)

    req_file = demo_dir / "request.json"
    events_file = demo_dir / "events.jsonl"
    statuses_file = demo_dir / "statuses.json"
    decision_file = demo_dir / "decision.json"
    result_file = demo_dir / "result.json"

    if not (req_file.exists() and events_file.exists() and result_file.exists()):
        raise HTTPException(status_code=503, detail=f"Incomplete demo recording in {demo_dir.name}")

    req_data = AssessmentRequest.model_validate_json(req_file.read_text(encoding="utf-8"))

    events: list[dict[str, Any]] = []
    for line in events_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            events.append(json.loads(line))

    statuses: list[dict[str, Any]] = []
    if statuses_file.exists():
        statuses = json.loads(statuses_file.read_text(encoding="utf-8"))

    decision = (
        SiteDecision.model_validate_json(decision_file.read_text(encoding="utf-8"))
        if decision_file.exists()
        else SiteDecision(confirmed=True)
    )

    run_id = f"demo-{uuid.uuid4().hex[:12]}"
    result = AssessmentResult.model_validate_json(result_file.read_text(encoding="utf-8"))
    result = result.model_copy(update={"run_id": run_id, "postcode": result.postcode or req_data.postcode})
    session = DemoReplaySession(
        run_id=run_id,
        slug=slug,
        request=req_data,
        events=events,
        snapshots=statuses,
        decision=decision,
        result=result,
        pacing_multiplier=pacing,
    )
    _SESSIONS[run_id] = session
    session.start()

    return {"run_id": run_id}


@router.get("/{run_id}/status", response_model=RunStatus)
async def get_demo_run_status(run_id: str) -> RunStatus:
    """Get the current progress status for an active demo replay."""
    if not run_id.startswith("demo-") or run_id not in _SESSIONS:
        raise HTTPException(status_code=404, detail=f"Demo run '{run_id}' not found")

    session = _SESSIONS[run_id]
    return session.get_status()


@router.get("/{run_id}/events")
async def stream_demo_run_events(
    run_id: str,
    last_event_id_header: str | None = Header(None, alias="Last-Event-ID"),
    last_event_id_query: int | None = Query(None, alias="last_event_id"),
) -> StreamingResponse:
    """Stream Server-Sent Events with recorded pacing for a demo replay."""
    if not run_id.startswith("demo-") or run_id not in _SESSIONS:
        raise HTTPException(status_code=404, detail=f"Demo run '{run_id}' not found")

    start_id = 0
    if last_event_id_header is not None and last_event_id_header.isdigit():
        start_id = int(last_event_id_header)
    elif last_event_id_query is not None:
        start_id = last_event_id_query

    session = _SESSIONS[run_id]
    return StreamingResponse(
        session.stream_events(start_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{run_id}/decision", status_code=204)
async def submit_demo_site_decision(run_id: str, decision: SiteDecision) -> Response:
    """Submit a confirmation decision for a paused demo replay."""
    if not run_id.startswith("demo-") or run_id not in _SESSIONS:
        raise HTTPException(status_code=404, detail=f"Demo run '{run_id}' not found")

    session = _SESSIONS[run_id]
    if not session.is_awaiting_confirmation:
        raise HTTPException(
            status_code=409,
            detail=f"Run is not awaiting confirmation (current status: {session.get_status().status})",
        )

    session.submit_decision(decision)
    return Response(status_code=204)


@router.get("/{run_id}/result", response_model=AssessmentResult)
async def get_demo_run_result(run_id: str) -> AssessmentResult | JSONResponse:
    """Get the recorded assessment result if completed, or 409 if replay is still running."""
    if not run_id.startswith("demo-") or run_id not in _SESSIONS:
        raise HTTPException(status_code=404, detail=f"Demo run '{run_id}' not found")

    session = _SESSIONS[run_id]
    if not session.is_completed:
        current_status = session.get_status().status
        return JSONResponse(
            status_code=409,
            content={
                "detail": f"Run is not finished yet (current status: {current_status})",
                "status": current_status,
            },
        )

    return session.result
