"""FastAPI route handlers for assessment runs."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from temporalio.client import WorkflowExecutionStatus, WorkflowUpdateFailedError

from bessible.api.ownership import OWNER_MEMO, assert_owner
from bessible.api.temporal import get_temporal_client, handle_temporal_error
from bessible.auth import User, current_user
from bessible.keystore import KeyStore, KeyStoreError, get_key_store
from bessible.models import AssessmentRequest, AssessmentResult, RunStatus, SiteDecision, Stage
from bessible.workflow import TASK_QUEUE, AssessmentWorkflow

if TYPE_CHECKING:
    from temporalio.client import WorkflowHandle

router = APIRouter(prefix="/runs", tags=["runs"])

ACTIVE_STATUSES = {"running", "awaiting_confirmation"}
FLOOR_MW = 5.0
RETRY_WINDOW_S = 30 * 60  # a completed run takes retries for this long after its last report


@router.post("", status_code=200)
async def start_run(
    req: AssessmentRequest,
    user: Annotated[User, Depends(current_user)],
    store: Annotated[KeyStore, Depends(get_key_store)],
) -> dict[str, str]:
    """Start an assessment workflow run owned by the caller and return its id.

    The run carries the caller's stored Google key as ciphertext only. There is no server-key fallback.
    """
    try:
        creds = await asyncio.to_thread(store.checked_credentials, user.uid)
    except KeyStoreError as exc:
        raise HTTPException(status_code=409, detail="stored_key_unreadable") from exc
    if creds is None:
        raise HTTPException(status_code=401, detail="missing_google_key")

    run_id = f"bessible-{uuid.uuid4()}"
    try:
        client = await get_temporal_client()
        await client.start_workflow(
            AssessmentWorkflow.run,
            # always ours: client-supplied values are overwritten
            req.model_copy(update={"credentials": creds, "retry_window_s": RETRY_WINDOW_S}),
            id=run_id,
            task_queue=TASK_QUEUE,
            memo={OWNER_MEMO: user.uid},
        )
    except Exception as exc:
        handle_temporal_error(exc, run_id)
        raise
    else:
        return {"run_id": run_id}


@router.get("/{run_id}/status", response_model=RunStatus)
async def get_run_status(run_id: str, user: Annotated[User, Depends(current_user)]) -> RunStatus:
    """Get the current execution status and stage state for a run."""
    if run_id.startswith("demo-"):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    try:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(run_id, result_type=AssessmentResult)
        await assert_owner(handle, run_id, user)
        status: RunStatus = await handle.query(AssessmentWorkflow.status)
    except Exception as exc:
        handle_temporal_error(exc, run_id)
        raise
    else:
        return status.model_copy(update={"run_id": run_id})


@router.post("/{run_id}/decision", status_code=204)
async def submit_site_decision(
    run_id: str, decision: SiteDecision, user: Annotated[User, Depends(current_user)]
) -> Response:
    """Submit a human-in-the-loop site confirmation or rejection decision."""
    if run_id.startswith("demo-"):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    try:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(run_id, result_type=AssessmentResult)
        await assert_owner(handle, run_id, user)
        status: RunStatus = await handle.query(AssessmentWorkflow.status)
    except Exception as exc:
        handle_temporal_error(exc, run_id)
        raise

    # A rejection may arrive early, while the run is still on its way to the confirmation step.
    allowed = ("awaiting_confirmation",) if decision.confirmed else ("running", "awaiting_confirmation")
    if status.status not in allowed:
        raise HTTPException(
            status_code=409,
            detail=f"Run is not awaiting confirmation (current status: {status.status})",
        )

    if decision.confirmed:
        cap = status.capacity
        if cap is None:
            raise HTTPException(
                status_code=400,
                detail="Cannot confirm without a capacity proposal",
            )

        is_flex = bool(decision.flexible_connection)
        allowed_min = FLOOR_MW
        allowed_max = cap.ceiling_mw if is_flex else cap.firm_mw
        chosen_mw = decision.capacity_mw if decision.capacity_mw is not None else cap.recommended_mw

        if chosen_mw < allowed_min or chosen_mw > allowed_max:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": (
                        f"Capacity {chosen_mw:g} MW is out of allowed range [{allowed_min:g}, {allowed_max:g}] MW"
                    ),
                    "allowed_min": allowed_min,
                    "allowed_max": allowed_max,
                },
            )

    try:
        await handle.execute_update(AssessmentWorkflow.decide_site, decision)
    except WorkflowUpdateFailedError as exc:
        # The workflow validator refused it, e.g. the site was already decided.
        raise HTTPException(status_code=409, detail=str(exc.cause or exc)) from exc
    except Exception as exc:
        handle_temporal_error(exc, run_id)
        raise

    return Response(status_code=204)


async def _running_response(handle: WorkflowHandle[AssessmentWorkflow, AssessmentResult]) -> Response:
    status: RunStatus = await handle.query(AssessmentWorkflow.status)
    return JSONResponse(
        status_code=409,
        content={
            "detail": f"Run is not finished yet (current status: {status.status})",
            "status": status.status,
        },
    )


@router.get("/{run_id}/result", response_model=AssessmentResult)
async def get_run_result(run_id: str, user: Annotated[User, Depends(current_user)]) -> AssessmentResult | Response:
    """Get the final assessment result if completed, or 409 if still in progress."""
    if run_id.startswith("demo-"):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    try:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(run_id, result_type=AssessmentResult)
        desc = await assert_owner(handle, run_id, user)
        if desc.status == WorkflowExecutionStatus.RUNNING:
            return await _open_run_result(handle)
        result: AssessmentResult = await handle.result()
    except Exception as exc:
        handle_temporal_error(exc, run_id)
        raise
    else:
        return result.model_copy(update={"run_id": run_id})  # runs finished before the field existed lack it


async def _open_run_result(handle: WorkflowHandle[AssessmentWorkflow, AssessmentResult]) -> AssessmentResult | Response:
    """A completed run stays open for retries: serve its latest result from the workflow's state."""
    status: RunStatus = await handle.query(AssessmentWorkflow.status)
    latest: AssessmentResult | None = await handle.query(AssessmentWorkflow.result)
    if latest is None or status.status != "completed":
        return await _running_response(handle)
    return latest


class RetryRequest(BaseModel):
    """The evidence stages to run again."""

    stages: list[Stage] = Field(min_length=1)


@router.post("/{run_id}/retry", status_code=204)
async def retry_run_stages(run_id: str, body: RetryRequest, user: Annotated[User, Depends(current_user)]) -> Response:
    """Re-run stages whose data gaps a retry may fill; poll `status` and `result` for the new report."""
    if run_id.startswith("demo-"):
        raise HTTPException(status_code=404, detail=f"Run '{run_id}' not found")
    try:
        client = await get_temporal_client()
        handle = client.get_workflow_handle(run_id, result_type=AssessmentResult)
        desc = await assert_owner(handle, run_id, user)
    except Exception as exc:
        handle_temporal_error(exc, run_id)
        raise
    if desc.status != WorkflowExecutionStatus.RUNNING:
        raise HTTPException(status_code=409, detail="This run has closed for retries: start a new run")
    try:
        await handle.execute_update(AssessmentWorkflow.retry_stages, body.stages)
    except WorkflowUpdateFailedError as exc:
        # The workflow validator refused it: not completed yet, nothing retryable, or no retries left.
        raise HTTPException(status_code=409, detail=str(exc.cause or exc)) from exc
    except Exception as exc:
        handle_temporal_error(exc, run_id)
        raise
    return Response(status_code=204)
