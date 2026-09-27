"""Assessment workflow orchestrating sequential stages, parallel groups, and human confirmation."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Literal

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from bessible import activities
    from bessible.models import (
        Artifact,
        AssessmentRequest,
        AssessmentResult,
        CapacityInput,
        CapacityOutput,
        ConfirmedSite,
        FinancialInput,
        FinancialOutput,
        GridOutput,
        LocationInput,
        LocationOutput,
        MarketOutput,
        NodeInput,
        PlanningInput,
        PlanningOutput,
        ReportOutput,
        RunStatus,
        SentimentOutput,
        SiteDecision,
        SiteLandOutput,
        Stage,
        SynthesisInput,
        TitleInput,
        TitleOutput,
    )

TASK_QUEUE = "bessible"

RETRY_POLICY = RetryPolicy(
    maximum_attempts=3,
    non_retryable_error_types=[
        "ValidationError",
        "LocationNotFound",
        "PageUnavailable",
        "PostcodeNotFound",
        "MissingGoogleKey",
        "InvalidCredentials",
    ],
)

DEFAULT_OPTS = {
    "start_to_close_timeout": timedelta(seconds=60),
    "retry_policy": RETRY_POLICY,
}

AGENT_OPTS = {
    "start_to_close_timeout": timedelta(seconds=180),
    "retry_policy": RETRY_POLICY,
}


@workflow.defn
class AssessmentWorkflow:
    """Orchestrates an end-to-end BESS site assessment."""

    def __init__(self) -> None:
        """Initialize workflow state."""
        self._status: Literal[
            "running",
            "awaiting_confirmation",
            "completed",
            "rejected",
            "out_of_area",
            "not_viable",
            "failed",
        ] = "running"
        self._stages: list[Stage] = []
        self._request: AssessmentRequest | None = None
        self._location: LocationOutput | None = None
        self._capacity: CapacityOutput | None = None
        self._title: TitleOutput | None = None
        self._boundary: TitleOutput | None = None
        self._decision: SiteDecision | None = None

    @workflow.query
    def status(self) -> RunStatus:
        """Query current status and active stages."""
        msg = None
        if self._capacity and not self._capacity.viable:
            msg = self._capacity.message
        return RunStatus(
            status=self._status,
            stages=list(self._stages),
            capacity=self._capacity,
            boundary=self._boundary,
            position=self._location.position if self._location else None,
            message=msg,
        )

    @workflow.update
    def decide_site(self, decision: SiteDecision) -> None:
        """Human-in-the-loop site decision update."""
        self._decision = decision

    @decide_site.validator
    def _validate_decide_site(self, decision: SiteDecision) -> None:
        """Validate that confirmation decision is allowed and within capacity bounds.

        A rejection is also accepted before the run reaches the confirmation step (the user moved to another
        location); the run then stops before the title stage.
        """
        if self._decision is not None:
            msg = "Site decision already made"
            raise ValueError(msg)
        allowed = ("running", "awaiting_confirmation") if not decision.confirmed else ("awaiting_confirmation",)
        if self._status not in allowed:
            msg = f"Not awaiting confirmation (current status: {self._status})"
            raise ValueError(msg)
        if decision.confirmed:
            if self._capacity is None:
                msg = "Cannot confirm without a capacity proposal"
                raise ValueError(msg)
            cap_mw = decision.capacity_mw if decision.capacity_mw is not None else self._capacity.recommended_mw
            if cap_mw <= 0:
                msg = f"Capacity must be positive (got {cap_mw:g} MW)"
                raise ValueError(msg)
            if cap_mw > self._capacity.ceiling_mw:
                msg = f"Capacity {cap_mw:g} MW exceeds ceiling headroom of {self._capacity.ceiling_mw:g} MW"
                raise ValueError(msg)
            is_flex = (
                decision.flexible_connection
                if decision.flexible_connection is not None
                else (self._request.flexible_connection if self._request else False)
            )
            if not is_flex and cap_mw > self._capacity.firm_mw:
                msg = (
                    f"Capacity {cap_mw:g} MW exceeds firm headroom of "
                    f"{self._capacity.firm_mw:g} MW (flexible connection disabled)"
                )
                raise ValueError(msg)

    async def _run_location_and_capacity(
        self, run_id: str, request: AssessmentRequest, all_artifacts: list[Artifact]
    ) -> AssessmentResult | None:
        """Run location and capacity stages, returning an early result if not viable."""
        self._status = "running"
        self._stages = ["location"]
        loc = await workflow.execute_activity(
            activities.resolve_location,
            LocationInput(run_id=run_id, request=request),
            **DEFAULT_OPTS,
        )
        self._location = loc
        all_artifacts.extend(loc.artifacts)

        self._stages = ["capacity"]
        cap = await workflow.execute_activity(
            activities.propose_capacity,
            CapacityInput(run_id=run_id, request=request, location=loc),
            **DEFAULT_OPTS,
        )
        self._capacity = cap
        all_artifacts.extend(cap.artifacts)

        run_dir = f"out/{run_id}"
        if cap.out_of_area:
            self._status = "out_of_area"
            self._stages = []
            return AssessmentResult(
                status="out_of_area", message=cap.message, capacity=cap, artifacts=all_artifacts, run_dir=run_dir
            )

        if not cap.viable:
            self._status = "not_viable"
            self._stages = []
            return AssessmentResult(
                status="not_viable", message=cap.message, capacity=cap, artifacts=all_artifacts, run_dir=run_dir
            )

        return None

    def _rejected(self, run_id: str, all_artifacts: list[Artifact]) -> AssessmentResult:
        self._status = "rejected"
        self._stages = []
        return AssessmentResult(status="rejected", artifacts=all_artifacts, run_dir=f"out/{run_id}")

    async def _await_confirmation(self, run_id: str, all_artifacts: list[Artifact]) -> ConfirmedSite | AssessmentResult:
        """Find title boundaries and await human confirmation."""
        if self._request is None or self._location is None or self._capacity is None:
            msg = "Workflow state incomplete before title stage"
            raise RuntimeError(msg)

        if self._decision is not None and not self._decision.confirmed:
            return self._rejected(run_id, all_artifacts)

        self._stages = ["title"]
        title = await workflow.execute_activity(
            activities.find_title_boundaries,
            TitleInput(
                run_id=run_id,
                request=self._request,
                location=self._location,
                capacity=self._capacity,
            ),
            **DEFAULT_OPTS,
        )
        self._title = title
        self._boundary = title
        all_artifacts.extend(title.artifacts)

        self._status = "awaiting_confirmation"
        self._stages = []
        await workflow.wait_condition(lambda: self._decision is not None)

        decision = self._decision
        if decision is None or not decision.confirmed:
            return self._rejected(run_id, all_artifacts)

        self._status = "running"
        chosen_pos = decision.position or self._location.position
        chosen_cap = decision.capacity_mw if decision.capacity_mw is not None else self._capacity.recommended_mw
        is_flex = (
            decision.flexible_connection
            if decision.flexible_connection is not None
            else (self._request.flexible_connection if self._request else False)
        )
        return ConfirmedSite(
            position=chosen_pos,
            capacity_mw=chosen_cap,
            boundary=title,
            footprint_geojson=decision.footprint_geojson,
            flexible_connection=is_flex,
        )

    async def _run_parallel_groups(
        self,
        run_id: str,
        site: ConfirmedSite,
        all_artifacts: list[Artifact],
    ) -> tuple[GridOutput, SiteLandOutput, MarketOutput, FinancialOutput, PlanningOutput, SentimentOutput]:
        """Execute parallel analysis groups and collect outputs."""
        if self._request is None or self._capacity is None:
            msg = "Workflow request or capacity missing before analysis"
            raise RuntimeError(msg)

        # Parallel Group 1
        self._stages = ["grid", "site_land", "market", "sentiment"]
        node_in = NodeInput(run_id=run_id, request=self._request, site=site, capacity=self._capacity)

        grid_fut = workflow.execute_activity(activities.grid_connection, node_in, **AGENT_OPTS)
        land_fut = workflow.execute_activity(activities.site_land, node_in, **AGENT_OPTS)
        market_fut = workflow.execute_activity(activities.market_revenue, node_in, **AGENT_OPTS)
        sentiment_fut = workflow.execute_activity(activities.local_sentiment, node_in, **AGENT_OPTS)

        grid, site_land, market, sentiment = await asyncio.gather(grid_fut, land_fut, market_fut, sentiment_fut)
        all_artifacts.extend(grid.artifacts)
        all_artifacts.extend(site_land.artifacts)
        all_artifacts.extend(market.artifacts)
        all_artifacts.extend(sentiment.artifacts)

        # Parallel Group 2
        self._stages = ["financial", "planning"]
        fin_in = FinancialInput(
            run_id=run_id,
            request=self._request,
            site=site,
            capacity=self._capacity,
            grid=grid,
            market=market,
            site_land=site_land,
        )
        plan_in = PlanningInput(
            run_id=run_id,
            request=self._request,
            site=site,
            capacity=self._capacity,
            grid=grid,
            site_land=site_land,
        )

        fin_fut = workflow.execute_activity(activities.financial_model, fin_in, **DEFAULT_OPTS)
        plan_fut = workflow.execute_activity(activities.regulatory_planning, plan_in, **DEFAULT_OPTS)

        fin, plan = await asyncio.gather(fin_fut, plan_fut)
        all_artifacts.extend(fin.artifacts)
        all_artifacts.extend(plan.artifacts)

        return grid, site_land, market, fin, plan, sentiment

    async def _run_synthesis(
        self,
        run_id: str,
        site: ConfirmedSite,
        analysis: tuple[GridOutput, SiteLandOutput, MarketOutput, FinancialOutput, PlanningOutput, SentimentOutput],
        all_artifacts: list[Artifact],
    ) -> ReportOutput:
        """Run the final synthesis stage."""
        if self._request is None or self._capacity is None:
            msg = "Workflow request or capacity missing before synthesis"
            raise RuntimeError(msg)
        grid, site_land, market, fin, plan, sentiment = analysis

        self._stages = ["synthesis"]
        synth_in = SynthesisInput(
            run_id=run_id,
            request=self._request,
            site=site,
            capacity=self._capacity,
            grid=grid,
            site_land=site_land,
            market=market,
            financial=fin,
            planning=plan,
            sentiment=sentiment,
            artifacts=all_artifacts,
        )
        report = await workflow.execute_activity(activities.synthesise, synth_in, **DEFAULT_OPTS)
        all_artifacts.extend(report.artifacts)
        return report

    @workflow.run
    async def run(self, request: AssessmentRequest) -> AssessmentResult:
        """Execute end-to-end BESS site assessment workflow."""
        run_id = workflow.info().workflow_id
        result = await self._assess(run_id, request)
        postcode = self._location.postcode if self._location else None
        return result.model_copy(update={"run_id": run_id, "postcode": postcode})

    async def _assess(self, run_id: str, request: AssessmentRequest) -> AssessmentResult:
        """All stages in order; each early stop returns its own result."""
        self._request = request
        all_artifacts: list[Artifact] = []

        early_result = await self._run_location_and_capacity(run_id, request, all_artifacts)
        if early_result is not None:
            return early_result

        confirm_result = await self._await_confirmation(run_id, all_artifacts)
        if isinstance(confirm_result, AssessmentResult):
            return confirm_result
        site = confirm_result

        analysis = await self._run_parallel_groups(run_id, site, all_artifacts)
        report = await self._run_synthesis(run_id, site, analysis, all_artifacts)

        self._status = "completed"
        self._stages = []
        return AssessmentResult(
            status="completed",
            report=report,
            financial=analysis[3],
            site=site,
            capacity=self._capacity,
            artifacts=all_artifacts,
            gaps=[g for out in (analysis[0], analysis[1], analysis[2], analysis[5]) for g in out.gaps],
            run_dir=f"out/{run_id}",
        )
