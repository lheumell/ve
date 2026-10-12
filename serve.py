#!/usr/bin/env python3
"""
serve.py — sert le site en local et ajoute le bouton « Nouvelle recherche ».

    python3 serve.py            # puis ouvre http://localhost:8000
    python3 serve.py 9000       # autre port

Le bouton lance ./publish.sh (collecte des 3 sites, commit et push de docs/lots.json), exactement comme le cron.
Seulement en local : le serveur n'écoute que sur 127.0.0.1, et la version GitHub Pages n'a pas de bouton.
"""
import fcntl, http.server, json, os, pathlib, subprocess, sys, threading, time

RACINE = pathlib.Path(__file__).resolve().parent
DOCS = RACINE / "docs"
LOG = RACINE / "refresh.log"
VERROU = RACINE / ".publish.lock"                 # le même que publish.sh : empêche un double run (cron + bouton)
CMD = os.environ.get("REFRESH_CMD", str(RACINE / "publish.sh"))

# Pages autorisées à appeler l'API : le site public et le site local. Tout autre site web est refusé.
ORIGINES = {"https://lheumell.github.io", "http://localhost:8000", "http://127.0.0.1:8000"}

etat = {"debut": None, "fin": None, "code": None}
mutex = threading.Lock()


def verrou_pris():
    """Vrai si un publish.sh tourne (lancé par ce serveur ou par le cron)."""
    with open(VERROU, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def lancer():
    with open(LOG, "w") as log:
        p = subprocess.run([CMD], cwd=RACINE, stdout=log, stderr=subprocess.STDOUT)
    etat.update(fin=time.time(), code=p.returncode)


def queue_log(n=4):
    try:
        lignes = [l.rstrip() for l in LOG.read_text(errors="replace").splitlines() if l.strip()]
    except OSError:
        return []
    return lignes[-n:]


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(DOCS), **kw)

    def log_message(self, *a):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        origine = self.headers.get("Origin")
        if origine in ORIGINES | {f"http://localhost:{self.server.server_port}", f"http://127.0.0.1:{self.server.server_port}"}:
            self.send_header("Access-Control-Allow-Origin", origine)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "X-Veille")
            self.send_header("Access-Control-Allow-Private-Network", "true")   # site https -> localhost
            self.send_header("Vary", "Origin")
        super().end_headers()

    def origine_ok(self):
        o = self.headers.get("Origin")
        return o is None or o in ORIGINES | {f"http://localhost:{self.server.server_port}",
                                             f"http://127.0.0.1:{self.server.server_port}"}

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def _json(self, code, obj):
        corps = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def do_GET(self):
        if self.path.split("?")[0] == "/api/status":
            return self._json(200, {"en_cours": verrou_pris(), **etat, "log": queue_log()})
        super().do_GET()

    def do_POST(self):
        if self.path != "/api/refresh":
            return self._json(404, {"erreur": "inconnu"})
        if not self.origine_ok() or self.headers.get("X-Veille") != "1":     # anti-CSRF : un autre site ne peut pas déclencher
            return self._json(403, {"erreur": "refusé"})
        with mutex:
            if verrou_pris():
                return self._json(409, {"erreur": "une recherche est déjà en cours"})
            etat.update(debut=time.time(), fin=None, code=None)
            threading.Thread(target=lancer, daemon=True).start()
        time.sleep(0.5)                                  # laisse publish.sh prendre le verrou
        self._json(202, {"en_cours": True, **etat})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Site local : http://localhost:{port}   (Ctrl+C pour arrêter)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
