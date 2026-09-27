"""Unit and integration tests for FastAPI HTTP server and SSE events bridge."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from temporalio.client import RPCError, RPCStatusCode, WorkflowExecutionStatus

from bessible import events
from bessible.api.app import app
from bessible.auth import User, current_user
from bessible.keystore import Keyring, KeyStore, get_key_store
from bessible.llm import setup_logfire
from bessible.models import (
    AssessmentResult,
    CapacityOutput,
    RunStatus,
)

USER = User(uid="user-a", email="a@example.com")

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """Yield a FastAPI TestClient signed in as USER, who has a stored key."""
    store = KeyStore(tmp_path / "keys.db", Keyring("k1", {"k1": b"test-master-secret"}))
    store.put(USER.uid, "AIza" + "a" * 35)
    app.dependency_overrides[current_user] = lambda: USER
    app.dependency_overrides[get_key_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_capacity_check_direct(client: TestClient) -> None:
    """POST /capacity/check returns grid capacity under 1 second without starting a workflow."""
    start_time = time.monotonic()
    # Coordinates for Dorking (within committed snapshot)
    res = client.post("/capacity/check", json={"position": [-0.33, 51.23], "flexible": False})
    elapsed = time.monotonic() - start_time

    assert res.status_code == 200
    assert elapsed < 1.0  # Under 1 second requirement
    data = res.json()
    assert data["viable"] is True
    assert data["substation"] == "Dorking Town 11kV"
    assert data["firm_mw"] > 0
    assert data["ceiling_mw"] >= data["firm_mw"]


def test_capacity_check_out_of_area(client: TestClient) -> None:
    """POST /capacity/check returns out_of_area for distant coordinates."""
    res = client.post("/capacity/check", json={"position": [-3.0, 55.0], "flexible": False})
    assert res.status_code == 200
    data = res.json()
    assert data["viable"] is False
    assert data["out_of_area"] is True


def test_areas_geojson(client: TestClient) -> None:
    """GET /data/areas.geojson returns a GeoJSON FeatureCollection."""
    res = client.get("/data/areas.geojson")
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "FeatureCollection"
    assert "features" in data


def test_inspire_endpoint(client: TestClient) -> None:
    """GET /inspire returns GeoJSON FeatureCollection."""
    res = client.get("/inspire?bbox=-0.34,51.22,-0.32,51.24")
    assert res.status_code == 200
    data = res.json()
    assert data["type"] == "FeatureCollection"


def test_start_run_validation(client: TestClient) -> None:
    """POST /runs with neither postcode nor property_url returns 422."""
    res = client.post("/runs", json={"battery_mw": 10.0})
    assert res.status_code == 422
    errors = res.json()["detail"]
    assert any("One of property_url, postcode or position must be provided" in str(e) for e in errors)


def test_temporal_unavailable_returns_503(client: TestClient) -> None:
    """Temporal connectivity failure returns HTTP 503 mentioning temporal server start-dev."""
    with patch("bessible.api.runs.get_temporal_client", side_effect=OSError("Connection refused")):
        res = client.post("/runs", json={"postcode": "RH4 1AD", "battery_mw": 10.0})
        assert res.status_code == 503
        assert "temporal server start-dev" in res.json()["detail"]

    with patch("bessible.api.runs.get_temporal_client", side_effect=OSError("Connection refused")):
        res = client.get("/runs/test-run-123/status")
        assert res.status_code == 503
        assert "temporal server start-dev" in res.json()["detail"]

    with patch("bessible.api.runs.get_temporal_client", side_effect=OSError("Connection refused")):
        res = client.get("/runs/test-run-123/result")
        assert res.status_code == 503
        assert "temporal server start-dev" in res.json()["detail"]

    with patch("bessible.api.runs.get_temporal_client", side_effect=OSError("Connection refused")):
        res = client.post("/runs/test-run-123/decision", json={"confirmed": True, "capacity_mw": 8.0})
        assert res.status_code == 503
        assert "temporal server start-dev" in res.json()["detail"]


def test_mocked_workflow_endpoints(client: TestClient) -> None:
    """Test start, status, decision, and result with a mocked Temporal client."""
    mock_client = MagicMock()
    mock_handle = MagicMock()

    mock_client.start_workflow = AsyncMock(return_value=mock_handle)
    mock_client.get_workflow_handle.return_value = mock_handle

    mock_status_awaiting = RunStatus(
        status="awaiting_confirmation",
        stages=["title"],
        capacity=CapacityOutput(
            viable=True,
            firm_mw=8.0,
            ceiling_mw=12.0,
            recommended_mw=8.0,
        ),
    )
    mock_handle.query = AsyncMock(return_value=mock_status_awaiting)

    mock_desc_running = MagicMock()
    mock_desc_running.status = WorkflowExecutionStatus.RUNNING
    mock_desc_running.memo_value = AsyncMock(return_value=USER.uid)
    mock_handle.describe = AsyncMock(return_value=mock_desc_running)

    with patch("bessible.api.runs.get_temporal_client", AsyncMock(return_value=mock_client)):
        # 1. Start run
        start_res = client.post("/runs", json={"postcode": "RH4 1AD", "battery_mw": 8.0})
        assert start_res.status_code == 200
        run_id = start_res.json()["run_id"]
        assert run_id.startswith("bessible-")

        # 2. Status
        status_res = client.get(f"/runs/{run_id}/status")
        assert status_res.status_code == 200
        assert status_res.json()["status"] == "awaiting_confirmation"
        assert status_res.json()["run_id"] == run_id

        # 3. Result while running returns 409
        result_res = client.get(f"/runs/{run_id}/result")
        assert result_res.status_code == 409
        assert result_res.json()["status"] == "awaiting_confirmation"

        # 4. Out-of-range decision returns 422 with allowed_min and allowed_max
        # Ceiling is 12 MW, firm is 8 MW; without flexible, max is 8 MW
        bad_decision_res = client.post(
            f"/runs/{run_id}/decision",
            json={"confirmed": True, "capacity_mw": 15.0, "flexible_connection": False},
        )
        assert bad_decision_res.status_code == 422
        bad_data = bad_decision_res.json()
        assert bad_data["allowed_min"] == 5.0
        assert bad_data["allowed_max"] == 8.0

        # Below floor 5.0 MW returns 422
        bad_floor_res = client.post(
            f"/runs/{run_id}/decision",
            json={"confirmed": True, "capacity_mw": 3.0, "flexible_connection": False},
        )
        assert bad_floor_res.status_code == 422

        # 5. Valid decision returns 204
        mock_handle.execute_update = AsyncMock(return_value=None)
        good_decision_res = client.post(
            f"/runs/{run_id}/decision",
            json={"confirmed": True, "capacity_mw": 8.0, "flexible_connection": False},
        )
        assert good_decision_res.status_code == 204
        mock_handle.execute_update.assert_awaited_once()

        # 6. Reject decision returns 204
        mock_handle.execute_update.reset_mock()
        reject_res = client.post(
            f"/runs/{run_id}/decision",
            json={"confirmed": False},
        )
        assert reject_res.status_code == 204
        mock_handle.execute_update.assert_awaited_once()

        # 6b. While the run is still running, a rejection is accepted but a confirmation is not
        mock_handle.query = AsyncMock(return_value=RunStatus(status="running", stages=["capacity"]))
        mock_handle.execute_update.reset_mock()
        early_reject_res = client.post(f"/runs/{run_id}/decision", json={"confirmed": False})
        assert early_reject_res.status_code == 204
        mock_handle.execute_update.assert_awaited_once()
        early_confirm_res = client.post(f"/runs/{run_id}/decision", json={"confirmed": True})
        assert early_confirm_res.status_code == 409
        mock_handle.query = AsyncMock(return_value=mock_status_awaiting)

        # 7. Completed result returns 200
        mock_desc_completed = MagicMock()
        mock_desc_completed.status = WorkflowExecutionStatus.COMPLETED
        mock_desc_completed.memo_value = AsyncMock(return_value=USER.uid)
        mock_handle.describe = AsyncMock(return_value=mock_desc_completed)
        mock_result = AssessmentResult(
            status="completed",
            run_dir=f"out/{run_id}",
        )
        mock_handle.result = AsyncMock(return_value=mock_result)

        completed_res = client.get(f"/runs/{run_id}/result")
        assert completed_res.status_code == 200
        assert completed_res.json()["status"] == "completed"


def test_open_completed_run_serves_latest_result_and_takes_retries(client: TestClient) -> None:
    """A run kept open for retries serves its latest result; /retry forwards the update or reports why not."""
    from temporalio.client import WorkflowUpdateFailedError
    from temporalio.exceptions import ApplicationError

    from bessible.api.runs import RETRY_WINDOW_S
    from bessible.workflow import AssessmentWorkflow

    latest = AssessmentResult(status="completed", run_dir="out/x", retries_left=3)
    mock_client, mock_handle = MagicMock(), MagicMock()
    mock_client.start_workflow = AsyncMock(return_value=mock_handle)
    mock_client.get_workflow_handle.return_value = mock_handle

    async def query(q: object) -> object:
        return latest if q is AssessmentWorkflow.result else RunStatus(status="completed")

    mock_handle.query = AsyncMock(side_effect=query)
    desc = MagicMock(status=WorkflowExecutionStatus.RUNNING)
    desc.memo_value = AsyncMock(return_value=USER.uid)
    mock_handle.describe = AsyncMock(return_value=desc)
    mock_handle.execute_update = AsyncMock(return_value=None)

    with patch("bessible.api.runs.get_temporal_client", AsyncMock(return_value=mock_client)):
        client.post("/runs", json={"postcode": "RH4 1AD", "retry_window_s": 5})
        assert mock_client.start_workflow.call_args.args[1].retry_window_s == RETRY_WINDOW_S  # the API's, not ours

        res = client.get("/runs/bessible-1/result")
        assert res.status_code == 200
        assert res.json()["retries_left"] == 3

        assert client.post("/runs/bessible-1/retry", json={"stages": ["site_land"]}).status_code == 204
        mock_handle.execute_update.assert_awaited_once_with(AssessmentWorkflow.retry_stages, ["site_land"])
        assert client.post("/runs/bessible-1/retry", json={"stages": []}).status_code == 422
        assert client.post("/runs/bessible-1/retry", json={"stages": ["nonsense"]}).status_code == 422

        refused = WorkflowUpdateFailedError(ApplicationError("Nothing a retry could fill"))
        mock_handle.execute_update = AsyncMock(side_effect=refused)
        assert client.post("/runs/bessible-1/retry", json={"stages": ["grid"]}).status_code == 409

        desc.status = WorkflowExecutionStatus.COMPLETED
        closed = client.post("/runs/bessible-1/retry", json={"stages": ["site_land"]})
        assert closed.status_code == 409
        assert "start a new run" in closed.json()["detail"]


def test_unknown_run_returns_404(client: TestClient) -> None:
    """Requests for unknown runs return 404."""
    rpc_not_found = RPCError("Workflow not found", RPCStatusCode.NOT_FOUND, None)

    mock_client = MagicMock()
    mock_handle = MagicMock()
    mock_handle.query = AsyncMock(side_effect=rpc_not_found)
    mock_handle.describe = AsyncMock(side_effect=rpc_not_found)
    mock_client.get_workflow_handle.return_value = mock_handle

    with patch("bessible.api.runs.get_temporal_client", AsyncMock(return_value=mock_client)):
        res = client.get("/runs/unknown-run-xyz/status")
        assert res.status_code == 404

        res = client.get("/runs/unknown-run-xyz/result")
        assert res.status_code == 404


def _owned_temporal(owner: str | None) -> AsyncMock:
    """A patched `get_temporal_client` whose only run is owned by `owner` and already finished."""
    desc = MagicMock()
    desc.status = WorkflowExecutionStatus.COMPLETED
    desc.memo_value = AsyncMock(return_value=owner)
    handle = MagicMock()
    handle.describe = AsyncMock(return_value=desc)
    temporal = MagicMock()
    temporal.get_workflow_handle.return_value = handle
    return AsyncMock(return_value=temporal)


def test_events_emission_and_sse_streaming(client: TestClient) -> None:
    """Test append-only event logging, Last-Event-ID, and SSE streaming."""
    with patch("bessible.api.events.get_temporal_client", _owned_temporal(USER.uid)):
        _check_events_emission_and_sse_streaming(client)


def _check_events_emission_and_sse_streaming(client: TestClient) -> None:
    run_id = f"test-events-{int(time.time())}"

    # Verify emit appends increasing IDs
    events.emit(run_id, "location", "Starting location")
    events.emit(run_id, "location", "Resolved location")
    events.emit(run_id, "capacity", "Evaluated capacity")
    events.emit(run_id, "title", "Retrieved title")

    events_path = events.get_events_path(run_id)
    assert events_path.exists()
    lines = [json.loads(line) for line in events_path.read_text().strip().split("\n")]
    assert len(lines) == 4
    assert [line["id"] for line in lines] == [1, 2, 3, 4]
    assert lines[0]["stage"] == "location"
    assert lines[2]["stage"] == "capacity"

    # SSE streaming from beginning
    res = client.get(f"/runs/{run_id}/events")
    assert res.status_code == 200
    content = res.text
    assert "id: 1" in content
    assert "id: 4" in content

    # SSE streaming with Last-Event-ID (reconnect resumes at id + 1)
    res_resumed = client.get(f"/runs/{run_id}/events", headers={"Last-Event-ID": "2"})
    assert res_resumed.status_code == 200
    resumed_content = res_resumed.text
    assert "id: 1" not in resumed_content
    assert "id: 2" not in resumed_content
    assert "id: 3" in resumed_content
    assert "id: 4" in resumed_content

    # SSE streaming with last_event_id query param
    res_param = client.get(f"/runs/{run_id}/events?last_event_id=3")
    assert res_param.status_code == 200
    param_content = res_param.text
    assert "id: 3" not in param_content
    assert "id: 4" in param_content

    # Clean up
    if events_path.parent.exists():
        for p in events_path.parent.iterdir():
            p.unlink()
        events_path.parent.rmdir()


def test_events_emit_never_raises() -> None:
    """events.emit never raises even if disk write fails."""
    with patch("bessible.events.get_events_path", side_effect=PermissionError("Mock write permission denied")):
        # Should log a warning and return silently, not raise
        events.emit("fake-run", "test", "should not crash")


def test_setup_logfire_swallows_errors() -> None:
    """setup_logfire catches any errors without raising."""
    with patch("logfire.configure", side_effect=RuntimeError("Invalid logfire token")):
        # Should not raise
        setup_logfire()
