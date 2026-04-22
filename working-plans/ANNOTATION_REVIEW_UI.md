# Annotation Review UI Behavior

This document defines the expected behavior of the annotation review interface.

It is not a pixel-perfect mockup. Its purpose is to specify:

- what the reviewer sees
- what actions are available
- how keyboard-first review works
- how the UI maps onto the review state machine

This spec assumes:

- the contracts in `working-plans/ANNOTATION_EVAL_CONTRACTS.md`
- the schema in `working-plans/ANNOTATION_REVIEW_SCHEMA.md`
- the state machine in `working-plans/ANNOTATION_REVIEW_STATE_MACHINE.md`

## Design Goals

- minimize time per reviewed candidate
- keep the human in explicit control
- make repeated-label sequences extremely fast
- avoid ambiguous or contradictory review outcomes
- keep context visible while directing attention to the detected subject

## Core UI Model

The review UI is candidate-centric.

One screen corresponds to one current candidate:

- one source image preview
- one detection box
- one active review target

The UI should not present multiple editable candidates at once in v1.

Reason:

- batch editing introduces accidental state ambiguity quickly
- the common case is rapid sequential review, not spreadsheet-style editing

## Screen Layout

The layout should be a single review workspace with four functional regions:

1. image panel
2. metadata panel
3. label/review panel
4. queue context panel

### 1. Image Panel

Primary visual content:

- full image preview
- detector box overlay on the subject being reviewed

Required behaviors:

- detection box is always visible by default
- image auto-fits to available viewport space
- reviewer can zoom into the boxed region
- reviewer can zoom back to fit view

Why full-image-first:

- preserves species-identification context
- avoids crop-only mistakes
- reduces preprocessing complexity

Optional later behavior:

- on-demand crop preview

That should not be required for v1.

### 2. Metadata Panel

This panel should summarize the most decision-relevant metadata without clutter.

Visible by default:

- candidate id
- source image path or concise relative path
- capture datetime
- detector confidence
- region hint if available
- burst group info if present

Collapsible extended metadata:

- focal length
- lens model
- rating
- existing keywords
- source image id

Important design choice:

- lens data is useful for later analysis but not central to labeling
- do not overemphasize it in the default layout

### 3. Label And Review Panel

This is the active input area.

Required elements:

- label input box
- autocomplete result list
- resolved mapping preview
- recent-label shortcuts
- primary outcome controls
- optional notes field

The label input should be focused by default when a candidate loads.

### 4. Queue Context Panel

This should provide just enough context to support speed without distracting from the current review target.

Recommended content:

- progress count
- current queue filter
- next/previous candidate controls
- burst action availability

Optional:

- a small list of adjacent candidates from the same burst or source image

I would defer thumbnail strips unless they prove necessary.

## Image Behavior

### Preview Source Priority

The UI should prefer fast preview sources over slow RAW rendering.

Preferred priority:

1. Lightroom Standard Preview when available and practical
2. embedded JPEG preview from the RAW file
3. slower RAW render fallback

This is a throughput requirement, not a cosmetic optimization.

### Box Overlay

The current candidate's bbox should be rendered clearly and consistently.

Requirements:

- border visible on light and dark backgrounds
- sufficient thickness at normal zoom
- optional dimming outside the box is allowed but should be subtle

Avoid:

- heavy visual effects that obscure plumage detail

### Zoom Behavior

Required actions:

- fit full image
- zoom to detection box
- restore prior zoom

Recommendation:

- `Z` toggles box zoom

The zoomed view should center the current candidate box and preserve enough nearby context to remain useful.

## Primary Outcome Model

The reviewer chooses exactly one primary outcome per candidate:

- labeled
- reject
- unsure
- not_a_bird
- bad_crop
- duplicate
- skip

Important:

- `skip` is queue-only and should not create an annotation row
- `stress` is not a primary outcome
- `stress` is only available when the current action is a labeled positive

This is stricter than a free-form toggle model on purpose.

## Label Entry Flow

### Default Flow

1. candidate loads
2. label input receives focus
3. reviewer types common name or uses recent-label shortcut
4. UI resolves selection to canonical mapping
5. reviewer saves
6. UI advances to next candidate

### Resolution Preview

Before save, the UI must show:

- canonical common name
- scientific name
- canonical truth label, optionally abbreviated for readability

If the typed label is ambiguous:

- the UI must require explicit selection
- `Enter` must not silently accept an arbitrary match

### Enter Key Behavior

`Enter` should:

- accept the currently selected valid resolution
- save as `labeled`
- advance to next candidate

If there is no valid selected resolution:

- `Enter` should do nothing destructive
- keep focus in the label flow

## Recent Label Reuse

Recent-label reuse is a primary workflow feature.

### Visible Recent Labels

The UI should show:

- one prominent “last used” label
- several additional recent labels

Recommended v1 behavior:

- show 5 visible recent labels
- keep deeper history available via autocomplete/search

### Required Shortcuts

- `R`
  apply last resolved label and advance
- `1-5`
  apply the corresponding recent label and advance

These shortcuts should only fire when a valid recent label exists.

### Safety Rule

Recent-label actions still require an explicit keystroke per candidate.

No label should ever silently carry forward to the next candidate.

## Stress Interaction

`stress` is an orthogonal modifier on a labeled positive sample.

To keep label hotkeys consistently auto-advancing, `stress` must be selected before the label action is committed.

Primary interaction:

- `T` toggles a pending `stress` modifier on the current candidate
- the next label commit consumes that modifier
- after save, the pending modifier resets for the next candidate

Examples:

- `T` then `R`
  mark the candidate as stress, apply last label, save, advance
- `T` then `1-5`
  mark the candidate as stress, apply recent label, save, advance
- `T` then typed label + `Enter`
  mark the typed label as stress, save, advance

Requirements:

- the UI must visibly indicate when pending `stress=true` is active
- pending `stress` must reset after save, skip, cancel, or navigation away from the candidate
- `stress` cannot be applied to non-label outcomes

Recommendation:

- do not keep `Shift+R` and `Shift+1-5` in v1

Reason:

- they are redundant once `stress` becomes a pre-label modifier
- removing them keeps the shortcut model simpler and more learnable

## Non-Label Outcome Actions

These actions should be available without entering a species label:

- `X`: mark `reject`
- `U`: mark `unsure`
- `N`: mark `not_a_bird`
- `B`: mark `bad_crop`
- `D`: mark `duplicate`
- `K`: skip

Behavior:

- each action saves the corresponding outcome immediately unless notes entry is active
- candidate advances after save

I would challenge one tempting UI pattern here: modal confirmation on every non-label action.

That would slow the workflow too much.

Better rule:

- make actions reversible through edit/reopen
- avoid extra confirmation except for destructive batch actions

## Edit And Reopen Behavior

The UI must support revisiting reviewed candidates.

When reopening:

- load existing annotation outcome
- prepopulate notes and relevant truth fields
- visually indicate that this is an edit, not a fresh review

On save:

- replace the prior annotation outcome

On cancel:

- restore prior queue state
- do not mutate the saved annotation

## Skip Behavior

`K` should:

- mark `candidates.review_status='skipped'`
- not create or modify an annotation row
- advance to next candidate

Skipped candidates should be easy to revisit via queue filters.

## Burst Behavior

Burst logic is an acceleration aid, not an inference engine.

### Burst Indicator

If the current candidate belongs to a burst:

- show burst id or concise indicator
- show how many remaining safe candidates are in the burst

### Burst Apply Action

If burst-apply is available, the UI should show a clearly named action such as:

- `Apply current label to remaining safe items in burst`

This action should only be enabled when:

- current candidate has a valid resolved label
- remaining target candidates are marked safe for burst application

### Burst Apply Shortcut

Recommendation:

- `A`

Behavior:

- apply current resolved label to remaining safe items in burst
- propagate `stress=true` only if the initiating labeled save used the pending stress modifier
- do not overwrite already reviewed candidates by default

### Confirmation

Burst apply should require one lightweight confirmation showing:

- number of targets
- overwrite behavior
- whether stress will be propagated

This is one place where confirmation is justified.

## Queue Behavior

### Default Ordering

Recommended default:

- capture datetime ascending or descending

Reason:

- date-ordered review increases the usefulness of recent-label reuse
- burst grouping becomes much more natural

### Filters

Minimum useful filters:

- unreviewed only
- skipped only
- reviewed only
- by folder
- by date range
- by burst group
- by detector confidence threshold

### Progress Display

Show:

- reviewed count
- remaining count
- current position in filtered queue

Do not overcomplicate this with too many dashboard metrics in v1.

## Notes Behavior

Notes should be optional and lightweight.

Requirements:

- available without leaving the current candidate
- collapsed by default
- preserved on edit

Notes should not interrupt the fast path.

## Error Behavior

If a save fails:

- do not advance
- preserve current input state
- show a clear error message

If preview loading fails:

- show metadata and fallback controls
- allow reviewer to skip or mark bad crop / reject if appropriate
- keep original source path accessible

## Accessibility And Throughput Guidance

The UI should prefer:

- high-contrast readable text
- obvious keyboard focus states
- minimal pointer dependency
- no unnecessary animation

This is not a showcase UI. It is an annotation tool. Throughput and clarity matter more than visual novelty.

## Strong Recommendation

The most important interaction design rule is this:

- the easiest action should be reusing the correct recent label
- the second easiest action should be switching to a different valid label
- nothing should happen without an explicit user action

If the implementation preserves that hierarchy, the tool will be fast without becoming careless.
