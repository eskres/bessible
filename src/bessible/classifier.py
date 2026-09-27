"""Typed classification, driven by Pydantic models, on one of three switchable backends.

Each field of the schema becomes one question: `Literal[...]` → pick one option, `bool` → yes/no.
The field description is the question. Every answer comes back with a confidence.

    class ParagraphLabels(BaseModel):
        kind: Literal["planning policy", "local news", "other"] = Field(description="What kind of text is this?")
        mentions_risk: bool = Field(description="The text mentions a risk for a battery storage project.")

    results = await classify(paragraphs, ParagraphLabels, model=gemini)
    results[0].labels.kind, results[0].confidence["kind"], results[0].model

Backends, tried in this order (`CLASSIFIER_BACKEND` picks where the chain starts; a backend error steps down):

- `modal`: open-jev DeBERTa on Modal (see sandbox/modal_classifier.py). Operator-enabled: only when the worker has a
  Modal token. An independent second opinion next to Gemini, with calibrated confidences.
- `llm`: structured output from the `model` the caller passes (the run owner's Gemini). Confidences are self-reported.
- `heuristic`: offline keyword rules, `ParagraphLabels` only. The last resort for news paragraphs.

`backends=` restricts the chain, e.g. `("modal",)` for a check that must not be done by the model under test.
`import modal` stays inside the Modal backend so the app starts without the optional dependency.
"""

from __future__ import annotations

import logging
from importlib.util import find_spec
from typing import TYPE_CHECKING, Any, Literal, cast, get_args, get_origin

from pydantic import BaseModel, Field, create_model
from pydantic_ai import Agent, ModelRetry

from bessible.config import settings
from bessible.suitability.labels import NO_CONCERN, ParagraphLabels

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic_ai.models import Model

logger = logging.getLogger(__name__)

type Backend = Literal["modal", "llm", "heuristic"]
BACKEND_ORDER: tuple[Backend, ...] = get_args(Backend.__value__)

MODAL_APP = "bessible-classifier"
MODAL_NAME = "open-jev-deberta-v3-large (Modal)"
HEURISTIC_NAME = "keyword heuristic"
LLM_BATCH_SIZE = 25


class ClassifierError(RuntimeError):
    """No backend could classify the text (all failed, or none was available)."""


class Classified[T: BaseModel](BaseModel):
    """One paragraph with its validated labels, a confidence (0-1) per field and the backend model that made them."""

    text: str
    labels: T
    confidence: dict[str, float]
    model: str = MODAL_NAME


def _questions(schema: type[BaseModel]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for name, field in schema.model_fields.items():
        instructions = field.description or name.replace("_", " ")
        if field.annotation is bool:
            questions.append({"type": "noul", "instructions": instructions})
        elif get_origin(field.annotation) is Literal:
            options = list(get_args(field.annotation))
            questions.append({"type": "choice", "instructions": instructions, "options": options})
        else:
            msg = f"{schema.__name__}.{name}: only Literal[...] and bool fields are supported"
            raise TypeError(msg)
    return questions


# ------------------------------------------------ modal ----------------------------------------------- #


def modal_enabled() -> bool:
    """True when the `modal` package is installed and the worker has a Modal token (env/.env or `modal setup`)."""
    if find_spec("modal") is None:
        return False
    if settings.modal_token_id and settings.modal_token_secret:
        return True
    from modal.config import config  # ruff: ignore[import-outside-top-level]

    return bool(config.get("token_id"))


async def _classify_modal[T: BaseModel](paragraphs: list[str], schema: type[T]) -> list[Classified[T]]:
    import modal  # ruff: ignore[import-outside-top-level] - optional dependency

    client = None
    if settings.modal_token_id and settings.modal_token_secret:  # .env is not loaded into os.environ
        client = await modal.Client.from_credentials.aio(
            settings.modal_token_id.get_secret_value(), settings.modal_token_secret.get_secret_value()
        )
    classifier = modal.Cls.from_name(MODAL_APP, "Classifier", client=client)()
    raw = await classifier.classify.remote.aio(paragraphs, _questions(schema))

    results: list[Classified[T]] = []
    for text, answers in zip(paragraphs, raw, strict=True):
        values: dict[str, Any] = {}
        confidence: dict[str, float] = {}
        for name, answer in zip(schema.model_fields, answers, strict=True):
            if "noul" in answer:
                p_yes = answer["noul"]
                values[name], confidence[name] = p_yes >= 0.5, max(p_yes, 1 - p_yes)  # ruff: ignore[magic-value-comparison]
            else:
                values[name], confidence[name] = answer["choice"], answer["confidence"]
        results.append(
            Classified[T](text=text, labels=schema.model_validate(values), confidence=confidence, model=MODAL_NAME)
        )
    return results


# ------------------------------------------------- llm ------------------------------------------------ #

LLM_INSTRUCTIONS = """\
You label numbered paragraphs. For every paragraph, answer each question in the schema from that paragraph alone.
Return one row per paragraph, with its `index` as given. `confidence` is your probability (0 to 1) that each answer
is right. Do not skip or merge paragraphs.
"""


def _batch_model(schema: type[BaseModel]) -> type[BaseModel]:
    """`{rows: [{index, labels: schema, confidence: {<field>: float}}]}` built from the schema."""
    confidence = create_model(  # type: ignore[call-overload]
        f"{schema.__name__}Confidence",
        **{name: (float, Field(ge=0, le=1, description=f"Confidence in `{name}`.")) for name in schema.model_fields},
    )
    row = create_model(
        f"{schema.__name__}Row",
        index=(int, Field(description="The paragraph number as given.")),
        labels=(schema, ...),
        confidence=(confidence, ...),
    )
    return create_model(f"{schema.__name__}Batch", rows=(list[row], ...))


async def _classify_llm[T: BaseModel](
    paragraphs: list[str], schema: type[T], model: Model | None
) -> list[Classified[T]]:
    if model is None:
        msg = "the llm backend needs a model"
        raise ClassifierError(msg)
    batch_model = _batch_model(schema)
    results: list[Classified[T]] = []
    for start in range(0, len(paragraphs), LLM_BATCH_SIZE):
        chunk = paragraphs[start : start + LLM_BATCH_SIZE]
        expected = list(range(len(chunk)))

        def complete(_ctx: Any, batch: Any, expected: list[int] = expected) -> Any:  # ruff: ignore[any-type]
            if sorted(row.index for row in batch.rows) != expected:
                msg = f"Return exactly one row for each paragraph number {expected[0]} to {expected[-1]}."
                raise ModelRetry(msg)
            return batch

        agent = Agent(
            name="classifier",
            output_type=batch_model,
            instructions=LLM_INSTRUCTIONS,
            defer_model_check=True,
        )
        agent.output_validator(complete)
        prompt = "\n\n".join(f"[{i}] {text}" for i, text in enumerate(chunk))
        run = await agent.run(prompt, model=model)
        rows: list[Any] = sorted(cast("Any", run.output).rows, key=lambda r: r.index)
        results.extend(
            Classified[T](
                text=chunk[row.index],
                labels=cast("T", row.labels),
                confidence=row.confidence.model_dump(),
                model=model.model_name,
            )
            for row in rows
        )
    return results


# ---------------------------------------------- heuristic --------------------------------------------- #


def _classify_heuristic[T: BaseModel](paragraphs: list[str], schema: type[T]) -> list[Classified[T]]:
    """Offline keyword rules for local-news paragraphs. Only `ParagraphLabels` is supported."""
    if schema is not ParagraphLabels:
        msg = f"the heuristic backend only labels {ParagraphLabels.__name__}, not {schema.__name__}"
        raise ClassifierError(msg)
    results: list[Classified[T]] = []
    for p in paragraphs:
        low = p.lower()
        is_relevant = any(w in low for w in ("battery", "bess", "solar", "substation", "storage", "energy"))
        is_against = any(w in low for w in ("object", "concern", "oppose", "fire", "danger", "noise", "traffic"))
        is_support = any(w in low for w in ("support", "welcome", "approve", "green", "net zero", "essential"))

        stance = "against" if is_against else ("supportive" if is_support else "neutral")
        concern = (
            (
                "fire safety"
                if "fire" in low
                else ("noise" if "noise" in low else ("traffic" if "traffic" in low else "land use"))
            )
            if is_against
            else NO_CONCERN
        )
        labels = ParagraphLabels(
            relevant=is_relevant,
            stance=stance,
            concern=concern,
            mentions_risk="fire" in low or "danger" in low or "runaway" in low,
        )
        results.append(
            Classified[T](
                text=p,
                labels=cast("T", labels),
                confidence={"relevant": 0.85, "stance": 0.80, "concern": 0.70, "mentions_risk": 0.75},
                model=HEURISTIC_NAME,
            )
        )
    return results


# ---------------------------------------------- dispatch --------------------------------------------- #


def _available[T: BaseModel](backend: Backend, schema: type[T], model: Model | None) -> bool:
    if backend == "modal":
        return modal_enabled()
    if backend == "llm":
        return model is not None
    return schema is ParagraphLabels


def chain(backends: Sequence[Backend] | None = None) -> list[Backend]:
    """The backends to try, in order: from `CLASSIFIER_BACKEND` down, limited to `backends` if given."""
    forced = settings.classifier_backend
    start = 0 if forced == "auto" else BACKEND_ORDER.index(forced)
    return [b for b in BACKEND_ORDER[start:] if backends is None or b in backends]


async def classify[T: BaseModel](
    paragraphs: list[str],
    schema: type[T],
    *,
    model: Model | None = None,
    backends: Sequence[Backend] | None = None,
) -> list[Classified[T]]:
    """Ask every field of `schema` about every paragraph.

    `model` is the run owner's model for the `llm` backend. `backends` limits which backends may answer.
    Raises `ClassifierError` when none is available or all of them fail.
    """
    if not paragraphs:
        return []
    last_error: Exception | None = None
    tried: list[Backend] = []
    for backend in chain(backends):
        if not _available(backend, schema, model):
            logger.debug("classifier backend %s is not available; skipping", backend)
            continue
        tried.append(backend)
        try:
            if backend == "modal":
                return await _classify_modal(paragraphs, schema)
            if backend == "llm":
                return await _classify_llm(paragraphs, schema, model)
            return _classify_heuristic(paragraphs, schema)
        except Exception as exc:  # ruff: ignore[blind-except]
            logger.warning("classifier backend %s failed, stepping down: %s: %s", backend, type(exc).__name__, exc)
            last_error = exc
    msg = f"no classifier backend could answer (tried {tried or 'none'})"
    raise ClassifierError(msg) from last_error
