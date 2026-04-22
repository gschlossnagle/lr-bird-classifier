# Initial User Testing Checklist

This document defines the minimum bar for putting the annotation workflow in front of a real user.

The goal of initial testing is not to validate the final model strategy. It is to validate whether the workflow is usable, fast, and correct enough to produce trustworthy labeled data.

## Test Goal

Initial user testing should answer these questions:

- can a user extract review candidates from a real Lightroom catalog slice?
- can the user move through candidates quickly without workflow friction?
- is the full-image boxed preview sufficient for real review work?
- does recent-label reuse make repeated species runs genuinely fast?
- do saved outcomes behave exactly as expected?

If the answer to any of those is no, more model sophistication should wait.

## Minimum Features Required Before Testing

### 1. Local Candidate Extraction Command

Required:

- runs fully locally
- accepts a Lightroom catalog path
- processes a limited subset of images
- runs a real bird detector
- stores candidates in the review database
- produces preview assets usable by the UI

Testing should start on a small slice of the catalog, not the full archive.

### 2. Local Review UI Shell

Required:

- runs fully locally on a machine with access to source images
- opens candidates from the review database
- displays full-image preview with detection box overlay
- supports zoom to the boxed subject
- supports label input
- supports recent-label reuse
- supports `stress`, `reject`, `unsure`, `not_a_bird`, `bad_crop`, `duplicate`, and `skip`
- saves and advances correctly

### 3. Canonical Label Resolution

Required:

- free-text common-name entry resolves to canonical label + scientific name
- ambiguous input is surfaced explicitly
- recent-label reuse applies canonical labels, not raw strings

### 4. Basic Queue Correctness

Required:

- unreviewed candidates can be opened
- skipped candidates can be revisited
- reviewed candidates can be reopened and edited
- save/skip/cancel behavior matches the state-machine spec

### 5. Minimal Export Path

Required:

- reviewed candidates can be exported to a benchmark manifest
- at least one materialized dataset export path works

This does not need to be production-polished for the first user test, but it must be real enough to prove the annotations are not trapped in the UI.

## Recommended Pilot Scope

Start with a deliberately small, realistic test slice.

Recommendation:

- 50 to 150 candidate detections
- a date-ordered subset from one or a few nearby shoots
- a mix of repeated species and a few hard cases

Reason:

- enough volume to reveal friction
- small enough to iterate quickly on UI defects

Do not start with:

- the whole catalog
- a fully balanced benchmark set
- a model-comparison experiment

That would blur workflow testing with evaluation testing.

## What To Observe During Testing

### Throughput

Record:

- approximate candidates reviewed per minute
- whether repeated-label bursts feel fast
- whether zoom is needed frequently

This matters more than elegance at this stage.

### Preview Adequacy

Watch for:

- birds too small to identify in the preview
- cases where boxed full-image view is insufficient
- whether the preview source is fast enough

If preview adequacy fails, stop and fix that before polishing the rest.

### Interaction Errors

Watch for:

- accidental wrong-label saves
- confusion about pending `stress`
- ambiguity around skip vs reject vs not_a_bird
- difficulty understanding burst actions

These are workflow bugs, not user mistakes, until proven otherwise.

### State Integrity

Verify:

- save creates the expected annotation outcome
- skip does not create annotations
- reopen/edit replaces the prior annotation cleanly
- recent-label history behaves sensibly

## Success Criteria For Initial Testing

Initial testing is successful if:

- the workflow can be run end to end locally
- the user can review a small real-world batch without serious friction
- save/skip/edit behavior is trustworthy
- repeated species runs are notably faster than fresh label entry
- preview quality is good enough to support real species identification most of the time

## Failure Criteria

Pause feature expansion if any of these happen:

- preview generation is too slow for practical review
- preview quality is too poor for reliable identification
- the reviewer cannot move quickly through repeated-label sequences
- the state model feels confusing or error-prone
- exported data cannot be trusted without manual cleanup

If these fail, the fix is almost certainly in preview/extraction/UI behavior, not in more advanced model work.

## Recommended Next Implementation Target

To reach initial user testing, the next build slice should be:

1. concrete preview provider
2. concrete detector backend
3. local extraction command
4. minimal local review UI shell

That is the shortest path to a real pilot.

