"""The switchable classifier: modal | llm | heuristic, fallback order, and running without `modal` installed."""

from __future__ import annotations

import subprocess
import sys
from typing import Literal

import pytest
from pydantic import BaseModel, Field
from pydantic_ai.models.test import TestModel

from bessible import classifier
from bessible.classifier import ClassifierError, classify
from bessible.suitability.labels import ParagraphLabels

TEXTS = ["Residents object to the battery fire risk.", "Council welcomes the green energy scheme."]


class Tone(BaseModel):
    tone: Literal["angry", "calm"] = Field(description="How does the text feel?")
    about_energy: bool = Field(description="The text is about energy.")


@pytest.fixture(autouse=True)  # ruff: ignore[pytest-fixture-autouse] - never let a .env override the chain
def default_settings(monkeypatch):
    monkeypatch.setattr(classifier.settings, "classifier_backend", "auto")


class FakeModalClassifier:
    """Stands in for the deployed Modal class: answers `choice` and `noul` per question."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.classify = self
        self.remote = self
        self.calls = 0

    async def aio(self, paragraphs, questions):
        self.calls += 1
        if self.fail:
            msg = "modal is down"
            raise RuntimeError(msg)
        answers = {"choice": {"choice": "angry", "confidence": 0.9}, "noul": {"noul": 0.2}}
        return [[answers[q["type"]] for q in questions] for _ in paragraphs]


def enable_modal(monkeypatch, fake: FakeModalClassifier) -> None:
    import modal

    monkeypatch.setattr(classifier, "modal_enabled", lambda: True)
    monkeypatch.setattr(modal.Cls, "from_name", lambda *_a, **_k: lambda: fake)


def disable_modal(monkeypatch) -> None:
    monkeypatch.setattr(classifier, "modal_enabled", lambda: False)


def llm_model(rows: list[dict] | None = None) -> TestModel:
    rows = rows or [
        {"index": i, "labels": {"tone": "calm", "about_energy": True}, "confidence": {"tone": 0.7, "about_energy": 0.6}}
        for i in range(len(TEXTS))
    ]
    return TestModel(custom_output_args={"rows": rows})


def test_questions_cover_literal_and_bool_fields():
    assert classifier._questions(Tone) == [  # ruff: ignore[private-member-access]
        {"type": "choice", "instructions": "How does the text feel?", "options": ["angry", "calm"]},
        {"type": "noul", "instructions": "The text is about energy."},
    ]


@pytest.mark.anyio
async def test_modal_backend_when_the_operator_enabled_it(monkeypatch):
    fake = FakeModalClassifier()
    enable_modal(monkeypatch, fake)
    results = await classify(TEXTS, Tone, model=llm_model())  # a model is present, but Modal comes first
    assert [r.labels for r in results] == [Tone(tone="angry", about_energy=False)] * 2
    assert results[0].confidence == {"tone": 0.9, "about_energy": 0.8}
    assert results[0].model == classifier.MODAL_NAME
    assert fake.calls == 1


@pytest.mark.anyio
async def test_llm_backend_without_modal(monkeypatch):
    disable_modal(monkeypatch)
    results = await classify(TEXTS, Tone, model=llm_model())
    assert [(r.text, r.labels.tone, r.confidence["tone"]) for r in results] == [(t, "calm", 0.7) for t in TEXTS]
    assert results[0].model == "test"


@pytest.mark.anyio
async def test_llm_backend_reorders_rows_by_index(monkeypatch):
    disable_modal(monkeypatch)
    rows = [
        {
            "index": 1,
            "labels": {"tone": "calm", "about_energy": True},
            "confidence": {"tone": 0.7, "about_energy": 0.6},
        },
        {
            "index": 0,
            "labels": {"tone": "angry", "about_energy": True},
            "confidence": {"tone": 0.8, "about_energy": 0.6},
        },
    ]
    results = await classify(TEXTS, Tone, model=llm_model(rows))
    assert [(r.text, r.labels.tone) for r in results] == [(TEXTS[0], "angry"), (TEXTS[1], "calm")]


@pytest.mark.anyio
async def test_modal_error_falls_back_to_llm(monkeypatch):
    fake = FakeModalClassifier(fail=True)
    enable_modal(monkeypatch, fake)
    results = await classify(TEXTS, Tone, model=llm_model())
    assert fake.calls == 1
    assert results[0].model == "test"


@pytest.mark.anyio
async def test_all_model_backends_failing_falls_back_to_the_heuristic(monkeypatch):
    enable_modal(monkeypatch, FakeModalClassifier(fail=True))
    wrong_rows = [
        {"index": 0, "labels": {"tone": "calm", "about_energy": True}, "confidence": {"tone": 1, "about_energy": 1}}
    ]
    results = await classify(TEXTS, ParagraphLabels, model=llm_model(wrong_rows))  # one row for two paragraphs
    assert {r.model for r in results} == {classifier.HEURISTIC_NAME}


@pytest.mark.anyio
async def test_heuristic_labels_and_confidences_are_unchanged(monkeypatch):
    disable_modal(monkeypatch)
    results = await classify(TEXTS, ParagraphLabels)  # no Modal, no model
    first, second = results
    assert first.labels == ParagraphLabels(relevant=True, stance="against", concern="fire safety", mentions_risk=True)
    assert second.labels == ParagraphLabels(relevant=True, stance="supportive", concern="no concern raised", mentions_risk=False)
    assert first.confidence == {"relevant": 0.85, "stance": 0.80, "concern": 0.70, "mentions_risk": 0.75}


@pytest.mark.anyio
async def test_heuristic_covers_news_paragraphs_only(monkeypatch):
    disable_modal(monkeypatch)
    with pytest.raises(ClassifierError):
        await classify(TEXTS, Tone)


@pytest.mark.anyio
async def test_backends_limit_the_chain(monkeypatch):
    disable_modal(monkeypatch)
    with pytest.raises(ClassifierError):
        await classify(TEXTS, ParagraphLabels, model=llm_model(), backends=("modal",))


@pytest.mark.anyio
async def test_forced_backend_starts_the_chain_there(monkeypatch):
    fake = FakeModalClassifier()
    enable_modal(monkeypatch, fake)
    monkeypatch.setattr(classifier.settings, "classifier_backend", "llm")
    results = await classify(TEXTS, Tone, model=llm_model())
    assert (fake.calls, results[0].model) == (0, "test")
    monkeypatch.setattr(classifier.settings, "classifier_backend", "heuristic")
    assert {r.model for r in await classify(TEXTS, ParagraphLabels, model=llm_model())} == {classifier.HEURISTIC_NAME}


@pytest.mark.anyio
async def test_no_paragraphs_needs_no_backend():
    assert await classify([], Tone) == []


def test_modal_is_off_without_the_package_or_a_token(monkeypatch):
    monkeypatch.setattr(classifier, "find_spec", lambda _name: None)
    assert not classifier.modal_enabled()


def test_the_app_imports_without_importing_modal():
    code = "import sys, bessible.classifier, bessible.possibility.policy, bessible.suitability.sentiment; sys.exit('modal' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
