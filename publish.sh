#!/usr/bin/env bash
# Collecte les lots et publie docs/lots.json sur GitHub Pages.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

"$PYTHON" build_site.py --sites "${SITES:-vpauto}" "$@"

if [ -n "$(git status --porcelain docs/lots.json)" ]; then
  git add docs/lots.json
  git commit -q -m "lots $(date '+%Y-%m-%d %H:%M')"
  git push -q
  echo "publié"
else
  echo "aucun changement"
fi
