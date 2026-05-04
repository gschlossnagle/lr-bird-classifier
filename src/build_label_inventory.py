"""
Build a canonical label inventory from existing region-species JSON files.
"""

from __future__ import annotations

import argparse
import io
import json
import pickle
import sys
import zipfile
from pathlib import Path

from .taxonomy import parse_label

DATA_DIR = Path(__file__).parent.parent / "data" / "region_species"
DEFAULT_MODELS_DIR = Path(__file__).parent.parent / "models"


def collect_labels(region_files: list[Path]) -> list[str]:
    labels: set[str] = set()
    for path in region_files:
        data = json.loads(path.read_text())
        labels.update(data.get("labels", []))
    return sorted(labels)


class _SkipTensorsUnpickler(pickle.Unpickler):
    """Unpickler that skips tensor storage when reading model metadata."""

    def persistent_load(self, pid):
        return None

    def find_class(self, module: str, name: str):
        if module == "torch._utils" and name in ("_rebuild_tensor_v2", "_rebuild_tensor"):
            return lambda *a, **kw: None
        if module == "torch.storage" and name == "_load_from_bytes":
            return lambda *a, **kw: None
        return super().find_class(module, name)


def load_model_labels(models_dir: Path) -> list[str]:
    """Load every bird label from a local iNat21 model file without loading weights."""
    pt_files = sorted(models_dir.glob("*inat21*.pt"))
    if not pt_files:
        pt_files = sorted(models_dir.glob("*.pt"))
    if not pt_files:
        raise FileNotFoundError(f"No .pt model files found in {models_dir}")

    pt_file = pt_files[0]
    with zipfile.ZipFile(pt_file, "r") as zf:
        pkl_names = [name for name in zf.namelist() if name.endswith("data.pkl")]
        if not pkl_names:
            raise ValueError(f"Unexpected model file format: {pt_file.name}")
        with zf.open(pkl_names[0]) as raw:
            data = _SkipTensorsUnpickler(io.BytesIO(raw.read())).load()

    class_to_idx = data.get("class_to_idx")
    if not class_to_idx:
        raise ValueError(f"class_to_idx not found in {pt_file.name}")

    sample = next(iter(class_to_idx))
    if "_" not in sample or not sample.split("_")[0].isdigit():
        raise ValueError(
            f"{pt_file.name} uses non-iNat21 labels (e.g. {sample!r}). "
            "Download an iNat21 model and retry."
        )

    labels = [
        label
        for label in class_to_idx
        if parse_label(label).get("is_bird")
    ]
    return sorted(labels)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a canonical label inventory from region-species files.")
    p.add_argument("--output", required=True, help="Path to the output text file")
    p.add_argument(
        "--region",
        action="append",
        default=[],
        help="Specific region filename stem to include, e.g. north_america (repeatable)",
    )
    p.add_argument(
        "--all-model-birds",
        action="store_true",
        help="Use every bird label from a local iNat21 model instead of region whitelists",
    )
    p.add_argument(
        "--models-dir",
        default=str(DEFAULT_MODELS_DIR),
        help="Directory containing local .pt model files for --all-model-birds",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if args.all_model_birds:
        labels = load_model_labels(Path(args.models_dir))
    elif args.region:
        region_files = [DATA_DIR / f"{name}.json" for name in args.region]
        missing = [str(path) for path in region_files if not path.exists()]
        if missing:
            raise SystemExit(f"Missing region file(s): {', '.join(missing)}")
        labels = collect_labels(region_files)
    else:
        region_files = sorted(DATA_DIR.glob("*.json"))
        labels = collect_labels(region_files)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(f"{label}\n" for label in labels), encoding="utf-8")
    print(f"Wrote {len(labels)} labels to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
