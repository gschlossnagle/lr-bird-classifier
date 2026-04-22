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
- boxed preview generation
- embedded-preview-first RAW loading
- benchmark manifest export and materialized export scaffolding
- sampling helpers
- minimal local review web UI
- classifier-backed top-1 suggestion in the review UI via key `0`
- conservative burst grouping and explicit burst-apply action

Focused local test status at pause:

- `python -m unittest tests.test_build_label_inventory_local tests.test_review_app_local tests.test_review_suggester_local tests.test_raw_utils_local tests.test_review_assets_local tests.test_review_store tests.test_eval_manifest_local tests.test_catalog_extract tests.test_sampling_local tests.test_yolo_detector_local`
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

Run the local review UI:

```bash
python -m src.review_app \
  --db /Users/george/src/codex-test/lr-bird-classifier/tmp/review.sqlite \
  --labels-file /Users/george/src/codex-test/lr-bird-classifier/tmp/bird_labels.txt
```

Open:

- `http://127.0.0.1:8765/review`

## Known Rough Edges

- the review UI is still a minimal shell, not a polished tool
- browser hotkeys require a page reload after JS changes
- suggestion support depends on the local classifier stack being runnable
- burst grouping is intentionally conservative and based on folder + close capture time
- burst apply is explicit only and does not overwrite already reviewed candidates
- preview generation and detection now prefer embedded RAW previews, but this still needs real-world validation for adequacy

## Most Likely Next Steps

If resuming from here, the most useful next work items are:

1. tighten the review UI ergonomics for faster annotation sessions
2. validate embedded-preview adequacy on harder bird images
3. improve reset/reload UX for the local review DB
4. start exporting a tight manually reviewed benchmark set
5. build the evaluation runner on top of the exported manifests

## Design Files

The working design stack lives here:

- `working-plans/ANNOTATION_EVAL_PLAN.md`
- `working-plans/ANNOTATION_EVAL_CONTRACTS.md`
- `working-plans/ANNOTATION_REVIEW_SCHEMA.md`
- `working-plans/ANNOTATION_REVIEW_STATE_MACHINE.md`
- `working-plans/ANNOTATION_REVIEW_UI.md`
- `working-plans/ANNOTATION_EVAL_BUILD_PLAN.md`
- `working-plans/INITIAL_USER_TESTING_CHECKLIST.md`

