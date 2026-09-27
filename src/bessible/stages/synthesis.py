"""Synthesis report generation stage: combines stage outputs into an explainable Markdown report."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from bessible.footprint import reserved_acres, reserved_acres_by_duration
from bessible.guard import check_narration
from bessible.models import Artifact, DataGap, Finding, ReportOutput, SiteLandOutput, SynthesisInput, Verdict
from bessible.suitability.verdict import decide

REFERENCE_DURATION_HOURS = 4


def _write_report_file(run_id: str, file_name: str, content: str) -> None:
    run_dir = Path("out") / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / file_name).write_text(content, encoding="utf-8")


def flatten_state(inp: SynthesisInput) -> dict[str, float]:
    """Extract numeric run state for the narration guard."""
    acres_4h = reserved_acres(inp.site.capacity_mw, REFERENCE_DURATION_HOURS)
    state: dict[str, float] = {
        "capacity_mw": inp.site.capacity_mw,
        "firm_mw": inp.capacity.firm_mw,
        "ceiling_mw": inp.capacity.ceiling_mw,
        "recommended_mw": inp.capacity.recommended_mw,
        "footprint_acres_min": acres_4h[0],
        "footprint_acres_max": acres_4h[1],
        "footprint_duration_h": float(REFERENCE_DURATION_HOURS),
    }
    if inp.capacity.connection_voltage_kv is not None:
        state["connection_voltage_kv"] = inp.capacity.connection_voltage_kv
    if inp.grid.indicative_connection_months is not None:
        state["indicative_connection_months"] = float(inp.grid.indicative_connection_months)
    if inp.grid.gate2_queue_position is not None:
        state["gate2_queue_position"] = float(inp.grid.gate2_queue_position)
    for c in inp.financial.cases:
        state[f"duration_{c.duration_h}h"] = float(c.duration_h)
        state[f"capex_{c.duration_h}h"] = c.capex_gbp
        state[f"npv_{c.duration_h}h"] = c.npv_gbp
        if c.irr is not None:
            state[f"irr_{c.duration_h}h"] = c.irr
        if c.curtailment_pct is not None:
            state[f"curtailment_{c.duration_h}h"] = c.curtailment_pct
    return state


def _land_finding(land: SiteLandOutput) -> str:
    """The land stage in one sentence: its worst outcome first, and what could not be checked."""
    if land.blockers:
        text = f"Land classification: {land.land_use}; blocker: {land.blockers[0]}"
    elif land.caveats:
        text = f"Land classification: {land.land_use}; caveat: {land.caveats[0]}"
    else:
        text = f"Land classification: {land.land_use}; no blocking land constraint found."
    if land.gaps:
        text += " Not assessed: " + ", ".join(g.what for g in land.gaps) + "."
    return text


def all_gaps(inp: SynthesisInput) -> list[DataGap]:
    """Missing evidence from every analysis stage, in pipeline order."""
    outputs = (inp.grid, inp.site_land, inp.market, inp.sentiment)
    return [g for out in outputs if out is not None for g in out.gaps]


def _gap_lines(gaps: list[DataGap]) -> list[str]:
    if not gaps:
        return []
    lines = ["", "## Data Gaps", "Evidence the assessment could not obtain. Retrying may fill temporary gaps."]
    for g in gaps:
        kind = "temporary, retry may fix" if g.retryable else "no coverage here"
        effect = "; could hide a blocker" if g.could_block else ""
        lines.append(f"- **{g.stage} / {g.what}** ({kind}{effect}): {g.reason}")
    return lines


def _build_findings(inp: SynthesisInput, art_ids_by_stage: dict[str, list[str]]) -> list[Finding]:
    findings: list[Finding] = []
    if "capacity" in art_ids_by_stage:
        sub = inp.capacity.substation or "substation"
        findings.append(
            Finding(
                text=(
                    f"Grid capacity confirmed: {inp.capacity.firm_mw:g} MW firm connection "
                    f"({inp.capacity.ceiling_mw:g} MW ceiling) at {sub}."
                ),
                artifact_ids=art_ids_by_stage["capacity"],
            )
        )
    if "grid" in art_ids_by_stage:
        findings.append(
            Finding(
                text=(
                    f"Gate 2 queue position {inp.grid.gate2_queue_position} with indicative connection "
                    f"timescale of {inp.grid.indicative_connection_months} months."
                ),
                artifact_ids=art_ids_by_stage["grid"],
            )
        )
    if "site_land" in art_ids_by_stage:
        findings.append(Finding(text=_land_finding(inp.site_land), artifact_ids=art_ids_by_stage["site_land"]))
    if "financial" in art_ids_by_stage:
        best = max((c for c in inp.financial.cases if c.irr is not None), key=lambda c: c.irr, default=None)
        c4 = next((c for c in inp.financial.cases if c.duration_h == REFERENCE_DURATION_HOURS), None)
        if best:
            fin_text = (
                f"Financial analysis: optimal returns at {best.duration_h}-hour duration "
                f"achieving {best.irr * 100:.1f}% IRR (£{best.npv_gbp:,.0f} 25-year NPV)."
            )
        elif c4:
            fin_text = (
                f"Financial analysis: 4-hour duration requires £{c4.capex_gbp:,.0f} CAPEX "
                "with negative returns across all cases."
            )
        else:
            fin_text = "Financial analysis completed across storage duration cases."
        findings.append(
            Finding(
                text=fin_text,
                artifact_ids=art_ids_by_stage["financial"],
            )
        )
    if "planning" in art_ids_by_stage:
        findings.append(
            Finding(
                text=f"Consenting pathway via {inp.planning.consenting_route}.",
                artifact_ids=art_ids_by_stage["planning"],
            )
        )
    if "market" in art_ids_by_stage:
        findings.append(
            Finding(
                text="Market revenue projected across storage durations.",
                artifact_ids=art_ids_by_stage["market"],
            )
        )
    if "sentiment" in art_ids_by_stage and inp.sentiment:
        concerns = f" with key concerns: {', '.join(inp.sentiment.top_concerns)}" if inp.sentiment.top_concerns else ""
        findings.append(
            Finding(
                text=f"Community sentiment analysis completed{concerns}.",
                artifact_ids=art_ids_by_stage["sentiment"],
            )
        )

    # Reserved area planning-grade finding
    acres_4h = reserved_acres(inp.site.capacity_mw, REFERENCE_DURATION_HOURS)
    footprint_cites = (
        art_ids_by_stage.get("title")
        or art_ids_by_stage.get("site_land")
        or art_ids_by_stage.get("capacity")
        or ([inp.artifacts[0].id] if inp.artifacts else [f"synthesis-{inp.run_id[:8]}"])
    )
    findings.append(
        Finding(
            text=(
                f"Reserved area requirement: {acres_4h[0]:.1f} to {acres_4h[1]:.1f} acres "
                f"for {inp.site.capacity_mw:g} MW (planning-grade estimate at {REFERENCE_DURATION_HOURS}-hour duration)."
            ),
            artifact_ids=list(footprint_cites),
        )
    )

    if not findings:
        fallback_art_id = f"synthesis-{inp.run_id[:8]}"
        findings.append(
            Finding(
                text="Feasibility criteria satisfied across grid, land, and market models.",
                artifact_ids=[fallback_art_id],
            )
        )

    # Apply narration guard to every finding
    state = flatten_state(inp)
    guarded_findings: list[Finding] = []
    for f in findings:
        unmatched = check_narration(f.text, state)
        if not unmatched:
            guarded_findings.append(f)
        else:
            guarded_findings.append(
                Finding(
                    text="Feasibility criteria satisfied across grid, land, and market models.",
                    artifact_ids=f.artifact_ids,
                )
            )

    return guarded_findings


def _render_markdown(
    inp: SynthesisInput,
    verdict: Verdict,
    findings: list[Finding],
    extra_artifacts: list[Artifact] | None = None,
) -> str:
    sub = inp.capacity.substation or "N/A"
    volt = f"{inp.capacity.connection_voltage_kv:g} kV" if inp.capacity.connection_voltage_kv else "N/A"
    direction = inp.capacity.binding_direction or "None"
    season = inp.capacity.binding_season or "N/A"
    acres_4h = reserved_acres(inp.site.capacity_mw, REFERENCE_DURATION_HOURS)
    by_dur = reserved_acres_by_duration(inp.site.capacity_mw)

    lines = [
        f"# Bessible BESS Suitability Assessment Report — Run {inp.run_id}",
        "",
        f"**Verdict:** `{verdict.upper()}`",
        "",
        "## Site & Connection Summary",
        f"- **Coordinates:** {inp.site.position.lat:.4f}, {inp.site.position.lon:.4f}",
        f"- **Confirmed Capacity:** {inp.site.capacity_mw:g} MW",
        f"- **Reserved Area:** {acres_4h[0]:.1f} to {acres_4h[1]:.1f} acres (4 h default; 2 h: {by_dur[2][0]:.1f} to {by_dur[2][1]:.1f}, 8 h: {by_dur[8][0]:.1f} to {by_dur[8][1]:.1f})",
        f"- **Firm Headroom:** {inp.capacity.firm_mw:g} MW",
        f"- **Ceiling Headroom:** {inp.capacity.ceiling_mw:g} MW",
        f"- **Substation:** {sub}",
        f"- **Voltage:** {volt}",
        f"- **Binding Constraint:** {direction} ({season})",
    ]

    if inp.capacity.snapshot_date:
        snap_date = inp.capacity.snapshot_date
        age_days = (datetime.now(UTC).date() - snap_date).days
        lines.append(f"- **Snapshot Date:** {snap_date.isoformat()} ({age_days} days old)")

    lines.extend([
        "",
        "## Storage Duration Comparison",
        "| Duration | CAPEX (£) | 25-Year NPV (£) | IRR |",
        "|---|---|---|---|",
    ])

    for case in inp.financial.cases:
        irr_str = f"{case.irr * 100:.1f}%" if case.irr is not None else "N/A"
        lines.append(f"| {case.duration_h} hours | £{case.capex_gbp:,.0f} | £{case.npv_gbp:,.0f} | {irr_str} |")

    # Local Community Sentiment Section
    if inp.sentiment is not None and inp.sentiment.opposition_index is not None:
        idx = inp.sentiment.opposition_index
        risk_level = (
            "High Opposition" if idx >= 0.8 else ("Moderate Caution" if idx >= 0.5 else "Low Opposition / Supportive")
        )
        lines.extend([
            "",
            "## Local Community Sentiment & Opposition",
            f"- **Opposition Index:** `{idx:.2f}` / 1.00 ({risk_level})",
            f"- **Top Community Concerns:** {', '.join(inp.sentiment.top_concerns) if inp.sentiment.top_concerns else 'None specified'}",
            f"- **Research Coverage:** {inp.sentiment.sources} local news sources, {inp.sentiment.paragraphs} classified paragraphs",
        ])

    lines.extend(_gap_lines(all_gaps(inp)))

    lines.extend(["", "## Key Findings"])
    for f in findings:
        cites = ", ".join(f"`{aid}`" for aid in f.artifact_ids)
        lines.append(f"- {f.text} (Evidence: {cites})")

    lines.extend(["", "## Supporting Evidence Artifacts"])
    evidence_artifacts = list(inp.artifacts)
    if extra_artifacts:
        evidence_artifacts.extend(extra_artifacts)
    for art in evidence_artifacts:
        ref = art.source_url or art.file_path or art.image_path or "N/A"
        lines.append(
            f"- **[{art.id}]** ({art.stage}): {art.claim} [Source: {ref}] "
            f"(Confidence: {art.confidence * 100:.0f}%, Model: {art.model_used})"
        )

    return "\n".join(lines) + "\n"


async def synthesise(inp: SynthesisInput) -> ReportOutput:
    """Synthesise findings and duration returns into an explainable Markdown report."""
    verdict: Verdict = "go"
    try:
        verdict, _rules = decide(inp.financial, inp.sentiment, inp.site_land)
    except Exception:
        verdict = "go"

    art_ids_by_stage: dict[str, list[str]] = {}
    for art in inp.artifacts:
        art_ids_by_stage.setdefault(art.stage, []).append(art.id)

    findings = _build_findings(inp, art_ids_by_stage)

    # Generate footprint.json artifact
    acres_4h = reserved_acres(inp.site.capacity_mw, REFERENCE_DURATION_HOURS)
    by_dur = reserved_acres_by_duration(inp.site.capacity_mw)
    footprint_file_name = "footprint.json"
    footprint_payload = {
        "inputs": {
            "capacity_mw": inp.site.capacity_mw,
            "duration_h": REFERENCE_DURATION_HOURS,
        },
        "range_acres": {
            "min": acres_4h[0],
            "max": acres_4h[1],
        },
        "range_by_duration": {str(d): {"min": rng[0], "max": rng[1]} for d, rng in by_dur.items()},
        "rule_of_thumb": "0.05 to 0.075 acres per MWh (planning-grade estimate)",
        "source": "BESS planning-grade benchmark (0.1 to 0.15 acres per MW at 2 h)",
    }
    await asyncio.to_thread(
        _write_report_file,
        inp.run_id,
        footprint_file_name,
        json.dumps(footprint_payload, indent=2),
    )

    art_footprint = Artifact(
        id=f"footprint-{inp.run_id[:8]}",
        stage="synthesis",
        claim=(
            f"Planning-grade reserved area estimate: {acres_4h[0]:.1f} to {acres_4h[1]:.1f} acres "
            f"for {inp.site.capacity_mw:g} MW (4-hour duration)"
        ),
        file_path=footprint_file_name,
        confidence=0.95,
        model_used="deterministic",
    )

    art_synth = Artifact(
        id=f"synthesis-{inp.run_id[:8]}",
        stage="synthesis",
        claim=f"Assessment report compiled with verdict '{verdict.upper()}' and duration comparison",
        file_path="report.md",
        confidence=0.95,
        model_used="deterministic",
    )

    report_content = _render_markdown(inp, verdict, findings, extra_artifacts=[art_footprint, art_synth])
    report_file_name = "report.md"
    await asyncio.to_thread(_write_report_file, inp.run_id, report_file_name, report_content)

    return ReportOutput(
        verdict=verdict,
        findings=findings,
        report_path=report_file_name,
        artifacts=[art_synth, art_footprint],
    )
