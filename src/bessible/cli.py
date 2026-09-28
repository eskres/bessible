"""Bessible command-line interface for starting assessments and managing site confirmation."""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from typing import TYPE_CHECKING, Any

from pydantic import HttpUrl, ValidationError
from temporalio.client import Client, WorkflowFailureError
from temporalio.contrib.pydantic import pydantic_data_converter

from bessible.config import settings
from bessible.credentials import CredentialsError, encrypt_google_key
from bessible.footprint import footprint_polygon, reserved_acres, reserved_acres_by_duration
from bessible.models import (
    AssessmentRequest,
    AssessmentResult,
    EncryptedCredentials,
    Position,
    RunStatus,
    SiteDecision,
    TitleOutput,
)
from bessible.recorder import record_live_run
from bessible.ukpn import SnapshotNotFoundError, get_snapshot, snapshot_age_days
from bessible.workflow import TASK_QUEUE, AssessmentWorkflow

if TYPE_CHECKING:
    from datetime import date

    from temporalio.client import WorkflowHandle

ACTIVE_STATUSES = {"running", "awaiting_confirmation"}
TERMINAL_STATUSES = {"completed", "rejected", "out_of_area", "not_viable", "failed"}
AFFIRMATIVE_RESPONSES = {"y", "yes"}
SIX_MONTHS_DAYS = 182


def check_snapshot_age_warning(today: date | None = None) -> bool:
    """Check snapshot age and print a warning to stderr if older than 6 months (182 days)."""
    try:
        snap = get_snapshot()
        age = snapshot_age_days(snap, today=today)
        if age > SIX_MONTHS_DAYS:
            print(  # ruff: ignore[print]
                f"Warning: UKPN snapshot from {snap.fetched_at.isoformat()} is {age} days old (> 6 months). "
                "Data may be stale; run `python -m bessible.ukpn.ingest --refresh` to update.",
                file=sys.stderr,
            )
            return True
    except (SnapshotNotFoundError, OSError, ValueError):
        pass
    return False


async def _get_client() -> Client:
    """Connect to the Temporal server or exit with a helpful instruction."""
    try:
        return await asyncio.wait_for(
            Client.connect(
                settings.temporal_address,
                namespace=settings.temporal_namespace,
                data_converter=pydantic_data_converter,
            ),
            timeout=3.0,
        )
    except (TimeoutError, ConnectionError, OSError, RuntimeError):
        print(  # ruff: ignore[print]
            f"Error: Could not connect to Temporal server at {settings.temporal_address}.\n"
            "Please ensure the server is running:\n"
            "  temporal server start-dev",
            file=sys.stderr,
        )
        sys.exit(1)


def _print_result(raw_result: AssessmentResult | dict[str, Any]) -> None:
    """Print detailed summary of an assessment result."""
    result = AssessmentResult.model_validate(raw_result) if isinstance(raw_result, dict) else raw_result
    print(f"\nStatus: {result.status.upper()}")  # ruff: ignore[print]
    if result.message:
        print(f"Message: {result.message}")  # ruff: ignore[print]

    if result.status == "rejected":
        print("Site confirmation was rejected.")  # ruff: ignore[print]
        return

    if result.report:
        print(f"Verdict: {result.report.verdict.upper()}")  # ruff: ignore[print]
        print("\nFindings:")  # ruff: ignore[print]
        for f in result.report.findings:
            cites = ", ".join(f.artifact_ids)
            print(f"  - {f.text} [{cites}]")  # ruff: ignore[print]

    if result.financial:
        print("\nStorage Duration Comparison:")  # ruff: ignore[print]
        print(f"  {'Duration':<10} {'CAPEX (£)':<15} {'25-Yr NPV (£)':<16} {'IRR':<8}")  # ruff: ignore[print]
        print("  " + "-" * 50)  # ruff: ignore[print]
        for c in result.financial.cases:
            irr_str = f"{c.irr * 100:.1f}%" if c.irr is not None else "N/A"
            print(f"  {f'{c.duration_h} hours':<10} £{c.capex_gbp:<14,.0f} £{c.npv_gbp:<15,.0f} {irr_str:<8}")  # ruff: ignore[print]

    print(f"\nArtifacts Directory: {result.run_dir}")  # ruff: ignore[print]
    if result.report:
        print(f"Report File: {result.run_dir}/{result.report.report_path}")  # ruff: ignore[print]


def _format_footprint(capacity_mw: float) -> str:
    """Format planning-grade area ranges across standard durations."""
    by_dur = reserved_acres_by_duration(capacity_mw)
    r4 = by_dur[4]
    r2 = by_dur[2]
    r8 = by_dur[8]
    return f"{r4[0]:.1f}-{r4[1]:.1f} acres (4 h default; 2 h: {r2[0]:.1f}-{r2[1]:.1f}, 8 h: {r8[0]:.1f}-{r8[1]:.1f})"


def _build_footprint_polygon(position: Position | None, capacity_mw: float) -> dict[str, Any] | None:
    """Generate footprint polygon for the given position and capacity at default 4h duration."""
    if position is None or capacity_mw <= 0:
        return None
    r4 = reserved_acres(capacity_mw, 4)
    mid_acres = (r4[0] + r4[1]) / 2.0
    return footprint_polygon(position, mid_acres)


def _print_titles(title: TitleOutput) -> None:
    """The pin polygon, the candidates and every title number with its source."""
    pin = title.pin_parcel
    where = f"INSPIRE {pin.inspire_id} ({pin.area_m2 / 10_000:.2f} ha)" if pin else "on no INSPIRE polygon"
    print(f"Pin polygon:        {where}; {len(title.candidates)} polygons around it")  # ruff: ignore[print]
    for n in title.title_numbers:
        owner = f", {n.proprietor}" if n.proprietor else ""
        print(f"Title number:       {n.title_number} ({n.source}, linked to the {n.link}{owner})")  # ruff: ignore[print]
    if not title.title_numbers:
        print("Title number:       not known (the INSPIRE index has none)")  # ruff: ignore[print]
    for note in title.notes:
        print(f"  note: {note}")  # ruff: ignore[print]


async def _handle_confirmation_prompt(
    handle: WorkflowHandle[Any, Any],
    status: RunStatus,
    *,
    auto_yes: bool,
) -> None:
    """Prompt the user for site confirmation and submit the update."""
    cap = status.capacity
    if cap is None:
        return

    print("\n--- Site Confirmation Required ---")  # ruff: ignore[print]
    print(f"Substation:         {cap.substation or 'N/A'}")  # ruff: ignore[print]
    print(f"Connection Voltage: {cap.connection_voltage_kv or 0:g} kV")  # ruff: ignore[print]
    print(f"Firm Headroom:      {cap.firm_mw:g} MW")  # ruff: ignore[print]
    print(f"Ceiling Headroom:   {cap.ceiling_mw:g} MW")  # ruff: ignore[print]
    print(f"Recommended:        {cap.recommended_mw:g} MW")  # ruff: ignore[print]
    print(f"Binding Direction:  {cap.binding_direction or 'None'} ({cap.binding_season or 'N/A'})")  # ruff: ignore[print]

    if status.boundary:
        _print_titles(status.boundary)
    print(f"Reserved Area:      {_format_footprint(cap.recommended_mw)}")  # ruff: ignore[print]

    if auto_yes:
        print(f"Auto-confirming recommended capacity: {cap.recommended_mw:g} MW (--yes)")  # ruff: ignore[print]
        footprint = _build_footprint_polygon(status.position, cap.recommended_mw)
        await handle.execute_update(
            AssessmentWorkflow.decide_site,
            SiteDecision(
                confirmed=True,
                capacity_mw=cap.recommended_mw,
                footprint_geojson=footprint,
            ),
        )
        return

    raw_input = await asyncio.to_thread(input, "\nConfirm site? [y/N]: ")
    resp = raw_input.strip().lower()
    if resp not in AFFIRMATIVE_RESPONSES:
        print("Rejecting site assessment.")  # ruff: ignore[print]
        await handle.execute_update(
            AssessmentWorkflow.decide_site,
            SiteDecision(confirmed=False),
        )
        return

    while True:
        prompt_msg = f"Enter capacity in MW [{cap.recommended_mw:g}]: "
        cap_str = (await asyncio.to_thread(input, prompt_msg)).strip()
        chosen_mw = float(cap_str) if cap_str else cap.recommended_mw
        print(f"Reserved Area:      {_format_footprint(chosen_mw)}")  # ruff: ignore[print]
        try:
            footprint = _build_footprint_polygon(status.position, chosen_mw)
            await handle.execute_update(
                AssessmentWorkflow.decide_site,
                SiteDecision(confirmed=True, capacity_mw=chosen_mw, footprint_geojson=footprint),
            )
            print(f"Confirmed site at {chosen_mw:g} MW.")  # ruff: ignore[print]
            break
        except Exception as exc:  # ruff: ignore[blind-except]
            print(f"Error: {exc}. Please enter a valid capacity between 0 and {cap.ceiling_mw:g} MW.")  # ruff: ignore[print]


async def _watch_workflow(handle: WorkflowHandle[Any, Any], *, auto_yes: bool) -> None:
    """Watch workflow progress, prompting for confirmation and printing output."""
    last_stages: list[str] = []
    has_prompted = False

    while True:
        status: RunStatus = await handle.query(AssessmentWorkflow.status)
        if status.stages != last_stages and status.stages:
            print(f"Running stage(s): {', '.join(status.stages)}...")  # ruff: ignore[print]
            last_stages = list(status.stages)

        if status.status == "awaiting_confirmation" and not has_prompted:
            has_prompted = True
            await _handle_confirmation_prompt(handle, status, auto_yes=auto_yes)

        if status.status in TERMINAL_STATUSES:
            break

        await asyncio.sleep(0.5)

    try:
        result = await handle.result()
        _print_result(result)
    except WorkflowFailureError as err:
        print(f"\nWorkflow failed: {err}", file=sys.stderr)  # ruff: ignore[print]
        sys.exit(1)


LOCAL_UID = "local-cli"


def _sealed_developer_key() -> EncryptedCredentials:
    """Your `GOOGLE_API_KEY` from `.env`, sealed for the local CLI user; exits when it is missing or unsealable."""
    if settings.google_api_key is None:
        print("GOOGLE_API_KEY is not set in .env: a run needs your Gemini key.", file=sys.stderr)  # ruff: ignore[print]
        sys.exit(1)
    try:
        return encrypt_google_key(LOCAL_UID, settings.google_api_key.get_secret_value())
    except CredentialsError as err:
        print(f"Cannot seal your Google key: {err}", file=sys.stderr)  # ruff: ignore[print]
        sys.exit(1)


async def cmd_start(args: argparse.Namespace) -> None:
    """Start an assessment workflow."""
    check_snapshot_age_warning()
    property_url = HttpUrl(args.url) if args.url else None
    credentials = _sealed_developer_key()
    try:
        req = AssessmentRequest(
            credentials=credentials,
            property_url=property_url,
            postcode=args.postcode,
            battery_mw=args.battery_mw,
            budget_gbp=args.budget_gbp,
            flexible_connection=args.flexible,
        )
    except ValidationError as err:
        print(f"Validation error: {err}", file=sys.stderr)  # ruff: ignore[print]
        sys.exit(1)

    client = await _get_client()
    run_id = f"bessible-{uuid.uuid4().hex[:8]}"

    handle = await client.start_workflow(
        AssessmentWorkflow.run,
        req,
        id=run_id,
        task_queue=TASK_QUEUE,
    )

    print(f"Started assessment run: {run_id}")  # ruff: ignore[print]
    if args.detach:
        return

    await _watch_workflow(handle, auto_yes=args.yes)


async def cmd_confirm(args: argparse.Namespace) -> None:
    """Submit a confirmation or rejection update for a paused run."""
    client = await _get_client()
    handle = client.get_workflow_handle(args.run_id, result_type=AssessmentResult)

    if args.reject:
        await handle.execute_update(
            AssessmentWorkflow.decide_site,
            SiteDecision(confirmed=False),
        )
        print(f"Run {args.run_id} rejected.")  # ruff: ignore[print]
        return

    status: RunStatus = await handle.query(AssessmentWorkflow.status)
    cap_mw = args.capacity_mw or (status.capacity.recommended_mw if status.capacity else None)
    footprint = _build_footprint_polygon(status.position, cap_mw) if status.position and cap_mw else None

    decision = SiteDecision(confirmed=True, capacity_mw=args.capacity_mw, footprint_geojson=footprint)
    await handle.execute_update(AssessmentWorkflow.decide_site, decision)
    cap_msg = f"with {args.capacity_mw:g} MW" if args.capacity_mw else "with default capacity"
    print(f"Run {args.run_id} confirmed {cap_msg}.")  # ruff: ignore[print]


async def cmd_result(args: argparse.Namespace) -> None:
    """Retrieve and display the status or result of a run."""
    client = await _get_client()
    handle = client.get_workflow_handle(args.run_id, result_type=AssessmentResult)

    status: RunStatus = await handle.query(AssessmentWorkflow.status)
    if status.status in ACTIVE_STATUSES:
        active = ", ".join(status.stages) if status.stages else "none"
        print(f"Run {args.run_id} is currently {status.status} (active stages: {active}).")  # ruff: ignore[print]
        return

    result = await handle.result()
    _print_result(result)


async def cmd_record(args: argparse.Namespace) -> None:
    """Run a demo preset live, auto-confirm the proposal, and save the run into `data/demo/<slug>`."""
    request = AssessmentRequest(
        postcode=args.postcode, flexible_connection=args.flexible, credentials=_sealed_developer_key()
    )
    out = await record_live_run(request, slug=args.slug, client=await _get_client())
    print(f"Recorded {args.postcode} into {out}")  # ruff: ignore[print]


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI argument parser."""
    parser = argparse.ArgumentParser(prog="bessible", description="Bessible BESS Assessment CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # start
    start_p = subparsers.add_parser("start", help="Start a site assessment")
    start_p.add_argument("url", nargs="?", help="Property link URL")
    start_p.add_argument("--postcode", help="UK postcode (e.g. 'OX14 4TE')")
    start_p.add_argument("--battery-mw", type=float, help="Target battery capacity in MW")
    start_p.add_argument("--budget-gbp", type=float, help="Total project budget in GBP")
    start_p.add_argument("--flexible", action="store_true", help="Enable flexible grid connection above firm headroom")
    start_p.add_argument("--detach", action="store_true", help="Print run ID and exit without waiting")
    start_p.add_argument("--yes", "-y", action="store_true", help="Automatically accept proposal defaults")

    # confirm
    confirm_p = subparsers.add_parser("confirm", help="Confirm or reject site for an awaiting run")
    confirm_p.add_argument("run_id", help="Workflow run ID")
    confirm_p.add_argument("--capacity-mw", type=float, help="Confirmed capacity in MW")
    confirm_p.add_argument("--reject", action="store_true", help="Reject the site")

    # result
    result_p = subparsers.add_parser("result", help="View result or status of a run")
    result_p.add_argument("run_id", help="Workflow run ID")

    # record
    record_p = subparsers.add_parser("record", help="Record a live run of a demo preset into data/demo/<slug>")
    record_p.add_argument("slug", help="Folder under data/demo (e.g. dorking)")
    record_p.add_argument("postcode", help="Demo preset postcode (e.g. 'RH4 1AD')")
    record_p.add_argument("--flexible", action="store_true", help="Allow a flexible grid connection")

    return parser


def main() -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "start":
        asyncio.run(cmd_start(args))
    elif args.command == "confirm":
        asyncio.run(cmd_confirm(args))
    elif args.command == "result":
        asyncio.run(cmd_result(args))
    elif args.command == "record":
        asyncio.run(cmd_record(args))


if __name__ == "__main__":
    main()
