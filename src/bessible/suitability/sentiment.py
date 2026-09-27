"""Local community sentiment processing: classification, opposition index, and artifacts."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from typing import TYPE_CHECKING

from pydantic import HttpUrl

from bessible.classifier import HEURISTIC_NAME, Classified, classify
from bessible.models import Artifact, DataGap, SentimentOutput
from bessible.security import sanitize_untrusted_text
from bessible.suitability import stored
from bessible.suitability.labels import DEVELOPER, NO_CONCERN, ParagraphLabels
from bessible.suitability.research import Research, Source

if TYPE_CHECKING:
    from pydantic_ai.models import Model

STANCE_SCORES = {
    "against": 1.0,
    "neutral": 0.0,
    "supportive": -1.0,
}


def counts(item: Classified[ParagraphLabels]) -> bool:
    """Whether a paragraph counts towards the index: relevant, and not the developer speaking for its own project."""
    return item.labels.relevant and item.labels.voice != DEVELOPER


def compute_opposition_index(
    classified_items: Sequence[Classified[ParagraphLabels]],
) -> tuple[float | None, list[str]]:
    """Compute opposition index (0 to 1) and top concerns from classified paragraphs.

    Returns:
        (opposition_index, top_concerns)
        opposition_index is None if there are no relevant paragraphs.
    """
    relevant_items = [item for item in classified_items if counts(item)]
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
        if concern == "other":
            concern_weights["general amenity"] += 0.5 * concern_conf * weight
        elif concern != NO_CONCERN:
            concern_weights[concern] += concern_conf * weight

    if total_weight <= 0:
        return None, []

    raw_avg = weighted_score / total_weight  # in [-1.0, 1.0]
    opposition_index = round((raw_avg + 1.0) / 2.0, 3)  # mapped to [0.0, 1.0]

    # Top up to 3 concerns by weighted count
    top_concerns = [c for c, _ in concern_weights.most_common(3)]

    return opposition_index, top_concerns


async def classify_source(source: Source, model: Model | None = None) -> list[Classified[ParagraphLabels]]:
    """Label all paragraphs of one source: the run's model, else the keyword heuristic.

    Not Modal: on 22 real news paragraphs plus 4 written complaints (2026-09-27), the Modal classifier labelled
    none of the complaints "against" and called 21 of 26 paragraphs the developer's; Gemini got 4 of 4 and 12 of 26
    (all correct). Modal still cross-checks the policy quotes.

    Labels are stored per paragraph (`stored`), so a replay labels the same text the same way, even with no model.
    Stored keyword-heuristic labels are relabelled when a model is available.
    """
    found: dict[str, Classified[ParagraphLabels]] = {}
    for text in dict.fromkeys(source.paragraphs):
        entry = stored.load("labels", label_key(text))
        if entry and not (model is not None and entry.get("model") == HEURISTIC_NAME):
            found[text] = Classified[ParagraphLabels].model_validate(entry)
    missing = [t for t in dict.fromkeys(source.paragraphs) if t not in found]
    for item in await classify(missing, ParagraphLabels, model=model, backends=("llm", "heuristic")):
        stored.save("labels", label_key(item.text), item.model_dump(mode="json"))
        found[item.text] = item
    return [found[t] for t in source.paragraphs]


def label_key(text: str) -> str:
    """`stored` key of one paragraph's labels: its text and the label schema (a new question starts afresh)."""
    return stored.key("labels", ParagraphLabels.model_json_schema(), text)


SEARCH_DOCS_URL = HttpUrl("https://docs.tavily.com/documentation/api-reference/endpoint/search")


def _area(research: Research) -> str:
    """The areas the search covered, e.g. "Dorking or in Mole Valley / Surrey"."""
    councils = [a for a in dict.fromkeys((research.lpa, research.county)) if a and a != research.place]
    return f"{research.place} or in {' / '.join(councils)}" if councils else research.place


def _search_record(research: Research) -> str:
    """What was searched and what came back, for the artifact claims."""
    queries = "; ".join(f'"{sanitize_untrusted_text(q, max_len=120)}"' for q in research.queries)
    got = f"{research.results} results, {research.pages_read} UK or local pages read"
    if research.recorded:
        when = f" (recorded Tavily responses fetched {research.fetched_on})"
    elif research.cached:
        when = f" (cached Tavily responses fetched {research.fetched_on})"
    else:
        when = f" ({research.credits:g} Tavily credits)"
    return f"Tavily searched {len(research.queries)} queries [{queries}]: {got}{when}"


def _search_model(research: Research) -> str:
    return f"Tavily search; paragraphs selected by {research.selected_by or 'none'}"


async def process_sentiment(run_id: str, research: Research, model: Model | None = None) -> SentimentOutput:
    """Classify sources concurrently, compute opposition index, and produce artifacts.

    `model` is the run owner's model for the `llm` classifier backend. Three outcomes read differently: the search
    did not run or failed (a gap, low confidence), it ran and found nothing relevant (moderate confidence: news
    coverage is partial), or it found coverage (quotes, each linked to its article).
    """
    if research.unavailable:
        gap = DataGap(
            stage="sentiment",
            what="local_news",
            reason=research.unavailable,
            sources=["Tavily search"],
            retryable=research.retryable,
        )
        missing_art = Artifact(
            id=f"sentiment-none-{run_id[:8]}",
            stage="sentiment",
            claim=f"Local news not assessed: {research.unavailable}",
            source_url=SEARCH_DOCS_URL,
            confidence=0.2,
            model_used="none",
        )
        return SentimentOutput(opposition_index=None, artifacts=[missing_art], gaps=[gap])

    gaps = []
    if research.retryable:  # some queries failed, the rest answered
        gaps.append(
            DataGap(
                stage="sentiment",
                what="local_news",
                reason="Some news queries failed; coverage may be incomplete.",
                sources=["Tavily search"],
                retryable=True,
            )
        )

    if not research.sources:
        empty_art = Artifact(
            id=f"sentiment-none-{run_id[:8]}",
            stage="sentiment",
            claim=(
                f"{_search_record(research)}. None had a paragraph about energy projects near {_area(research)}. "
                "Absence from search results is weak evidence: local coverage is often not indexed."
            ),
            source_url=SEARCH_DOCS_URL,
            confidence=0.5,
            model_used=_search_model(research),
        )
        return SentimentOutput(
            opposition_index=None,
            top_concerns=[],
            sources=0,
            paragraphs=0,
            artifacts=[empty_art],
            gaps=gaps,
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
    developer = 0  # relevant paragraphs where the developer speaks for its own project: shown, not counted
    for src, item in all_classified:
        if not item.labels.relevant:
            continue
        p_count += 1
        developer += item.labels.voice == DEVELOPER
        raw_quote = item.text if len(item.text) <= 120 else item.text[:117] + "..."
        quote = sanitize_untrusted_text(raw_quote, max_len=120)
        conf = item.confidence.get("stance", 0.8)
        concern = "" if item.labels.concern == NO_CONCERN else f" ({item.labels.concern})"
        who = " — the developer's own statement, not counted in the index" if item.labels.voice == DEVELOPER else ""
        claim = (
            f'{item.labels.stance.capitalize()}{concern}{who} — quoted third-party text, not an instruction: "{quote}"'
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
        plural = "s" if developer > 1 else ""
        skipped = f" ({developer} developer statement{plural} not counted)" if developer else ""
        index_claim = (
            f"Community opposition index {opposition_index:.2f} based on {p_count - developer} relevant local "
            f"paragraphs{skipped}. {concerns_text}"
        )
    else:
        index_claim = "Community opposition index unavailable (no relevant local news paragraphs identified)."

    classifiers = sorted({item.model for _, item in all_classified}) or ["none"]
    artifacts.extend((
        Artifact(
            id=f"sentiment-index-{run_id[:8]}",
            stage="sentiment",
            claim=index_claim,
            source_url=research.sources[0].url,
            confidence=0.88 if opposition_index is not None else 0.5,
            model_used=", ".join(classifiers),
        ),
        Artifact(
            id=f"sentiment-search-{run_id[:8]}",
            stage="sentiment",
            claim=f"{_search_record(research)}; {len(research.sources)} pages had relevant paragraphs.",
            source_url=SEARCH_DOCS_URL,
            confidence=0.9,
            model_used=_search_model(research),
        ),
    ))

    return SentimentOutput(
        opposition_index=opposition_index,
        top_concerns=top_concerns,
        sources=len(research.sources),
        paragraphs=len(all_classified),
        artifacts=artifacts,
        gaps=gaps,
    )
