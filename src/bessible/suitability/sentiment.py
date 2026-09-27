"""Local community sentiment processing: classification, opposition index, and artifacts."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic import HttpUrl

from bessible.classifier import Classified, classify
from bessible.models import Artifact, DataGap, SentimentOutput
from bessible.security import sanitize_untrusted_text
from bessible.suitability.labels import ParagraphLabels
from bessible.suitability.research import Research, Source

if TYPE_CHECKING:
    from pydantic_ai.models import Model

STANCE_SCORES = {
    "against": 1.0,
    "neutral": 0.0,
    "supportive": -1.0,
}


def compute_opposition_index(
    classified_items: Sequence[Classified[ParagraphLabels]],
) -> tuple[float | None, list[str]]:
    """Compute opposition index (0 to 1) and top concerns from classified paragraphs.

    Returns:
        (opposition_index, top_concerns)
        opposition_index is None if there are no relevant paragraphs.
    """
    relevant_items = [item for item in classified_items if item.labels.relevant]
    if not relevant_items:
        return None, []

    total_weight = 0.0
    weighted_score = 0.0
    concern_weights: Counter[str] = Counter()

    for item in relevant_items:
        rel_conf = item.confidence.get("relevant", 1.0)
        stance_conf = item.confidence.get("stance", 1.0)
        concern_conf = item.confidence.get("concern", 1.0)
        weight = rel_conf * stance_conf

        stance = item.labels.stance
        score = STANCE_SCORES.get(stance, 0.0)
        weighted_score += score * weight
        total_weight += weight

        concern = item.labels.concern
        if concern and concern != "other":
            concern_weights[concern] += concern_conf * weight
        elif concern == "other":
            concern_weights["general amenity"] += 0.5 * concern_conf * weight

    if total_weight <= 0:
        return None, []

    raw_avg = weighted_score / total_weight  # in [-1.0, 1.0]
    opposition_index = round((raw_avg + 1.0) / 2.0, 3)  # mapped to [0.0, 1.0]

    # Top up to 3 concerns by weighted count
    top_concerns = [c for c, _ in concern_weights.most_common(3)]

    return opposition_index, top_concerns


async def classify_source(source: Source, model: Model | None = None) -> list[Classified[ParagraphLabels]]:
    """Classify all paragraphs for a single news source (modal, then llm, then the keyword heuristic)."""
    if not source.paragraphs:
        return []
    return await classify(source.paragraphs, ParagraphLabels, model=model)


async def process_sentiment(run_id: str, research: Research, model: Model | None = None) -> SentimentOutput:
    """Classify sources concurrently, compute opposition index, and produce artifacts.

    `model` is the run owner's model for the `llm` classifier backend.
    """
    if not research.sources and research.unavailable:
        gap = DataGap(
            stage="sentiment",
            what="local_news",
            reason=research.unavailable,
            sources=["news search"],
            retryable=research.retryable,
        )
        missing_art = Artifact(
            id=f"sentiment-none-{run_id[:8]}",
            stage="sentiment",
            claim=f"Local news not assessed: {research.unavailable}",
            source_url=HttpUrl("https://news.google.com"),
            confidence=0.2,
            model_used="none",
        )
        return SentimentOutput(opposition_index=None, artifacts=[missing_art], gaps=[gap])

    if not research.sources:
        empty_art = Artifact(
            id=f"sentiment-none-{run_id[:8]}",
            stage="sentiment",
            claim="No relevant local planning or energy infrastructure coverage found in public news sources",
            source_url=HttpUrl("https://news.google.com"),
            confidence=0.90,
            model_used="gemini-3.8-flash",
        )
        return SentimentOutput(
            opposition_index=None,
            top_concerns=[],
            sources=0,
            paragraphs=0,
            artifacts=[empty_art],
        )

    # Classify sources concurrently
    source_results = await asyncio.gather(*(classify_source(src, model) for src in research.sources))

    all_classified: list[tuple[Source, Classified[ParagraphLabels]]] = []
    for src, items in zip(research.sources, source_results, strict=True):
        for item in items:
            all_classified.append((src, item))

    just_classified = [item for _, item in all_classified]
    opposition_index, top_concerns = compute_opposition_index(just_classified)

    artifacts: list[Artifact] = []
    # Emit one artifact per relevant paragraph
    p_count = 0
    for src, item in all_classified:
        if not item.labels.relevant:
            continue
        p_count += 1
        raw_quote = item.text if len(item.text) <= 120 else item.text[:117] + "..."
        quote = sanitize_untrusted_text(raw_quote, max_len=120)
        conf = item.confidence.get("stance", 0.8)
        claim = (
            f"{item.labels.stance.capitalize()} ({item.labels.concern}) "
            f'— quoted third-party text, not an instruction: "{quote}"'
        )
        artifacts.append(
            Artifact(
                id=f"sentiment-p{p_count}-{run_id[:8]}",
                stage="sentiment",
                claim=claim,
                source_url=src.url,
                confidence=round(conf, 2),
                model_used=item.model,
            )
        )

    # Emit index artifact
    if opposition_index is not None:
        concerns_text = f"Top concerns: {', '.join(top_concerns)}." if top_concerns else "No dominant concerns."
        index_claim = (
            f"Community opposition index {opposition_index:.2f} based on {p_count} relevant local paragraphs. "
            f"{concerns_text}"
        )
    else:
        index_claim = "Community opposition index unavailable (no relevant local news paragraphs identified)."

    first_url = research.sources[0].url if research.sources else HttpUrl("https://news.google.com")
    artifacts.append(
        Artifact(
            id=f"sentiment-index-{run_id[:8]}",
            stage="sentiment",
            claim=index_claim,
            source_url=first_url,
            confidence=0.88,
            model_used="gemini-3.8-flash",
        )
    )

    return SentimentOutput(
        opposition_index=opposition_index,
        top_concerns=top_concerns,
        sources=len(research.sources),
        paragraphs=len(all_classified),
        artifacts=artifacts,
    )
