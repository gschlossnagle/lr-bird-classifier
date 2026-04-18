#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# lr-bird-classifier setup script
#
# Downloads the default classification model, fetches species common names
# from iNaturalist, and builds regional species whitelists for all supported
# regions.  Run this once after 'pip install -r requirements.txt'.
#
# Usage:
#   bash scripts/setup.sh
# ---------------------------------------------------------------------------

set -euo pipefail

PYTHON=".venv/bin/python"
BOLD="\033[1m"
GREEN="\033[0;32m"
RESET="\033[0m"

step() { echo -e "\n${BOLD}${GREEN}==>${RESET}${BOLD} $*${RESET}"; }
info() { echo "    $*"; }

# ── Preflight ────────────────────────────────────────────────────────────────

if [ ! -f "$PYTHON" ]; then
  echo "Error: virtual environment not found at .venv/"
  echo ""
  echo "Please set it up first:"
  echo "  python -m venv .venv"
  echo "  source .venv/bin/activate"
  echo "  pip install -r requirements.txt"
  exit 1
fi

if [ ! -f "requirements.txt" ]; then
  echo "Error: run this script from the lr-bird-classifier root directory."
  exit 1
fi

echo ""
echo -e "${BOLD}lr-bird-classifier setup${RESET}"
echo "This will download the classification model and build lookup data."
echo "Expect 5–15 minutes depending on your internet connection."

# ── Step 1: Model ────────────────────────────────────────────────────────────

step "1/3  Downloading classification model (rope_vit_reg4_b14 / capi-inat21)"
info "Model file: models/rope_vit_reg4_b14_capi-inat21.pt"
info "(~350 MB — may take a moment)"
echo ""
$PYTHON -m birder.tools download-model rope_vit_reg4_b14_capi-inat21

# ── Step 2: Common names ─────────────────────────────────────────────────────

step "2/3  Fetching species common names from iNaturalist"
info "Writes: data/taxonomy_cache.json"
info "(A few minutes — one-time network fetch)"
echo ""
$PYTHON -c "
from src.classifier import Classifier
clf = Classifier()
clf.fetch_common_names()
print('  Common names cached.')
"

# ── Step 3: Regional whitelists ───────────────────────────────────────────────

step "3/3  Building regional species whitelists"
info "Writes: data/region_species/{north_america,central_america,south_america,"
info "         europe,africa,asia,oceania}.json"
info "(A few minutes — queries iNaturalist for each region)"
echo ""
$PYTHON -m src.build_region_lists --all

# ── Done ─────────────────────────────────────────────────────────────────────

echo ""
echo -e "${BOLD}${GREEN}Setup complete!${RESET}"
echo ""
echo "Try a dry run against your catalog:"
echo "  .venv/bin/python -m src.run /path/to/catalog.lrcat --dry-run"
echo ""
echo "See README.md for full usage."
