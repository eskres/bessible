## Context

See proposal.md for the why. Builds on `add-temporal-pipeline-skeleton`: its models (`NodeInput`, `MarketOutput`, `FinancialInput`, `FinancialOutput`, `DurationCase`, `SynthesisInput`, `ReportOutput`, `Artifact`), its stage-function contract (plain async functions in `stages/`, thin activity wrappers), and the Pydantic data converter. Existing pieces reused:

- `bessible.llm.gemini_model()`: gemini-3.8-flash through `GoogleProvider`. Keys come from `settings`; `.env` is not in `os.environ`, so never build a model from a string like `"google:..."`.
- `bessible.classifier.classify(paragraphs, Schema)`: the Modal app `bessible-classifier` (open-jev DeBERTa, L4). `Literal` fields become choice questions and `bool` fields yes/no. Returns `list[Classified[T]]` with `.labels` and `.confidence[field]`. Reads about 256 tokens per paragraph. Cold start is about 20 s and warm calls about 0.8 s.

## Goals / Non-Goals

**Goals:** the agents are visibly agentic (search, tool calls, a recommendation) while every number and the verdict stay deterministic. Each stage can be built and tested alone from fixtures.

**Non-Goals:** agents that pick the verdict or compute figures; multi-agent chat.

## Decisions

### Workflow placement

```
confirm ─┬─ grid_connection (placeholder) ─┐
         ├─ site_land       (placeholder) ─┤
         ├─ market_revenue                 ─┼─ financial_model ─┐
         └─ local_sentiment               ─┘  regulatory_planning (placeholder) ─┴─ synthesise
```

The skeleton gains `"sentiment"` in `Stage` and a `local_sentiment` activity in the first parallel group. `SynthesisInput` gains `sentiment: SentimentOutput`. Both are additions made in the skeleton change by Josh.

### Durable agents / Activity execution

Each agent is a Pydantic AI `Agent` executed inside its respective stage activity (`local_sentiment`, `financial_model`). Each stage runs as a discrete Temporal activity with configured retry policies and timeouts (`AGENT_OPTS`), cleanly encapsulating local caching (news fixtures), data transformations, tool calls, and model invocations.

*Note on TemporalAgent:* An earlier draft considered wrapping agents in `TemporalAgent` to register individual model and tool calls as separate Temporal activities. In practice, running coarse-grained stage activities keeps workflow orchestration pure, avoids splitting fixture caching and I/O across micro-activities, and avoids reliance on the deprecated `TemporalAgent` wrapper.

### Layout

```
src/bessible/suitability/
  labels.py      # classifier schemas, shared with feasibility (Jack) for planning/grid paragraphs
  research.py    # news research agent (Gemini + Google Search)
  sentiment.py   # split, classify, aggregate
  assumptions.py # load + validate data/assumptions/finance.json
  finance.py     # pure calculations
  analyst.py     # financial analyst agent (tools = finance.py)
  verdict.py     # rules + narration + guard
src/bessible/stages/market.py, financial.py, synthesis.py   # replaced
src/bessible/stages/sentiment.py                            # new
```

### Local sentiment

```python
class Source(BaseModel):
    url: HttpUrl; title: str; published: date | None; paragraphs: list[str]   # each paragraph <= ~200 words
class Research(BaseModel):
    place: str; lpa: str | None; county: str | None; sources: list[Source]   # + search record: queries, credits, ...

class ParagraphLabels(BaseModel):            # labels.py; field descriptions are the classifier's questions
    relevant: bool = Field(description="The text is about an energy project or infrastructure near a local community.")
    voice: Literal["residents, campaigners or councillors", "the developer or its consultants",
                   "a reporter or official stating facts"] = Field(description="Whose view does the text give?")
    stance: Literal["against", "neutral", "supportive"] = Field(
        description="Does the text express or report opposition to the project, support for it, or neither?")
    concern: Literal["no concern raised", "fire safety", "noise", "visual impact", "traffic", "land use", "ecology",
                     "heritage", "consultation or process", "other"] = Field(
        description="Which worry or objection about the project does the text raise? Choose 'no concern raised' if it raises none.")
    mentions_risk: bool = Field(description="The text mentions a risk for a battery storage project.")

# Sentiment paragraphs are labelled by the run's model (Gemini), else keyword rules; not Modal. On 22 real paragraphs
# plus 4 written complaints (2026-09-27) Modal labelled 0 of 4 complaints "against" and its answers changed with the
# other questions in the same call. Research: Tavily Search (4 scoped queries) + Extract for UK results without text.

class SentimentOutput(BaseModel):
    opposition_index: float | None           # 0-1, None = unknown
    top_concerns: list[str]                  # up to 3
    sources: int; paragraphs: int
    artifacts: list[Artifact]

async def local_sentiment(inp: NodeInput) -> SentimentOutput
```

- **Research agent:** Gemini with the Google Search native tool (`WebSearchTool`). Output type `Research`. The prompt gives the place name, the LPA if known, and the query themes (battery storage, solar farm, substation, planning objection). The agent returns paragraphs, not whole pages, which keeps within the classifier's 256-token limit and avoids a separate scraper. How the native tool attaches to the agent is confirmed in task 1.1.
- **Cache:** every Tavily response is cached under `out/cache/tavily/` for 7 days, keyed by a hash of the request body. Committed recordings under `data/recorded/tavily/` (same key) serve the offline demo; `scripts/news_research.py --record` writes them.
- **Index:** `against = +1, neutral = 0, supportive = -1`, weighted by stance confidence × relevance confidence, averaged, then mapped from [-1, 1] to [0, 1]. Top concerns are the weighted counts over relevant paragraphs.
- **Classification:** one `classify()` call per source (a batch of its paragraphs). Sources run concurrently with `asyncio.gather`.

**Why Gemini for labels (updated 2026-09-27):** the plan was Modal for labels, for calibrated confidence. Measured on real coverage, Modal missed implied objections and its answers changed with the other questions asked in the same call, so the run's model labels sentiment and Modal cross-checks policy quotes. Tavily finds and fetches the coverage; the model only selects verbatim paragraphs.

### Market revenue (simplified)

`market_revenue` reads `revenue_gbp_per_mw_year` per duration from `finance.json` (sourced public ranges) and fills `MarketOutput.revenue_gbp_per_mw_year` (4 h) plus an optional `by_duration: dict[int, Range]`. No live source.

### Financial model

```python
class Range(BaseModel):  low: float; mid: float; high: float
class Assumption(BaseModel):  value: Range; unit: str; source: str; date: date

class CaseResult(BaseModel):
    duration_h: Literal[2, 4, 8]
    capex_gbp: Range; opex_gbp_year: Range; revenue_gbp_year: Range
    npv_gbp: Range; irr: Range | None; payback_years: Range | None
    over_budget: bool; curtailment_pct: float

def evaluate(mw: float, duration_h: int, distance_km: float | None, firm_mw: float,
             budget_gbp: float | None, a: Assumptions) -> CaseResult        # pure, < 10 ms
```

- **CAPEX** = battery £/MWh × MW × h + balance of plant £/MW × MW + 33 kV cable £/km × distance.
- **OPEX:** £/MW/year.
- **Revenue:** £/MW/year × MW × availability × (1 − curtailment).
- **Curtailment** = `max(0, (mw - firm_mw) / mw) × haircut`, with the haircut an assumption.
- **Financing:** debt share × CAPEX as an amortising loan at the interest rate, plus the arrangement fee in year 0.
- **NPV** at the discount rate over the project life. **IRR** by bisection on equity cash flows.
- **Low / mid / high:** evaluated separately by pairing the pessimistic ends (high cost with low revenue for "low", and so on).

`DurationCase` (skeleton) gains optional `low`, `high`, `payback_years`, `over_budget`. `FinancialOutput` gains `recommended_h` and `rationale`.

**Analyst agent:** Gemini, `output_type=Recommendation(duration_h, rationale, compared: list[int])`, and one tool, `evaluate_case(duration_h)`, that calls `evaluate` with the run's inputs. The stage computes all three cases in code first, whatever the agent does, so the table never depends on the model. The agent's job is the comparison and the reasoning. The guard (below) checks its rationale.

### Verdict

```python
HURDLE_IRR = 0.08; OPPOSITION_MAYBE = 0.5; OPPOSITION_NO = 0.8        # in finance.json, echoed in the report
def decide(fin: FinancialOutput, sent: SentimentOutput) -> tuple[Verdict, list[str]]   # verdict + rule lines
```

- **no_go:** mid IRR is none or below half the hurdle, or opposition ≥ 0.8.
- **maybe:** below the hurdle, over budget, or opposition ≥ 0.5.
- **go:** otherwise.

Gemini writes the findings from the typed values with `output_type=list[Finding]`, where every `Finding` must cite ids.

**Guard:** a regex pulls every number with `£`, `%`, `MW`, `MWh`, `km` or `years`, plus bare numbers, from each finding and checks it against a flattened dict of every computed value, allowing 5% relative tolerance. A finding that fails gets one rewrite request naming the bad numbers, then falls back to a template built from the typed values. This is the same guard idea as `add-financial-model` F4.

## Risks / Trade-offs

- [Google Search grounding untested with this key and model] → Task 1.1 is a 30-minute spike. The fallback is fixtures written by hand for the demo sites, with the agent then only extracting paragraphs from those saved pages.
- [Search returns national or irrelevant news] → The prompt restricts by place, and the classifier's `relevant` label filters. With no relevant paragraphs, the index is `None` and the verdict uses finances only.
- [Modal cold start (~20 s) during the demo] → Set `min_containers=1` before presenting. The cache makes reruns instant.
- [Assumption values are wrong or disputed] → They are all in one file with sources, shown in the report, and labelled a screening estimate. Changing them needs no code.
- [Agent loops or exceeds time] → Activity timeout of 180 s (`AGENT_OPTS`) and a small request limit per agent run.

## Open Questions

- The exact public sources for CAPEX, OPEX and revenue ranges (task 2.1 fills them in and cites them; the approach does not change).
