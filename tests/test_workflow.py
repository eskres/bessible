"""End-to-end integration test of AssessmentWorkflow with Suitability Engine."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from temporalio.client import WorkflowUpdateFailedError
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from bessible.activities import ALL_ACTIVITIES
from bessible.models import AssessmentRequest, SiteDecision
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
