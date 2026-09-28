"""End-to-end integration test of AssessmentWorkflow with Suitability Engine."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError, WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from bessible import stages
from bessible.activities import ALL_ACTIVITIES
from bessible.models import (
    AssessmentRequest,
    AssessmentResult,
    DataGap,
    LocationInput,
    LocationOutput,
    MarketOutput,
    NodeInput,
    SiteDecision,
    SiteLandOutput,
)
from bessible.workflow import TASK_QUEUE, AssessmentWorkflow

if TYPE_CHECKING:
    from bessible.models import EncryptedCredentials
    from tests.conftest import FakeGemini


@pytest.mark.anyio
async def test_workflow_end_to_end_suitability(fake_gemini: FakeGemini, run_credentials: EncryptedCredentials):
    """Verify full AssessmentWorkflow executes with suitability engine and human confirmation."""
    async with (
        await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[AssessmentWorkflow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        handle = await env.client.start_workflow(
            AssessmentWorkflow.run,
            AssessmentRequest(postcode="RH4 1AD", budget_gbp=10_000_000.0, credentials=run_credentials),
            id=f"test-wf-{uuid.uuid4().hex[:8]}",
            task_queue=TASK_QUEUE,
        )

        # Wait until awaiting_confirmation
        st = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            st = await handle.query(AssessmentWorkflow.status)
            if st.status == "awaiting_confirmation":
                break
        assert st is not None
        assert st.status == "awaiting_confirmation"

        # Human confirmation update
        footprint = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0], [0.0, 0.0]]],
        }
        await handle.execute_update(
            AssessmentWorkflow.decide_site,
            SiteDecision(confirmed=True, capacity_mw=8.0, footprint_geojson=footprint),
        )

        # Await workflow completion
        result = await handle.result()
        assert result.status == "completed"
        assert result.run_id == handle.id
        assert result.postcode == "RH4 1AD"
        assert result.report is not None
        assert result.report.verdict in ("go", "maybe", "no_go")
        assert len(result.report.findings) >= 2
        assert any("Reserved area" in f.text for f in result.report.findings)
        assert result.financial is not None
        assert len(result.financial.cases) == 3
        assert result.financial.recommended_h in (2, 4, 8)
        assert len(result.artifacts) >= 10
        assert any(a.file_path == "footprint.json" for a in result.artifacts)
        assert (Path(result.run_dir) / "footprint.json").exists()


@pytest.mark.anyio
async def test_workflow_end_to_end_80mw_grid_level(fake_gemini: FakeGemini, run_credentials: EncryptedCredentials):
    """Verify 80 MW request uses 132 kV grid substation and completes full workflow."""
    async with (
        await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[AssessmentWorkflow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        handle = await env.client.start_workflow(
            AssessmentWorkflow.run,
            AssessmentRequest(
                postcode="RH4 1AD", battery_mw=80.0, budget_gbp=50_000_000.0, credentials=run_credentials
            ),
            id=f"test-wf-80mw-{uuid.uuid4().hex[:8]}",
            task_queue=TASK_QUEUE,
        )

        st = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            st = await handle.query(AssessmentWorkflow.status)
            if st.status == "awaiting_confirmation":
                break
        assert st is not None
        assert st.status == "awaiting_confirmation"
        assert st.capacity is not None
        assert st.capacity.connection_voltage_kv == 132.0
        assert st.capacity.substation == "Leatherhead 132kV"
        assert st.capacity.firm_mw == 85.0

        await handle.execute_update(
            AssessmentWorkflow.decide_site,
            SiteDecision(confirmed=True, capacity_mw=80.0),
        )

        result = await handle.result()
        assert result.status == "completed"
        assert result.report is not None
        report_md = Path(f"{result.run_dir}/report.md").read_text(encoding="utf-8")
        assert "132 kV" in report_md
        assert any("grid-and-primary-sites" in a.claim for a in result.artifacts)
        assert any("132 kV connection" in a.claim for a in result.artifacts)


@pytest.mark.anyio
async def test_workflow_early_rejection_stops_before_title(
    fake_gemini: FakeGemini, run_credentials: EncryptedCredentials
):
    """A rejection sent while the run is still running ends it as rejected, and a second decision is refused."""
    async with (
        await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env,
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[AssessmentWorkflow],
            activities=ALL_ACTIVITIES,
        ),
    ):
        handle = await env.client.start_workflow(
            AssessmentWorkflow.run,
            AssessmentRequest(postcode="RH4 1AD", budget_gbp=10_000_000.0, credentials=run_credentials),
            id=f"test-wf-{uuid.uuid4().hex[:8]}",
            task_queue=TASK_QUEUE,
        )

        await handle.execute_update(AssessmentWorkflow.decide_site, SiteDecision(confirmed=False))
        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(AssessmentWorkflow.decide_site, SiteDecision(confirmed=True))

        result = await handle.result()
        assert result.status == "rejected"
        st = await handle.query(AssessmentWorkflow.status)
        assert st.boundary is None  # the title stage never ran


@pytest.mark.anyio
async def test_failed_stage_reports_failed_status_with_reason(run_credentials: EncryptedCredentials):
    """A run whose location stage fails reports "failed" and the reason, not "running" at its last stage."""

    @activity.defn(name="resolve_location")
    async def no_location(_inp: LocationInput) -> LocationOutput:
        msg = "Found address 'Land at Welland' but no valid UK postcode or coordinates."
        raise ApplicationError(msg, type="LocationNotFound", non_retryable=True)

    acts = [no_location if a.__name__ == "resolve_location" else a for a in ALL_ACTIVITIES]
    async with (
        await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env,
        Worker(env.client, task_queue=TASK_QUEUE, workflows=[AssessmentWorkflow], activities=acts),
    ):
        handle = await env.client.start_workflow(
            AssessmentWorkflow.run,
            AssessmentRequest(link="https://example.com/listing", credentials=run_credentials),
            id=f"test-wf-{uuid.uuid4().hex[:8]}",
            task_queue=TASK_QUEUE,
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()

        st = await handle.query(AssessmentWorkflow.status)
        assert st.status == "failed"
        assert st.stages == []
        assert st.message is not None
        assert "no valid UK postcode" in st.message


@pytest.mark.anyio
async def test_retry_reruns_only_the_failed_stage(fake_gemini: FakeGemini, run_credentials: EncryptedCredentials):
    """A completed run stays open; a retry re-runs the stage with a retryable gap, then financial onwards."""
    calls = {"site_land": 0, "market": 0}

    @activity.defn(name="site_land")
    async def flaky_site_land(_inp: NodeInput) -> SiteLandOutput:
        calls["site_land"] += 1
        if calls["site_land"] == 1:
            gap = DataGap(
                stage="site_land",
                what="outside_flood_zone_3",
                reason="EA: flood zones failed.",
                sources=["EA: flood zones"],
                retryable=True,
                could_block=True,
            )
            return SiteLandOutput(land_use="Agricultural", gaps=[gap])
        return SiteLandOutput(land_use="Agricultural")

    @activity.defn(name="market_revenue")
    async def counted_market(inp: NodeInput) -> MarketOutput:
        calls["market"] += 1
        return await stages.market.market_revenue(inp)

    swapped = {"site_land": flaky_site_land, "market_revenue": counted_market}
    acts = [swapped.get(a.__name__, a) for a in ALL_ACTIVITIES]
    async with (
        await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env,
        Worker(env.client, task_queue=TASK_QUEUE, workflows=[AssessmentWorkflow], activities=acts),
    ):
        req = AssessmentRequest(postcode="RH4 1AD", credentials=run_credentials, retry_window_s=20)
        handle = await env.client.start_workflow(
            AssessmentWorkflow.run, req, id=f"test-wf-{uuid.uuid4().hex[:8]}", task_queue=TASK_QUEUE
        )

        async def until(status: str, retries_left: int | None = None) -> AssessmentResult | None:
            for _ in range(200):
                await asyncio.sleep(0.1)
                st = await handle.query(AssessmentWorkflow.status)
                res = await handle.query(AssessmentWorkflow.result)
                if st.status == status and (retries_left is None or (res and res.retries_left == retries_left)):
                    return res
            pytest.fail(f"run never reached {status}")

        await until("awaiting_confirmation")
        await handle.execute_update(AssessmentWorkflow.decide_site, SiteDecision(confirmed=True, capacity_mw=8.0))
        first = await until("completed", retries_left=3)
        assert first.report.verdict in ("maybe", "no_go")
        assert "site_land" in {g.stage for g in first.gaps if g.retryable}

        with pytest.raises(WorkflowUpdateFailedError):  # grid reads a snapshot: nothing a retry could fill
            await handle.execute_update(AssessmentWorkflow.retry_stages, ["grid"])

        await handle.execute_update(AssessmentWorkflow.retry_stages, ["site_land"])
        second = await until("completed", retries_left=2)
        assert all(g.stage != "site_land" for g in second.gaps)
        assert calls == {"site_land": 2, "market": 1}  # market's first output was kept

        with pytest.raises(WorkflowUpdateFailedError):  # site_land has no gap left to fill
            await handle.execute_update(AssessmentWorkflow.retry_stages, ["site_land"])

        final = await handle.result()  # the window closes with no further retry
        assert final.retries_left == 2
        assert final.report is not None


@pytest.mark.anyio
async def test_workflow_rejects_unknown_title_ids_then_confirms_clicked_polygon(
    fake_gemini: FakeGemini, run_credentials: EncryptedCredentials
):
    """The decision's `title_ids` must be candidates; a valid click becomes the confirmed site boundary."""
    async with (
        await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env,
        Worker(env.client, task_queue=TASK_QUEUE, workflows=[AssessmentWorkflow], activities=ALL_ACTIVITIES),
    ):
        handle = await env.client.start_workflow(
            AssessmentWorkflow.run,
            AssessmentRequest(postcode="RH4 1AD", credentials=run_credentials),
            id=f"test-wf-titles-{uuid.uuid4().hex[:8]}",
            task_queue=TASK_QUEUE,
        )
        st = None
        for _ in range(50):
            await asyncio.sleep(0.1)
            st = await handle.query(AssessmentWorkflow.status)
            if st.status == "awaiting_confirmation":
                break
        assert st is not None
        assert st.boundary is not None
        assert st.boundary.candidates  # the saved planning.data search (conftest)
        chosen = st.boundary.candidates[0].inspire_id

        with pytest.raises(WorkflowUpdateFailedError):
            await handle.execute_update(
                AssessmentWorkflow.decide_site, SiteDecision(confirmed=True, capacity_mw=8.0, title_ids=["no-such-id"])
            )
        assert (await handle.query(AssessmentWorkflow.status)).status == "awaiting_confirmation"

        await handle.execute_update(
            AssessmentWorkflow.decide_site, SiteDecision(confirmed=True, capacity_mw=8.0, title_ids=[chosen])
        )
        result = await handle.result()
        assert result.status == "completed"
        assert result.site is not None
        assert result.site.boundary.inspire_ids == [chosen]
        assert any(a.claim.startswith("User confirmed the site as clicked polygons") for a in result.artifacts)
