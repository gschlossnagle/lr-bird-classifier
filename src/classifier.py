"""
Bird classifier using birder's rope_vit_reg4_b14 / capi-inat21 model.

Provides a simple Classifier class that accepts image paths (including
camera RAW files) and returns ranked species predictions with common names.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import torch
from PIL import Image
from torchvision import transforms

from birder.common import fs_ops

from .raw_utils import load_image
from .taxonomy import build_label_map, fetch_all_bird_common_names, get_common_name

log = logging.getLogger(__name__)

DEFAULT_NETWORK = "rope_vit_reg4_b14"
DEFAULT_TAG = "capi-inat21"


@dataclass
class Prediction:
    """A single species prediction."""
    rank: int
    common_name: str
    sci_name: str
    confidence: float        # 0.0 – 1.0
    is_bird: bool
    label: str               # raw iNat21 label


class Classifier:
    """
    Bird classifier wrapping birder's iNat21 model.

    Usage::

        clf = Classifier()
        predictions = clf.predict("/path/to/image.ARW")
        for p in predictions:
            print(f"{p.rank}. {p.common_name} ({p.confidence:.1%})")
    """

    def __init__(
        self,
        network: str = DEFAULT_NETWORK,
        tag: str = DEFAULT_TAG,
        *,
        device: Optional[str] = None,
        models_dir: Optional[str | Path] = None,
        top_k: int = 5,
        birds_only: bool = True,
    ) -> None:
        """
        Args:
            network: birder network name.
            tag: birder model tag.
            device: torch device string. Auto-detects MPS → CUDA → CPU if None.
            models_dir: directory containing downloaded .pt model files.
                        Defaults to 'models/' relative to cwd.
            top_k: number of top predictions to return.
            birds_only: if True, non-bird predictions are excluded from results.
        """
        self.network = network
        self.tag = tag
        self.top_k = top_k
        self.birds_only = birds_only

        # Device selection
        if device:
            self.device = torch.device(device)
        elif torch.backends.mps.is_available():
            self.device = torch.device("mps")
        elif torch.cuda.is_available():
            self.device = torch.device("cuda")
        else:
            self.device = torch.device("cpu")
        log.info(f"Using device: {self.device}")

        # Change to models_dir if specified so birder can find the .pt file
        if models_dir:
            import os
            os.chdir(Path(models_dir).parent)

        # Load model
        log.info(f"Loading model {network}/{tag}...")
        self._net, (self._class_to_idx, self._signature, self._rgb_stats, *_) = (
            fs_ops.load_model(self.device, network, tag=tag, inference=True)
        )
        self._net.eval()

        # Build label → common name map (from cache, no API calls at init)
        log.info("Building label map...")
        self._label_map = build_label_map(
            self._class_to_idx,
            birds_only=False,       # keep full map; filter at predict time
            fetch_missing=False,    # don't block init on network calls
        )
        self._idx_to_label = {v: k for k, v in self._class_to_idx.items()}

        # Build image transform
        size = self._signature.get("inputs", [{}])[0].get("data_shape", [None, None, 224, 224])[-1]
        self._transform = transforms.Compose([
            transforms.Resize((size, size)),
            transforms.ToTensor(),
            transforms.Normalize(
                mean=self._rgb_stats["mean"],
                std=self._rgb_stats["std"],
            ),
        ])
        log.info("Classifier ready.")

    def predict(self, image_path: str | Path, top_k: Optional[int] = None) -> list[Prediction]:
        """
        Classify a single image and return ranked predictions.

        Args:
            image_path: path to an image file (JPEG, PNG, TIFF, ARW, CR2, NEF, etc.)
            top_k: override the instance-level top_k for this call.

        Returns:
            List of Prediction objects, sorted by confidence descending.
        """
        img = load_image(image_path)
        return self.predict_image(img, top_k=top_k)

    def predict_image(self, image: Image.Image, top_k: Optional[int] = None) -> list[Prediction]:
        """Classify a PIL image and return ranked predictions."""
        k = top_k or self.top_k
        img = image.convert("RGB")
        tensor = self._transform(img).unsqueeze(0).to(self.device)

        with torch.no_grad():
            logits = self._net(tensor)
            probs = torch.softmax(logits, dim=1)[0]

        # Get top candidates (fetch more if birds_only so we have enough after filtering)
        fetch_k = k * 5 if self.birds_only else k
        fetch_k = min(fetch_k, len(self._class_to_idx))
        top = torch.topk(probs, fetch_k)

        predictions: list[Prediction] = []
        for prob, idx in zip(top.values.tolist(), top.indices.tolist()):
            label = self._idx_to_label[idx]
            from .taxonomy import parse_label
            parsed = parse_label(label)
            is_bird = parsed.get("is_bird", False)

            if self.birds_only and not is_bird:
                continue

            display_name = get_common_name(label, self._label_map)
            sci_name = parsed.get("sci_name", label)

            predictions.append(Prediction(
                rank=0,           # assigned below
                common_name=display_name.title() if display_name else sci_name,
                sci_name=sci_name,
                confidence=prob,
                is_bird=is_bird,
                label=label,
            ))

            if len(predictions) >= k:
                break

        for i, p in enumerate(predictions, 1):
            p.rank = i

        return predictions

    def fetch_common_names(self) -> None:
        """
        Populate the taxonomy cache with common names from iNaturalist.

        Uses a paginated bulk query against the Aves taxon (~20 API pages)
        rather than per-species lookups (~10 000 calls), so this completes
        in a few minutes rather than over an hour.  Safe to re-run; already-
        cached entries are not re-fetched.
        """
        fetch_all_bird_common_names(self._class_to_idx)
        # Rebuild the in-memory label map from the now-populated cache
        self._label_map = build_label_map(
            self._class_to_idx,
            birds_only=False,
            fetch_missing=False,
        )
        log.info("Common names fetched and cached.")
