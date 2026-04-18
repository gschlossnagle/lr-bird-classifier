# lr-bird-classifier

Automatically classifies bird photos in a Lightroom Classic catalog and writes
species keywords back into the catalog hierarchy.  A CV model identifies the
species; taxonomy lookups translate scientific names to common names; and the
keyword tree is built to match a photographer-friendly structure.

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

### Download the model

The classifier uses the `rope_vit_reg4_b14` model with the `capi-inat21` tag
from the [birder project](https://github.com/birder-project/birder).  Place
the downloaded `.pt` file in the `models/` directory:

```
models/rope_vit_reg4_b14_capi-inat21.pt
```

Refer to the birder documentation for the download command.

### Fetch species common names (first run only)

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

### Build a regional species whitelist (recommended)

Geo-filtering restricts predictions to species actually observed in the photo's
region, eliminating implausible results.  Build a whitelist before the first run:

```bash
# Standard regions
.venv/bin/python -m src.build_region_lists north_america
.venv/bin/python -m src.build_region_lists europe

# By ISO country code
.venv/bin/python -m src.build_region_lists AU

# All built-in regions at once
.venv/bin/python -m src.build_region_lists --all
```

Results are saved to `data/region_species/{region}.json` and reused on every
subsequent run.

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
.venv/bin/python -m src.run catalog.lrcat --region US

# Target a specific folder (substring match)
.venv/bin/python -m src.run catalog.lrcat --folder "2024/Birds"

# Target a specific folder on a specific disk (absolute path prefix)
.venv/bin/python -m src.run catalog.lrcat --folder /Volumes/FastDrive/Photos/Birds

# Include Photoshop documents alongside RAW files
.venv/bin/python -m src.run catalog.lrcat --formats RAW,DNG,PSD

# Lower confidence threshold, tag top 3 predictions
.venv/bin/python -m src.run catalog.lrcat --min-confidence 0.15 --top-k 3

# Re-classify images that were already auto-tagged
.venv/bin/python -m src.run catalog.lrcat --no-skip-tagged

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
  --min-confidence N    Minimum confidence 0–1 to apply a keyword (default: 0.25)
  --top-k N             Number of top predictions to tag per image (default: 1)
  --dry-run             Classify but do not write to the catalog
  --no-backup           Skip catalog backup before writing
  --limit N             Process at most N images (useful for testing)
  --remap FROM:TO       Remap a path prefix, e.g. /Volumes/Old:/Volumes/New
  --region REGION       Region or country-code hint when photos have no GPS
                        (north_america, europe, US, GB, AU, …)
  --no-geo-filter       Disable geographic species filtering entirely
  --no-skip-tagged      Re-classify images that already have species keywords
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

data/
  taxonomy_cache.json          iNat scientific name → common name (auto-populated)
  taxonomy_synonyms.json       Old → current scientific name synonyms
  taxonomy_order_names.json    Order scientific name → hyphenated display name
  region_species/
    north_america.json         Per-region species whitelists (built manually)
    europe.json
    …

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
