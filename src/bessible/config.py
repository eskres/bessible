"""App configuration and settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """App config, loaded from environment variables and .env."""

    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    google_api_key: SecretStr | None = None  # the developer's own key: CLI and scripts only, never a fallback for a run
    gemini_model: str = "gemini-3.8-flash"

    pydantic_ai_gateway_api_key: SecretStr | None = None
    pydantic_ai_gateway_base_url: str = "https://gateway-eu.pydantic.dev/proxy"
    modal_gateway_route: str = "modal"
    modal_model: str = "google/gemma-4-31B-it"
    # Operator-side Modal token for the classifier. Never per user, never in workflow input.
    modal_token_id: SecretStr | None = None
    modal_token_secret: SecretStr | None = None
    classifier_backend: Literal["auto", "modal", "llm", "heuristic"] = "auto"

    logfire_token: SecretStr | None = None
    typesafe_api_key: SecretStr | None = None
    ukpn_api_key: SecretStr | None = None  # ukpowernetworks.opendatasoft.com
    ssen_api_key: SecretStr | None = None  # ssentransmission.opendatasoft.com
    nged_api_key: SecretStr | None = None  # connecteddata.nationalgrid.co.uk
    spen_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("spen_api_key", "sp_energy_api_key")
    )  # spenergynetworks.opendatasoft.com
    npg_api_key: SecretStr | None = None  # northernpowergrid.opendatasoft.com
    os_api_key: SecretStr | None = None  # osdatahub.os.uk (Ordnance Survey maps)
    # Server key for the Google Routes API (cable routes). Not the browser Maps key. Tests unset it to stay offline.
    google_routes_api_key: SecretStr | None = None
    # Operator-side Tavily key for the local news search (a data source, like UKPN_API_KEY; never per user).
    # Tests unset it to stay offline.
    tavily_api_key: SecretStr | None = None

    # Outside the bundled (Dorking-only) UKPN snapshot, look up live DNO headroom. Tests turn it off to stay offline.
    live_capacity: bool = True

    data_dir: Path = Path(__file__).resolve().parents[2] / "data"  # committed fixtures and UKPN snapshot
    cache_dir: Path = Path(__file__).resolve().parents[2] / "out" / "cache"  # live API responses; gitignored

    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"

    # Demo deployment: Firebase sign-in and encrypted per-user Google keys.
    # AUTH_ENABLED=false turns sign-in off in the API and the web UI: every caller is one local user. Local dev only.
    auth_enabled: bool = True
    firebase_project_id: str | None = Field(
        default=None, validation_alias=AliasChoices("firebase_project_id", "next_public_firebase_project_id")
    )
    allowed_emails: str | None = None  # comma-separated; empty means anyone who signs in
    key_encryption_secret: SecretStr | None = None  # master secret; root-owned EnvironmentFile on the VM
    key_encryption_key_id: str = "k1"  # id stamped on new ciphertexts
    key_encryption_previous: dict[str, SecretStr] = {}  # old key_id -> secret, kept while rotating
    key_db_path: Path = Path(__file__).resolve().parents[2] / "out" / "keys.db"  # per-user sealed Google keys


settings = Settings()
