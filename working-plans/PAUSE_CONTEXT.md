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
