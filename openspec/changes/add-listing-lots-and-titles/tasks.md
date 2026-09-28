## 1. Setup and privacy

- [ ] 1.1 Check the what3words API free allowance for convert-to-coordinates and its caching terms; write the answer in `design.md` (Open Questions); verify by one call with a test key on an invented what3words address
- [ ] 1.2 Add `what3words_api_key` to `config.py` and `WHAT3WORDS_API_KEY=` to `.env.example`; the autouse test fixture unsets it; verify `check_env.py` reports it as optional
- [~] 1.3 (partial: pages only. Live pages now write to `out/cache/pages/`, reads try `data/fixtures/pages/` first; postcodes and the move of existing untracked files are left) Split the caches: live writes for pages and postcodes go to `out/cache/`; reads try `data/fixtures/` first, then `out/cache/`; verify a run on a new link leaves `git status` clean and a curated fixture still resolves with the network off
- [ ] 1.4 Script to promote a cached page into `data/fixtures/pages/`, trimmed to its blocks and text; verify it refuses to overwrite and prints the fixture path

## 2. Step 1: structured data (0 tokens)

- [ ] 2.1 `listing/blocks.py`: capture JSON-LD, app state (`__NEXT_DATA__`, `window.PAGE_MODEL`, `__INITIAL_STATE__` / `__PRELOADED_STATE__`, `self.__next_f`) and meta tags before `html_to_text`; store `blocks` in the cache entry; verify old text-only entries still load and a Next.js test page yields its app state
- [ ] 2.2 Record one trimmed, anonymised fixture per portal (Savills, Rightmove, Zoopla, Knight Frank, OnTheMarket) and write the field paths found into each reader's module; verify no real address, postcode or what3words is in the fixtures (pre-commit hook passes)
- [ ] 2.3 Portal readers and `jsonld.py` (pure `PageBlocks -> ListingFacts | None`), chosen by host; verify each fixture gives point, area, description and images, and an emptied fixture returns `None`

## 3. Step 2: lots from one small call

- [ ] 3.1 `listing/lots.py`: `ListingLots` output (lots, total acres, tenure, plus today's location fields), input capped at 6,000 characters of description and notes, or the listing window of the page text; verify with a stub model that the prompt holds no boilerplate and stays under the cap
- [ ] 3.2 Skip rule: no call when step 1 gives the lots, or a point and no lot cue; verify a single-house fixture makes zero model calls and a two-lot description makes one
- [ ] 3.3 One live call on an invented two-lot farm description; verify two lots with the right acreages, and the artifact names the model

## 4. Step 3: titles by acreage (0 tokens)

- [ ] 4.1 `api/what3words.py` wire models and a converter used only with a key; verify with `httpx.MockTransport` for success, bad address and quota errors
- [ ] 4.2 Anchors per lot (portal polygon, what3words, listing point by best area match, postcode); verify the no-key case anchors one lot and lists the other as not placed
- [ ] 4.3 `listing/titles.py`: one planning.data polygon search per listing, greedy growth to acreage (20 m adjacency, ±10% or ±2 acres, 60-polygon cap, no title in two groups), score; unit-test on synthetic square fields for matched, overshoot, no-acreage and two-lot cases
- [~] 4.4 (partial: the placeholder square is gone; the stage returns `pin_parcel`, `candidates` and `site_parcels` from one planning.data polygon search, with `title_number`, `boundary_geojson`, `area_m2` as the union. `groups` wait for lots, 4.3) `stages/title.py` returns `groups` and `candidates` instead of the placeholder square; `TitleOutput` keeps `title_number`, `boundary_geojson`, `area_m2` as the union; verify `test_stages.py` passes and a two-lot fixture gives two groups
- [ ] 4.5 `resolve_from_link` runs the tiers and fills `LocationOutput.listing`; verify the existing single-property fixtures resolve to the same postcode and point

## 5. Step 4: human check

- [x] 5.1 `SiteDecision.title_ids`, validated against the candidates; the confirmed union becomes `ConfirmedSite.boundary` and an edit artifact; verify an unknown id is rejected and no ids keeps the proposal
- [x] 5.2 `collate` accepts a site polygon (the confirmed union) instead of the single title; verify areas and designations are measured against the union
- [~] 5.3 (partial: types, candidates drawn faintly, pin polygon outlined, site filled, click toggles, footprint share per polygon, `/inspire` bbox search and a "Load title polygons in view" button; the per-lot acreage summary waits for lots) Web: `types.ts` for groups and candidates; the map draws candidates faintly and chosen titles per lot, click toggles, a per-lot acreage summary; fill `/inspire` with a bbox search; verify in the browser that clicking a title changes the matched acreage and the confirmed run uses it
- [x] 5.4 `uv run pytest`, `ruff`, `mypy`; `npm run build` and `npm run lint` in `web/`

## 6. Step 5: plan image on low confidence

- [ ] 6.1 Gate: run only when a placed lot scores below 0.8 or an acreage lot has no anchor; verify a high-score fixture downloads no image
- [ ] 6.2 Plan pick: caption and file-name hints, then image statistics on 256 px downloads, floor plan gallery excluded, then at most one thumbnail classification call; verify on 3 recorded galleries that the sale plan is picked, and floor plans never are
- [ ] 6.3 One vision call: plan at up to 1,024 px beside our numbered candidate drawing; structured output of polygon numbers per lot; groups updated with `method="plan_image"`; cached per image URL and candidate set; verify a second run makes no model call and artifacts name the model
- [ ] 6.4 Token check: log tokens per step on 3 to 5 recorded listings; verify typical listings stay near 2k tokens (or 0) and the plan step under 6k
