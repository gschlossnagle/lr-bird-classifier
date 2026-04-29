# Pause Context

This note is the shortest path to resuming work on branch `annotation-eval-workflow` without re-deriving the current state.

## Current Status

The annotation/evaluation workflow is now functional enough for early local review testing.

Implemented:

- review SQLite store and validation
- queue helpers
- common-name resolver with canonicalization and compact-name support
- extraction orchestrator
- YOLO detector integration
- duplicate bird-box suppression in the YOLO detector
- duplicate suppression now combines:
  - heavy overlap / high IoU
  - near-containment
  - moderate-overlap plus weaker-confidence duplicate suppression
  - tiny-secondary-fragment suppression when a much larger bird box exists in-frame
- boxed preview generation
- embedded-preview-first RAW loading
- benchmark manifest export and materialized export scaffolding
- sampling helpers
- minimal local review web UI
- classifier-backed top-1 suggestion in the review UI via key `0`
- burst grouping from EXIF sequence metadata for `Make=SONY`, with conservative fallback
- burst apply in the review UI
- summary page with species/outcome counts
- automatic queue top-up from `review_app`

Focused local test status at pause:

- `python -m unittest tests.test_yolo_detector_local tests.test_catalog_extract tests.test_review_store tests.test_review_app_local`
- all tests passing at pause

## Local Runtime Assumptions

- YOLO is the chosen detector for v1
- review UI is fully local and intended for internal use
- extraction opens the Lightroom catalog read-only only
- PSD/PSB are excluded from extraction defaults
- generated runtime state lives under `tmp/` and `.ultralytics/` and is intentionally gitignored

## Pilot Data Used

Catalog used for testing:

- `/Users/george/Desktop/Master LR Catalog/Master/Master-v13-3.lrcat`

Useful folder filter:

- `VeroBeach`

Temporary local outputs used during development:

- DB: `/Users/george/src/codex-test/lr-bird-classifier/tmp/review.sqlite`
- previews: `/Users/george/src/codex-test/lr-bird-classifier/tmp/review_previews`
- labels: `/Users/george/src/codex-test/lr-bird-classifier/tmp/bird_labels.txt`

## Commands To Resume

Build label inventory:

```bash
python -m src.build_label_inventory \
  --output /Users/george/src/codex-test/lr-bird-classifier/tmp/bird_labels.txt
```

Run extraction on a small VeroBeach slice:

```bash
python -m src.extract_candidates \
  --catalog "/Users/george/Desktop/Master LR Catalog/Master/Master-v13-3.lrcat" \
  --db /Users/george/src/codex-test/lr-bird-classifier/tmp/review.sqlite \
  --preview-dir /Users/george/src/codex-test/lr-bird-classifier/tmp/review_previews \
  --detector src.detectors.yolo.YoloBirdDetector \
  --folder VeroBeach \
  --limit 25
```

Rebuild a scoped review queue from scratch:

```bash
python -m src.extract_candidates \
  --catalog "/Users/george/Desktop/Master LR Catalog/Master/Master-v13-3.lrcat" \
  --db /Users/george/src/codex-test/lr-bird-classifier/tmp/review.sqlite \
  --preview-dir /Users/george/src/codex-test/lr-bird-classifier/tmp/review_previews \
  --detector src.detectors.yolo.YoloBirdDetector \
  --folder VeroBeach \
  --limit 300 \
  --rescan
```

Run the local review UI:

```bash
python -m src.review_app \
  --db /Users/george/src/codex-test/lr-bird-classifier/tmp/review.sqlite \
  --labels-file /Users/george/src/codex-test/lr-bird-classifier/tmp/bird_labels.txt \
  --catalog "/Users/george/Desktop/Master LR Catalog/Master/Master-v13-3.lrcat" \
  --detector src.detectors.yolo.YoloBirdDetector \
  --preview-dir /Users/george/src/codex-test/lr-bird-classifier/tmp/review_previews \
  --folder VeroBeach \
  --batch-limit 100
```

Open:

- `http://127.0.0.1:8765/review`

Reset review state without rerunning extraction:

```bash
python -m src.reset_review_state \
  --db /Users/george/src/codex-test/lr-bird-classifier/tmp/review.sqlite
```

## Known Rough Edges

- the review UI is still a minimal shell, not a polished tool
- browser hotkeys require a page reload after JS changes
- suggestion support depends on the local classifier stack being runnable
- burst apply is explicit only and does not overwrite already reviewed candidates
- preview generation and detection now prefer embedded RAW previews, but this still needs real-world validation for adequacy
- duplicate detections have improved substantially, but the queue must be rebuilt with `--rescan` after detector-rule changes before the UI will reflect them
- after changing detector dedupe or burst-safe rules, a `--rescan` extraction is needed to rebuild stored candidates

## Most Likely Next Steps

If resuming from here, the most useful next work items are:

1. rerun a scoped `VeroBeach` `--rescan` so the latest detector dedupe rules are reflected in `review.sqlite`
2. continue live review and confirm the recent problem files no longer appear twice
3. once detector behavior is stable enough, move to extraction performance profiling
4. export a first tight manually reviewed benchmark set
5. build the evaluation runner on top of the exported manifests

## Performance Notes

Current extraction throughput is still too slow for large folders.

Measured on representative `VeroBeach` RAW files:

- `load_image(...)`: about `0.17s` per image
- `YoloBirdDetector.detect(...)`: about `0.47s` to `0.51s` per image
- first preview render for a source image: about `0.30s` to `0.35s`
- cached repeat preview render on the same source image: about `0.01s`

Current conclusions:

- main cost is YOLO inference
- second cost is image loading / embedded preview extraction
- preview generation mattered more before per-image preview caching was added
- DB work is minor

Next profiling step, once detector behavior is stable:

- add lightweight phase timing to extraction (`load_image`, `detect`, `build_preview`, DB writes)
- keep a fixed `VeroBeach` slice for before/after comparisons
- likely next optimization target is sharing a single decoded preview image between YOLO and preview rendering instead of loading twice

## Specific Open Issue At Pause

The current live issue is duplicate detections on the same source image. This shows up as:

- the UI appearing to require two passes on the same file before advancing
- some burst frames incorrectly treated as multi-detection / not burst-safe

Concrete examples discussed:

- `/Users/george/Desktop/Photos/2026/VeroBeach/2026-04-04/a9iii-1/A9307675.ARW`
- `/Users/george/Desktop/Photos/2026/VeroBeach/2026-04-04/a9iii-1/A9307576.ARW`
- `/Users/george/Desktop/Photos/2026/VeroBeach/2026-04-04/a9iii-1/A9307668.ARW`
- `/Users/george/Desktop/Photos/2026/VeroBeach/2026-04-04/a9iii-1/A9307726.ARW`
- `/Users/george/Desktop/Photos/2026/VeroBeach/2026-04-04/a9iii-1/A9307889.ARW`
- `/Users/george/Desktop/Photos/2026/VeroBeach/2026-04-04/a9iii-1/A9308231.ARW`
- `/Users/george/Desktop/Photos/2026/VeroBeach/2026-04-04/a9iii-1/A9308395.ARW`

Current status:

- detector-side dedupe heuristics have been expanded several times and now cover the known reported duplicate-box patterns in live detector checks
- `--rescan` now clears only the selected extraction scope before rebuilding, instead of merely resetting the cursor
- the next required step is not more theory work; it is to rebuild the scoped queue and verify the listed files no longer evaluate twice in the UI

## Design Files

The working design stack lives here:

- `working-plans/ANNOTATION_EVAL_PLAN.md`
- `working-plans/ANNOTATION_EVAL_CONTRACTS.md`
- `working-plans/ANNOTATION_REVIEW_SCHEMA.md`
- `working-plans/ANNOTATION_REVIEW_STATE_MACHINE.md`
- `working-plans/ANNOTATION_REVIEW_UI.md`
- `working-plans/ANNOTATION_EVAL_BUILD_PLAN.md`
- `working-plans/INITIAL_USER_TESTING_CHECKLIST.md`

## Review Run / Apply Pickup

This section captures the newer regular-user attended review work so it can be
resumed cleanly without re-deriving intent from chat history.

### Product Direction Locked In

There are now two distinct review consumers:

- `src.review_app`
  - expert-oriented
  - used to build ground-truth / evaluation datasets
  - retains expert outcomes like `reject`, `unsure`, `not_a_bird`,
    `bad_crop`, `duplicate`, and `stress`

- `src.review_run`
  - not implemented yet at pause
  - regular-user attended tagging workflow
  - should auto-apply high-confidence results
  - should defer lower-confidence images into `review_app`
  - should expose only `Save Label` and `Skip` in the review UI

The architectural plan for this is written in:

- `working-plans/REVIEW_RUN_IMPLEMENTATION_PLAN.md`

Recent planning commits relevant to this work:

- `df07694` `Add review-run implementation plan`
- `f3255ed` `Refine review-run UI workflow plan`
- `c8c45f4` `Update review-run plan for UI split`

### Code Completed In This Slice

The following foundational work is implemented but not committed yet:

- new shared label writer:
  - `src/label_apply.py`
- `src.run` refactored to use the shared writer
- `src.tag_bird` refactored to use the shared writer
- `src.review_store` schema bumped to v4
  - adds `review_scopes.workflow_type`
  - adds `candidate_seed_suggestions`
- `src.review_app` now has first-pass workflow-aware behavior:
  - `run_hybrid_review` only allows `save` and `skip`
  - server-side action validation rejects expert-only actions for that workflow
  - hybrid scopes hide stress / reject-style actions in the candidate view
  - hybrid scopes show a simplified summary
  - seeded suggestions are preferred before live classifier suggestions
- `src.catalog` now has `get_managed_keyword_names_for_image(...)`

Tests added/updated:

- `tests/test_label_apply.py`
- `tests/test_review_store.py`
- `tests/test_review_app_local.py`

### Verified State At Pause

Focused checks that passed after the above changes:

```bash
python -m unittest tests.test_review_store tests.test_label_apply tests.test_review_app_local
python -m py_compile src/label_apply.py src/run.py src/tag_bird.py src/review_store.py src/review_app.py
```

Note:

- one test run emitted a network-resolution warning from the eBird reference
  path during `tests.test_review_app_local`, but the suite still passed

### Worktree State At Pause

Uncommitted modified files:

- `src/catalog.py`
- `src/review_app.py`
- `src/review_store.py`
- `src/run.py`
- `src/tag_bird.py`
- `tests/test_review_app_local.py`
- `tests/test_review_store.py`

Untracked new files:

- `src/label_apply.py`
- `tests/test_label_apply.py`

### What Was In Progress When Paused

The next implementation slice had started conceptually but not yet landed:

1. `src.review_run`
   - new orchestration CLI
   - reuse current `run.py` image-selection / classifier / geo-filter behavior
   - auto-apply high-confidence images
   - seed low-confidence images into `review.db`
   - optionally host or launch `review_app`
   - after review, run reviewed-label apply

2. `src.apply_review_labels`
   - standalone expert / utility CLI
   - bulk apply reviewed labels from `review.db` to Lightroom

3. shared apply engine / state:
   - `src/review_apply.py`
   - `src/review_apply_state.py`

No files for those modules were created before pause.

### Recommended Next Steps

Resume in this order:

1. commit or stash the currently uncommitted foundation changes before starting
   the next slice
2. add `src/review_apply_state.py`
   - small SQLite ledger for apply runs and image-level fingerprints
3. add `src/review_apply.py`
   - query reviewed rows from `review.db`
   - group by image
   - consolidate to desired image-level outcomes
   - compare desired state to Lightroom current managed state using
     `get_managed_keyword_names_for_image(...)`
   - apply only when needed
4. add `src/apply_review_labels.py`
   - thin CLI around the shared apply engine
5. add `src/review_run.py`
   - use existing `run.py` logic as the model
   - add review DB seeding for low-confidence images
   - reuse `review_app` for the interactive phase

### Important Constraints To Preserve

- `src.run` stays the unattended entrypoint
- `src.review_app` does not write to Lightroom directly
- `run_hybrid_review` in `review_app` must remain label-and-skip only
- apply logic must always check Lightroom state directly for idempotency, even
  if an apply-state DB exists
- the shared writer in `src/label_apply.py` should remain the only place where
  managed Lightroom/XMP write semantics live
