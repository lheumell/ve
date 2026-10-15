#!/usr/bin/env bash
# Collecte les lots (tous sites, sans filtre) et publie docs/lots.json sur GitHub Pages.
# Lancé chaque jour par cron : voir `crontab -l`.
set -euo pipefail
cd "$(dirname "$0")"

# Rotation : garde les 500 dernières lignes de cron.log (troncature en place, cron garde son descripteur ouvert).
rotation() {
  [ -f cron.log ] || return 0
  local tmp; tmp="$(mktemp)"
  tail -n 500 cron.log > "$tmp" && cat "$tmp" > cron.log
  rm -f "$tmp"
}
trap rotation EXIT

# Un seul run à la fois.
exec 9>.publish.lock
flock -n 9 || { echo "déjà en cours, abandon"; exit 0; }

echo "=== $(date '+%Y-%m-%d %H:%M:%S') ==="

# cron n'a pas d'environnement graphique : Interenchères (Cloudflare) exige un navigateur visible → WSLg.
export DISPLAY="${DISPLAY:-:0}"
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/mnt/wslg/runtime-dir}"

PYTHON=".venv/bin/python"
[ -x "$PYTHON" ] || PYTHON="python3"

timeout 30m "$PYTHON" build_site.py --sites "${SITES:-vpauto,alcopa,interencheres}" "$@"

# Garde-fou : si le nombre de lots s'effondre (< 50 % de la version publiée), on ne publie pas.
avant=$(git show HEAD:docs/lots.json 2>/dev/null | "$PYTHON" -c 'import json,sys; print(len(json.load(sys.stdin)["lots"]))' 2>/dev/null || echo 0)
apres=$("$PYTHON" -c 'import json; print(len(json.load(open("docs/lots.json"))["lots"]))')
if [ "$avant" -gt 0 ] && [ $((apres * 2)) -lt "$avant" ]; then
  echo "ABANDON : $apres lots contre $avant publiés, collecte suspecte — rien n'est publié"
  git checkout -- docs/lots.json
  exit 1
fi

if [ -n "$(git status --porcelain docs/lots.json)" ]; then
  git add docs/lots.json
  git commit -q -m "lots $(date '+%Y-%m-%d %H:%M')"
  git push -q
  echo "publié ($apres lots)"
else
  echo "aucun changement"
fi

# Alertes Telegram (voir notify.py) : un bot absent ou en panne ne doit jamais bloquer la publication.
"$PYTHON" notify.py || echo "notify.py a échoué (ignoré)"
