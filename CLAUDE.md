# Bessible — CLAUDE.md

Tech {Europe} London AI Hackathon, 19 Sep 2026. 3-person team, ~2 hours of build time, live demo on a Mac.
Judging: ~50% technical execution, ~30% presentation, ~20% solves a real problem. Must use ≥2 event tech partners.

## What we're building
**Bessible**: give it a link to a property; it assesses the site for a Battery Energy Storage System (BESS).
Pipeline: grid connection check → title boundaries (government API) → **human-in-the-loop** confirmation in the CLI →
**feasibility engine** (planning policy, permissions, battery size vs. user's inputs) → **suitability engine**
(agentic: local news, financial model with interest/fees) → report.
**Explainable AI**: every step returns `Artifact`s (claim, source link or downloaded file, generated image, confidence,
model used); the report is built only from artifacts. CLI first; a web UI is optional later.

## Decisions (don't relitigate without the team)
- **Temporal** orchestrates the pipeline: each stage is an activity with typed Pydantic input/output, for clear
  delegation, per-stage debugging, retries, and human-in-the-loop via signals/updates. Workflow code stays
  deterministic (no I/O, no `datetime.now()`, no random) — all I/O goes in activities.
- **Teammates write stages as plain async functions (or Modal functions) with Pydantic in/out**; Josh wraps them as
  activities. Teammates shouldn't need to touch workflow code.
- **Pydantic** validates every input/output. **Pydantic AI** for agents (built-in Temporal integration).
- **Targeted model usage**: Gemini (Google DeepMind keys, high limits) for reasoning calls; **Modal** for niche/specialist
  models and heavy compute, e.g. an open-weight model on a Modal endpoint behind the Pydantic AI Gateway
  (method: github.com/laisbsc/demo_hack_tech_eu), Jev (TypeSafe) for typed classification. Every model call should
  record which model produced each artifact.
- **Runs locally** (Temporal dev server + worker on the demo Mac); only model/compute calls go to the cloud.
- Partners in play: **Modal** (headline), **Pydantic**, **Google DeepMind** (Gemini). Others listed: Tavily, n8n,
  Superlinked, Mubit, Aikido, Slng.ai.

## Team & planning
- The team lead writes the OpenSpec plan for everyone: `openspec/changes/<change>/` (proposal, design, specs, tasks).
  Implement with `/opsx:apply <change>`. Don't create changes unless asked.
- Individual 1-page plans: `docs/plans/<name>.md` (template in `TEMPLATE.md`).
- **Josh** owns: Temporal framework (workflow, worker, dummy activities, HITL CLI), Pydantic models + Pydantic AI setup,
  Modal + model wiring, env setup.

## Setup & commands
- `./scripts/setup.sh` — installs uv, Temporal CLI, Node (Homebrew on Mac), `uv sync`, web `npm ci`, `.env`, Modal login.
- `./scripts/dev.sh` — starts Temporal (reuses a running one; persists to `out/temporal.db`), the map worker and web UI;
  Ctrl+C stops what it started; logs in `out/logs/`. Must stay bash 3.2-compatible (macOS demo machine).
- `temporal server start-dev` — local Temporal (UI http://localhost:8233). Linux CLI lives in `~/.temporalio/bin`.
- `uv run python scripts/check_env.py [--live]` — checks keys, Temporal, Modal login; `--live` pings Gemini + Modal.
- Python via **uv** only: `uv run ...`, `uv add <pkg>` (never pip). Python 3.13–3.14. Package code in `src/bessible/`.
- `sandbox/` (except `sandbox/map_session/`) and `out/` are gitignored. `sandbox/workflow_demo.py` is a working single-file Temporal demo of the whole
  pipeline with dummy activities and the HITL prompt (`uv run python sandbox/workflow_demo.py start <url>`).

## Code map
- `src/bessible/config.py` — `settings` (pydantic-settings, reads `.env`; empty values count as unset).
- `src/bessible/llm.py` — `gemini_model()`, `modal_model()` (gateway route), `setup_logfire()`. Keys are passed from
  `settings` explicitly because `.env` is not loaded into `os.environ`.
- `src/bessible/api/` — Pydantic models of every external data API, exactly as the wire speaks (one module per source).
- `src/bessible/location/` — the tidy layer on top: `await collate(Coordinates(lat, lon)) -> LocationData`. `title` is
  the title boundary everything is measured against; `deterministic` holds facts to compute on (locality, terrain,
  flood, land, designations, grid), `agentic` holds documents / notes / search terms for agents; `sources` lists every
  upstream call (URLs double as artifact sources). `uv run python -m bessible.location <lat> <lon>` dumps it as JSON.
- `src/bessible/possibility/` — can a battery be built here at all? `assess(Proposal(location, battery_mw)) ->
  PossibilityReport`. `hard.py`: the deterministic checks, each a plain `Proposal -> Check` function (pass / warn / fail /
  unknown + reason, facts, source URLs) listed in `HARD_CHECKS`; thresholds live on `Limits`. No I/O.
  `policy.py`: the first Pydantic AI agent. `brief_for(proposal) -> PolicyBrief`, `await read_policy(brief) ->
  PolicyReview` (Gemini opens the local plan URLs from `LocationData.agentic` with native web search + fetch, because
  councils block plain downloads), `policy_check(review) -> Check`; `cross_check` re-labels each quote with the Modal
  classifier. `assess_with_policy` = hard checks, then the agent only if nothing blocks. `pipeline.py` turns checks into
  `Artifact`s / `SiteLandOutput` for the existing collation (`SynthesisInput`).
  `uv run python -m bessible.possibility <lat> <lon> [mw] [hours] [--policy] [--json]` runs it on a live location.

- `src/bessible/suitability/research.py` — local news: `research_local_news(LocationData)` builds ≤4 queries from
  `locality` / `search_terms`, searches Tavily (`api/tavily.py`, operator key `TAVILY_API_KEY`) with page text, and
  keeps only paragraphs found verbatim in that text (the model may only select). Responses cache in `out/cache/tavily/`;
  `data/recorded/tavily/` holds dated recordings for the offline demo. `uv run python scripts/news_research.py <lat>
  <lon> [--model] [--record] [--fixture NAME]` runs it live and prints every quote with its URL.

- `sandbox/map_session/` — tracked prototype, the base for the final build (the rest of `sandbox/` is gitignored).
  `workflow.py`: `AssessWorkflow` (task queue `bessible-web`): AI suggests area → human edits on the map (`submit_area`
  update, validated) → `confirm_area` → engines; the UI polls the `state` query. `activities.py` (dummies),
  `models.py` (Pydantic, mirrored in `web/lib/types.ts`), `worker.py` (`uv run python sandbox/map_session/worker.py`).
  `web/`: Next.js map UI; its route handlers are the only Temporal client and call workflow/query/update names as
  strings (`web/lib/temporal.ts`), so rename both sides together. See `web/README.md`.

## Secrets
- `.env` is gitignored; `.env.example` lists every key. Keys are Josh's accounts, shared privately — never commit,
  paste into chat, or print secret values. Teammates join Josh's Modal workspace rather than sharing a Modal token.

## Working style
- Keep docs short and focused; a person's plan covers only their own part.
- Hackathon pace: working end-to-end first, then polish. Prefer small, tested steps.
