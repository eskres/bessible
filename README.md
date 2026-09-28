# Bessible — BESS Site Assessment Agent

Bessible assesses real estate properties for Battery Energy Storage Systems (BESS) feasibility and suitability in the UK. Given a **map pin location**, **UK postcode**, or **property listing URL**, it coordinates live DNO grid headroom analysis, road-following cable routing to serving substations, title boundary identification with compound sizing, planning policy and flood risk constraints, market revenue projections, community sentiment, and multi-duration financial returns—all backed by full explainability and durable human-in-the-loop orchestration.

---

## Key Features

- **Multi-Modal Screening:** Click directly on the Google Map to drop a pin (no postcode required), enter any UK postcode, or paste a commercial property listing link.
- **5 UK Distribution Network Operators (DNOs):** Live grid headroom checks across UK Power Networks (UKPN), National Grid Electricity Distribution (NGED), Scottish and Southern Electricity Networks (SSEN), SP Energy Networks (SPEN), and Northern Powergrid (NPg), with connection-voltage-level caps (11 kV, 33 kV, 132 kV) and clean out-of-area handling.
- **Cable Run Pricing:** The map draws the straight line from the site to the serving substation; the cable is priced on that line times a detour factor (`cable_detour_factor` in `data/assumptions/finance.json`, default 1.5x).
- **Interactive Google Maps:** Vector map with a 2 km screening radius dimming mask, draggable Reserved Compound overlay, and statutory designation boundaries.
- **Realistic Financial Modeling:** Evaluates 1-hour, 2-hour, and 4-hour battery durations with capex breakdown (batteries, BoP, road cabling, DNO connection), revenue stacking (wholesale arbitrage, frequency response, capacity market) calibrated against Modo Energy and BNEF benchmarks, NPV, IRR, and payback calculations.
- **Durable Orchestration (Temporal):** Resilient multi-stage pipeline with live SSE agent telemetry, automatic activity retries, and early stop for non-viable or out-of-area sites.
- **Human-in-the-Loop Control:** Interactive proposal card allowing developers to adjust target capacity, toggle flexible connections above firm headroom, approve proposals, or reject and explore another site early.
- **Keyless Demo Workspace:** Explore the platform without API keys or credentials using 3 pre-recorded live runs or browser simulation.
- **Security & BYOK:** Firebase Google sign-in, Bring Your Own Key (BYOK) encrypted with AES-GCM (`KEY_ENCRYPTION_SECRET`), Traefik rate limiting, SSRF protection with DNS pinning, and prompt-injection sanitization.

---

## Quick Start (Local)

Run the whole stack on your machine: Temporal, the worker, the FastAPI server and the web UI.

```bash
./scripts/setup.sh    # once: installs uv, Temporal CLI, Node, dependencies, and creates .env
./scripts/dev.sh      # starts everything; Ctrl+C stops it
```

* Web UI: **http://localhost:3000** (opens automatically on macOS)
* API: **http://localhost:8000**
* Temporal UI: **http://localhost:8233**
* Logs: `out/logs/`

`.env` needs at least `GOOGLE_API_KEY` and `KEY_ENCRYPTION_SECRET` (see [Prerequisites](#1-prerequisites--environment-check)).

**Run without sign-in:** with Firebase keys in `.env`, the app asks for Google sign-in. To skip it locally:

```bash
AUTH_ENABLED=false ./scripts/dev.sh
```

Or set `AUTH_ENABLED=false` in `.env`. This turns sign-in off in both the web UI and the API: every request runs as
one local user, whose Google key starts as `GOOGLE_API_KEY` from `.env` (a key saved in Settings replaces it). Use it
for local development only, never on a public deployment.

The sections below start each service by hand, for debugging one part.

---

## Running the Backend

### 1. Prerequisites & Environment Check

Ensure your `.env` contains the required keys (`GOOGLE_API_KEY`, `KEY_ENCRYPTION_SECRET`, `PYDANTIC_AI_GATEWAY_API_KEY`):

```bash
# Copy example configuration template
cp .env.example .env

# Generate a master key encryption secret (required for sealing per-user keys)
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
# Set KEY_ENCRYPTION_SECRET=<generated_secret> in your .env

# Check keys, Temporal reachability, and Modal login
uv run python scripts/check_env.py

# Optional: verify model execution with a live ping
uv run python scripts/check_env.py --live
```

### 1b. Optional: Set Up Modal (Self-Hosting)

Modal is optional. Without it, the classifier falls back to Gemini (`llm`), then to keyword rules (`heuristic`). The
policy `cross_check` needs Modal: when Modal is off, the check does not run.

Modal runs the open-jev DeBERTa classifier on a GPU. Your own Modal workspace pays for it (the operator, not the users).

1. Create an account at [modal.com](https://modal.com) (the Starter plan includes $30/month of free compute).
2. Install the Modal extra. A plain `uv sync` includes it. On a server that uses `uv sync --no-dev`, run:
   ```bash
   uv sync --no-dev --extra modal
   ```
3. Log in to Modal. Pick one:
   - On your own machine: `uv run modal setup` (opens a browser; `./scripts/setup.sh` runs this for you).
   - On a server or in Docker: create a token in Modal (**Settings → API Tokens**), then set both values in `.env`:
     ```bash
     MODAL_TOKEN_ID=ak-...
     MODAL_TOKEN_SECRET=as-...
     ```
4. Deploy the classifier to your workspace. The worker looks up a Modal app named `bessible-classifier` with a class
   `Classifier`. The class has a method `classify(paragraphs: list[str], questions: list[dict]) -> list[list[dict]]`
   (see `_classify_modal` and `_questions` in `src/bessible/classifier.py` for the exact shapes):
   ```bash
   uv run modal deploy <path-to-classifier-app>.py
   ```
5. Choose the backend in `.env`. `auto` uses Modal when a token exists. `modal` starts the chain at Modal.
   ```bash
   CLASSIFIER_BACKEND=auto
   ```
6. Check the setup:
   ```bash
   uv run python scripts/check_env.py
   ```
   The output shows `Modal login` as passed.

**Optional: open-weight model through the Pydantic AI Gateway.** `llm.modal_model()` calls a model on Modal
(default `google/gemma-4-31B-it`) through the [Pydantic AI Gateway](https://ai.pydantic.dev/gateway/). To use it:

1. Serve the model on Modal with an OpenAI-compatible endpoint. One method is in
   [laisbsc/demo_hack_tech_eu](https://github.com/laisbsc/demo_hack_tech_eu).
2. In the Gateway, add a provider route that points at the Modal endpoint.
3. Set `PYDANTIC_AI_GATEWAY_API_KEY`, `MODAL_GATEWAY_ROUTE` (the route name) and `MODAL_MODEL` in `.env`.
4. Run `uv run python scripts/check_env.py --live`. The output shows `Modal via gateway` as passed.

### 2. Start Temporal Server

The backend uses [Temporal](https://temporal.io) to orchestrate durable workflow execution:

```bash
temporal server start-dev
```
* Temporal Web UI is accessible at **http://localhost:8233**.

### 3. Start the Backend Worker

In a separate terminal, launch the Bessible worker listening on task queue `bessible`:

```bash
uv run python -m bessible.worker
```

> **Demo Tip (Activity Retries):** To demonstrate live retry recovery on transient failures, launch the worker with:
> ```bash
> BESSIBLE_DEMO_FAIL_ONCE=1 uv run python -m bessible.worker
> ```

### 4. Start the FastAPI HTTP Server

To serve the web UI, SSE progress traces, fast capacity checks, and demo replays:

```bash
uv run uvicorn bessible.api.app:app --host 0.0.0.0 --port 8000
```

* Health check: **http://localhost:8000/health**
* Each user's Google key is stored encrypted (with `KEY_ENCRYPTION_SECRET`) in `out/keys.db` (`/app/out/keys.db` in Docker). Set `KEY_DB_PATH` in `.env` to customize storage.

---

## Running the Frontend

The web UI is a Next.js 16 application featuring an interactive Google Maps site map, live SSE pipeline trace, human-in-the-loop decision controls, and synthesized report viewing.

### 1. Prerequisites

- **Node.js 20+** (`node -v` >= 20.9)
- **FastAPI backend** running on `http://localhost:8000`
- **`GOOGLE_MAPS_API_KEY`** in the root `.env` (a browser key for the Maps JavaScript API; without it the map displays a "Map unavailable" fallback). `GOOGLE_MAPS_MAP_ID` is optional for vector map styling.

### 2. Install Dependencies

Navigate to the `web/` directory and install the packages:

```bash
cd web
npm install
```

### 3. Start the Development Server

```bash
npm run dev
```

* The frontend is accessible at **http://localhost:3000**.
* By default, it communicates with the API at `http://localhost:8000`. If running on a different port or host, set `NEXT_PUBLIC_API_URL` (e.g. `NEXT_PUBLIC_API_URL=http://localhost:8080 npm run dev`).

### Workspaces: Live vs Keyless Demo

The web app supports two workspace modes:

1. **Live Workspace:** Authenticated mode (Firebase Google sign-in) with Bring Your Own Key (BYOK) for live agent reasoning, live DNO headroom queries, road cable routing, and full report generation. Locally, `AUTH_ENABLED=false` skips sign-in (see [Quick Start](#quick-start-local)).
2. **Demo Workspace:** Keyless mode requiring no login, no API keys, and no Temporal server. Includes 3 pre-recorded presets from live runs:
   - **Dorking (RH4 1AD):** Primary substation connection with viable headroom in UK Power Networks (UKPN) territory.
   - **Histon (CB24 9ZR):** Primary substation connection near Cambridge in UKPN territory.
   - **Manchester (M1 1AD):** Out-of-area scenario in Electricity North West (ENWL) territory, demonstrating clean early termination and supported operator guidance.
   - **Browser Simulation:** Entering any other UK postcode or coordinate in demo mode simulates realistic screening client-side.
   - **URL State Shortcuts:**
     - `http://localhost:3000/?state=demo` — open Demo Workspace directly.
     - `http://localhost:3000/?state=confirm` — inspect the human-in-the-loop confirmation card UI.
     - `http://localhost:3000/?state=report` — inspect the synthesized report viewer UI.

### All-in-One Dev Script

`./scripts/dev.sh` starts Temporal, the worker, the API and the web UI in one command. See [Quick Start](#quick-start-local).

---

## CLI Usage

The backend CLI (`bessible.cli`) allows you to start assessments, confirm site parameters, record demo runs, and inspect results.

### Starting an Assessment

```bash
# Interactive mode (guides you through progress & prompts for human-in-the-loop decision)
uv run python -m bessible.cli start --postcode "OX14 4TE"

# Start with property link, target capacity, and budget
uv run python -m bessible.cli start "https://example.com/property" --battery-mw 20 --budget-gbp 10000000

# Enable flexible connection (allows connecting above firm headroom up to network ceiling)
uv run python -m bessible.cli start --postcode "OX14 4TE" --flexible

# Auto-confirm defaults without interactive prompting
uv run python -m bessible.cli start --postcode "OX14 4TE" --yes

# Detached mode (starts workflow in background and prints run ID)
uv run python -m bessible.cli start --postcode "OX14 4TE" --detach
```

### Confirming a Paused Run (Human-in-the-Loop)

When a run reaches the `awaiting_confirmation` checkpoint:

```bash
# Confirm using recommended capacity
uv run python -m bessible.cli confirm <run-id>

# Confirm with custom capacity within approved range
uv run python -m bessible.cli confirm <run-id> --capacity-mw 15.0

# Reject site proposal (cancels downstream stages cleanly)
uv run python -m bessible.cli confirm <run-id> --reject
```

### Viewing Run Results & Reports

```bash
# View active progress, duration comparison table, and artifact paths
uv run python -m bessible.cli result <run-id>
```

All generated evidence artifacts, GeoJSON boundaries, and Markdown reports are saved to:
`out/<run-id>/report.md`

### Recording a Demo Preset

To record a live run into `data/demo/<slug>` for keyless demo replay:

```bash
uv run python -m bessible.cli record <slug> <postcode> [--flexible]

# Example:
uv run python -m bessible.cli record dorking "RH4 1AD"
```

The recorder handles runs that end before confirmation (such as out-of-area sites), assigns unique run IDs, sanitizes all configured secrets from recorded event streams, and preserves exact financial and capacity outputs.

---

## Grid & DNO Coverage

Bessible supports live grid headroom checks across **5 UK Distribution Network Operators (DNOs)**:

| Operator | Coverage Area | Headroom Check |
|---|---|---|
| **UK Power Networks (UKPN)** | East of England, London, South East | Bundled snapshot + live heatmap API verification |
| **National Grid Electricity Distribution (NGED)** | East & West Midlands, South West, South Wales | Live Connected Data Portal API |
| **Scottish and Southern Electricity Networks (SSEN)** | North of Scotland, Central Southern England | Live Open Data Portal API |
| **SP Energy Networks (SPEN)** | Central & Southern Scotland, Merseyside, North Wales | Live Open Data Portal API |
| **Northern Powergrid (NPg)** | North East England, Yorkshire | Live Open Data Portal API |

* **Live Headroom by Default:** `settings.live_capacity` is enabled by default. UKPN snapshot headroom is verified against live heatmap data on every run; other operators query live portal endpoints.
* **Per-Operator Connection Voltage Limits:** Caps headroom by proposed connection voltage (e.g. 8 MW for 11 kV busbars, 50 MW for 33 kV primaries, and grid-level connections for 132 kV+).
* **Out-of-Area Guidance:** Sites outside supported DNO license areas (e.g. Electricity North West / ENWL) cleanly terminate early with an informative explanation listing supported networks.

---

## Authoritative Data Sources

Bessible combines and cross-references data across public and commercial energy infrastructure sources:

- **Grid Headroom & Substations:** DNO Long Term Development Statements (LTDS) and Open Data Portals (UKPN, NGED, SSEN, SPEN, NPg).
- **Existing & Queued Generation:** DESNZ Renewable Energy Planning Database (REPD).
- **Title Boundaries & Sizing:** HM Land Registry INSPIRE Index Polygons.
- **Cable Routing:** Google Routes API (Essentials) for road network distance and routing to serving substations.
- **Environmental & Statutory Constraints:** Environment Agency Flood Zones (2 and 3), Areas of Outstanding Natural Beauty (AONB / National Landscapes), Sites of Special Scientific Interest (SSSI), Green Belt, Ramsar, SPAs, and SACs.
- **Planning Data:** Local Planning Authority (LPA) datasets, Article 4 directions, conservation areas, and listed buildings.
- **Financial Benchmarks:** Capex benchmarks from BloombergNEF (BNEF) and battery revenue projections calibrated with Modo Energy market data.

---

## Standalone Diagnostics & Data Tools

### Site Data Report

Generate a consolidated raw environmental and grid data HTML report for any coordinate:

```bash
uv run python scripts/site_report.py 51.2362 -0.3323 [radius_km]
```

### Location Pipeline Collation

Test coordinate geocoding, boundary retrieval, flood zones, and designations:

```bash
uv run python -m bessible.location 51.2471 -0.2668 --full
```

### Automated Tests & Linting

```bash
# Run backend test suite
uv run pytest

# Check code formatting and linting
uv run ruff check src/ tests/
```

---

## Architecture & How to Replace a Stage

The pipeline runs as a durable Temporal workflow (`AssessmentWorkflow`):

1. **Sequential Front**: `resolve_location` &rarr; `propose_capacity` (with early stop for out-of-area or non-viable sites) &rarr; `find_title_boundaries`.
2. **Human-in-the-Loop Checkpoint**: Pauses with `status="awaiting_confirmation"`. Validates user decision via `decide_site` update signal (approve capacity, adjust slider, or early rejection).
3. **Parallel Group 1**: `grid_connection`, `site_land`, `market_revenue`, `local_sentiment`.
4. **Parallel Group 2**: `financial_model`, `regulatory_planning`.
5. **Synthesis**: Compiles Markdown report and verifies that all claims cite evidence artifact IDs.
6. **Retries** (web runs only): each Group 1 stage reports the evidence it could not get as `gaps` (`DataGap`: retryable when a source failed, `could_block` when it might hide a land blocker). A run started through the API stays open for `retry_window_s` (30 min) after its report; the `retry_stages` update (`POST /runs/{id}/retry`) re-runs only the named stages with retryable gaps, then Group 2 and synthesis, up to 3 times. `GET /runs/{id}/result` serves the latest report meanwhile. CLI and recorded runs end with their report.

Each assessment stage is an independent `async` function in `src/bessible/stages/<stage>.py`. You can swap or customize any stage implementation without modifying workflow or worker logic.

### Stage Signatures

```python
from bessible.models import (
    CapacityInput, CapacityOutput,
    FinancialInput, FinancialOutput,
    GridOutput, LocationInput, LocationOutput,
    MarketOutput, NodeInput, PlanningInput,
    PlanningOutput, ReportOutput, SentimentOutput,
    SiteLandOutput, SynthesisInput, TitleInput, TitleOutput,
)

# 1. Location (src/bessible/stages/location.py)
async def resolve_location(inp: LocationInput) -> LocationOutput: ...

# 2. Grid Capacity (src/bessible/stages/capacity.py)
async def propose_capacity(inp: CapacityInput) -> CapacityOutput: ...

# 3. Title Boundaries (src/bessible/stages/title.py)
async def find_title_boundaries(inp: TitleInput) -> TitleOutput: ...

# 4. Grid Connection (src/bessible/stages/grid.py)
async def grid_connection(inp: NodeInput) -> GridOutput: ...

# 5. Site & Land Constraints (src/bessible/stages/site_land.py)
async def site_land(inp: NodeInput) -> SiteLandOutput: ...

# 6. Market Revenue Projections (src/bessible/stages/market.py)
async def market_revenue(inp: NodeInput) -> MarketOutput: ...

# 7. Local Community Sentiment (src/bessible/stages/sentiment.py)
async def local_sentiment(inp: NodeInput) -> SentimentOutput: ...

# 8. Financial Model (src/bessible/stages/financial.py)
async def financial_model(inp: FinancialInput) -> FinancialOutput: ...

# 9. Regulatory & Planning (src/bessible/stages/planning.py)
async def regulatory_planning(inp: PlanningInput) -> PlanningOutput: ...

# 10. Synthesis & Report (src/bessible/stages/synthesis.py)
async def synthesise(inp: SynthesisInput) -> ReportOutput: ...
```

---

## Security & Privacy

- **Bring Your Own Key (BYOK):** Each user enters their own Google Gemini API key via the web UI Key Panel. Keys are stored encrypted with AES-GCM (`KEY_ENCRYPTION_SECRET`) in `out/keys.db`.
- **Run Isolation:** Encrypted user keys are decrypted strictly in-memory inside Temporal activities for the duration of a run; keys are never logged or stored in workflow history.
- **SSRF Defense:** Property link resolution enforces public IP validation and DNS pinning, rejecting attempts to access loopback, link-local, or private IP addresses.
- **Prompt Injection Defense:** External page content scraped from listing URLs or news articles is sanitized to strip prompt-injection patterns before insertion into LLM prompts.
- **Rate Limiting:** Traefik Docker labels and API middleware enforce per-IP rate limits on sensitive endpoints (`/runs`, `/capacity`, `/me`).

---

## Setup Script Reference

```bash
./scripts/setup.sh    # Installs uv, Temporal CLI, Node, Python deps, creates .env, logs in to Modal
./scripts/dev.sh      # Starts Temporal + worker + FastAPI + web UI in one command
```

---

## Containerisation & Deployment (GHCR & Docker)

Bessible is containerised and configured for automated continuous deployment to the **GitHub Container Registry (GHCR)** (`ghcr.io`).

### 1. Docker Compose (Server Deployment behind Traefik)

`docker-compose.yml` runs the complete stack—Temporal Server (with SQLite persistence), FastAPI Backend, Background
Worker, and Next.js Frontend—on a server that already runs [Traefik](https://traefik.io). **It is not for local use:**
no service publishes a port, and traffic reaches the containers only through Traefik. To run locally, use
[`./scripts/dev.sh`](#quick-start-local).

Requirements on the server:
* An external Docker network named `TRAEFIK_NETWORK` (default `traefik_proxy`) that Traefik is attached to.
* `DOMAIN` and `TRAEFIK_ENTRYPOINT` set in `.env` (see `.env.example`).

```bash
# Ensure your API keys are in .env
cp .env.example .env

# Build and start all services
docker compose up --build
```

Services started:
* **Web UI:** `https://$DOMAIN/`
* **FastAPI Backend:** `https://$DOMAIN/` under `/runs`, `/me`, `/demo`, `/capacity`, `/site-data`, `/inspire`, `/health`, `/data`
* **Temporal Server:** internal only (`temporal:7233`, SQLite database in the `temporal-data` volume)
* **Temporal Worker:** Background worker listening on queue `bessible`

To stop the containers:
```bash
docker compose down
```

**Upgrading a deployment from before the key store moved:** keys used to live at `/var/lib/bessible/keys.db`
(`bessible-data` volume). Copy them once, before pulling the new images, or users re-enter their Google key:
```bash
docker compose exec api cp /var/lib/bessible/keys.db /app/out/keys.db
```

### 2. Building Images Locally

Build individual images using Docker:

```bash
# Build the Python backend image (FastAPI server + Temporal worker)
docker build -t bessible-backend -f Dockerfile .

# Build the Next.js web frontend image
docker build -t bessible-web -f web/Dockerfile ./web
```

Run individual containers:

```bash
# Run FastAPI server
docker run -p 8000:8000 --env-file .env bessible-backend

# Run worker (connects to Temporal on host or network)
docker run --env-file .env bessible-backend python -m bessible.worker

# Run Next.js frontend
docker run -p 3000:3000 -e BACKEND_URL="http://localhost:8000" bessible-web
```

### 3. GitHub Container Registry (GHCR) CI/CD

The repository includes a GitHub Actions workflow (`.github/workflows/docker-publish.yml`) that automatically builds and publishes OCI container images to GHCR whenever changes are pushed to `main` or version tags (`v*`) are created:

* **Backend Image (API & Worker):** `ghcr.io/<owner>/bessible-backend:latest`
* **Web Frontend Image:** `ghcr.io/<owner>/bessible-web:latest`

#### Pulling and Running from GHCR

Authenticate with GHCR using your GitHub Personal Access Token (with `read:packages` scope):

```bash
echo $GITHUB_TOKEN | docker login ghcr.io -u <YOUR_GITHUB_USERNAME> --password-stdin
```

Pull and run the pre-built images:

```bash
# Pull images
docker pull ghcr.io/<owner>/bessible-backend:latest
docker pull ghcr.io/<owner>/bessible-web:latest

# Run API server
docker run -d -p 8000:8000 --env-file .env ghcr.io/<owner>/bessible-backend:latest

# Run Worker
docker run -d --env-file .env ghcr.io/<owner>/bessible-backend:latest python -m bessible.worker

# Run Frontend
docker run -d -p 3000:3000 -e BACKEND_URL="http://<api-host>:8000" ghcr.io/<owner>/bessible-web:latest
```
