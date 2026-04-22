# Annotation Review State Machine

This document defines the behavior of the review workflow as a state machine.

Its purpose is to remove ambiguity around:

- queue transitions
- when annotation rows are created or replaced
- how relabeling works
- how skip and burst-apply behavior interact with review state

This spec assumes the SQLite ownership model defined in `working-plans/ANNOTATION_REVIEW_SCHEMA.md`.

## Core Principle

There are two different kinds of state:

- queue state
- semantic annotation outcome

They must not be conflated.

Queue state lives in:

- `candidates.review_status`

Semantic outcome lives in:

- `annotations.annotation_status`

## Queue States

Allowed queue states:

- `unreviewed`
- `in_review`
- `reviewed`
- `skipped`

Meaning:

- `unreviewed`
  candidate has not yet received any final human action
- `in_review`
  candidate is currently open in an active review session
- `reviewed`
  candidate has a final human outcome stored in `annotations`
- `skipped`
  candidate was intentionally deferred without a final human outcome

## Annotation Outcomes

Allowed annotation outcomes:

- `labeled`
- `reject`
- `unsure`
- `not_a_bird`
- `bad_crop`
- `duplicate`

These are final semantic outcomes for the current review pass, but they are still editable later.

## Initial State

When a candidate is created by extraction:

- `candidates.review_status = 'unreviewed'`
- no row exists in `annotations`

## Primary State Transitions

### Open Candidate

When a reviewer opens a candidate from the queue:

- `unreviewed -> in_review`
- `skipped -> in_review`
- `reviewed -> in_review` only when explicitly editing/reopening

Rule:

- opening a candidate for passive viewing should not happen outside the review flow
- if the UI allows non-destructive preview browsing, it must not change state

## Save Labeled Positive

Action:

- reviewer assigns a canonical species label and saves

Effects:

- create or replace row in `annotations`
- set `annotation_status = 'labeled'`
- set `candidates.review_status = 'reviewed'`
- set `candidates.reviewed_at = now`
- append to `label_history`

Required annotation fields:

- `truth_common_name`
- `truth_sci_name`
- `truth_label`
- `taxon_class`

Optional flags:

- `stress`

Forbidden combinations:

- `stress=1` with non-`labeled` outcome

UI implication:

- `stress` should be chosen before the label commit action is executed
- after a labeled save completes, the pending UI stress modifier resets

## Save Reject-Style Outcome

Actions:

- mark `reject`
- mark `unsure`
- mark `not_a_bird`
- mark `bad_crop`
- mark `duplicate`

Effects:

- create or replace row in `annotations`
- set appropriate `annotation_status`
- set matching boolean flags
- set `candidates.review_status = 'reviewed'`
- set `candidates.reviewed_at = now`

Special cases:

- `duplicate` may preserve or omit truth fields
- `not_a_bird` should normally not store truth fields

## Skip Candidate

Action:

- reviewer explicitly skips the current candidate for later

Effects:

- do not create an `annotations` row
- set `candidates.review_status = 'skipped'`

Important:

- skip is not a semantic annotation result
- skip should not be exported

## Reopen Reviewed Candidate

Action:

- reviewer explicitly edits a previously reviewed candidate

Transition:

- `reviewed -> in_review`

Effects:

- existing annotation remains until replaced or deleted by the next save action

Design choice:

- do not delete the annotation row on entry to edit mode
- replace it only on explicit save

Reason:

- protects against accidental data loss from abandoned edits

## Cancel Edit

Action:

- reviewer opens a reviewed or unreviewed candidate and cancels without saving

Effects:

- if candidate originally entered from `reviewed`, restore `review_status='reviewed'`
- if candidate originally entered from `unreviewed`, restore `review_status='unreviewed'`
- if candidate originally entered from `skipped`, restore `review_status='skipped'`

This implies the UI needs to remember the prior queue state while the candidate is open.

## Relabel Reviewed Candidate

Action:

- reviewer changes a previously saved species outcome

Effects:

- replace the existing `annotations` row for that `candidate_id`
- set new truth fields and flags
- set `annotated_at = now`
- append new entry to `label_history`
- keep `candidates.review_status = 'reviewed'`

This should be implemented as replace semantics, not in-place partial mutation.

Reason:

- clearer invariants
- simpler validation

## Change From One Outcome Type To Another

Examples:

- `labeled -> duplicate`
- `duplicate -> labeled`
- `not_a_bird -> labeled`
- `reject -> labeled`

Allowed:

- yes, all of these are allowed on explicit edit

Mechanism:

- replace the `annotations` row with a new normalized state

Rule:

- there should only ever be one current annotation row per candidate

## Burst-Apply Semantics

Burst application is a bulk save convenience, not a different annotation type.

### Preconditions

Burst-apply should only be enabled when:

- the current candidate has a resolved label
- the candidate's image has a `burst_group_id`
- all remaining applicable candidates in the burst are marked `safe_single_subject_burst=1`
- none of the target candidates are already `reviewed` unless the user explicitly chose overwrite mode

### Default Behavior

`apply current label to remaining safe items in burst` should:

- apply the current resolved label
- preserve `stress=false` by default unless the action explicitly includes stress
- create annotation rows for each target
- set each target candidate to `review_status='reviewed'`
- append one `label_history` event per applied annotation or one grouped event depending on implementation

### Overwrite Policy

Default:

- do not overwrite already reviewed candidates

Optional future action:

- explicit `apply to all reviewed and unreviewed in burst`

I would defer overwrite support unless it becomes clearly necessary.

### Stress With Burst-Apply

If the initiating action is a stress-labeled save, then burst-apply should propagate:

- same truth label
- `stress=true`

This is acceptable only because the user explicitly triggered the burst action after reviewing the current candidate.

## Pending UI Modifiers

The only planned transient UI modifier is:

- pending `stress`

Properties:

- it exists only in UI state, not database state
- it applies to the next labeled save on the current candidate
- it resets after save, cancel, skip, or navigation away from the candidate

Reason:

- keeps label hotkeys as single-step commit actions
- avoids introducing a post-label stress confirmation flow

## Recent Label History Semantics

An entry should be added to `label_history` when:

- a `labeled` annotation is saved
- a burst-apply creates labeled annotations

An entry should not be added when:

- candidate is skipped
- outcome is `not_a_bird`
- outcome is `reject`
- outcome is `bad_crop`
- outcome is `unsure`

Reason:

- recent-label history should reflect reusable positive labels, not generic workflow states

## Queue Advancement Rules

After save:

- advance to next queue candidate by default

After skip:

- advance to next queue candidate by default

After cancel:

- return to queue without advancing unless the UI intentionally advances

Recommendation:

- keep cancel non-advancing

Reason:

- avoids accidentally losing place during review

## Validation Rules During Save

### Labeled Save

Must validate:

- truth fields all present
- `annotation_status='labeled'`
- `stress` is boolean
- reject-style flags are not set

### Reject-Style Save

Must validate:

- exactly one primary semantic outcome is chosen
- `stress=false`

I would challenge one tempting implementation choice here: allowing multiple conflicting semantic flags at once.

Bad idea:

- one row marked both `reject` and `not_a_bird`

Better rule:

- one primary outcome
- optional `stress` only for labeled positives

This makes export and metrics much cleaner.

## Recommended UI-Level Outcome Model

The UI should behave as if the reviewer chooses exactly one of:

- labeled
- reject
- unsure
- not_a_bird
- bad_crop
- duplicate
- skip

Then optional modifiers:

- `stress` only when labeled
- notes optional

That is cleaner than exposing many independent toggles that can create contradictory states.

## State Transition Summary

### Queue State

```text
unreviewed -> in_review
skipped    -> in_review
reviewed   -> in_review   (explicit edit only)

in_review  -> reviewed    (save outcome)
in_review  -> skipped     (skip)
in_review  -> prior state (cancel)
```

### Annotation State

```text
none       -> labeled
none       -> reject
none       -> unsure
none       -> not_a_bird
none       -> bad_crop
none       -> duplicate

any saved outcome -> any other saved outcome   (explicit replace on edit)
```

## Strong Recommendation

The biggest place this system can go wrong is allowing contradictory semantic flags to accumulate.

So I would recommend a stricter model than the earlier UI language implied:

- one primary outcome per candidate
- `stress` as the only orthogonal modifier
- skip stays queue-only

That is simpler, safer, and easier to export and score.
