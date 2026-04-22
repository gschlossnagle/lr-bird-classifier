# Annotation And Evaluation Plan

This document is the working design note for the annotation workflow, benchmark construction, and model-evaluation framework on branch `annotation-eval-workflow`.

It is intended to stay current as the design changes. The goal is to keep the reasoning and decisions close to the code instead of relying on thread history.

## Scope

The current project is a bird-only Lightroom classifier. The planned expansion adds:

- multi-model support
- ensemble evaluation and selection
- evaluation datasets for control, real-world, stress, and reject cases
- a detector-assisted annotation workflow for building labeled benchmark data

This document covers design only. It does not imply that every section has been implemented.

## Guiding Principles

- Separate candidate discovery from truth labeling.
- Separate truth labeling from benchmark split construction.
- Optimize the annotation UI for rapid human confirmation, not automatic inference.
- Keep evaluation logic independent of Lightroom writeback.
- Use the same inference pipeline for `run` and `eval` so benchmark results reflect production behavior.
- The annotation UI and review workflow must run fully locally on a machine with direct access to the target images.
- Once a benchmark set is finalized, it should be materialized into a durable standalone dataset outside the Lightroom catalog and preview cache.

## Planned Evaluation Sets

There will be multiple evaluation pools with distinct purposes.

### `control_inat`

Purpose:
- compare models in a relatively clean biodiversity-image domain
- measure whether a broader wildlife model degrades bird performance
- provide a stable control benchmark

Characteristics:
- iNat-style labeled images
- birds, mammals, reptiles
- mostly clear single-subject images
- should still include some near-neighbor confusions

### `catalog_real_world`

Purpose:
- estimate tagging quality on the user's actual Lightroom workflow
- test thresholding, abstention, and geo-filtering under realistic conditions

Characteristics:
- manually labeled from the personal catalog
- sampled, not cherry-picked
- should reflect actual shooting conditions and archive composition
- once finalized, exported samples should be copied as rendered JPEG assets into a standalone dataset location

### `catalog_stress`

Purpose:
- expose failure modes and compare models on hard cases

Characteristics:
- manually flagged difficult positives
- curated, not sampled representatively
- examples include distant subjects, occlusions, confusing species, partial views, and difficult lighting

### `catalog_reject`

Purpose:
- measure whether the system refrains from confidently tagging unusable or irrelevant images

Characteristics:
- detector false positives
- no visible bird
- bird too small to identify
- severe blur or unusable crop

## Annotation Workflow

The annotation pipeline has three stages:

1. candidate extraction
2. human review and labeling
3. benchmark export and sampling

### Candidate Extraction

The extractor scans Lightroom catalog images and runs a bird detector. The detector should be abstracted behind an interface, even if YOLO is the first implementation.

Each candidate is object-level:

- one source image
- one detection box
- one annotation target

The extractor should persist:

- source image path
- image metadata
- detector confidence
- bbox coordinates
- review status
- optional burst grouping metadata

The extractor should not be treated as ground truth.

### Review UI

The review interface should be keyboard-first and optimized for rapid, explicit human confirmation.

Primary review presentation:

- full image preview with detection box overlay
- optional zoom to the detection box
- source metadata summary
- label input with autocomplete and canonical resolution
- recent-label history

The default design no longer requires a precomputed crop image for every candidate. A boxed full-image preview is the primary artifact for v1.

Performance note:

- the vast majority of review candidates are expected to come from RAW source images
- preview generation must optimize for speed rather than full-fidelity RAW rendering
- preferred preview sources for review should be, in order of practicality:
  - Lightroom Standard Preview when available
  - embedded JPEG preview from the RAW file
  - slower fallback rendering only when no fast preview source exists

This should remain an explicit implementation requirement because preview latency will strongly affect annotation throughput.

Dataset durability note:

- review-time previews are workflow assets and may depend on Lightroom state or ephemeral cache locations
- finalized benchmark datasets should be exported as standalone JPEG renders plus manifest files in a separate durable location
- evaluation should be able to run against those exported datasets without requiring ongoing Lightroom catalog access
- finalized exported JPEGs should obey a maximum file-size cap so the dataset remains practical to store, sync, and reuse

Catalog scan note:

- the annotation extraction workflow should skip PSD and PSB files entirely
- the goal is to build an evaluation dataset from photographic source material, not derivative Photoshop documents

### Review Actions

For each candidate, the reviewer can:

- assign a species label
- mark `stress`
- mark `reject`
- mark `unsure`
- mark `not_a_bird`
- mark `bad_crop`
- mark `duplicate`
- skip for later

Important design decision:

- `stress` is an orthogonal flag, not part of the label
- `real_world` is not manually assigned in the UI
- confirmed non-stress labeled items become the pool for `catalog_real_world`

### Label Resolution

The reviewer enters a common name. The system resolves that to:

- canonical common name
- scientific name
- canonical truth label
- taxon class

If the mapping is ambiguous, the user must choose explicitly. The tool should never silently choose between ambiguous taxonomy matches.

### Recent Label Reuse

Repeated species runs are expected when reviewing date-ordered images. The UI should optimize for reuse without auto-labeling.

Required behavior:

- recent resolved labels are visible and quickly reusable
- explicit confirmation is required per candidate
- labels are reused as canonical mappings, not as raw free text

Current shortcut plan:

- `R`: apply last resolved label and advance
- `1-5`: apply recent label and advance
- `Shift+R`: apply last resolved label, mark `stress`, advance
- `Shift+1-5`: apply recent label, mark `stress`, advance
- `T`: toggle `stress`
- `Enter`: save current typed or selected label and advance

Additional status toggles:

- `X`: reject
- `U`: unsure
- `N`: not a bird
- `B`: bad crop
- `D`: duplicate
- `K`: skip

## Burst Grouping

Burst grouping is useful, but it should be treated as a guarded heuristic rather than a universal rule.

Working assumption:

- a burst can often be treated as a single tracked subject when it contains exactly one subject across frames

Constraints:

- no automatic carry-forward of labels
- burst-level application must always be explicit
- the UI may offer `apply current label to remaining safe items in burst`

A burst is only considered safe for batch application when:

- images are in the same burst group
- each relevant frame has exactly one bird detection
- no frame in the burst violates single-subject assumptions

## Bias Control In Catalog Sampling

Naive random sampling over all bird images would bias the benchmark toward common species in the archive.

The current plan is:

- use detector-assisted extraction to build a candidate pool
- manually label reviewed items
- generate evaluation sets using stratified sampling

Planned sampling modes:

- `natural`
- `balanced`
- `hybrid`

Recommended default for benchmark generation:

- `hybrid`

Expected hybrid behavior:

- minimum examples per species where available
- maximum cap per species
- remaining quota filled using underrepresentation-aware weighting

## Evaluation Framework

The evaluation framework should be a first-class subsystem, not an ad hoc script.

Planned modules:

- `src/pipeline.py`
- `src/eval.py`
- `src/eval_manifest.py`
- `src/eval_metrics.py`
- `src/eval_report.py`

The core requirement is that the same prediction pipeline should support both production tagging and offline evaluation.

### Manifest Expectations

The preferred manifest format is JSONL.

Each sample should include at least:

- image path
- truth label
- truth scientific name

Recommended optional fields include:

- sample id
- taxon class
- region
- split source
- difficulty
- notes
- multi-label support fields for future use

Current recommendation:

- support schema fields for multi-label data now
- defer multi-label scoring behavior in v1

### Metrics

Planned metrics:

- top-1 accuracy
- top-3 accuracy
- top-5 accuracy
- per-class accuracy
- per-region accuracy
- runtime per image
- accepted prediction precision at threshold
- coverage at threshold
- geo-filter delta
- calibration-oriented metrics for future ensemble work

The primary product-facing metric should not be raw top-1 accuracy alone. It should emphasize the quality of predictions the system would actually write.

## Multi-Model And Ensemble Direction

The broader design target is to support:

- multiple model backends
- taxon-aware model selection
- ensemble comparison and possible deployment

Planned design principles:

- refactor bird-only assumptions into taxon-aware abstractions
- persist per-model outputs during evaluation
- do not ship ensemble fusion until the evaluation framework can prove it improves results

The design should be allowed to reject the ensemble idea if evaluation shows that a simpler configuration performs as well or better.

## Current Open Questions

- exact SQLite schema for annotation persistence
- exact UI implementation form for the review tool
- exact export contract between annotation output and evaluation manifest input
- whether burst-level review should be part of v1 or a follow-on optimization

## Change Log

### 2026-04-22

- Created working branch `annotation-eval-workflow`.
- Established separate benchmark pools for control, real-world, stress, and reject.
- Chose detector-assisted candidate extraction instead of species-classifier-based bird discovery.
- Chose full-image preview with detection box overlay as the primary review presentation for v1.
- Chose explicit `stress` flagging separate from species labels.
- Chose recent-label reuse as a core keyboard-first workflow feature.
- Chose burst grouping as an assistive heuristic, not an automatic labeling rule.
- Added a separate contracts spec in `working-plans/ANNOTATION_EVAL_CONTRACTS.md`.
- Added a SQLite review-schema spec in `working-plans/ANNOTATION_REVIEW_SCHEMA.md`.
- Added a review state-machine spec in `working-plans/ANNOTATION_REVIEW_STATE_MACHINE.md`.
- Added a RAW-preview performance note: prefer Lightroom Standard Previews or embedded JPEG previews over full RAW rendering for review throughput.
- Added a UI behavior spec in `working-plans/ANNOTATION_REVIEW_UI.md`.
- Revised the stress interaction model so stress is selected before the label action, preserving auto-advance behavior for label hotkeys.
- Added an implementation sequence in `working-plans/ANNOTATION_EVAL_BUILD_PLAN.md`.
- Added explicit local-only execution and durable exported-dataset requirements.
- Implemented `src/review_validation.py` for candidate, annotation, and manifest validation.
- Implemented `src/review_store.py` for SQLite-backed review persistence and recent-label history.
- Implemented `src/review_queue.py` for basic queue navigation over candidates.
- Implemented `src/label_resolver.py` for canonical common-name to label resolution against an explicit label inventory.
- Implemented `src/export_manifest.py` for manifest-only and materialized dataset export.
- Implemented `src/eval_manifest.py` for JSONL manifest load/write validation.
- Implemented `src/catalog_extract.py` as a detector- and preview-provider-agnostic extraction orchestrator.
- Implemented `src/review_assets.py` with a first concrete boxed-preview provider.
- Implemented `src/extract_candidates.py` as a local extraction CLI wired to the review store.
- Implemented `src/detectors/yolo.py` as a first YOLO-backed bird detector integration behind the detector abstraction.
- Implemented `src/review_app.py` as a minimal fully local web review UI.
- Implemented `src/build_label_inventory.py` to generate a canonical bird label inventory from existing region-species files.
- Implemented `src/sampling.py` for natural, balanced, and hybrid benchmark sampling.
- Added focused local tests covering review store, manifest loading, extraction orchestration, preview generation, review UI helpers, label-inventory generation, and sampling.
- Added `working-plans/INITIAL_USER_TESTING_CHECKLIST.md` to define the minimum bar for pilot testing.
