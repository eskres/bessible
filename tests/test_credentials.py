"""The Google key is sealed for its owner and opens only for them."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from bessible.credentials import (
    InvalidCredentialsError,
    MissingGoogleKeyError,
    decrypt_google_key,
    decrypt_tavily_key,
)
from bessible.llm import run_model

if TYPE_CHECKING:
    from collections.abc import Callable

    from bessible.models import EncryptedCredentials

KEY = "AIza-test-key-1234"


def test_round_trip_and_no_plaintext(seal: Callable[[str, str], EncryptedCredentials]):
    sealed = seal("user-a", KEY)
    assert KEY not in sealed.model_dump_json()
    assert decrypt_google_key(sealed) == KEY


def test_same_key_seals_differently_each_time(seal: Callable[[str, str], EncryptedCredentials]):
    assert seal("user-a", KEY).google_ct != seal("user-a", KEY).google_ct


def test_ciphertext_copied_to_another_user_fails(seal: Callable[[str, str], EncryptedCredentials]):
    stolen = seal("user-a", KEY).model_copy(update={"uid": "user-b"})
    with pytest.raises(InvalidCredentialsError):
        decrypt_google_key(stolen)


def test_tampered_or_unknown_key_id_fails(seal: Callable[[str, str], EncryptedCredentials]):
    sealed = seal("user-a", KEY)
    with pytest.raises(InvalidCredentialsError):
        decrypt_google_key(sealed.model_copy(update={"google_ct": sealed.google_ct[:-4] + "AAAA"}))
    with pytest.raises(InvalidCredentialsError):
        decrypt_google_key(sealed.model_copy(update={"key_id": "v0"}))


def test_previous_key_id_decrypts_when_configured(
    seal: Callable[[str, str], EncryptedCredentials], monkeypatch: pytest.MonkeyPatch
):
    sealed = seal("user-a", KEY).model_copy(update={"key_id": "old-key"})
    with pytest.raises(InvalidCredentialsError):
        decrypt_google_key(sealed)
    monkeypatch.setattr(
        "bessible.config.settings.key_encryption_previous",
        {"old-key": SecretStr("test-master-secret")},
    )
    assert decrypt_google_key(sealed) == KEY


@pytest.mark.parametrize("credentials", [None, "empty"])
def test_no_key_raises_and_never_falls_back(
    credentials: str | None, seal: Callable[[str, str], EncryptedCredentials], monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("bessible.config.settings.google_api_key", SecretStr("SERVER-KEY"))
    empty = seal("user-a", KEY).model_copy(update={"google_ct": ""}) if credentials else None
    with pytest.raises(MissingGoogleKeyError):
        run_model(empty)


def test_tavily_key_is_optional_and_bound_to_its_owner(seal: Callable[[str, str], EncryptedCredentials]):
    sealed = seal("user-a", KEY)
    assert decrypt_tavily_key(sealed) is None
    assert decrypt_tavily_key(None) is None
    tavily = seal("user-a", "tvly-secret")
    with_tavily = sealed.model_copy(update={"tavily_ct": tavily.google_ct, "tavily_key_id": tavily.key_id})
    assert decrypt_tavily_key(with_tavily) == "tvly-secret"
    with pytest.raises(InvalidCredentialsError):
        decrypt_tavily_key(with_tavily.model_copy(update={"uid": "user-b"}))
