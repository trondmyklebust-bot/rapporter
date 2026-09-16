#!/usr/bin/env python3
"""
Lokal server for Chrome-utvidelsen «NB videobeskrivelse».

Utvidelsen sender adressen til videostrømmen (HLS/.m3u8 fra Wowza, eller en
mp4-URL) hit. Serveren kjører beskriv_video.analyser(): ffmpeg henter
stillbilder og lydspor fra strømmen, bildene går til gemma4 på NB-inferens og
lyden til NB-Whisper parallelt, og en sluttbeskrivelse flettes sammen.

Kun standardbiblioteket. Start med:
    python3 server.py                 # lytter på http://127.0.0.1:8170
    NB_INFERENS_TOKEN=xxx python3 server.py --port 8170

Ruter:
    GET  /helse            → {"ok": true, ...}
    POST /jobb             → {"id": "..."}   body: {"kilde": url, "referer": ..., "antall": 8,
                                                   "transkriber": true, "bit_sekunder": 20, ...}
    GET  /jobb/<id>        → {"status": "kjører"|"ferdig"|"feil", "logg": [...], "resultat": {...}}
    GET  /modeller         → {"modeller": [{"navn", "syn", "kapabiliteter"}], "standard": "..."}
    GET  /storyboard/<id>  → storyboardet for jobben som ferdig HTML-side
    GET  /jobber           → liste over jobber
"""

from __future__ import annotations

import argparse
import html
import json
import re
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beskriv_video as bv  # noqa: E402

JOBBER: dict[str, dict] = {}
LAS = threading.Lock()
MAKS_JOBBER = 50

# Storyboardene legges her og hentes via /storyboard/<id>.
STORYBOARD_MAPPE = Path(__file__).resolve().parent / "storyboards"

# Modellista hentes fra inferensserveren og holdes en stund, siden den
# krever ett /api/show-kall per modell.
MODELL_CACHE: dict = {"tid": 0.0, "modeller": []}
MODELL_CACHE_SEKUNDER = 300
MODELL_LAS = threading.Lock()


STARTSIDE_CSS = """
:root { --blaa: #1F4E79; --lys: #EBF3FB; --graa: #6c757d; --kant: #dde3ef;
        --bg: #f7f9fc; --kort: #fff; --tekst: #1a1a1a; --ok: #1a7a45; --feil: #b3261e; }
@media (prefers-color-scheme: dark) {
  :root { --blaa: #7bb3e3; --lys: #1a2733; --graa: #97a1ad; --kant: #2c333c;
          --bg: #14181d; --kort: #1c2128; --tekst: #e6e9ee; --ok: #6fcf97; --feil: #f28b82; }
}
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: flex; flex-direction: column;
       background: var(--bg); color: var(--tekst);
       font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
main { flex: 1 0 auto; width: 100%; max-width: 900px; margin: 0 auto; padding: 32px 16px 48px; }
footer { margin-top: auto; border-top: 1px solid var(--kant); color: var(--graa);
         font-size: .85rem; padding: 14px 16px; text-align: center; }
h1 { margin: 0 0 6px; font-size: 1.9rem; color: var(--blaa); }
h2 { font-size: 1.05rem; color: var(--blaa); margin: 32px 0 10px; font-weight: 600; }
p.meta { color: var(--graa); margin: 0; font-size: .9rem; }
.status { display: grid; grid-template-columns: max-content 1fr; gap: 4px 16px;
          background: var(--kort); border: 1px solid var(--kant); border-radius: 10px;
          padding: 14px 18px; font-size: .92rem; overflow-wrap: anywhere; }
.status dt { color: var(--graa); }
.status dd { margin: 0; }
.ok { color: var(--ok); } .nei { color: var(--feil); }
ol { padding-left: 1.3em; } code { font-size: .9em; }
table { width: 100%; border-collapse: collapse; background: var(--kort);
        border: 1px solid var(--kant); border-radius: 10px; overflow: hidden; }
td { padding: 8px 12px; border-top: 1px solid var(--kant); }
tr:first-child td { border-top: 0; }
td.t { color: var(--graa); white-space: nowrap; width: 1%; font-variant-numeric: tabular-nums; }
a { color: var(--blaa); }
"""


def lagrede_storyboards() -> list[dict]:
    """Storyboardene i mappa, nyeste først, med tittelen fra <title>."""
    liste = []
    for fil in STORYBOARD_MAPPE.glob("*.html"):
        with fil.open(encoding="utf-8", errors="replace") as f:
            m = re.search(r"<title>(.*?)</title>", f.read(4000), re.S)
        liste.append({"id": fil.stem, "tittel": html.unescape(m.group(1).strip()) if m else fil.stem,
                      "tid": fil.stat().st_mtime})
    return sorted(liste, key=lambda s: -s["tid"])


def startside() -> str:
    e = html.escape
    ffmpeg = bool(__import__("shutil").which("ffmpeg"))
    rader = "".join(
        f'<tr><td class="t">{time.strftime("%d.%m.%Y %H:%M", time.localtime(s["tid"]))}</td>'
        f'<td><a href="/storyboard/{e(s["id"])}" target="_blank" rel="noopener">{e(s["tittel"])}</a></td></tr>'
        for s in lagrede_storyboards())
    return f"""<!DOCTYPE html>
<html lang="no"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Videobeskrivelse</title><style>{STARTSIDE_CSS}</style></head>
<body><main>
<h1>Videobeskrivelse</h1>
<p class="meta">Video på nb.no beskrevet med en modell på NBs inferensserver, og talen transkribert med NB-Whisper.</p>
<h2>Status</h2>
<dl class="status">
  <dt>Versjon</dt><dd>{e(versjon())}</dd>
  <dt>Modell</dt><dd>{e(INNSTILLINGER["modell"] or bv.STANDARD_MODELL)}</dd>
  <dt>Inferens</dt><dd>{e(INNSTILLINGER["url"])}</dd>
  <dt>Whisper</dt><dd>{e(INNSTILLINGER["whisper_url"])}</dd>
  <dt>Musikkfilter</dt><dd>{"på" if bv.STANDARD_MUSIKKFILTER else "av"} som standard</dd>
  <dt>ffmpeg</dt><dd class="{'ok' if ffmpeg else 'nei'}">{"funnet" if ffmpeg else "mangler i PATH"}</dd>
</dl>
<h2>Slik bruker du den</h2>
<ol>
  <li>Last inn utvidelsen <code>chrome-utvidelse/</code> i <code>chrome://extensions</code>
      (eller <code>nb-videobeskrivelse</code> fra nb-verktoy).</li>
  <li>Åpne en film på nb.no og trykk play.</li>
  <li>Velg «Videobeskrivelse» i NB-verktøydokken nede til høyre.</li>
</ol>
<h2>Storyboards</h2>
{f"<table>{rader}</table>" if rader else '<p class="meta">Ingen storyboards ennå.</p>'}
</main>
<footer>NB videobeskrivelse · <a href="/helse">/helse</a> · <a href="/jobber">/jobber</a></footer>
</body></html>"""


def versjon() -> str:
    """Kortformen av git-commiten koden kjører fra, så det er synlig hva som kjører."""
    try:
        r = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return "ukjent"


def modelliste(frisk: bool = False) -> list[dict]:
    """
    Modellene serveren tilbyr, hver med om den kan se bilder.

    `syn` er True, False, eller None når serveren ikke kunne svare på
    hva modellen kan.
    """
    with MODELL_LAS:
        fersk_nok = time.time() - MODELL_CACHE["tid"] < MODELL_CACHE_SEKUNDER
        if MODELL_CACHE["modeller"] and fersk_nok and not frisk:
            return MODELL_CACHE["modeller"]

    url, token = INNSTILLINGER["url"], INNSTILLINGER["token"]
    navn = bv.tilgjengelige_modeller(url, token)
    if not navn:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(navn))) as pool:
        kapabiliteter = list(pool.map(
            lambda n: bv.modell_kapabiliteter(url, token, n), navn))
    modeller = [
        {"navn": n,
         "syn": ("vision" in k) if k is not None else None,
         "kapabiliteter": k or []}
        for n, k in zip(navn, kapabiliteter)
    ]
    with MODELL_LAS:
        MODELL_CACHE["modeller"] = modeller
        MODELL_CACHE["tid"] = time.time()
    return modeller

INNSTILLINGER = {
    "url": os.environ.get("NB_INFERENS_URL", bv.STANDARD_URL),
    "token": os.environ.get("NB_INFERENS_TOKEN"),
    "modell": os.environ.get("NB_INFERENS_MODELL"),  # None → gemma4:26b-a4b-it-q8_0 på Ollama
    "whisper_url": os.environ.get("NB_WHISPER_URL", bv.STANDARD_WHISPER_URL),
}


def _ny_jobb(kilde: str, param: dict) -> str:
    jobb_id = uuid.uuid4().hex[:12]
    jobb = {
        "id": jobb_id,
        "kilde": kilde,
        "urn": param.get("urn"),
        "tittel": param.get("tittel"),
        "status": "kjører",
        "startet": time.time(),
        "logg": [],
        "resultat": None,
        "feil": None,
        "storyboard": False,
    }
    with LAS:
        JOBBER[jobb_id] = jobb
        # Rydd bort de eldste hvis lista vokser
        if len(JOBBER) > MAKS_JOBBER:
            for gammel in sorted(JOBBER.values(), key=lambda j: j["startet"])[: len(JOBBER) - MAKS_JOBBER]:
                JOBBER.pop(gammel["id"], None)

    def logg(melding: str) -> None:
        with LAS:
            jobb["logg"].append({"tid": round(time.time() - jobb["startet"], 1), "melding": melding})
        print(f"[{jobb_id}] {melding}", file=sys.stderr, flush=True)

    def kjor() -> None:
        try:
            resultat = bv.analyser(
                kilde,
                antall=int(param.get("antall") or 8),
                bredde=int(param.get("bredde") or 768),
                url=param.get("inferens_url") or INNSTILLINGER["url"],
                token=INNSTILLINGER["token"],
                modell=param.get("modell") or INNSTILLINGER["modell"],
                backend=param.get("backend") or "auto",
                per_kall=int(param.get("per_kall") or bv.BILDER_PER_KALL),
                timeout=int(param.get("timeout") or 600),
                transkriber_lyd=bool(param.get("transkriber", True)),
                whisper_url=param.get("whisper_url") or INNSTILLINGER["whisper_url"],
                sprak=param.get("sprak") or "no",
                bit_sekunder=int(param.get("bit_sekunder", bv.STANDARD_BIT_SEKUNDER)),
                samtolk=bool(param.get("samtolk", True)),
                lydfilter=param.get("lydfilter") or "ingen",
                musikkfilter=bool(param.get("musikkfilter", bv.STANDARD_MUSIKKFILTER)),
                referer=param.get("referer"),
                user_agent=param.get("user_agent"),
                storyboard=(STORYBOARD_MAPPE / f"{jobb_id}.html"
                            if param.get("storyboard", True) else None),
                tittel=param.get("tittel"),
                logg=logg,
            )
            with LAS:
                jobb["resultat"] = resultat
                jobb["storyboard"] = bool(resultat.get("storyboard"))
                jobb["status"] = "ferdig"
            logg("Ferdig")
        except Exception as e:  # noqa: BLE001 - alt skal tilbake til utvidelsen
            with LAS:
                jobb["feil"] = str(e)
                jobb["status"] = "feil"
            logg(f"Feil: {e}")

    threading.Thread(target=kjor, daemon=True).start()
    return jobb_id


class Handler(BaseHTTPRequestHandler):
    server_version = "NBVideobeskrivelse/1.0"

    # ---- hjelpere ---------------------------------------------------------

    def _cors(self) -> None:
        # Utvidelsen kaller fra chrome-extension://..., så vi svarer alle opphav.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, status: int, data) -> None:
        kropp = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(kropp)))
        self.end_headers()
        self.wfile.write(kropp)

    def _les_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(n))
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def log_message(self, fmt, *args):  # roligere logg
        if self.path.startswith("/jobb/"):
            return
        super().log_message(fmt, *args)

    # ---- ruter --------------------------------------------------------------

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        sti = self.path.split("?", 1)[0].rstrip("/")
        if sti == "":
            kropp = startside().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(kropp)))
            self.end_headers()
            self.wfile.write(kropp)
        elif sti == "/helse":
            self._json(200, {
                "ok": True,
                "versjon": versjon(),
                "ruter": ["/helse", "/jobb", "/jobb/<id>", "/jobber", "/modeller",
                          "/storyboard/<id>"],
                "inferens_url": INNSTILLINGER["url"],
                "modell": INNSTILLINGER["modell"] or bv.STANDARD_MODELL,
                "whisper_url": INNSTILLINGER["whisper_url"],
                "bit_sekunder": bv.STANDARD_BIT_SEKUNDER,
                "ffmpeg": bool(__import__("shutil").which("ffmpeg")),
            })
        elif sti == "/modeller":
            frisk = "frisk" in self.path
            modeller = modelliste(frisk)
            svar = {
                "modeller": modeller,
                "standard": INNSTILLINGER["modell"] or bv.STANDARD_MODELL,
            }
            if not modeller:
                svar["hint"] = (
                    f"Fikk ingen modelliste fra {INNSTILLINGER['url']}. "
                    "Er du på NB-nett, og er NB_INFERENS_TOKEN riktig eller fjernet?"
                )
            self._json(200, svar)
        elif sti.startswith("/storyboard/"):
            jobb_id = sti[len("/storyboard/"):]
            fil = STORYBOARD_MAPPE / f"{jobb_id}.html"
            # Ingen stier utenfor mappa, uansett hva som står i URL-en.
            if "/" in jobb_id or ".." in jobb_id or not fil.is_file():
                self._json(404, {"feil": "fant ikke storyboardet"})
                return
            kropp = fil.read_bytes()
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(kropp)))
            self.end_headers()
            self.wfile.write(kropp)
        elif sti == "/jobber":
            with LAS:
                liste = [{k: j[k] for k in ("id", "kilde", "urn", "tittel", "status",
                                            "startet", "storyboard")}
                         for j in sorted(JOBBER.values(), key=lambda j: -j["startet"])]
            self._json(200, liste)
        elif sti.startswith("/jobb/"):
            jobb_id = sti[len("/jobb/"):]
            with LAS:
                jobb = JOBBER.get(jobb_id)
                svar = json.loads(json.dumps(jobb, ensure_ascii=False)) if jobb else None
            if svar is None:
                self._json(404, {"feil": "ukjent jobb"})
            else:
                self._json(200, svar)
        else:
            self._json(404, {"feil": "ukjent rute"})

    def do_POST(self):
        sti = self.path.split("?", 1)[0].rstrip("/")
        if sti != "/jobb":
            self._json(404, {"feil": "ukjent rute"})
            return
        param = self._les_json()
        kilde = (param.get("kilde") or "").strip()
        if not kilde:
            self._json(400, {"feil": "mangler 'kilde' (URL til strøm eller fil)"})
            return
        if not bv.er_url(kilde) and not Path(kilde).is_file():
            self._json(400, {"feil": f"kilden er verken URL eller eksisterende fil: {kilde}"})
            return
        jobb_id = _ny_jobb(kilde, param)
        self._json(202, {"id": jobb_id})


def main() -> None:
    p = argparse.ArgumentParser(description="Lokal server for NB videobeskrivelse.")
    p.add_argument("--vert", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8170)
    a = p.parse_args()
    try:
        bv.sjekk_verktoy()
    except bv.Feil as e:
        print(f"Advarsel: {e}", file=sys.stderr)
    server = ThreadingHTTPServer((a.vert, a.port), Handler)
    print(f"NB videobeskrivelse lytter på http://{a.vert}:{a.port}  "
          f"(inferens: {INNSTILLINGER['url']}, whisper: {INNSTILLINGER['whisper_url']})",
          file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
