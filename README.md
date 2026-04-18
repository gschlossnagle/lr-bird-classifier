# lr-bird-classifier

Like many bird photographers, keeping up with keyword taxonomy in Lightroom
is the least enjoyable part of the hobby — and identifying every species
correctly in the field is harder still.  This tool tackles both problems at
once: it scans your Lightroom Classic catalog, runs each bird photo through
a computer-vision model to identify the species, and writes a clean
hierarchical keyword structure back into the catalog using both common and
scientific names.

A range of options lets you target just part of your library — a specific
folder, a minimum star rating, a confidence threshold — so you can roll it
out gradually and verify results before committing to the whole archive.

> **⚠️ Lightroom has no public API for catalog access.**  This tool reads
> and writes the undocumented SQLite internals of your `.lrcat` file.  It
> works reliably in practice, but this is inherently unsupported territory.
> **Always back up your catalog before running** (the tool can do this
> automatically).  Never run it while Lightroom is open.

This project builds on the [birder-project/birder](https://github.com/birder-project/birder)
library for the heavy lifting of bird classification.  Big thanks to Ofer Hasson.

## What it does

For each untagged RAW/DNG (or TIFF, PSD, PSB) bird photo the script:

1. Runs the image through a ViT bird-classification model
2. Filters predictions to species that actually occur in the photo's geographic
   region (derived from GPS EXIF or a `--region` hint)
3. Writes a keyword hierarchy into the Lightroom catalog:

```
Bird-Species
  └── Bald Eagle

Birds
  └── Order
        └── Hawks-Eagles-Kites-Allies      ← order tagged
              └── Bald Eagle               ← species tagged
  └── Scientific
        └── Accipitriformes               ← order tagged
              └── Accipitridae            ← family tagged
                    └── Haliaeetus leucocephalus  ← species tagged

Classifier-Confidence
  └── High                                ← confidence band tagged (see below)
```

4. Keeps any co-located `.xmp` sidecar in sync via exiftool so Lightroom does
   not show "metadata on disk is out of sync" warnings.

---

## Requirements

| Requirement | Notes |
|---|---|
| Python 3.12+ | |
| [exiftool](https://exiftool.org) | `brew install exiftool` on macOS |
| PyTorch 2.10+ | CPU works; MPS (Apple Silicon) or CUDA strongly recommended |
| rawpy | For camera RAW decoding (ARW, CR2, NEF, ORF, DNG, …) |

---

## Installation

```bash
git clone https://github.com/gschlossnagle/lr-bird-classifier.git
cd lr-bird-classifier

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

### Quick setup

After `pip install`, run the setup script to download the model, fetch species
common names, and build all regional whitelists in one step:

```bash
bash scripts/setup.sh
```

This takes 5–15 minutes depending on your internet connection.  When it
finishes you are ready to run:

```bash
.venv/bin/python -m src.run /path/to/catalog.lrcat --dry-run
```

### Advanced setup

If you want more control — a different model, specific regions only, or a
staged setup — you can run each step manually.

#### Download the model

Models are downloaded via the [birder project](https://github.com/birder-project/birder)
tools, which are included in `requirements.txt`.

The default model is `rope_vit_reg4_b14` with tag `capi-inat21`:

```bash
.venv/bin/python -m birder.tools download-model rope_vit_reg4_b14_capi-inat21
```

This downloads `rope_vit_reg4_b14_capi-inat21.pt` into the `models/` directory.

#### Using a different model

Any birder pretrained classification model trained on iNat21 can be used as a
drop-in replacement.  To see all available pretrained models:

```bash
.venv/bin/python -m birder.tools list-models --pretrained
```

Filter to iNat21 models:

```bash
.venv/bin/python -m birder.tools list-models --pretrained --filter '*inat21*'
```

Download and use an alternative:

```bash
# Download a lighter/faster model
.venv/bin/python -m birder.tools download-model mvit_v2_t_il-all

# Run classification with it
.venv/bin/python -m src.run catalog.lrcat --model mvit_v2_t/il-all
```

The `--model` argument takes the birder `network/tag` form (slash-separated),
which matches how the model file is named: `{network}_{tag}.pt`.

#### Fetch species common names (first run only)

Common names are resolved from the iNaturalist API and cached locally.  Run
once after install:

```bash
.venv/bin/python -c "
from src.classifier import Classifier
clf = Classifier()
clf.fetch_common_names()
"
```

This takes a few minutes and writes `data/taxonomy_cache.json`.  Subsequent
runs use the cache with no network calls.

#### Build a regional species whitelist (recommended)

Geo-filtering restricts predictions to species actually observed in the photo's
region, eliminating implausible results.  Build a whitelist before the first run:

```bash
# Standard regions
.venv/bin/python -m src.build_region_lists north_america
.venv/bin/python -m src.build_region_lists europe

# US sub-regions
.venv/bin/python -m src.build_region_lists us_northeast
.venv/bin/python -m src.build_region_lists us_southeast

# Individual US states (two-letter code)
.venv/bin/python -m src.build_region_lists md

# By ISO country code
.venv/bin/python -m src.build_region_lists AU

# All built-in regions at once (continents + US sub-regions + all 50 US states + DC)
.venv/bin/python -m src.build_region_lists --all
```

Results are saved to `data/region_species/{region}.json` and reused on every
subsequent run.

#### Available regions

| Region name | Coverage |
|---|---|
| `any` | No filtering — all model species pass through |
| `north_america` | All of North America |
| `central_america` | Central America & Caribbean |
| `south_america` | South America |
| `europe` | Europe |
| `africa` | Africa |
| `asia` | Asia |
| `oceania` | Oceania |
| `canada` | All of Canada |
| `alaska` | Alaska |
| `hawaii` | Hawaii |
| `us_pacific` | CA, OR, WA |
| `us_mountain` | ID, MT, WY, NV, UT, CO |
| `us_southwest` | AZ, NM, TX, OK |
| `us_midwest` | ND, SD, MN, NE, KS, IA, MO, WI, IL, MI, IN, OH, KY |
| `us_southeast` | FL, GA, SC, NC, AL, MS, LA, TN, AR |
| `us_northeast` | ME, NH, VT, MA, RI, CT, NY, NJ, PA, MD, DE, VA, WV, DC |
| `MD`, `FL`, `CA`, … | Any US two-letter postal abbreviation (falls back to parent sub-region if no state-specific list exists) |
| ISO country codes | `GB`, `AU`, `MX`, … → mapped to nearest broad region |

When photos have GPS coordinates the region is **auto-detected** from EXIF:
US photos resolve to the matching state sub-region; other countries use the
broad continental region.  The `--region` flag overrides this for photos
without GPS.

**Finer regions improve accuracy.**  A narrower whitelist means fewer
plausible-but-wrong species compete for the top prediction.  In practice,
using `us_northeast` instead of `north_america` for a Maryland photo
eliminates hundreds of Pacific coast and southern species from contention,
reducing false positives noticeably — and using `md` (if you build a
state-level whitelist) narrows it further still.  If your photos are all from
one region, always set the most specific region that covers your shoot
locations.

---

## Usage

**Always close Lightroom before running** — LR holds an exclusive lock on the
catalog and concurrent writes will be lost or corrupt it.

```bash
.venv/bin/python -m src.run /path/to/catalog.lrcat [options]
```

### Common invocations

```bash
# Dry run — classify but write nothing
.venv/bin/python -m src.run catalog.lrcat --dry-run

# Live run, RAW + DNG, GPS-derived region
.venv/bin/python -m src.run catalog.lrcat

# Specify region explicitly (no GPS in photos)
.venv/bin/python -m src.run catalog.lrcat --region us_northeast

# Use a US state abbreviation for finer-grained filtering
.venv/bin/python -m src.run catalog.lrcat --region MD

# Disable geo-filtering entirely (classify any bird worldwide)
.venv/bin/python -m src.run catalog.lrcat --region any

# Target a specific folder (substring match)
.venv/bin/python -m src.run catalog.lrcat --folder "2024/Birds"

# Target a specific folder on a specific disk (absolute path prefix)
.venv/bin/python -m src.run catalog.lrcat --folder /Volumes/FastDrive/Photos/Birds

# Include Photoshop documents alongside RAW files
.venv/bin/python -m src.run catalog.lrcat --formats RAW,DNG,PSD

# Only classify 3-star-and-above selects
.venv/bin/python -m src.run catalog.lrcat --min-stars 3

# Lower confidence threshold, tag top 3 predictions
.venv/bin/python -m src.run catalog.lrcat --min-confidence 0.15 --top-k 3

# Use an alternative model
.venv/bin/python -m src.run catalog.lrcat --model mvit_v2_t/il-all

# Re-classify images that were already auto-tagged
.venv/bin/python -m src.run catalog.lrcat --no-skip-tagged

# Re-classify only images whose recorded confidence was below 50%
.venv/bin/python -m src.run catalog.lrcat --retag-below-confidence 0.5

# Remap a volume that moved between runs
.venv/bin/python -m src.run catalog.lrcat --remap /Volumes/OldDrive:/Volumes/NewDrive
```

### All options

```
positional arguments:
  catalog               Path to the .lrcat file

options:
  --formats FORMATS     Comma-separated file formats (default: RAW,DNG).
                        Add PSD for Photoshop documents.
  --folder FOLDER       Filter by folder path. Absolute paths (starting with /)
                        match as a prefix; otherwise substring match.
  --min-stars N         Only classify images with a Lightroom star rating of at
                        least N (1–5). Unrated images are excluded when set.
  --model NETWORK/TAG   Model to use (default: rope_vit_reg4_b14/capi-inat21).
                        See 'python -m birder.tools list-models --pretrained'.
  --min-confidence N    Minimum confidence 0–1 to apply a keyword (default: 0.25)
  --top-k N             Number of top predictions to tag per image (default: 1)
  --dry-run             Classify but do not write to the catalog
  --no-backup           Skip catalog backup before writing
  --limit N             Process at most N images (useful for testing)
  --remap FROM:TO       Remap a path prefix, e.g. /Volumes/Old:/Volumes/New
  --region REGION       Region hint when photos have no GPS, or to override
                        GPS-derived region. Accepts: named region
                        (north_america, us_northeast, alaska, …), US state
                        abbreviation (MD, FL, CA, …), ISO country code
                        (GB, AU, MX, …), or 'any' to disable geo-filtering.
                        GPS auto-detection resolves US photos to the matching
                        state sub-region; other countries to the broad region.
  --no-geo-filter       Disable geographic species filtering entirely
  --no-skip-tagged      Re-classify images that already have species keywords
  --retag-below-confidence N
                        Re-classify images whose previously recorded best
                        confidence is below N. Old keywords are removed first.
                        Requires a classification log from a prior run.
  -v, --verbose         Debug logging
```

---

## Supported file formats

| Format | Extensions | Notes |
|---|---|---|
| Camera RAW | `.arw` `.cr2` `.cr3` `.nef` `.orf` `.raf` `.rw2` `.dng` `.pef` `.srw` | Decoded via rawpy |
| TIFF | `.tif` `.tiff` | Decoded via Pillow |
| JPEG | `.jpg` `.jpeg` | Decoded via Pillow |
| Photoshop | `.psd` `.psb` | Embedded JPEG preview extracted via exiftool; requires **Maximize Compatibility** enabled in Photoshop |

Lightroom stores ORF, ARW, CR2 etc. with `fileFormat = 'RAW'`, so all of these
are included when you pass `--formats RAW`.  PSD and PSB both appear as
`fileFormat = 'PSD'`.

---

## Confidence scoring

Every classification is recorded in a SQLite log co-located with the catalog:

```
/path/to/Catalog.lrcat
/path/to/Catalog_lr_classifier.sqlite   ← auto-created on first run
```

The log stores the model name, timestamp, species label, and raw confidence
score (0.0–1.0) for every prediction that was written to the catalog.

A `Classifier-Confidence` keyword is also written directly to the catalog,
enabling Lightroom smart collection filtering without leaving LR:

| Keyword | Confidence range |
|---------|-----------------|
| Very High | ≥ 90% |
| High | ≥ 75% |
| Medium | ≥ 50% |
| Low | ≥ 25% |
| Very Low | < 25% |

**Example smart collection rule:** `Keywords  |  contain all  |  Classifier-Confidence/Very Low`

After each run a confidence distribution summary is printed:

```
Confidence (all-time):  mean=78%  ≥90%=41%  ≥75%=29%  ≥50%=22%  <50%=8%  (312 images)
```

Use `--retag-below-confidence` to re-run classification on weak results after
a model update — old keywords are automatically removed before re-tagging.

---

## Wiping auto-classification tags

To remove all auto-classification keywords (Bird-Species, Birds, and
Classifier-Confidence hierarchies) from the catalog:

```bash
# Remove all auto-tags from every classified image
.venv/bin/python -m src.wipe /path/to/catalog.lrcat

# Limit to a specific folder (same matching rules as --folder in src.run)
.venv/bin/python -m src.wipe /path/to/catalog.lrcat --folder "2024/Costa Rica"

# Remove only images whose best overall confidence was below 50%
.venv/bin/python -m src.wipe /path/to/catalog.lrcat --below-confidence 0.5

# Remove a broad misclassification — all images tagged as a specific species
.venv/bin/python -m src.wipe /path/to/catalog.lrcat --species "Penelope purpurascens"

# Same, but only where the model was under 60% confident on that species
.venv/bin/python -m src.wipe /path/to/catalog.lrcat --species "Penelope purpurascens" --below-confidence 0.6

# Combine all three filters
.venv/bin/python -m src.wipe /path/to/catalog.lrcat \
    --species "Penelope purpurascens" --below-confidence 0.6 --folder "2024/Costa Rica"

# Preview what would be removed without writing anything
.venv/bin/python -m src.wipe /path/to/catalog.lrcat --species "Penelope purpurascens" --dry-run
```

`--species` matches the scientific name recorded in the classification log
(case-insensitive).  When combined with `--below-confidence`, the threshold
applies to that specific species' confidence rather than the image's overall
best confidence — so you can surgically remove weak identifications of one
species without touching other tags on the same image.

Keywords applied via the Lightroom keyword panel (i.e. not by this tool) are
never touched — only the three hierarchies this tool writes are removed.

---

## XMP sidecar sync

When Lightroom finds that a catalog keyword differs from what is recorded in the
image's `.xmp` sidecar file it shows a yellow "metadata conflict" badge and the
"metadata on disk is out of sync" warning.

After writing keywords to the catalog, the script calls `exiftool` to append
matching entries to the sidecar:

- `dc:subject` — flat keyword names compatible with all XMP-aware apps
- `lr:hierarchicalSubject` — pipe-separated hierarchy paths read by Lightroom's
  keyword panel

If no sidecar exists for an image the XMP step is silently skipped — originals
are never modified.

---

## Idempotency

By default the script skips images that already carry any keyword under the
`Birds > Order` hierarchy.  Re-running against the same catalog is safe and
fast — only new or untagged images are processed.

To force re-classification of previously tagged images:

```bash
.venv/bin/python -m src.run catalog.lrcat --no-skip-tagged
```

### Manually-classed images

Tag any image with the Lightroom keyword **"manually classed"** to permanently
exclude it from auto-classification.  This lets you correct a mis-identification
in Lightroom and be confident the script will never overwrite your correction.

---

## Project layout

```
src/
  run.py               Main entry point
  classifier.py        birder model wrapper; returns ranked Prediction objects
  catalog.py           Lightroom .lrcat SQLite read/write interface
  taxonomy.py          iNat21 label parsing, common name cache, order display names
  geo_filter.py        Regional species whitelist filtering
  build_region_lists.py  CLI tool to build per-region whitelists from iNaturalist
  raw_utils.py         Image loading (RAW via rawpy, PSD via exiftool, rest via Pillow)
  preview.py           JPEG preview extraction from PSD/PSB via exiftool
  xmp_writer.py        XMP sidecar keyword sync via exiftool
  classification_log.py  Per-image confidence log (SQLite, co-located with catalog)
  wipe.py              CLI tool to remove auto-classification tags from a catalog

data/
  taxonomy_cache.json          iNat scientific name → common name (auto-populated)
  taxonomy_synonyms.json       Old → current scientific name synonyms
  taxonomy_order_names.json    Order scientific name → hyphenated display name
  region_species/
    north_america.json         Per-region species whitelists (built manually)
    europe.json
    …

/path/to/Catalog_lr_classifier.sqlite   ← classification log, created alongside catalog

models/
  rope_vit_reg4_b14_capi-inat21.pt   Downloaded model weights (not in repo)
```

---

## Known limitations / future work

### Multi-bird photos

The classifier is a single-label model — it sees the whole frame and returns
one probability distribution. When two species appear in the same photo it
will typically lock onto the most prominent subject. The `--top-k` flag is a
partial workaround (e.g. `--top-k 2 --min-confidence 0.15` may tag both when
confidence is split), but is not reliable.

The proper fix is a two-stage pipeline: an object detector to crop each
individual bird, followed by per-crop classification. Each crop would then
contribute its own keywords to the same image. This is deferred as future work.
In the meantime, use the `manually classed` keyword to lock any mis-tagged
multi-bird images so they are not overwritten on subsequent runs.

---

## Licenses

### This project

Copyright 2026 George Schlossnagle.
Licensed under the **Apache License, Version 2.0** — see [`LICENSE`](LICENSE).

### birder

The [birder library](https://github.com/birder-project/birder) is licensed
under the **Apache License, Version 2.0**.  Some individual network
implementations carry additional per-file restrictions; consult the birder
source for details.

### Model weights — `rope_vit_reg4_b14 / capi-inat21`

The pre-trained weights are distributed by the birder project and are
fine-tuned on the iNaturalist 2021 (iNat21) dataset.

The iNat21 dataset is subject to the
[iNaturalist Terms of Service](https://www.inaturalist.org/terms) and the
[Visipedia dataset terms](https://github.com/visipedia/inat_comp/tree/master/2021),
which restrict use to **non-commercial research and educational purposes only**.

> **The model weights are therefore also restricted to non-commercial use.**
> Do not use this tool in a commercial product or service without first
> obtaining appropriate permissions from iNaturalist / Cornell University.

The CAPI self-supervised pretraining method
([arXiv:2502.08769](https://arxiv.org/abs/2502.08769)) is licensed under
Apache 2.0 by the birder project.

### PyTorch

[PyTorch](https://github.com/pytorch/pytorch) is licensed under the
**BSD 3-Clause License**.

### rawpy / libraw

[rawpy](https://github.com/letmaik/rawpy) is licensed under the **MIT License**.
It links against [LibRaw](https://www.libraw.org), which is available under the
LGPL 2.1 or CDDL 1.0 licenses.

### Pillow

[Pillow](https://github.com/python-pillow/Pillow) is licensed under the
**HPND License** (open source, permissive).

### exiftool

[ExifTool](https://exiftool.org) by Phil Harvey is released under the
**Perl Artistic License / GPL** (dual-licensed).  It is invoked as an external
process and is not linked or distributed with this project.

### iNaturalist API

Species common names and regional occurrence data are fetched from the
[iNaturalist API](https://api.inaturalist.org/v1/docs/) and cached locally.
Usage is subject to the [iNaturalist Terms of Service](https://www.inaturalist.org/terms).
