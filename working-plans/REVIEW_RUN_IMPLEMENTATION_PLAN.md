# Review Run Implementation Plan

This document specifies the planned attended-review workflow for regular users,
the supporting bulk-apply utility, and the boundaries between those tools and
the existing expert-oriented review workflow.

It is intentionally implementation-focused. The goal is to make the build order,
module boundaries, schema changes, and idempotency rules explicit before code
changes start.

## Problem Statement

The project currently has two distinct behaviors:

- `src.run` classifies and tags Lightroom images fully unattended
- `src.review_app` supports interactive review in a separate SQLite review DB

Those workflows solve different problems today:

- `src.run` is for periodic catalog tagging
- `src.review_app` is for expert review and ground-truth dataset creation

The missing piece is a low-friction regular-user attended flow that:

1. auto-tags easy images immediately
2. routes lower-confidence images into the existing review UI
3. applies the reviewed labels back to Lightroom afterwards
4. remains idempotent across reruns and interruptions

That new flow must not turn `review_app` into a Lightroom write tool, and it
must not force the user to classify the same image twice.

## Goals

- Keep `src.run` focused on unattended tagging.
- Add a separate regular-user attended entrypoint.
- Reuse `review_app` as directly as possible to minimize UI drift.
- Add a separate bulk-apply tool that can be used both by experts and by the
  regular-user attended workflow internally.
- Make all phases idempotent.
- Always verify Lightroom state directly during apply, even when an internal
  apply ledger says an image was already handled.

## Non-Goals

- Do not make `review_app` write directly to Lightroom on each review action.
- Do not merge the expert ground-truth workflow and the regular-user review-run
  workflow into one indistinguishable command.
- Do not rely solely on an apply-state DB for idempotency.
- Do not create a second independent tagging/write implementation.

## Tool Roles

### `src.run`

Purpose:

- fully unattended classification and tagging

Rules:

- remains the existing production auto-tagging tool
- does not gain major attended-review orchestration responsibilities
- may be internally refactored onto shared libraries, but its user-facing role
  stays narrow

### `src.review_run`

Purpose:

- regular-user attended workflow

Rules:

- intended to be run periodically by less technical users
- should feel like one end-to-end workflow
- should do the final Lightroom writeback as part of the overall flow
- should use `review_app` for the interactive phase rather than inventing a new
  UI

### `src.review_app`

Purpose:

- expert-oriented review UI
- human labeling into `review.db`

Rules:

- remains a review-state tool
- still suitable for ground-truth dataset creation
- must not write to Lightroom directly
- should support the new regular-user workflow by reusing the same core review
  mechanics with minimal UI branching

### `src.apply_review_labels`

Purpose:

- explicit bulk-apply utility

Rules:

- can be used directly after `review_app`
- is also used internally by `review_run`
- owns the image-level consolidation and idempotent writeback behavior

## User Workflows

### Regular User Workflow

1. User runs `src.review_run`.
2. High-confidence images are tagged unattended.
3. Lower-confidence images are deferred into `review.db`.
4. `review_app` is used to review the deferred queue.
5. Reviewed labels are bulk-applied back to Lightroom.
6. Rerunning later safely skips already-correct items and resumes unfinished
   work.

### Expert Workflow

1. User runs extraction and `src.review_app` for review/ground-truth work.
2. Review state is stored in `review.db`.
3. If desired, user later runs `src.apply_review_labels` to bulk sync reviewed
   outcomes to Lightroom.

The expert workflow remains decoupled from Lightroom writes until the explicit
apply step.

## Core Architectural Decisions

### Separate CLI For Attended Review

The attended flow will not be added as a major `src.run` mode flag.

Instead:

- keep `src.run` unattended
- add `src.review_run` as a dedicated regular-user CLI

Reason:

- the operational model diverges materially from `src.run`
- attended orchestration would otherwise pollute the unattended entrypoint
- a dedicated CLI keeps the help surface and maintenance burden clearer

### Separate Apply Phase

The interactive review UI will not write to Lightroom automatically.

Instead:

- `review_app` writes review decisions into `review.db`
- `apply_review_labels` reads `review.db` and applies results to Lightroom
- `review_run` invokes that same apply logic internally as a later phase

Reason:

- keeps review fast and resumable
- makes writeback explicit and auditable
- enables dry-run and conflict reporting
- avoids catalog side effects merely from browsing or editing review state

### Shared Lightroom/XMP Writer

All Lightroom/XMP managed label writes must converge on one shared module.

That shared writer will be used by:

- `src.run`
- `src.review_run`
- `src.apply_review_labels`
- `src.tag_bird`

Reason:

- prevents drift between unattended, manual, and review-derived tagging
- centralizes managed-tag semantics
- centralizes Lightroom-state inspection and verification helpers

## Threshold Model For `review_run`

`review_run` uses two conceptually separate thresholds:

- `--min-confidence`
  - minimum confidence required to treat a prediction as a valid bird
    classification candidate at all

- `--auto-apply-threshold`
  - if the best prediction meets or exceeds this threshold, `review_run`
    applies it unattended
  - otherwise the image is deferred into interactive review

Constraint:

- `auto_apply_threshold >= min_confidence`

Recommended v1 behavior:

- if no prediction meets `min-confidence`, skip and report
- if best prediction is `>= auto-apply-threshold`, auto-apply
- if best prediction is `>= min-confidence` but `< auto-apply-threshold`,
  defer to review

## Review DB Design

The review DB remains the source of truth for human review outcomes.

### Scope Typing

Add `workflow_type` to `review_scopes`.

Values:

- `detector_review`
- `run_hybrid_review`

Purpose:

- allow the DB to hold both expert detector-review scopes and regular-user
  review-run scopes
- enable minimal workflow-aware rendering or behavior when necessary

### Seeded Classifier Suggestions

Add table `candidate_seed_suggestions`.

Schema:

- `candidate_id TEXT PRIMARY KEY`
- `model TEXT NOT NULL`
- `best_truth_label TEXT`
- `best_common_name TEXT`
- `best_sci_name TEXT`
- `best_confidence REAL`
- `top_predictions_json TEXT`
- `geo_filtered INTEGER NOT NULL DEFAULT 0`
- `seeded_at TEXT NOT NULL`

Purpose:

- preserve the exact `review_run` classifier suggestion shown to the reviewer
- avoid suggestion drift caused by recomputing suggestions inside `review_app`
- allow `review_app` to prefer the seeded suggestion when present

### Hybrid-Review Candidate Shape

For `run_hybrid_review`, create exactly one candidate per source image.

Rules:

- one `images` row per source image in scope
- one `candidates` row per image for the regular-user review-run scope
- stable candidate ID, e.g. `runimg_{source_image_id}`
- preview is full-image rather than detector-box-centered semantics

Reason:

- the regular-user workflow is image-labeling oriented, not object-detection
  benchmark oriented
- one image should not require multiple pseudo-review passes unless there is a
  real design reason

## Apply DB Design

Add a small standalone SQLite DB for bulk-apply tracking.

This DB is not the sole idempotency authority. It is an incremental ledger and
audit trail. Lightroom catalog state is still checked on every apply run.

### `apply_runs`

- `id INTEGER PRIMARY KEY`
- `started_at TEXT NOT NULL`
- `ended_at TEXT`
- `review_db_path TEXT NOT NULL`
- `catalog_path TEXT NOT NULL`
- `scope_key TEXT`
- `policy_version TEXT NOT NULL`
- `dry_run INTEGER NOT NULL`
- `summary_json TEXT`

### `image_apply_state`

- `image_key TEXT PRIMARY KEY`
- `catalog_path TEXT NOT NULL`
- `source_image_id INTEGER`
- `source_image_path TEXT NOT NULL`
- `scope_key TEXT`
- `last_review_fingerprint TEXT NOT NULL`
- `last_catalog_fingerprint TEXT`
- `last_applied_outcome_json TEXT NOT NULL`
- `last_applied_at TEXT NOT NULL`
- `last_run_id INTEGER NOT NULL`
- `status TEXT NOT NULL`
- `message TEXT`

### `image_apply_events`

- `id INTEGER PRIMARY KEY`
- `run_id INTEGER NOT NULL`
- `image_key TEXT NOT NULL`
- `event_type TEXT NOT NULL`
- `review_fingerprint TEXT`
- `catalog_fingerprint_before TEXT`
- `catalog_fingerprint_after TEXT`
- `details_json TEXT`
- `created_at TEXT NOT NULL`

### Image Key Strategy

Preferred:

- normalized catalog path + Lightroom source image ID

Fallback:

- normalized catalog path + resolved source image path

## Idempotency Contract

Idempotency is a mandatory design constraint.

### Review Seeding Idempotency

Running `review_run` multiple times with unchanged inputs must not:

- duplicate review scopes
- duplicate image rows for the same logical scope
- duplicate hybrid-review candidates
- duplicate seeded suggestion rows
- reapply unchanged high-confidence images unless explicitly requested

### Review Annotation Idempotency

The review DB must continue to behave as an upsert-based annotation store:

- one annotation row per candidate
- candidate review state updated in place
- seeded suggestions overwritten deterministically

### Apply Idempotency

Running `apply_review_labels` or the apply phase of `review_run` multiple times
must not:

- duplicate Lightroom keyword assignments
- reapply already-correct image states needlessly
- create duplicate apply ledger state
- silently trust stale apply ledger entries when Lightroom state differs

### Lightroom Verification Requirement

Every apply run must always:

1. compute desired state from `review.db`
2. inspect current Lightroom managed state
3. compare desired vs actual state
4. skip verified-correct images
5. apply only when needed
6. re-read Lightroom state after write
7. update `apply.db`

`apply.db` is an optimization and audit log, not a substitute for checking the
catalog.

## Managed Lightroom State Boundary

The shared writer must define and own a narrow managed-state boundary.

Managed state includes:

- `Bird-Species > ...`
- `Birds > Order > ...`
- `Birds > Scientific > ...`
- confidence-band keywords
- optional manually-classed marker when policy enables it

The writer may replace only this managed state.

The writer must not remove or rewrite unrelated user keywords.

## Image-Level Consolidation Rules

Bulk apply works at the image level, not the candidate level.

### Default v1 Policy

For each source image:

- if there is exactly one distinct labeled species outcome:
  - apply that species
- if there are multiple distinct labeled species outcomes:
  - if `--allow-multi-species`, apply all
  - otherwise report a conflict and skip
- if there are no labeled species outcomes:
  - do not apply species tags
  - optionally report non-applying outcomes

Statuses that do not apply species labels by default:

- `reject`
- `unsure`
- `not_a_bird`
- `bad_crop`
- `duplicate`
- `skipped`
- `unreviewed`

This default is intentionally conservative.

## Fingerprints

### Review Fingerprint

Compute a stable fingerprint from the desired normalized image-level outcome.

Inputs may include:

- sorted truth labels to apply
- whether multi-species apply is enabled
- whether stress affects apply policy
- whether manual marker policy is enabled
- policy version

### Catalog Fingerprint

Compute a stable fingerprint from Lightroom’s current managed label state.

Inputs may include:

- currently attached managed species labels
- current managed hierarchy completeness
- confidence-band marker if managed
- manual-classed marker if managed

### Decision Rules

- if the review fingerprint is unchanged and Lightroom already matches desired
  state, record a verified skip
- if the review fingerprint is unchanged but Lightroom differs, repair the
  image by applying again
- if the review fingerprint changed, apply the new desired state
- if `--force-reapply` is set, apply even when unchanged, then verify again

## Module Plan

### `src/label_apply.py`

New shared module.

Responsibilities:

- normalize species payloads for writeback
- inspect current managed Lightroom state
- compute current managed Lightroom fingerprint
- remove prior managed auto labels when requested
- apply one or more species labels
- sync XMP deterministically
- update classification or manual logs as appropriate

All Lightroom/XMP writes must go through this module.

### `src/review_run_seed.py`

New shared module for attended-run seeding.

Responsibilities:

- image enumeration using `run.py`-equivalent rules
- skip/manual/already-tagged logic
- classifier invocation
- geo-filtering
- auto-apply vs defer decisioning
- inserting deferred items into the review DB
- writing seeded suggestion rows

### `src/review_apply_state.py`

New module for `apply.db` schema and persistence.

Responsibilities:

- schema init/migration
- run start/finish bookkeeping
- image state lookup/upsert
- event logging

### `src/review_apply.py`

New shared apply engine.

Responsibilities:

- query reviewed image-level data from `review.db`
- consolidate candidate outcomes per image
- compute review fingerprints
- inspect Lightroom current state via `label_apply.py`
- compare review state, catalog state, and apply ledger state
- apply changes when needed
- verify post-write state
- write apply ledger updates

### `src/review_run.py`

New regular-user CLI.

Responsibilities:

- parse regular-user arguments
- run seeding and auto-apply phase
- launch or surface the `review_app` review session
- invoke the apply engine afterward unless disabled
- print a concise, user-friendly summary

### `src/apply_review_labels.py`

New expert/utility CLI.

Responsibilities:

- parse apply-specific arguments
- configure the shared apply engine
- run dry-run or real apply
- emit summary and optional report

## File-By-File Build Plan

### 1. Add Shared Lightroom/XMP Writer

Create:

- `src/label_apply.py`

Refactor:

- `src/run.py`
- `src/tag_bird.py`

Outcome:

- all managed label writes now go through one path

### 2. Extend Review DB Schema

Update:

- `src/review_store.py`

Add:

- `workflow_type` support
- seeded suggestion table and accessors
- any helper methods needed by hybrid-review seeding and reviewed-image queries

Outcome:

- review DB can hold both workflow types safely

### 3. Add Attended-Run Seeding Engine

Create:

- `src/review_run_seed.py`

Outcome:

- shared logic exists for regular-user attended review preparation

### 4. Add `review_run` CLI

Create:

- `src/review_run.py`

Outcome:

- regular users have a dedicated entrypoint with low-friction defaults

### 5. Teach `review_app` To Prefer Seeded Suggestions

Update:

- `src/review_app.py`

Changes:

- if a seeded suggestion exists for the candidate, use it first
- otherwise fall back to the existing live suggestion behavior
- keep UI branching minimal

Outcome:

- suggestion behavior stays aligned with `review_run`

### 6. Add Apply Ledger Persistence

Create:

- `src/review_apply_state.py`

Outcome:

- apply runs and image-level apply state are tracked explicitly

### 7. Add Shared Apply Engine

Create:

- `src/review_apply.py`

Outcome:

- one reusable engine exists for both expert apply and regular-user post-review
  apply

### 8. Add Standalone Apply CLI

Create:

- `src/apply_review_labels.py`

Outcome:

- expert workflow has an explicit bulk-apply tool

### 9. Stabilize Docs

Update:

- `README.md`
- `docs/GROUND_TRUTH_EVALUATION_WORKFLOW.md`
- optionally add a dedicated review-run workflow doc

Outcome:

- the two workflows and their intended audiences are documented clearly

## CLI Plan

### `src.review_run`

Suggested flags:

- `--catalog PATH`
- `--review-db PATH`
- `--preview-dir PATH`
- `--labels-file PATH`
- `--formats CSV`
- `--folder TEXT`
- `--min-confidence FLOAT`
- `--auto-apply-threshold FLOAT`
- `--top-k INT`
- `--min-stars INT`
- `--model NETWORK/TAG`
- `--region REGION`
- `--no-geo-filter`
- `--no-skip-tagged`
- `--retag-below-confidence FLOAT`
- `--remap FROM:TO`
- `--host HOST`
- `--port PORT`
- `--no-launch-ui`
- `--no-apply-reviewed`
- `--dry-run`
- `--verbose`

Behavior:

- validate threshold relationship
- if no deferred items remain, finish without UI
- if deferred items exist, surface a local review session
- after review, run apply unless explicitly disabled

### `src.apply_review_labels`

Suggested flags:

- `--db PATH`
- `--catalog PATH`
- `--state-db PATH`
- `--scope KEY`
- `--dry-run`
- `--force-reapply`
- `--allow-multi-species`
- `--include-stress`
- `--mark-manually-classed`
- `--only-status labeled`
- `--report PATH`
- `--verbose`

Behavior:

- explicit apply or dry-run against reviewed rows
- conflict reporting
- verification summary

## Recommended v1 Behavior Choices

### Browser/Launch Behavior

Recommended:

- `review_run` always prints the local review URL
- optional browser auto-open can be deferred or added later

Reason:

- simpler and more robust across environments

### Review Completion Behavior

Recommended:

- `review_run` does not try to infer that every review item is complete in a
  complex way
- it should apply whatever is currently reviewed after the UI session phase and
  report remaining unreviewed items clearly

Reason:

- simpler first version
- still low-friction for regular users

### No-Confident-Prediction Images

Recommended v1:

- skip and report
- do not defer them into the review queue yet

Reason:

- avoids turning the attended queue into a general abstention triage workflow
  immediately

### Manual-Classed Marker For Reviewed Outcomes

Recommended:

- reviewed-and-applied outcomes are marked as manually classed by default

Reason:

- prevents the unattended run from fighting reviewed results later

## Testing Plan

### Unit Tests

Add coverage for:

- managed-label state inspection
- managed-label fingerprint generation
- review fingerprint generation
- seeded suggestion upsert/read
- hybrid-review idempotent candidate creation
- image-level consolidation rules
- conflict handling

### Integration Tests

Add coverage for:

- `review_run` rerun does not duplicate deferred candidates
- `apply_review_labels` rerun with unchanged review data is a verified no-op
- `apply.db` says applied but Lightroom catalog state differs, causing repair
- multi-species conflict skip/report behavior
- unrelated user keywords survive managed-state replacement

### Manual Tests

Exercise:

- normal regular-user `review_run` flow
- expert `review_app` then `apply_review_labels`
- interrupted apply then rerun
- interrupted review-run seeding then rerun

## Implementation Order

Recommended build order:

1. `src/label_apply.py`
2. refactor `src/run.py`
3. refactor `src/tag_bird.py`
4. extend `src/review_store.py`
5. add `src/review_run_seed.py`
6. update `src/review_app.py` for seeded suggestions
7. add `src/review_apply_state.py`
8. add `src/review_apply.py`
9. add `src/apply_review_labels.py`
10. add `src/review_run.py`
11. update docs and test coverage

Reason:

- shared write semantics need to stabilize first
- DB contracts should exist before orchestration builds on them
- apply engine should exist before `review_run` depends on it for final
  writeback

## Minimum Viable Milestone

The smallest useful milestone that satisfies the main workflow is:

1. shared writer extraction
2. `review_run` seeding plus auto-apply plus review UI launch
3. seeded suggestion support in `review_app`
4. `apply_review_labels` with single-species-only consolidation
5. Lightroom verification plus `apply.db` ledger

That is enough to deliver:

- low-friction regular-user attended review
- no double-classification burden
- explicit expert apply support
- full idempotent writeback semantics

## Acceptance Criteria

The implementation is complete when the following are true:

- `src.run` still performs unattended tagging and uses the shared writer
- `src.tag_bird` uses the shared writer
- `src.review_run` exists and supports the hybrid attended flow
- `review_app` can review deferred `review_run` items without a separate UI
- `src.apply_review_labels` can bulk-apply reviewed results from `review.db`
- rerunning `review_run` does not duplicate review work
- rerunning apply does not duplicate Lightroom changes
- apply always verifies the Lightroom catalog directly before deciding to skip
- the expert ground-truth review workflow remains intact
