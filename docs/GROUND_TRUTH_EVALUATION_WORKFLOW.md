# Ground-Truth Evaluation Workflow

This project now supports a second workflow beyond Lightroom keywording: using
the catalog as a source pool for building a human-reviewed ground-truth dataset
to compare bird-classification models.

The purpose of this workflow is not to auto-tag a catalog. It is to create a
reviewed reference set that can be used to answer questions like:

- Which classifier is most accurate on my real photos?
- Which model fails on difficult or unusual images?
- Does a model that performs well on easy catalog images also hold up on a
  stress set?
- Are model differences real, or just artifacts of evaluating on a biased
  sample from one trip or one burst?

## Why Use This Workflow

General bird benchmarks are useful, but they do not fully reflect a specific
photographer's catalog. Your library has its own bias profile:

- your camera bodies and lenses
- your editing habits
- your geography
- your species mix
- your style of framing and cropping
- your frequency of burst shooting

If you want to choose models for this project intelligently, you need a test set
drawn from your own data and reviewed by a human.

This workflow exists to produce that set in a disciplined way.

## What The Tool Does

At a high level, the workflow is:

1. Read a Lightroom catalog in read-only mode.
2. Select source images from a chosen scope.
3. Run a bird detector to propose review candidates.
4. Render review previews with the detection boxes overlaid.
5. Present those candidates in a local annotation UI.
6. Record human labels and review outcomes in SQLite.
7. Export reviewed subsets as manifests or standalone JPEG datasets for model
   evaluation.

The review UI is local-first. It is designed to run on a machine that can see
the original photo files and the Lightroom catalog.

When source EXIF supports it, the review UI may also show an `Estimated subject
box size` readout. This is a rough real-world size estimate derived from focus
distance, 35mm-equivalent focal length, and the detector box geometry. It is a
useful cue, not ground truth.

The review UI may also show an external eBird / Macaulay reference panel for
the current species suggestion or selected label. This is a reviewer aid only:

- it is resolved from the official eBird taxonomy species code
- it links to the eBird species page and a Macaulay reference asset
- it may show a remote reference image in the UI
- it requires internet access at review time
- the media is not stored locally as part of the review database
- the media is not exported with benchmark datasets

## Core Idea

The workflow deliberately separates four things:

1. Candidate extraction
2. Human annotation
3. Benchmark export
4. Model evaluation

That separation matters. If those concerns are mixed together, the data becomes
hard to reason about and model comparisons become less credible.

## Recommended Use

Use this workflow when you want to build a benchmark set from a real catalog
slice and compare multiple classifiers against the same reviewed truth.

Typical pattern:

1. Choose a bounded scope.
   Examples:
   - a folder like `VeroBeach`
   - a date range
   - a star-rated subset
   - specific source file types via `--formats`

2. Review candidates in the local UI.
   For each detection, assign:
   - a canonical species label
   - or a non-species outcome such as `not_a_bird`, `reject`, `unsure`,
     `bad_crop`, or `duplicate`

3. Use `stress` for hard-but-valid examples.
   The `stress` flag is for images that should still be part of evaluation, but
   deserve separate tracking because they are difficult. Examples:
   - classifier suggestion disagrees with the correct answer
   - classifier confidence is low
   - the subject is small, occluded, unusual, blurred, or otherwise difficult

4. Export reviewed sets.
   Typical pools are:
   - `catalog_real_world`
   - `catalog_stress`
   - `catalog_reject`

5. Run one or more models against the exported benchmark.
   Because the benchmark is human-reviewed and fixed, model comparisons are
   meaningful.

## Why Human Review Matters

The detector is only proposing candidates. It is not creating truth.

The classifier suggestion in the UI is also only a suggestion. It is useful for
speed, but the human reviewer remains the source of truth.

This is the key distinction:

- model output is hypothesis
- review annotation is truth

If that boundary is blurred, the resulting benchmark becomes circular and much
less trustworthy.

## Label Outcomes

The most important review outcomes are:

- `labeled`
  A valid species label was assigned.

- `stress`
  A valid species label was assigned, but the image is intentionally marked as a
  harder evaluation example.

- `not_a_bird`
  The detector proposed a non-bird false positive.

- `reject`
  The detection may involve a bird, but the sample should not enter the normal
  benchmark pool because it is too poor or low-value.

- `unsure`
  The reviewer could not confidently identify the subject.

- `bad_crop`
  The detector crop or detection proposal is too poor to be useful.

- `duplicate`
  The sample is redundant for benchmark purposes.

## Why Folder And Date Matter

A benchmark can look artificially strong if it is dominated by one outing, one
species cluster, one camera setup, or one burst sequence.

This workflow therefore keeps metadata like:

- capture datetime
- folder path
- burst grouping

That makes it possible later to sample more carefully so the benchmark is not
accidentally dominated by one trip or one shooting session.

Even when GPS is absent, folder structure and capture time are usually enough to
reduce obvious trip-level bias.

## Why Export Standalone JPEGs

Once a reviewed set is selected, it should be exportable as a standalone dataset
outside the Lightroom catalog and outside Lightroom's preview cache.

That matters for reproducibility:

- the benchmark should survive catalog reorganization
- the benchmark should not depend on Lightroom internals
- the same exported set should be reusable across model runs

In practice, that means:

- keep the review database as the annotation source of truth
- materialize final benchmark images as JPEGs in a separate location
- export a manifest alongside them

## Suggested Operating Model

For a first useful benchmark:

1. Pick one bounded slice of the catalog.
2. Review until you have a few hundred labeled candidates.
3. Separate normal and stress examples.
4. Export the reviewed set.
5. Run at least two candidate classifiers against it.
6. Compare:
   - top-1 accuracy
   - per-species behavior
   - stress-set behavior
   - false-positive behavior on `not_a_bird`

This is better than choosing a model based only on public benchmarks or
subjective spot checks.

## Current Workflow Components

The repo currently contains support for:

- detector-backed candidate extraction from Lightroom
- read-only catalog access for the review workflow
- local review UI with keyboard shortcuts
- recent-label reuse
- burst-aware review acceleration
- classifier suggestions during review
- SQLite review state
- export of reviewed datasets and manifests

The workflow is intentionally local and internal-use oriented. It is meant to
help choose and validate models against real catalog data, not to be a generic
multi-user annotation platform.

## Practical Guidance

- Start with a narrow scope, not the whole catalog.
- Keep review moving; do not overdesign taxonomy edge cases up front.
- Treat detector and classifier outputs as fallible.
- Use `stress` intentionally, not indiscriminately.
- Prefer a benchmark that is smaller and clean over one that is large and noisy.
- Reuse the same reviewed benchmark when comparing models.
- Only rebuild the benchmark deliberately, not casually, or comparisons across
  runs become noisy.

## In Short

Use this workflow when you want a defensible, human-reviewed benchmark drawn
from your own Lightroom catalog so model comparisons are grounded in the actual
images you care about.
