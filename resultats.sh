#!/usr/bin/env bash
# Relève les prix d'adjudication Interenchères du jour (resultats.py) et publie docs/historique.json.
# Lancé par cron à 16h45 : voir `crontab -l`.
set -euo pipefail
cd "$(dirname "$0")"

# Même verrou que publish.sh : jamais deux écritures en parallèle sur docs/.
exec 9>.publish.lock
flock -n 9 || { echo "déjà en cours, abandon"; exit 0; }

echo "=== résultats $(date '+%Y-%m-%d %H:%M:%S') ==="

# cron n'a pas d'environnement graphique : Interenchères (Cloudflare) exige un navigateur visible → WSLg.
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/mnt/wslg/runtime-dir}"

PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

timeout 20m "$PYTHON" resultats.py "$@"

if [ -n "$(git status --porcelain docs/historique.json)" ]; then
  git add docs/historique.json
  git commit -q -m "resultats $(date '+%Y-%m-%d %H:%M')"
  git push -q
  echo "publié"
else
  echo "aucun changement"
fi
