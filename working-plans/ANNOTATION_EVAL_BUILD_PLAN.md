# Annotation And Evaluation Build Plan

This document turns the design specs into an implementation sequence.

The goal is not just to list tasks. It is to make the dependencies and stop-points explicit so we do not build the wrong thing too far before validating it.

This plan assumes the documents in `working-plans/` are the current source of truth for design decisions.

## Build Strategy

The right sequence is:

1. establish stable foundations
2. build the annotation workflow end to end
3. build benchmark export and validation
4. build the evaluation runner
5. only then expand into model-comparison and ensemble work

I want to stress-test one recurring temptation here: starting with multi-model or ensemble infrastructure first. That would be backwards. Without annotation and evaluation plumbing, model-comparison work has no reliable target.

## Phase 0: Repository Setup

Purpose:

- create the scaffolding needed for implementation without yet committing to heavy product decisions

Tasks:

- add the `working-plans/` docs already defined
- decide where new modules should live under `src/`
- choose whether the review UI is implemented as a local web app or a minimal desktop app
- add any lightweight developer dependencies needed for the review workflow

Deliverable:

- agreed implementation surface and module layout

Stop-point:

- do not start detector or UI work until the UI implementation form is chosen

Recommendation:

- default to a lightweight local web UI

Reason:

- better fit for image preview, zoom, and keyboard-driven review than a TTY
- lower friction than a native desktop app for v1

Additional requirement:

- the UI must run fully locally on a machine with direct access to the source images
- no remote rendering or hosted dependency should be assumed for core review behavior

## Phase 1: Core Review Store And Contracts

Purpose:

- make the persistence layer real before any UI logic starts encoding its own state rules

Tasks:

- implement the SQLite schema from `ANNOTATION_REVIEW_SCHEMA.md`
- add store initialization and migrations
- add typed access helpers or thin data-layer wrappers
- implement validation helpers for:
  - candidate rows
  - annotation rows
  - manifest rows

Suggested modules:

- `src/review_store.py`
- `src/review_validation.py`

Deliverables:

- database initialization works
- candidates can be inserted and queried
- annotations can be inserted, replaced, and validated

Checkpoint:

- write focused tests against schema invariants and replace semantics

Risk:

- if we skip validation now, the UI layer will quietly create inconsistent states that become hard to clean up later

## Phase 2: Candidate Extraction Pipeline

Purpose:

- produce reviewable bird candidates from the Lightroom catalog

Tasks:

- implement Lightroom image enumeration for candidate generation
- add preview acquisition pipeline
- add detector interface abstraction
- implement first detector backend, likely YOLO-based
- persist one candidate row per detection
- persist preview asset path and bbox metadata
- compute burst grouping heuristics

Suggested modules:

- `src/catalog_extract.py`
- `src/detector.py`
- `src/detectors/yolo.py`
- `src/review_assets.py`
- `src/burst_grouping.py`

Important constraint:

- optimize preview generation for speed
- do not default to full RAW rendering

Preferred preview-source investigation order:

1. embedded RAW preview extraction
2. Lightroom Standard Preview access
3. fallback RAW rendering

Checkpoint:

- run extraction on a small real catalog slice
- measure preview latency and detection throughput
- inspect whether preview resolution is sufficient for small and difficult birds

Stop-point:

- do not proceed to full annotation workflow until preview adequacy is checked on real images

Reason:

- this is one of the highest-risk usability assumptions in the whole system

## Phase 3: Review Queue And Annotation UI

Purpose:

- make human review fast, explicit, and resumable

Tasks:

- build queue loader over candidate rows
- implement single-candidate review screen
- implement image preview with bbox overlay and zoom-to-box
- implement label input and canonical name resolution
- implement recent-label reuse
- implement primary semantic outcomes
- implement pending `stress` modifier behavior
- implement skip/reopen/edit flows
- implement burst-apply for safe single-subject bursts

Suggested modules:

- `src/review_queue.py`
- `src/label_resolver.py`
- `src/review_tool.py`

Deliverables:

- one candidate can be reviewed end to end
- labels resolve to canonical truth fields
- recent-label shortcuts work
- queue state updates correctly
- resume works after closing the tool

Checkpoint:

- manually review a small batch from the real catalog
- confirm that repeated-label workflow is genuinely fast
- confirm that the state machine matches observed UI behavior

Risk:

- the biggest failure mode here is death by friction
- if labeling consecutive species runs is not almost effortless, the tool will be technically complete but practically unused

## Phase 4: Benchmark Export

Purpose:

- convert reviewed candidates into stable benchmark manifests

Tasks:

- implement export joins across images, candidates, and annotations
- export JSONL manifests for:
  - `catalog_real_world`
  - `catalog_stress`
  - `catalog_reject`
- implement materialized dataset export that writes standalone JPEG benchmark assets to a separate durable location
- enforce a configured maximum exported JPEG size during materialized dataset export
- enforce a configured minimum JPEG quality floor during materialized dataset export
- implement manifest validator
- add sampling logic for `natural`, `balanced`, and `hybrid` export modes

Suggested modules:

- `src/export_manifest.py`
- `src/eval_manifest.py`
- `src/sampling.py`

Checkpoint:

- export a reviewed sample set
- validate schema correctness
- verify that hybrid sampling prevents common-species dominance
- verify that the exported dataset can be used without Lightroom catalog access
- verify that exported JPEG sizing remains under the configured cap without making difficult species-review cases unusable

Important caution:

- benchmark export should remain a separate step from annotation
- do not hardwire split assignment logic into the review save flow

## Phase 5: Evaluation Runner

Purpose:

- evaluate one or more model configurations against benchmark manifests

Tasks:

- extract shared inference logic from current `src/run.py`
- build reusable prediction pipeline module
- implement manifest loader
- implement single-model evaluation
- emit per-sample result rows and summary metrics
- support geo-filter toggles and manifest-provided region hints

Suggested modules:

- `src/pipeline.py`
- `src/eval.py`
- `src/eval_metrics.py`
- `src/eval_report.py`

Deliverables:

- evaluate a benchmark manifest with one model
- produce summary and per-sample outputs
- compute primary metrics including accepted precision and coverage

Checkpoint:

- benchmark the current bird model against an initial control or catalog set
- verify that reported outputs match expectations on hand-checked examples

## Phase 6: Control Dataset Support

Purpose:

- add clean external benchmark inputs alongside catalog-derived sets

Tasks:

- support import of `control_inat` manifests
- validate that benchmark manifests from external sources align with the same schema
- ensure metrics are sliced separately by `source_type`

Checkpoint:

- evaluate the same model on both control and catalog-derived data
- compare deltas instead of collapsing them into one headline metric

Reason:

- if control and catalog-real-world results diverge, that is important signal, not noise to average away

## Phase 7: Multi-Model Comparison

Purpose:

- compare candidate models before building ensemble logic

Tasks:

- implement model registry/configuration surface
- allow evaluation of multiple named models in the same run
- persist per-model raw predictions in result outputs
- compare model slices by taxon, source type, and region

Suggested modules:

- `src/model_registry.py`

Checkpoint:

- confirm whether a general wildlife model actually improves the target use cases
- confirm whether bird-specialist performance is degraded or preserved

Strong caution:

- do not build ensemble fusion until per-model evaluation is working cleanly

## Phase 8: Ensemble Evaluation

Purpose:

- test whether an ensemble earns its complexity

Tasks:

- implement ensemble strategy surface
- start with simple weighted fusion
- compare against the best single model
- inspect calibration quality across models

Suggested modules:

- `src/ensemble.py`

Checkpoint:

- require evidence that ensemble improves meaningful metrics
- if not, keep the simpler single-model path

This is an explicit decision gate, not a guaranteed implementation step.

## Recommended Implementation Order Inside The Codebase

If building sequentially with minimal thrash, the internal order should be:

1. `review_store`
2. `review_validation`
3. `catalog_extract`
4. `review_assets`
5. `detector` / first detector backend
6. `review_queue`
7. `label_resolver`
8. review UI
9. `export_manifest`
10. `sampling`
11. shared `pipeline`
12. `eval`
13. `eval_metrics`
14. `eval_report`
15. `model_registry`
16. `ensemble`

## Suggested Validation Gates

These are the points where we should stop and verify before continuing.

### Gate 1: Preview Feasibility

Question:

- are fast preview sources actually adequate for review?

If no:

- revisit preview-source strategy before UI completion

### Gate 2: Annotation Throughput

Question:

- can the reviewer move quickly through repeated-label clusters without frustration?

If no:

- fix the UI before investing in exporter or eval sophistication

### Gate 3: Manifest Quality

Question:

- do exported manifests represent the intended benchmark populations without obvious species-frequency bias?

If no:

- fix sampling/export before trusting evaluation results

### Gate 4: Single-Model Evaluation Credibility

Question:

- do evaluation outputs match hand-inspected examples and expected behavior?

If no:

- stop before multi-model comparison

### Gate 5: Ensemble Justification

Question:

- does ensemble beat the best single model enough to justify complexity?

If no:

- reject or defer the ensemble path

## What I Would Defer

To keep v1 disciplined, I would defer:

- full multi-label scoring logic
- polished analytics dashboards
- aggressive burst-overwrite workflows
- native desktop app packaging
- crop-asset-first review mode
- production integration of multi-model ensemble tagging

These can all wait until the annotation and evaluation pipeline proves itself.

## Recommended Next Build Step

The next build step should be:

- implement the review-store layer and validation helpers

That is the right foundation because every later phase depends on it, and it is the cheapest place to enforce correctness.
