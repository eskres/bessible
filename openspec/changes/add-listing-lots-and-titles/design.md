## Context

`fetch_page_text` fetches a page (SSRF-safe), caches `{url, fetched_at, text}` under `data/fixtures/pages/<sha1>.json`, and `html_to_text` drops every `<script>`, so JSON-LD and app state are lost. `extract_location` sends the first 15,000 characters to Gemini for one address, postcode and point. `stages/title.py` returns a placeholder square; the map draws the one title containing the point from `collate` (`GET /site-data`). The workflow's `_await_confirmation` waits for a `SiteDecision` before the engines run.

What we saw on a real farm listing (a Next.js portal): the app state at `props.initialReduxState.propertyDetail.property` had `Latitude`, `Longitude`, `SizeAcres`, `Size.Hectares`, `AddressLine1/2`, `LongDescription` (about 4,900 clean characters listing each lot and its acreage), `AdditionalInformation` (a what3words per lot), `ImagesGallery` (photos, and the sale plan), `FloorPlanGallery` (building floor plans, not the land plan) and `PolygonSet` (null there, may be set elsewhere). The page also had a schema.org `application/ld+json` block.

## Goals / Non-Goals

**Goals:** every lot of a listing on the map as a group of titles close to its stated acreage; the user can fix the set with clicks; typical listings cost about 2k tokens or none; single-property links behave as today; tried listings stay out of git.

**Non-Goals:** browser rendering; title numbers or ownership; drawing boundaries from plan images.

## Decisions

### Layout

```
src/bessible/listing/
  blocks.py        # PageBlocks: JSON-LD, app state, meta tags, captured before scripts are stripped
  portals/         # one reader per portal: savills.py, rightmove.py, zoopla.py, knightfrank.py, onthemarket.py
  jsonld.py        # generic schema.org reader (fallback)
  lots.py          # step 2: the one small model call
  titles.py        # step 3: grow title groups by acreage (pure, plus one fetch)
  plan.py          # step 5: pick the plan image, one vision call
  resolve.py       # the tiers in order -> ListingSites
src/bessible/api/what3words.py   # wire models for convert-to-coordinates
tests/fixtures/listings/<portal>.json   # trimmed, invented or anonymised blocks per portal
```

### Models

```python
class Anchor(BaseModel):          # where a lot is known to be
    position: Position
    kind: Literal["listing_point", "what3words", "postcode", "polygon"]
    source: str                    # URL or field path, for the artifact

class Lot(BaseModel):
    name: str                      # "Lot 1", or "Whole" for a single property
    acres: float | None
    what3words: str | None
    postcode: str | None
    address: str | None
    anchor: Anchor | None

class ListingFacts(BaseModel):     # step 1 output
    reader: str                    # "savills", "json-ld", "meta", ...
    point: Position | None
    total_acres: float | None
    address: str | None
    postcode: str | None
    description: str | None        # the listing's own text, for step 2
    notes: str | None              # additional information (lot notes, what3words)
    images: list[ListingImage]     # url, caption, gallery ("images" | "floorplans")
    polygon: Geometry | None
    lots: list[Lot]                # only when the structured data states them

class TitleGroup(BaseModel):       # step 3 output, one per placed lot
    lot: str
    target_acres: float | None
    inspire_ids: list[str]
    area_acres: float
    score: float                   # 1 - |area - target| / target, clamped to [0, 1]
    method: Literal["acreage", "single_title", "portal_polygon", "plan_image", "user"]
```

`LocationOutput` gains `listing: ListingSummary | None` (lots, total acres, reader). `TitleOutput` gains `groups: list[TitleGroup]` and `candidates` (every polygon fetched, as a FeatureCollection, for the map); `title_number`, `boundary_geojson` and `area_m2` stay and describe the union of the chosen groups. `SiteDecision` gains `title_ids: list[str] | None`; the confirmed union becomes `ConfirmedSite.boundary`.

### Step 1: structured data before stripping

`fetch.py` captures `PageBlocks` from the raw HTML before `html_to_text`: every `application/ld+json` block; app state from `<script id="__NEXT_DATA__">`, `window.PAGE_MODEL =`, `window.__INITIAL_STATE__` / `__PRELOADED_STATE__`, and `self.__next_f.push` payloads; and meta tags (`og:*`, `place:location:*`, `geo.position`, `ICBM`). The cache entry becomes `{url, fetched_at, text, blocks}`. Old entries with `text` only still work; they just have no step 1.

Readers are chosen by host. Each reader is a pure function `PageBlocks -> ListingFacts | None` with the field paths written down in the module, tested against a recorded fixture. The JSON-LD reader (`geo`, `address`, `floorSize`, `image`) runs when no portal reader matches or it returns nothing. Meta tags give a point as the last resort. Field paths for portals other than Savills are unknown until a fixture is recorded (task 2.2); a reader that finds none of its paths returns `None`, never an error.

**Why:** the app state is the portal's own data: exact coordinates, acreage and a clean description, at no token cost. Boilerplate never reaches a model.

### Step 2: one small model call on the listing's own text

Input: `description` + `notes` from step 1, capped at 6,000 characters (about 1.5k tokens). Without step 1, the input is a window of the page text starting at the first line with a listing cue (acres, hectares, lot, guide price, bedrooms), after dropping lines that repeat across the portal's pages (cookie, menu, footer), with the same cap. Output (Gemini, structured): `lots`, `total_acres`, `tenure`, and the fields of today's `ExtractedLocation` (address, postcode, lat, lon, country, confidence), so one call replaces the current one.

Skipped when step 1 gives the lots, or gives a point and the description has no lot cue (`\blots?\b` with a number or "Lot 1"), which is the single-property case: zero tokens. Prompt about 300 tokens, output about 200: about 2k per call. The regex fallback of today stays for no model.

### Anchors

- **what3words:** `GET https://api.what3words.com/v3/convert-to-coordinates` with `WHAT3WORDS_API_KEY` (`settings.what3words_api_key`). Optional. Its free allowance for this endpoint must be checked before we rely on it (task 1.1); without a key or allowance, what3words are shown to the user as text and not resolved. Results are not stored beyond the run cache unless the terms allow it.
- **Postcode per lot:** geocoded as today; a postcode centroid is a weak anchor (it can sit on a road or another property), so it only seeds when nothing better exists.
- **Listing point:** anchors the lot it falls in. Which lot that is, is decided by area match: the group is grown once and scored against each unplaced lot's acreage and against the total; the best score wins.
- A lot with no anchor is listed as "not placed" in the confirmation view; the user can click titles for it.

### Step 3: grow title groups by acreage

One planning.data search per listing: `dataset=title-boundary`, `geometry=<WKT circle around the anchors>`, `geometry_relation=intersects`, `limit=500`. The radius is 1.5 × the radius of a circle with the lot's stated area, at least 300 m, at most 3 km. The same polygons are the map's clickable candidates.

Per lot, greedily: start with the polygon containing the anchor (smallest if several). Repeatedly add the neighbour (within 20 m, so a lane or stream between fields counts as adjacent) that brings the total area closest to the target; stop when the total is within tolerance, when every addition makes the match worse, or at 60 polygons. Tolerance: ±10% or ±2 acres, whichever is larger. Polygons already in another lot's group are skipped. With no stated acreage, the group is the containing polygon only (`single_title`), as today. A portal polygon, when present, selects the polygons it covers by more than half (`portal_polygon`) and skips growth.

Pure function over shapely geometries in British National Grid, so it is unit-tested without the network.

**Why greedy by area:** fields sold together are adjacent, and the acreage is the one number every listing states. It can pick a neighbour's field of the right size; the human check and step 5 catch that.

### Step 4: the human check

The title activity returns the groups and candidates before the workflow waits. The map draws every candidate faintly, chosen polygons filled in their lot's colour, and a summary per lot: stated acres, matched acres, score. Clicking a candidate toggles it in the selected lot. The decision carries `title_ids`; the workflow validates that each id is a candidate. The confirmed union is written to `boundary.geojson`, and `collate` accepts it as the site polygon instead of looking up the one title, so every later figure is measured against it. A confirmation without `title_ids` keeps the proposed groups.

The existing `/inspire` stub is filled with a bbox search on the same dataset so the user can add polygons outside the candidate radius.

### Step 5: plan image, only on low confidence

Runs when any placed lot scores below 0.8, or a lot with a stated acreage has no anchor. Otherwise skipped (zero tokens).

1. **Pick the plan image cheaply.** Candidates are the main gallery images, not the floor plan gallery (on the listing we saw it held building floor plans). Rank by caption and file name hints ("plan", "sale plan", "site plan", "boundary", "lot", "map"), then by image statistics on a 256 px download: share of near-white pixels, number of distinct colours, and saturated outline pixels (plans are mostly white with coloured boundary lines; photos are not). If the top two are close, one classification call sends up to 12 thumbnails at 256 px and asks for the index of the land plan or none (about 3.5k tokens).
2. **One vision call.** The plan image at up to 1,024 px, and the candidate polygons drawn by us on a plain background with a short number on each and the current groups outlined. Structured output: for each lot on the plan, the polygon numbers inside its boundary, and a confidence. About 2.5k tokens.

A lot whose plan match changes the group gets `method="plan_image"`; the user still confirms. Cached by (image URL, candidate ids) in the run cache. Cap: one classification and one comparison call per listing, 6k tokens in total.

### Token budget

| Step | Model | Cap | Typical |
|---|---|---|---|
| 1 structured | none | 0 | 0 |
| 2 lots | Gemini (`settings.gemini_model`) | 6,000 input chars, 1 call | ~2k tokens, or 0 |
| 3 titles | none | 0 | 0 |
| 5 plan pick | Gemini, low-res images | 12 thumbnails, 1 call | 0 (hints usually decide) |
| 5 plan compare | Gemini vision | 1 call, 1,024 px | 0 (only on low confidence) |

Every model call produces an `Artifact` with `model_used` set; deterministic steps record their source (`savills app state`, `json-ld`, `planning.data title-boundary`, `what3words`).

### Artifacts

- Location: "Listing gives {n} lots, {total} acres ({reader})", source the listing URL.
- One per lot group: "Lot 1: 12 INSPIRE polygons, 198.4 acres against 199 stated (score 0.99)", `file_path` the group's GeoJSON, `model_used` `title-acreage-match` or the vision model.
- One for the human edit: "User added 2 and removed 1 polygon; confirmed site 205.1 acres".

### Keeping tried listings out of git

Live fetches write to `out/cache/pages/`, `out/cache/postcodes/` and `out/cache/what3words/` (`out/` is gitignored and already shared by the Docker services). Lookups read the committed fixtures first, then the live cache, then the network; writes never go to `data/fixtures/`. A small script promotes a chosen page to `data/fixtures/pages/` on purpose, trimming it to the blocks and text the tests need. Committed fixtures for portals are invented or anonymised.

### Implemented first slice: `TitleParcel` and title numbers (2026-09-27)

Before lots, the title stage works per polygon (`src/bessible/titles/`, `stages/title.py`):

- **`TitleParcel`** (`models.py`): one INSPIRE polygon (`inspire_id`, `geometry`, `area_m2`, planning.data `source_url`), plus `title_number` / `title_source` / `title_source_url` / `title_link` (`"polygon"` or `"site"`) only when a named source gives one, and `footprint_overlap_pct` (share of the BESS footprint on it). `TitleOutput` holds `pin_parcel`, `candidates` (one polygon search, radius as above, nearest 600 kept), `site_parcels`, `inspire_ids`, `title_numbers` (`TitleNumber`, each with its source) and `notes`; `title_number` is the pin polygon's number or None.
- **After HITL**, the `confirm_title_site` activity measures the footprint (or the `title_ids` the user clicked) against the candidates, fetches more polygons when the footprint leaves the search circle or the user added ids from `/inspire` (`SiteDecision.added_ids`), and returns the union as `ConfirmedSite.boundary`; `collate(..., site_polygon=)` measures later stages against it.
- **Title numbers** are never inferred from an INSPIRE id, a neighbour or a model (the free index has none; HMLR refuses an id -> number table; the National Polygon Service is paid). Free sources, best first:
  1. **Listing text** (`titles/numbers.py`): only numbers after "title number(s)" / "registered under title", as an unbroken list; linked to the site.
  2. **HMLR CCOD / OCOD** (`titles/hmlr.py`, `HMLR_API_KEY`, licences accepted once in the web service): the monthly full files are downloaded to `out/cache/hmlr/` and indexed by postcode (`scripts/hmlr_ownership.py`); rows at the site postcode or within 150 m are site-level candidates naming the company proprietor. Only `ccod` and `ocod` can be requested.
  3. **The user** can give a number per site polygon through the API / CLI (`SiteDecision.user_title_numbers`): polygon-level, recorded as "entered by the user, not checked". The web confirmation view has no input for it (dropped 2026-09-28: nothing downstream reads it and it cannot be checked).

## Risks / Trade-offs

- [Portal changes its app-state shape] → Readers return `None` and the tiers fall through to JSON-LD, meta tags and step 2; fixtures show which path broke.
- [Portals block bots (Zoopla, Rightmove)] → Fails as today with the `--postcode` hint; curated fixtures cover the demo.
- [Greedy growth takes a neighbour's field of the right size] → Score and candidates shown; the user clicks it off; step 5 on low scores.
- [INSPIRE polygons do not match sale lots (unregistered land, roads, one title across many polygons)] → Tolerance, 20 m adjacency, the user adds or removes.
- [what3words allowance or terms] → Optional; the page point still anchors one lot; not stored beyond the run cache.
- [Larger cache entries] → Only the blocks we read are stored, not the raw HTML.
- [Page text as prompt injection] → Same untrusted-text handling as today (`sanitize_untrusted_text`, data-only prompt); step 2 sees less text than today.

## Migration Plan

1. Optional: set `WHAT3WORDS_API_KEY` after checking the allowance.
2. Move existing untracked cache files out of `data/fixtures/` into `out/cache/`; keep only curated demo pages committed.
3. Deploy. Single-property links give the same point as before; multi-lot listings show groups.

## Open Questions

- The what3words free allowance for convert-to-coordinates, and whether its terms allow caching (task 1.1).
- App-state field paths for the four portals other than Savills (task 2.2).
- Score threshold 0.8 and tolerance ±10%: tune on 3 to 5 recorded listings.
