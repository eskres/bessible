"""Assessment workflow orchestrating sequential stages, parallel groups, and human confirmation."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from bessible import activities
    from bessible.models import (
        Artifact,
        AssessmentRequest,
        AssessmentResult,
        CapacityInput,
        CapacityOutput,
        ConfirmedSite,
        DataGap,
        FinancialInput,
        FinancialOutput,
        GridOutput,
        LocationInput,
        LocationOutput,
        MarketOutput,
        NodeInput,
        PlanningInput,
        PlanningOutput,
        RunStatus,
        SentimentOutput,
        SiteDecision,
        SiteLandOutput,
        Stage,
        SynthesisInput,
        TitleInput,
        TitleOutput,
        TitleSiteInput,
    )

if TYPE_CHECKING:
    from collections.abc import Awaitable

TASK_QUEUE = "bessible"
GATHER_STAGES: frozenset[Stage] = frozenset({"grid", "site_land", "market", "sentiment"})  # what a retry can re-run
MAX_RETRIES = 3  # bounds the run's history; a new run is the way past it

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
        self._pre_artifacts: list[Artifact] = []
        self._result: AssessmentResult | None = None
        self._retry: list[Stage] | None = None
        self._retries_left = 0
        self._error: str | None = None  # why the run failed, for the `status` query

    @workflow.query
    def status(self) -> RunStatus:
        """Query current status and active stages."""
        msg = self._error
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

    @workflow.query
    def result(self) -> AssessmentResult | None:
        """The latest completed result, while the run stays open for retries."""
        return self._result

    @workflow.update
    def retry_stages(self, stages: list[Stage]) -> None:
        """Re-run evidence stages whose data gaps a retry may fill; financial, planning and the report follow."""
        self._retry = stages
        self._status = "running"  # at once, so a poll right after the update never sees the old "completed"

    @retry_stages.validator
    def _validate_retry_stages(self, stages: list[Stage]) -> None:
        if self._status != "completed" or self._result is None:
            msg = f"Only a completed run can be retried (current status: {self._status})"
            raise ValueError(msg)
        if self._retry is not None:
            msg = "A retry is already running"
            raise ValueError(msg)
        if self._retries_left <= 0:
            msg = "No retries left on this run: start a new one"
            raise ValueError(msg)
        retryable: set[Stage] = {g.stage for g in self._result.gaps if g.retryable}
        if not stages or not set(stages) <= retryable:
            msg = f"Nothing a retry could fill in {sorted(set(stages) - retryable)}; retryable: {sorted(retryable)}"
            raise ValueError(msg)

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
            candidates = {p.inspire_id for p in self._title.candidates} if self._title else set()
            decision.check_titles(candidates)

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
        self._stages = ["title"]
        site_title = await workflow.execute_activity(
            activities.confirm_title_site,
            TitleSiteInput(
                run_id=run_id,
                title=title,
                origin=self._location.position,
                position=chosen_pos,
                capacity_mw=chosen_cap,
                footprint_geojson=decision.footprint_geojson,
                title_ids=decision.title_ids,
                added_ids=decision.added_ids,
                user_title_numbers=decision.user_title_numbers,
            ),
            **DEFAULT_OPTS,
        )
        self._boundary = site_title
        all_artifacts.extend(site_title.artifacts)
        return ConfirmedSite(
            position=chosen_pos,
            capacity_mw=chosen_cap,
            boundary=site_title,
            footprint_geojson=decision.footprint_geojson,
            flexible_connection=is_flex,
        )

    async def _run_parallel_groups(
        self,
        run_id: str,
        site: ConfirmedSite,
        keep: Analysis | None = None,
        rerun: frozenset[Stage] = GATHER_STAGES,
    ) -> Analysis:
        """Execute the parallel analysis groups. With `keep`, only the `rerun` stages of group 1 run again."""
        if self._request is None or self._capacity is None:
            msg = "Workflow request or capacity missing before analysis"
            raise RuntimeError(msg)

        # Parallel Group 1: the stages that gather evidence
        self._stages = [s for s in ("grid", "site_land", "market", "sentiment") if s in rerun]
        node_in = NodeInput(run_id=run_id, request=self._request, site=site, capacity=self._capacity)

        def gather(stage: Stage, fn: Any, kept: Any) -> Awaitable[Any]:  # ruff: ignore[any-type]
            if keep is not None and stage not in rerun:
                return _kept(kept)
            return workflow.execute_activity(fn, node_in, **AGENT_OPTS)

        grid, site_land, market, sentiment = await asyncio.gather(
            gather("grid", activities.grid_connection, keep and keep.grid),
            gather("site_land", activities.site_land, keep and keep.site_land),
            gather("market", activities.market_revenue, keep and keep.market),
            gather("sentiment", activities.local_sentiment, keep and keep.sentiment),
        )

        # Parallel Group 2: always re-run, since they read group 1
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
        return Analysis(
            grid=grid, site_land=site_land, market=market, sentiment=sentiment, financial=fin, planning=plan
        )

    async def _report(self, run_id: str, site: ConfirmedSite, analysis: Analysis) -> AssessmentResult:
        """Run the final synthesis stage and assemble the completed result."""
        if self._request is None or self._capacity is None:
            msg = "Workflow request or capacity missing before synthesis"
            raise RuntimeError(msg)
        all_artifacts = self._pre_artifacts + analysis.artifacts()

        self._stages = ["synthesis"]
        synth_in = SynthesisInput(
            run_id=run_id,
            request=self._request,
            site=site,
            capacity=self._capacity,
            grid=analysis.grid,
            site_land=analysis.site_land,
            market=analysis.market,
            financial=analysis.financial,
            planning=analysis.planning,
            sentiment=analysis.sentiment,
            artifacts=all_artifacts,
        )
        report = await workflow.execute_activity(activities.synthesise, synth_in, **DEFAULT_OPTS)
        return AssessmentResult(
            status="completed",
            report=report,
            financial=analysis.financial,
            site=site,
            capacity=self._capacity,
            artifacts=all_artifacts + report.artifacts,
            gaps=analysis.gaps(),
            retries_left=self._retries_left,
            run_id=run_id,  # also set by `run`; here too, so the `result` query serves it during the retry window
            postcode=self._location.postcode if self._location else None,
            run_dir=f"out/{run_id}",
        )

    async def _offer_retries(self, run_id: str, site: ConfirmedSite, analysis: Analysis, window: timedelta) -> None:
        """Stay open for `retry_stages` until the window passes without one, or the retries run out."""
        while self._retries_left > 0:
            try:
                await workflow.wait_condition(lambda: self._retry is not None, timeout=window)
            except TimeoutError:
                return
            rerun = frozenset(self._retry or ())
            self._retries_left -= 1
            analysis = await self._run_parallel_groups(run_id, site, keep=analysis, rerun=rerun)
            self._result = await self._report(run_id, site, analysis)
            self._retry = None
            self._status = "completed"
            self._stages = []

    @workflow.run
    async def run(self, request: AssessmentRequest) -> AssessmentResult:
        """Execute end-to-end BESS site assessment workflow."""
        run_id = workflow.info().workflow_id
        try:
            result = await self._assess(run_id, request)
        except ActivityError as exc:
            # Without this, the `status` query of a failed run replays to its last stage and reports "running".
            self._status = "failed"
            self._stages = []
            cause = exc.cause
            self._error = cause.message if isinstance(cause, ApplicationError) else str(cause or exc)
            raise
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
        self._pre_artifacts = all_artifacts
        if request.retry_window_s > 0:
            self._retries_left = MAX_RETRIES

        analysis = await self._run_parallel_groups(run_id, site)
        self._result = await self._report(run_id, site, analysis)
        self._status = "completed"
        self._stages = []

        if request.retry_window_s > 0:
            await self._offer_retries(run_id, site, analysis, timedelta(seconds=request.retry_window_s))
        return self._result


@dataclass(frozen=True)
class Analysis:
    """The analysis stages' outputs, kept between retries (workflow state only, never serialised)."""

    grid: GridOutput
    site_land: SiteLandOutput
    market: MarketOutput
    sentiment: SentimentOutput
    financial: FinancialOutput
    planning: PlanningOutput

    def artifacts(self) -> list[Artifact]:
        """Every analysis stage's artifacts, in pipeline order."""
        outputs = (self.grid, self.site_land, self.market, self.sentiment, self.financial, self.planning)
        return [a for out in outputs for a in out.artifacts]

    def gaps(self) -> list[DataGap]:
        """The evidence stages' data gaps."""
        return [g for out in (self.grid, self.site_land, self.market, self.sentiment) for g in out.gaps]


async def _kept[T](value: T) -> T:  # ruff: ignore[unused-async] - gathered alongside activities
    """A stage output carried over from the previous pass, as an awaitable to gather with fresh ones."""
    return value
