"""Unit and integration tests for Demo Mode recording and replay."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from bessible.api.app import app
from bessible.models import (
    AssessmentRequest,
    AssessmentResult,
    CapacityOutput,
    EncryptedCredentials,
    Finding,
    Position,
    ReportOutput,
    RunStatus,
    SiteDecision,
    TitleOutput,
)
from bessible.recorder import (
    SecretDetectedError,
    assert_no_secrets,
    normalize_events_relative_timings,
    save_run_recording,
    scan_for_secrets,
)


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def test_secret_scanner_detects_sensitive_material(tmp_path: Path):
    clean_dir = tmp_path / "clean"
    clean_dir.mkdir()
    (clean_dir / "request.json").write_text(json.dumps({"postcode": "RH4 1AD", "credentials": None}))
    assert scan_for_secrets(clean_dir) == []
    assert_no_secrets(clean_dir)

    # Google API key detection
    bad_dir_1 = tmp_path / "bad1"
    bad_dir_1.mkdir()
    (bad_dir_1 / "leak.txt").write_text("AIzaSyDummyKeyForTestingPurposes1234567")
    findings_1 = scan_for_secrets(bad_dir_1)
    assert len(findings_1) > 0
    assert any("Google API Key" in f for f in findings_1)
    with pytest.raises(SecretDetectedError):
        assert_no_secrets(bad_dir_1)

    # Generic secret key detection
    bad_dir_2 = tmp_path / "bad2"
    bad_dir_2.mkdir()
    (bad_dir_2 / "data.json").write_text(json.dumps({"token": "sk-123456789012345678901234"}))
    assert len(scan_for_secrets(bad_dir_2)) > 0
    with pytest.raises(SecretDetectedError):
        assert_no_secrets(bad_dir_2)

    # Encrypted credentials leakage detection
    bad_dir_3 = tmp_path / "bad3"
    bad_dir_3.mkdir()
    (bad_dir_3 / "leak.json").write_text('{"google_ct": "ciphertext12345678"}')
    assert len(scan_for_secrets(bad_dir_3)) > 0
    with pytest.raises(SecretDetectedError):
        assert_no_secrets(bad_dir_3)


def test_normalize_events_relative_timings():
    raw = [
        {"id": 1, "t": "2026-09-22T10:00:00.000Z", "stage": "location", "msg": "start"},
        {"id": 2, "t": "2026-09-22T10:00:01.500Z", "stage": "location", "msg": "found"},
        {"id": 3, "t": "2026-09-22T10:00:04.000Z", "stage": "capacity", "msg": "capacity"},
    ]
    norm = normalize_events_relative_timings(raw)
    assert len(norm) == 3
    assert norm[0]["offset_s"] == 0.0
    assert norm[1]["offset_s"] == 1.5
    assert norm[2]["offset_s"] == 4.0


def test_save_run_recording_strips_credentials_and_saves_artifacts(tmp_path: Path):
    dest = tmp_path / "demo_save"
    req = AssessmentRequest(
        postcode="RH4 1AD",
        credentials=EncryptedCredentials(uid="u1", key_id="k1", google_ct="secret_ct"),
    )
    events = [
        {"id": 1, "t": "2026-09-22T10:00:00.000Z", "stage": "location", "msg": "loc"},
        {"id": 2, "t": "2026-09-22T10:00:01.000Z", "stage": "title", "msg": "title"},
    ]
    statuses = [
        RunStatus(status="running", stages=["location"]),
        RunStatus(status="awaiting_confirmation", stages=[]),
    ]
    dec = SiteDecision(confirmed=True, capacity_mw=8.0)
    res = AssessmentResult(
        status="completed",
        run_dir="out/demo",
        report=ReportOutput(
            verdict="go",
            findings=[Finding(text="Good site", artifact_ids=["a1"])],
            report_path="report.md",
        ),
    )

    out = save_run_recording(
        dest,
        request=req,
        events=events,
        statuses=statuses,
        decision=dec,
        result=res,
    )
    assert out.exists()
    assert (out / "request.json").exists()
    assert (out / "events.jsonl").exists()
    assert (out / "statuses.json").exists()
    assert (out / "decision.json").exists()
    assert (out / "result.json").exists()

    saved_req = json.loads((out / "request.json").read_text(encoding="utf-8"))
    assert saved_req.get("credentials") is None
    assert "secret_ct" not in (out / "request.json").read_text()


def test_demo_replay_full_lifecycle(client: TestClient):
    # 1. Start demo replay with speedup
    resp = client.post("/demo/runs", json={"slug": "dorking", "pacing_multiplier": 0.0})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "run_id" in data
    run_id = data["run_id"]
    assert run_id.startswith("demo-")

    # 2. Check status: replay reaches awaiting_confirmation quickly when pacing=0
    import time

    status_data = None
    for _ in range(50):
        s_resp = client.get(f"/demo/runs/{run_id}/status")
        assert s_resp.status_code == 200
        st = s_resp.json()
        if st["status"] == "awaiting_confirmation":
            status_data = st
            break
        time.sleep(0.05)

    assert status_data is not None
    assert status_data["status"] == "awaiting_confirmation"
    assert status_data["capacity"] is not None
    assert status_data["boundary"] is not None
    assert status_data["position"] is not None

    # 3. Result while in progress returns 409
    res_resp = client.get(f"/demo/runs/{run_id}/result")
    assert res_resp.status_code == 409
    assert res_resp.json()["status"] == "awaiting_confirmation"

    # 4. Submit confirmation decision
    dec_resp = client.post(
        f"/demo/runs/{run_id}/decision",
        json={"confirmed": True, "capacity_mw": 8.0},
    )
    assert dec_resp.status_code == 204

    # 5. Wait for completion
    completed_data = None
    for _ in range(50):
        s_resp = client.get(f"/demo/runs/{run_id}/status")
        assert s_resp.status_code == 200
        st = s_resp.json()
        if st["status"] == "completed":
            completed_data = st
            break
        time.sleep(0.05)

    assert completed_data is not None
    assert completed_data["status"] == "completed"

    # 6. Retrieve completed result
    result_resp = client.get(f"/demo/runs/{run_id}/result")
    assert result_resp.status_code == 200
    res = result_resp.json()
    assert res["status"] == "completed"
    assert res["run_id"] == run_id
    assert res["postcode"] == "RH4 1AD"
    recorded = json.loads(Path("data/demo/dorking/result.json").read_text(encoding="utf-8"))
    assert res["report"]["verdict"] == recorded["report"]["verdict"]
    assert len(res["report"]["findings"]) > 0
    assert res["financial"]["discount_rate_pct"] is not None
    assert res["financial"]["project_life_years"] is not None


def test_demo_replay_events_sse(client: TestClient):
    # Start replay with pacing=0
    resp = client.post("/demo/runs", json={"slug": "dorking", "pacing_multiplier": 0.0})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]

    # Confirm to let it run through
    import time

    for _ in range(30):
        st = client.get(f"/demo/runs/{run_id}/status").json()
        if st["status"] == "awaiting_confirmation":
            client.post(f"/demo/runs/{run_id}/decision", json={"confirmed": True})
            break
        time.sleep(0.05)

    # Stream events
    with client.stream("GET", f"/demo/runs/{run_id}/events") as stream:
        lines = list(stream.iter_lines())
        data_lines = [l for l in lines if l.startswith("data:")]
        assert len(data_lines) > 0
        ev1 = json.loads(data_lines[0].replace("data:", "").strip())
        assert "id" in ev1
        assert "stage" in ev1
        assert "msg" in ev1


def test_demo_id_isolation_from_real_routes(client: TestClient):
    from bessible.auth import User, current_user

    app.dependency_overrides[current_user] = lambda: User(uid="test-user")
    try:
        # Real routes reject demo- prefixed ids with 404
        # Status route
        resp = client.get("/runs/demo-12345/status")
        assert resp.status_code == 404

        # Decision route
        resp = client.post("/runs/demo-12345/decision", json={"confirmed": True})
        assert resp.status_code == 404

        # Result route
        resp = client.get("/runs/demo-12345/result")
        assert resp.status_code == 404

        # Events route
        resp = client.get("/runs/demo-12345/events")
        assert resp.status_code == 404

        # Demo routes reject non-demo ids with 404
        assert client.get("/demo/runs/bessible-12345/status").status_code == 404
        assert client.get("/demo/runs/bessible-12345/result").status_code == 404
        assert client.post("/demo/runs/bessible-12345/decision", json={"confirmed": True}).status_code == 404
    finally:
        app.dependency_overrides.pop(current_user, None)


@pytest.mark.parametrize("slug", ["dorking", "histon", "manchester"])
def test_every_demo_preset_has_a_valid_recording(slug: str):
    demo_dir = Path("data/demo") / slug
    result = AssessmentResult.model_validate_json((demo_dir / "result.json").read_text(encoding="utf-8"))
    request = AssessmentRequest.model_validate_json((demo_dir / "request.json").read_text(encoding="utf-8"))
    assert request.credentials is None
    assert scan_for_secrets(demo_dir) == []
    if result.status == "completed":
        assert (demo_dir / "decision.json").exists()
        assert result.financial is not None
        assert result.financial.discount_rate_pct is not None


def test_demo_replay_out_of_area_ends_without_the_gate(client: TestClient):
    import time

    run_id = client.post("/demo/runs", json={"slug": "manchester", "pacing_multiplier": 0.0}).json()["run_id"]
    status = None
    for _ in range(50):
        status = client.get(f"/demo/runs/{run_id}/status").json()
        if status["status"] == "out_of_area":
            break
        time.sleep(0.05)

    assert status is not None
    assert status["status"] == "out_of_area"
    assert status["message"]
    res = client.get(f"/demo/runs/{run_id}/result")
    assert res.status_code == 200
    assert res.json()["status"] == "out_of_area"
    with client.stream("GET", f"/demo/runs/{run_id}/events") as stream:
        assert any(line.startswith("data:") for line in stream.iter_lines())


def test_demo_replay_rejects_a_slug_outside_data_demo(client: TestClient):
    assert client.post("/demo/runs", json={"slug": "../dorking"}).status_code == 422


def test_save_run_recording_without_a_decision_writes_no_decision_file(tmp_path: Path):
    out = save_run_recording(
        tmp_path / "early_end",
        request=AssessmentRequest(postcode="M1 1AD"),
        events=[],
        statuses=[RunStatus(status="out_of_area", stages=[])],
        decision=None,
        result=AssessmentResult(status="out_of_area", run_dir="out/demo"),
    )
    assert (out / "result.json").exists()
    assert not (out / "decision.json").exists()


def _await_status(client: TestClient, run_id: str, want: str) -> dict:
    import time

    for _ in range(100):
        st = client.get(f"/demo/runs/{run_id}/status").json()
        if st["status"] == want:
            return st
        time.sleep(0.05)
    pytest.fail(f"{run_id} never reached {want}")


def test_a_moved_pin_is_re_assessed_instead_of_replayed(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    from bessible import events
    from bessible.api import demo

    seen: dict[str, SiteDecision] = {}

    async def fake_rerun(run_id, request, gate, recorded, decision):
        seen["decision"] = decision
        events.emit(run_id, "site_land", "Site land constraints assessed: 0 constraints noted")
        moved = gate.boundary.model_copy(update={"inspire_ids": ["moved"]})
        site = recorded.site.model_copy(update={"position": decision.position, "boundary": moved})
        return recorded.model_copy(update={"run_id": run_id, "site": site})

    monkeypatch.setattr(demo, "rerun", fake_rerun)
    run_id = client.post("/demo/runs", json={"slug": "histon", "pacing_multiplier": 0.0}).json()["run_id"]
    gate = _await_status(client, run_id, "awaiting_confirmation")
    pin = gate["position"]
    moved = {"lat": pin["lat"] - 0.007, "lon": pin["lon"] + 0.02}  # toward Arbury

    assert client.post(f"/demo/runs/{run_id}/decision", json={"confirmed": True, "position": moved}).status_code == 204
    done = _await_status(client, run_id, "completed")

    assert seen["decision"].position == Position(**moved)
    assert done["boundary"]["inspire_ids"] == ["moved"]
    assert client.get(f"/demo/runs/{run_id}/result").json()["site"]["position"] == moved
    trace = [
        json.loads(line[6:])["msg"]
        for line in client.get(f"/demo/runs/{run_id}/events").text.splitlines()
        if line.startswith("data: ")
    ]
    assert "Site land constraints assessed: 0 constraints noted" in trace


def test_changes_site_only_for_a_different_choice():
    from bessible.demo_rerun import changes_site

    here = Position(lat=52.245, lon=0.1082)
    gate = RunStatus(
        status="awaiting_confirmation",
        position=here,
        capacity=CapacityOutput(viable=True, firm_mw=37.7, ceiling_mw=50, recommended_mw=37.7),
    )
    request = AssessmentRequest(postcode="CB24 9ZR")

    def changes(**decision) -> bool:
        return changes_site(SiteDecision(confirmed=True, **decision), gate, request)

    assert not changes()
    assert not changes(position=here, capacity_mw=37.7, flexible_connection=False, title_ids=None)
    assert changes(position=Position(lat=52.24, lon=0.12))
    assert changes(capacity_mw=20)
    assert changes(flexible_connection=True)
    assert changes(title_ids=["39165193"])
