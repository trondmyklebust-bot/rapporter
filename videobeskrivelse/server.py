#!/usr/bin/env python3
"""
Lokal server for Chrome-utvidelsen «NB videobeskrivelse».

Utvidelsen sender adressen til videostrømmen (HLS/.m3u8 fra Wowza, eller en
mp4-URL) hit. Serveren kjører beskriv_video.analyser(): ffmpeg henter
stillbilder og lydspor fra strømmen, bildene går til gemma4 på NB-inferens og
lyden til NB-Whisper parallelt, og en sluttbeskrivelse flettes sammen.

Kun standardbiblioteket. Start med:
    python3 server.py                 # lytter på http://127.0.0.1:8765
    NB_INFERENS_TOKEN=xxx python3 server.py --port 8765

Ruter:
    GET  /helse            → {"ok": true, ...}
    POST /jobb             → {"id": "..."}   body: {"kilde": url, "referer": ..., "antall": 8, ...}
    GET  /jobb/<id>        → {"status": "kjører"|"ferdig"|"feil", "logg": [...], "resultat": {...}}
    GET  /jobber           → liste over jobber
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beskriv_video as bv  # noqa: E402

JOBBER: dict[str, dict] = {}
LAS = threading.Lock()
MAKS_JOBBER = 50

INNSTILLINGER = {
    "url": os.environ.get("NB_INFERENS_URL", bv.STANDARD_URL),
    "token": os.environ.get("NB_INFERENS_TOKEN"),
    "modell": os.environ.get("NB_INFERENS_MODELL"),  # None → gemma4:e4b på Ollama
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
                referer=param.get("referer"),
                user_agent=param.get("user_agent"),
                logg=logg,
            )
            with LAS:
                jobb["resultat"] = resultat
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
        if sti in ("", "/helse"):
            self._json(200, {
                "ok": True,
                "inferens_url": INNSTILLINGER["url"],
                "modell": INNSTILLINGER["modell"] or bv.STANDARD_MODELL,
                "whisper_url": INNSTILLINGER["whisper_url"],
                "ffmpeg": bool(__import__("shutil").which("ffmpeg")),
            })
        elif sti == "/jobber":
            with LAS:
                liste = [{k: j[k] for k in ("id", "kilde", "urn", "tittel", "status", "startet")}
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
    p.add_argument("--port", type=int, default=8765)
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
