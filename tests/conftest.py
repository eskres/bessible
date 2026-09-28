from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from pydantic_ai.models.function import AgentInfo, FunctionModel
from temporalio import activity

from bessible.config import settings
from bessible.credentials import encrypt_google_key
from bessible.location import Agentic, Coordinates, Deterministic, Locality, LocationData
from bessible.market.sources import STREAM_NAMES, FixtureSource
from bessible.planning.route import LpaLookup
from bessible.ukpn.snapshot import load_snapshot

UKPN_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ukpn"

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic_ai.messages import ModelMessage, ModelResponse

    from bessible.models import EncryptedCredentials


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - every stage test must stay offline
def offline_lpa_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep stage tests off the network; route tests exercise lookup_lpa with a mock transport."""

    async def fake(*_args: object, **_kwargs: object) -> LpaLookup:
        return LpaLookup(entity=626002, reference="E60000002", name="Darlington LPA")

    monkeypatch.setattr("bessible.stages.planning.lookup_lpa", fake)


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - every stage test must stay offline
def offline_capacity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Out-of-area capacity keeps the snapshot result instead of calling live DNO APIs."""
    monkeypatch.setattr(settings, "live_capacity", False)


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - every stage test must stay offline
def offline_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Routes API key, so cable routes fall back to straight lines; route tests pass a mock client."""
    monkeypatch.setattr(settings, "google_routes_api_key", None)


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - every stage test must stay offline
def offline_queue_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    """The grid stage finds no queue dates instead of calling UKPN and NESO; test_queue_dates uses a mock client."""

    async def none(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("bessible.stages.grid.queue_timescale", none)


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - every stage test must stay offline
def offline_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """The market stage serves the committed fixtures instead of calling Elexon and NESO."""
    monkeypatch.setattr(
        "bessible.stages.market.default_sources", lambda: [FixtureSource(name) for name in STREAM_NAMES]
    )


TITLE_FIXTURE = Path(__file__).parent / "api" / "fixtures" / "planning_data_title_boundary_dorking_60m.geojson"


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - every stage test must stay offline
def offline_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The title stage's polygon search answers with a real saved planning.data response (Dorking, 60 m)."""
    from bessible.api import planning_data

    async def fake(*_args: object, **_kwargs: object) -> tuple[planning_data.EntityGeoJsonResponse, bool]:
        return planning_data.EntityGeoJsonResponse.model_validate_json(TITLE_FIXTURE.read_text()), False

    monkeypatch.setattr("bessible.titles.search.search_titles", fake)


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - every stage test must stay offline
def offline_news(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No Tavily key, an empty cache and no recordings; the sentiment stage's place names come from a fake lookup."""
    monkeypatch.setattr(settings, "tavily_api_key", None)
    monkeypatch.setattr(settings, "hmlr_api_key", None)  # the title stage skips CCOD / OCOD
    monkeypatch.setattr(settings, "cache_dir", tmp_path / "cache")
    monkeypatch.setattr("bessible.suitability.research.RECORDED_DIR", tmp_path / "recorded")
    monkeypatch.setattr("bessible.suitability.stored.RECORDED_ROOT", tmp_path / "recorded")

    async def fake(coords: Coordinates, **_kwargs: object) -> LocationData:
        where = Locality(place="Dorking", district="Mole Valley", planning_authority="Mole Valley", county="Surrey")
        return LocationData(
            coords=coords,
            deterministic=Deterministic(locality=where),
            agentic=Agentic(search_terms=["Dorking", "Mole Valley", "Surrey"]),
        )

    monkeypatch.setattr("bessible.stages.sentiment.locality", fake)


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - tests pin values from the fixture snapshot
def ukpn_fixture_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every `get_snapshot` importer at the small fixture snapshot, not the full one in data/ukpn."""
    snapshot = load_snapshot(UKPN_FIXTURE_DIR)
    for module in ("bessible.stages.capacity", "bessible.stages.grid", "bessible.api.capacity", "bessible.cli"):
        monkeypatch.setattr(f"{module}.get_snapshot", lambda: snapshot)


class FakeGemini:
    """Stands in for `bessible.llm.gemini_model`: no network, and it records which key built each model."""

    def __init__(self) -> None:
        self.built: list[tuple[str | None, str]] = []  # (workflow id, api key), one per model built

    def __call__(self, api_key: str) -> FunctionModel:
        try:
            workflow_id: str | None = activity.info().workflow_id
        except RuntimeError:  # called outside an activity
            workflow_id = None
        self.built.append((workflow_id, api_key))

        def offline(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            msg = "offline"
            raise RuntimeError(msg)  # every agent then takes its deterministic fallback

        return FunctionModel(offline)


@pytest.fixture
def fake_gemini(monkeypatch: pytest.MonkeyPatch) -> FakeGemini:
    fake = FakeGemini()
    monkeypatch.setattr("bessible.llm.gemini_model", fake)
    return fake


@pytest.fixture
def seal(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str], EncryptedCredentials]:
    """`seal(uid, api_key)` with a test master secret, as the API would."""
    monkeypatch.setattr(settings, "key_encryption_secret", SecretStr("test-master-secret"))
    return encrypt_google_key


@pytest.fixture
def run_credentials(seal: Callable[[str, str], EncryptedCredentials]) -> EncryptedCredentials:
    return seal("test-user", "test-google-key")
