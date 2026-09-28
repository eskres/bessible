"""Recorder and secret scanner for capturing real runs into reproducible demo recordings."""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import SecretStr

from bessible.config import settings
from bessible.models import (
    AssessmentRequest,
    AssessmentResult,
    RunStatus,
    SiteDecision,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

# Patterns that indicate secret or sensitive credential material
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Google API Key", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("Generic API Key", re.compile(r"(?:sk|ak|as)-[0-9a-zA-Z]{20,}")),
    (
        "JWT / ID Token",
        re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ),
    ("Private Key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("Encrypted Key Material", re.compile(r'"google_ct"\s*:\s*"[^"]+"')),
    ("Key Storage Field", re.compile(r'"google_key"\s*:\s*"[^"]+"')),
    ("Master Secret Field", re.compile(r'"master_secret"\s*:\s*"[^"]+"')),
]


class SecretDetectedError(ValueError):
    """Raised when recorded demo artifacts contain API keys or credential material."""


def scan_for_secrets(target: Path | str, known_secrets: Sequence[str] | None = None) -> list[str]:
    """Scan a file or directory recursively for any secret or key material.

    Returns:
        List of human-readable descriptions of detected secrets (empty if clean).
    """
    path = Path(target)
    findings: list[str] = []

    files_to_check: list[Path] = []
    if path.is_file():
        files_to_check.append(path)
    elif path.is_dir():
        for p in path.rglob("*"):
            if p.is_file():
                files_to_check.append(p)

    check_secrets = [s.strip() for s in (known_secrets or []) if len(s.strip()) >= 10]

    for file_path in files_to_check:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("Could not read %s for secret scan: %s", file_path, exc)
            continue

        for label, pattern in SECRET_PATTERNS:
            match = pattern.search(content)
            if match:
                findings.append(f"{file_path.name}: matched {label} ({match.group(0)[:8]}...)")

        for secret in check_secrets:
            if secret in content:
                findings.append(f"{file_path.name}: matched configured runtime secret ({secret[:8]}...)")

    return findings


def assert_no_secrets(target: Path | str, known_secrets: Sequence[str] | None = None) -> None:
    """Scan target and raise SecretDetectedError if any secret or credential material is found."""
    findings = scan_for_secrets(target, known_secrets=known_secrets)
    if findings:
        detail = "; ".join(findings)
        msg = f"Key material or credentials found in recorded demo: {detail}"
        raise SecretDetectedError(msg)


def _configured_secrets() -> list[str]:
    """Every secret value set in `settings`, so a recording that echoes a real key fails the scan."""
    values = [getattr(settings, name) for name in type(settings).model_fields]
    values += list(settings.key_encryption_previous.values())
    return [v.get_secret_value() for v in values if isinstance(v, SecretStr)]


def normalize_events_relative_timings(raw_events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compute relative elapsed time in seconds (`offset_s`) for each event relative to the start."""
    if not raw_events:
        return []

    # Parse timestamps if present
    parsed_times: list[datetime | None] = []
    for ev in raw_events:
        t_str = ev.get("t")
        t_val: datetime | None = None
        if isinstance(t_str, str):
            with contextlib.suppress(ValueError):
                t_val = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
        parsed_times.append(t_val)

    first_time = next((t for t in parsed_times if t is not None), None)

    normalized: list[dict[str, Any]] = []
    for idx, ev in enumerate(raw_events):
        t_val = parsed_times[idx]
        offset_s = 0.0
        if first_time is not None and t_val is not None:
            offset_s = max(0.0, round((t_val - first_time).total_seconds(), 2))
        elif "offset_s" in ev:
            offset_s = float(ev["offset_s"])

        item = {
            "id": int(ev.get("id", idx + 1)),
            "offset_s": offset_s,
            "t": ev.get("t", ""),
            "stage": str(ev.get("stage", "")),
            "msg": str(ev.get("msg", "")),
        }
        normalized.append(item)

    return normalized


def save_run_recording(
    dest_dir: Path | str,
    *,
    request: AssessmentRequest,
    events: Sequence[dict[str, Any]],
    statuses: Sequence[dict[str, Any] | RunStatus],
    decision: SiteDecision | None,
    result: AssessmentResult,
) -> Path:
    """Save all five recorded artifacts into `dest_dir` and assert no secrets are present.

    A run that stops before site confirmation (out of area, not viable) has no decision, so it has no
    `decision.json`.

    Artifacts saved:
      - `request.json`
      - `events.jsonl`
      - `statuses.json`
      - `decision.json`
      - `result.json`
    """
    out_dir = Path(dest_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Clean request: credentials stripped completely
    clean_req = request.model_copy(update={"credentials": None})
    req_json = clean_req.model_dump_json(indent=2)
    (out_dir / "request.json").write_text(req_json, encoding="utf-8")

    # 2. Timed events with relative offsets
    norm_events = normalize_events_relative_timings(events)
    events_lines = [json.dumps(ev) for ev in norm_events]
    (out_dir / "events.jsonl").write_text("\n".join(events_lines) + ("\n" if events_lines else ""), encoding="utf-8")

    # 3. Status snapshots per stage transition
    normalized_statuses: list[dict[str, Any]] = []
    for item in statuses:
        if isinstance(item, RunStatus):
            normalized_statuses.append({"stage": item.stages[0] if item.stages else item.status, "status": item.model_dump(mode="json")})
        elif isinstance(item, dict) and "status" in item:
            normalized_statuses.append(item)
        elif isinstance(item, dict):
            # Dict of RunStatus fields directly
            normalized_statuses.append({"stage": item.get("stages", [None])[0] or item.get("status"), "status": item})

    # Compact: the snapshots and result carry thousands of polygon coordinates, which indenting triples in size
    statuses_json = json.dumps(normalized_statuses, separators=(",", ":"), ensure_ascii=False)
    (out_dir / "statuses.json").write_text(statuses_json, encoding="utf-8")

    # 4. Human-in-the-loop decision
    if decision is None:
        (out_dir / "decision.json").unlink(missing_ok=True)
    else:
        (out_dir / "decision.json").write_text(decision.model_dump_json(indent=2), encoding="utf-8")

    # 5. Assessment result
    res_json = result.model_dump_json()
    (out_dir / "result.json").write_text(res_json, encoding="utf-8")

    # Mandatory security check
    try:
        assert_no_secrets(out_dir, known_secrets=_configured_secrets())
    except SecretDetectedError:
        # Clean up any partial files if secret detected
        for fname in ["request.json", "events.jsonl", "statuses.json", "decision.json", "result.json"]:
            (out_dir / fname).unlink(missing_ok=True)
        raise

    return out_dir


async def record_live_run(
    request: AssessmentRequest,
    *,
    slug: str = "dorking",
    dest_dir: Path | None = None,
    decision: SiteDecision | None = None,
    client: Any | None = None,
) -> Path:
    """Execute an assessment workflow end to end, observe state transitions, and save a demo recording."""
    from bessible.api.temporal import get_temporal_client
    from bessible.footprint import footprint_polygon, reserved_acres
    from bessible.workflow import TASK_QUEUE, AssessmentWorkflow

    target_dir = dest_dir or (settings.data_dir / "demo" / slug)
    c = client or await get_temporal_client()

    run_id = f"bessible-record-{slug}-{uuid.uuid4().hex[:8]}"  # unique, so events never append to an older recording
    handle = await c.start_workflow(
        AssessmentWorkflow.run,
        request,
        id=run_id,
        task_queue=TASK_QUEUE,
    )

    statuses: list[dict[str, Any]] = []
    last_key: tuple[str, tuple[str, ...]] | None = None

    # Track status transitions through the execution
    confirmed = False
    dec: SiteDecision | None = None
    while True:
        status: RunStatus = await handle.query(AssessmentWorkflow.status)
        current_key = (status.status, tuple(status.stages))
        if current_key != last_key:
            stage_name = status.stages[0] if status.stages else status.status
            statuses.append({"stage": stage_name, "status": status.model_dump(mode="json")})
            last_key = current_key

        if status.status == "awaiting_confirmation" and not confirmed:
            confirmed = True
            if decision is None:
                cap = status.capacity
                rec_mw = cap.recommended_mw if cap else 5.0
                r4 = reserved_acres(rec_mw, 4)
                poly = footprint_polygon(status.position, (r4[0] + r4[1]) / 2.0)
                dec = SiteDecision(confirmed=True, capacity_mw=rec_mw, footprint_geojson=poly)
            else:
                dec = decision
            await handle.execute_update(AssessmentWorkflow.decide_site, dec)

        if status.status in ("completed", "rejected", "out_of_area", "not_viable", "failed"):
            break

        await asyncio.sleep(0.3)

    result: AssessmentResult = await handle.result()

    # Read events emitted to out/<run_id>/events.jsonl
    events_file = settings.data_dir.parent / "out" / run_id / "events.jsonl"
    events: list[dict[str, Any]] = []
    if events_file.exists():
        for line in events_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                with contextlib.suppress(json.JSONDecodeError):
                    events.append(json.loads(line))

    return save_run_recording(
        target_dir,
        request=request,
        events=events,
        statuses=statuses,
        decision=dec,
        result=result,
    )

