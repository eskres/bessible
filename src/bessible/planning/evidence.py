"""DESNZ REPD planning evidence and Gemini citation-backed summary."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field, HttpUrl
from pydantic_ai import Agent

from bessible.config import settings
from bessible.models import NearbyProject

if TYPE_CHECKING:
    from pydantic_ai.models import Model

log = logging.getLogger(__name__)

POLICY_FILE = Path(__file__).parent / "policy.json"


class PolicyItem(BaseModel):
    """A fixed statutory or national planning policy statement."""

    id: str
    title: str
    statement: str
    source_url: HttpUrl


class CitedStatement(BaseModel):
    """A summary statement citing one or more project or policy IDs."""

    text: str
    cites: list[str] = Field(default_factory=list, description="IDs of cited REPD projects or policy items")


class PlanningSummary(BaseModel):
    """Synthesised planning summary backed by citations."""

    statements: list[CitedStatement] = Field(default_factory=list)
    model_used: str = ""


def load_policy(path: Path | None = None) -> list[PolicyItem]:
    """Load the fixed national planning policy set from policy.json."""
    p = path or POLICY_FILE
    if not p.exists():
        log.warning("Policy file not found at %s", p)
        return []
    raw = json.loads(p.read_text(encoding="utf-8"))
    return [PolicyItem.model_validate(item) for item in raw]


def validate_citations(summary: PlanningSummary, valid_ids: set[str]) -> bool:
    """Verify that every statement has at least one citation and all citations match valid IDs.

    Returns False if any statement is uncited or cites an unknown ID.
    """
    if not summary.statements:
        return False
    for stmt in summary.statements:
        if not stmt.cites:
            log.warning("Planning summary statement has no citations: %r", stmt.text)
            return False
        for cite_id in stmt.cites:
            if cite_id not in valid_ids:
                log.warning("Planning summary statement cited unknown ID %r: %r", cite_id, stmt.text)
                return False
    return True


def build_evidence_prompt(projects: list[NearbyProject], policy: list[PolicyItem]) -> str:
    """Build prompt containing only fixed policy items and listed nearby battery projects."""
    lines: list[str] = [
        "You are a UK energy infrastructure planning specialist.",
        "Summarise local planning precedent and relevant policy for a proposed Battery Energy Storage System (BESS).",
        "",
        "AVAILABLE NEARBY REPD BATTERY PROJECTS (search radius: 5 km):",
    ]

    if projects:
        lines.extend(
            f"- ID: {p.id} | Name: {p.name} | Capacity: {p.capacity} | "
            f"Status: {p.status} | Date: {p.status_date} | Distance: {p.distance_km:g} km"
            for p in projects
        )
    else:
        lines.append("- No existing or proposed battery projects found within 5 km.")

    lines.extend([
        "",
        "AVAILABLE FIXED NATIONAL POLICIES:",
    ])
    lines.extend(f"- ID: {pol.id} | Title: {pol.title} | Statement: {pol.statement}" for pol in policy)

    valid_ids = [p.id for p in projects] + [pol.id for pol in policy]
    lines.extend([
        "",
        "VALID IDS THAT YOU ARE PERMITTED TO CITE IN 'cites':",
    ])
    lines.extend(f"  * {vid}" for vid in valid_ids)

    lines.extend([
        "",
        "MANDATORY RULES:",
        "1. Produce 1 to 3 concise statements in 'statements'.",
        "2. Every statement MUST cite at least one ID in its 'cites' list.",
        "3. You MUST NEVER cite an ID that is not listed under VALID IDS.",
        "4. Do not invent any facts, numbers, or planning outcomes.",
    ])

    return "\n".join(lines)


def create_summary_agent(model: Model | str) -> Agent[None, PlanningSummary]:
    """Create a Pydantic AI agent configured for planning evidence summarisation."""
    return Agent(
        model,
        name="planning_evidence_summariser",
        output_type=PlanningSummary,
        system_prompt=(
            "You are a planning consultant summarising evidence for a BESS planning application in England.\n"
            "Only produce statements directly backed by the provided REPD records and national policies.\n"
            "Every statement must explicitly cite the relevant project or policy IDs."
        ),
    )


async def summarise(
    projects: list[NearbyProject],
    policy: list[PolicyItem],
    model: Model | str,
) -> PlanningSummary | None:
    """Summarise planning evidence with the run's `model` and strict citation validation.

    If any statement is uncited or cites an invalid ID, the summary is rejected and None is returned.
    """
    valid_ids = {p.id for p in projects} | {pol.id for pol in policy}
    agent = create_summary_agent(model=model)
    prompt = build_evidence_prompt(projects, policy)

    try:
        res = await agent.run(prompt)
    except Exception as exc:  # ruff: ignore[blind-except]
        log.warning("Planning summary agent call failed: %s", exc)
        return None

    summary = res.output
    if not validate_citations(summary, valid_ids):
        log.warning("Planning summary rejected: citation check failed")
        return None

    model_name = (
        getattr(model, "model_name", None)
        or getattr(model, "name", None)
        or (str(model) if isinstance(model, str) else settings.gemini_model)
    )
    summary.model_used = str(model_name)
    return summary
