# Annotation Review SQLite Schema

This document maps the annotation and evaluation contracts onto the local SQLite persistence layer used by the review workflow.

It is intentionally narrower than the broader plan documents. Its job is to define:

- tables
- keys
- constraints
- state ownership

The main purpose is to prevent the review tool from collapsing extractor state, human annotation state, and export state into one ambiguous record.

## Design Corrections

Before locking the schema, two design corrections are worth stating explicitly.

### 1. `review_status` And `annotation_status` Must Not Mean The Same Thing

The earlier design risked duplicating state in two places.

Use this split instead:

- `candidates.review_status`
  workflow position in the queue
- `annotations.annotation_status`
  semantic human outcome for that candidate

This is not redundant. A candidate can be:

- `review_status='reviewed'`
- `annotation_status='duplicate'`

or:

- `review_status='skipped'`
- no annotation row yet

### 2. Preview Ownership Belongs To The Candidate

The earlier design could have implied that preview assets belong to source images.

That is too loose. The review UI is candidate-centric, and the overlay preview may differ by detection box even when several candidates come from the same source image.

So:

- source-image metadata lives in `images`
- detection-specific preview assets live in `candidates`

## Table Overview

The local review store should use these tables:

- `images`
- `candidates`
- `annotations`
- `label_history`
- `review_sessions`
- `review_session_events` optional

## `images`

One row per source image in the Lightroom-derived candidate pool.

Suggested schema:

```sql
CREATE TABLE images (
    id                  INTEGER PRIMARY KEY,
    source_image_id     INTEGER,
    source_image_path   TEXT NOT NULL UNIQUE,
    capture_datetime    TEXT,
    folder              TEXT,
    rating              REAL,
    gps_lat             REAL,
    gps_lon             REAL,
    region_hint         TEXT,
    lens_model          TEXT,
    focal_length        REAL,
    existing_keywords   TEXT,
    burst_group_id      TEXT,
    near_duplicate_group TEXT,
    created_at          TEXT NOT NULL
);
```

Notes:

- `source_image_id` is the Lightroom image identifier if available
- `existing_keywords` can be stored as JSON text
- `burst_group_id` belongs at the image level because it is an image-sequencing concept, not a detection concept

Recommended indexes:

```sql
CREATE INDEX idx_images_capture_datetime ON images (capture_datetime);
CREATE INDEX idx_images_burst_group_id   ON images (burst_group_id);
CREATE INDEX idx_images_region_hint      ON images (region_hint);
```

## `candidates`

One row per detector-produced review target.

Suggested schema:

```sql
CREATE TABLE candidates (
    id                       TEXT PRIMARY KEY,
    image_id                 INTEGER NOT NULL,
    detector_name            TEXT NOT NULL,
    detected_class           TEXT NOT NULL,
    detector_confidence      REAL NOT NULL,
    bbox_x1                  INTEGER NOT NULL,
    bbox_y1                  INTEGER NOT NULL,
    bbox_x2                  INTEGER NOT NULL,
    bbox_y2                  INTEGER NOT NULL,
    bbox_area_fraction       REAL,
    preview_image_path       TEXT NOT NULL,
    safe_single_subject_burst INTEGER NOT NULL DEFAULT 0,
    review_status            TEXT NOT NULL DEFAULT 'unreviewed',
    reviewed_at              TEXT,
    created_at               TEXT NOT NULL,
    FOREIGN KEY (image_id) REFERENCES images(id)
);
```

### Allowed `review_status` Values

- `unreviewed`
- `in_review`
- `reviewed`
- `skipped`

Rules:

- `review_status='reviewed'` means a human outcome exists in `annotations`
- `review_status='skipped'` means no final human outcome exists yet
- `review_status` should never encode semantic labels like `reject` or `stress`

Recommended indexes:

```sql
CREATE INDEX idx_candidates_image_id       ON candidates (image_id);
CREATE INDEX idx_candidates_review_status  ON candidates (review_status);
CREATE INDEX idx_candidates_detector_conf  ON candidates (detector_confidence);
```

Recommended checks:

```sql
CHECK (bbox_x2 > bbox_x1),
CHECK (bbox_y2 > bbox_y1),
CHECK (review_status IN ('unreviewed', 'in_review', 'reviewed', 'skipped')),
CHECK (safe_single_subject_burst IN (0, 1))
```

## `annotations`

One row per reviewed candidate with the human outcome.

Suggested schema:

```sql
CREATE TABLE annotations (
    candidate_id         TEXT PRIMARY KEY,
    annotation_status    TEXT NOT NULL,
    truth_common_name    TEXT,
    truth_sci_name       TEXT,
    truth_label          TEXT,
    taxon_class          TEXT,
    resolved_from_input  TEXT,
    stress               INTEGER NOT NULL DEFAULT 0,
    reject_sample        INTEGER NOT NULL DEFAULT 0,
    unsure               INTEGER NOT NULL DEFAULT 0,
    not_a_bird           INTEGER NOT NULL DEFAULT 0,
    bad_crop             INTEGER NOT NULL DEFAULT 0,
    duplicate_sample     INTEGER NOT NULL DEFAULT 0,
    notes                TEXT,
    annotated_by         TEXT,
    annotated_at         TEXT NOT NULL,
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);
```

### Allowed `annotation_status` Values

- `labeled`
- `reject`
- `unsure`
- `not_a_bird`
- `bad_crop`
- `duplicate`

Deliberate choice:

- `skipped` does not belong in `annotations`

Reason:

- skipping is queue workflow state, not a semantic annotation outcome

### Annotation Invariants

For `annotation_status='labeled'`:

- `truth_common_name` is required
- `truth_sci_name` is required
- `truth_label` is required
- `taxon_class` is required
- `not_a_bird=0`
- `reject_sample=0`
- `unsure=0`
- `bad_crop=0`

For `annotation_status='not_a_bird'`:

- `not_a_bird=1`
- `truth_label` should normally be NULL

For `annotation_status='reject'`:

- `reject_sample=1`

For `annotation_status='duplicate'`:

- `duplicate_sample=1`
- truth fields may be present or absent

For `stress=1`:

- `annotation_status` must be `labeled`

I would not attempt to enforce every one of these via SQLite `CHECK` constraints in v1 because the conditions are cross-field and can become awkward. Enforce the simple invariants in SQL and the richer ones in application validation.

Recommended indexes:

```sql
CREATE INDEX idx_annotations_status      ON annotations (annotation_status);
CREATE INDEX idx_annotations_truth_label ON annotations (truth_label);
CREATE INDEX idx_annotations_stress      ON annotations (stress);
CREATE INDEX idx_annotations_reject      ON annotations (reject_sample);
```

Recommended checks:

```sql
CHECK (annotation_status IN ('labeled', 'reject', 'unsure', 'not_a_bird', 'bad_crop', 'duplicate')),
CHECK (stress IN (0, 1)),
CHECK (reject_sample IN (0, 1)),
CHECK (unsure IN (0, 1)),
CHECK (not_a_bird IN (0, 1)),
CHECK (bad_crop IN (0, 1)),
CHECK (duplicate_sample IN (0, 1))
```

## `label_history`

One row per accepted label-use event for fast reuse in the UI.

Suggested schema:

```sql
CREATE TABLE label_history (
    id                INTEGER PRIMARY KEY,
    truth_common_name TEXT NOT NULL,
    truth_sci_name    TEXT NOT NULL,
    truth_label       TEXT NOT NULL,
    taxon_class       TEXT,
    used_at           TEXT NOT NULL
);
```

Important choice:

- this is append-only history, not a deduplicated lookup table

Reason:

- the UI needs recency order, not just a set of distinct labels

Recommended indexes:

```sql
CREATE INDEX idx_label_history_used_at ON label_history (used_at DESC);
CREATE INDEX idx_label_history_label   ON label_history (truth_label);
```

## `review_sessions`

One row per review session.

Suggested schema:

```sql
CREATE TABLE review_sessions (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    user_id       TEXT,
    queue_filter  TEXT,
    notes         TEXT
);
```

This is optional for v1, but useful enough that I would include it if the implementation cost is low.

## `review_session_events` Optional

If we want an audit trail without overloading the main tables, use an event log.

Suggested schema:

```sql
CREATE TABLE review_session_events (
    id            INTEGER PRIMARY KEY,
    session_id    INTEGER,
    candidate_id  TEXT,
    event_type    TEXT NOT NULL,
    event_payload TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (session_id) REFERENCES review_sessions(id),
    FOREIGN KEY (candidate_id) REFERENCES candidates(id)
);
```

This should remain optional. It is useful for debugging but not necessary to unlock the review workflow.

## Normalized Ownership Model

To keep responsibilities clean:

- `images` owns source metadata
- `candidates` owns detector-specific review targets
- `annotations` owns semantic human judgment
- `label_history` owns reuse ergonomics
- `review_sessions` owns operator/session metadata

This means:

- benchmark export should read from `images + candidates + annotations`
- recent-label UI should read from `label_history`
- queue logic should read from `candidates.review_status`

## Export Mapping

### To Annotation Record Contract

`annotations` is the local normalized form of the annotation record contract.

Export row construction should join:

- `annotations`
- `candidates`
- `images`

### To Benchmark Manifest Contract

Benchmark manifest rows should be generated from the same join, but should not include review-tool-only internals like:

- `review_status`
- `annotated_by` unless explicitly needed
- session ids

## Validation Requirements

Before insert or update:

- `candidates.preview_image_path` must exist
- bbox coordinates must be valid
- labeled annotations must have all truth fields
- `stress=1` requires `annotation_status='labeled'`
- `review_status='reviewed'` requires a matching row in `annotations`

These should be enforced in application code with lightweight SQL checks where practical.

## Recommendation

This schema is sufficient for v1 and intentionally avoids over-engineering around future features like full multi-label scoring or crop-asset management.

The next likely refinement should be a short state-machine spec covering:

- review transitions
- relabel/edit behavior
- burst-apply semantics

