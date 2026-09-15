#!/usr/bin/env python3
"""
Beskriv en video med gemma4 på NBs inferensserver.

Verken Ollama, LM Studio eller vLLM tar video som input, så skriptet trekker ut
stillbilder med ffmpeg, sender dem til modellen og ber den beskrive forløpet.

Backend velges automatisk: svarer serveren på /api/version brukes Ollamas /api/chat
(med think:false), ellers OpenAI-ruten /v1/chat/completions (LM Studio, vLLM).

Krever: python3 (kun standardbiblioteket), ffmpeg og ffprobe i PATH.

Eksempler:
    python3 beskriv_video.py film.mp4
    python3 beskriv_video.py film.mp4 --antall 12 --ut beskrivelse.json
    NB_INFERENS_TOKEN=xxx python3 beskriv_video.py film.mp4 --modell gemma4:e4b
    python3 beskriv_video.py film.mp4 --url http://localhost:11434   # lokal Ollama
    python3 beskriv_video.py film.mp4 --url http://localhost:1234    # LM Studio, første modell
    python3 beskriv_video.py film.mp4 --url http://localhost:1234 --modell qwen/qwen3-vl-8b
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

STANDARD_URL = "https://api.inference.nb.no"
STANDARD_MODELL = "gemma4:e4b"

# Hvor mange bilder som sendes i ett kall. Flere bilder gir bedre sammenheng,
# men øker kontekstbruken. 8 bilder er trygt innenfor num_ctx=32768.
BILDER_PER_KALL = 8

SYSTEM_PROMPT = (
    "Du er en assistent som beskriver videoer for et bibliotek. Du får en serie "
    "stillbilder hentet fra en video i kronologisk rekkefølge, hvert merket med "
    "tidspunkt. Beskriv hva som skjer i videoen på norsk bokmål: sted, personer, "
    "objekter, handlinger, tekst som er synlig, og hvordan innholdet utvikler seg "
    "over tid. Vær konkret og nøktern. Ikke gjett på ting du ikke kan se. "
    "Skriv sammenhengende prosa, ikke en liste per bilde."
)

OPTIONS = {
    "temperature": 0.3,
    "top_p": 0.95,
    "top_k": 64,
    "repeat_penalty": 1.1,
    "num_ctx": 32768,
    "num_predict": 1024,
}


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def sjekk_verktoy() -> None:
    mangler = [v for v in ("ffmpeg", "ffprobe") if shutil.which(v) is None]
    if mangler:
        sys.exit(f"Fant ikke {', '.join(mangler)} i PATH. Installer ffmpeg først.")


def varighet_sekunder(video: Path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
        capture_output=True, text=True, check=True,
    )
    try:
        return float(r.stdout.strip())
    except ValueError:
        sys.exit(f"Klarte ikke lese varighet fra ffprobe: {r.stdout!r}")


def trekk_ut_bilder(video: Path, antall: int, bredde: int, mappe: Path) -> list[tuple[float, Path]]:
    """Hent `antall` bilder jevnt fordelt over videoen. Returnerer (sekund, fil)."""
    varighet = varighet_sekunder(video)
    if varighet <= 0:
        sys.exit("Videoen har ingen varighet.")
    antall = max(1, min(antall, int(varighet) or 1))
    bilder: list[tuple[float, Path]] = []
    for i in range(antall):
        t = varighet * (i + 0.5) / antall
        ut = mappe / f"bilde_{i + 1:03d}.jpg"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={bredde}:-2", "-q:v", "3", str(ut)],
            check=True,
        )
        if ut.exists():
            bilder.append((t, ut))
    if not bilder:
        sys.exit("ffmpeg produserte ingen bilder.")
    return bilder


def tidsstempel(sek: float) -> str:
    m, s = divmod(int(sek), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

def _post_json(url: str, token: str | None, payload: dict, timeout: int) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as svar:
            return json.loads(svar.read())
    except urllib.error.HTTPError as e:
        kropp = e.read().decode(errors="replace")
        sys.exit(f"HTTP {e.code} fra {url}: {kropp[:500]}")
    except urllib.error.URLError as e:
        sys.exit(f"Fikk ikke kontakt med {url}: {e.reason}")


def _get_json(url: str, token: str | None, timeout: int = 10) -> dict | None:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as svar:
            return json.loads(svar.read())
    except (urllib.error.URLError, ValueError, OSError):
        return None


def finn_backend(url: str, token: str | None) -> str:
    """'ollama' hvis serveren svarer på /api/version, ellers 'openai'."""
    if _get_json(f"{url.rstrip('/')}/api/version", token) is not None:
        return "ollama"
    return "openai"


def forste_modell(url: str, token: str | None) -> str:
    data = _get_json(f"{url.rstrip('/')}/v1/models", token)
    modeller = [m.get("id") for m in (data or {}).get("data", []) if m.get("id")]
    if not modeller:
        sys.exit(f"Fant ingen modeller på {url}/v1/models. Oppgi --modell.")
    return modeller[0]


def chat(backend: str, url: str, token: str | None, modell: str,
         system: str, tekst: str, bilder: list[Path], timeout: int) -> str:
    """Send ett kall med tekst og eventuelle bilder, returner svaret som tekst."""
    base = url.rstrip("/")
    if backend == "ollama":
        bruker = {"role": "user", "content": tekst}
        if bilder:
            bruker["images"] = [base64_fil(f) for f in bilder]
        payload = {
            "model": modell,
            "messages": [{"role": "system", "content": system}, bruker],
            "stream": False,
            "think": False,
            "options": OPTIONS,
        }
        data = _post_json(f"{base}/api/chat", token, payload, timeout)
        return data.get("message", {}).get("content", "").strip()

    # OpenAI-kompatibel rute (LM Studio, vLLM): bilder som data-URI i content-deler.
    deler: list[dict] = [{"type": "text", "text": tekst}]
    for f in bilder:
        deler.append({"type": "image_url",
                      "image_url": {"url": f"data:image/jpeg;base64,{base64_fil(f)}"}})
    payload = {
        "model": modell,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": deler}],
        "stream": False,
        "temperature": OPTIONS["temperature"],
        "top_p": OPTIONS["top_p"],
        "max_tokens": OPTIONS["num_predict"],
    }
    data = _post_json(f"{base}/v1/chat/completions", token, payload, timeout)
    valg = data.get("choices") or [{}]
    return (valg[0].get("message") or {}).get("content", "").strip()


def base64_fil(sti: Path) -> str:
    return base64.b64encode(sti.read_bytes()).decode()


def beskriv_gruppe(backend, url, token, modell, gruppe, del_nr, antall_deler, varighet, timeout) -> str:
    tider = ", ".join(tidsstempel(t) for t, _ in gruppe)
    if antall_deler > 1:
        innledning = (f"Dette er del {del_nr} av {antall_deler} av en video som varer "
                      f"{tidsstempel(varighet)}. ")
    else:
        innledning = f"Videoen varer {tidsstempel(varighet)}. "
    tekst = (innledning + f"Bildene er tatt ved {tider}. "
             "Beskriv hva som skjer i denne delen av videoen.")
    return chat(backend, url, token, modell, SYSTEM_PROMPT, tekst, [f for _, f in gruppe], timeout)


def oppsummer(backend, url, token, modell, delbeskrivelser: list[str], varighet, timeout) -> str:
    deler = "\n\n".join(f"Del {i + 1}:\n{d}" for i, d in enumerate(delbeskrivelser))
    tekst = (f"Under følger beskrivelser av {len(delbeskrivelser)} påfølgende deler av en "
             f"video som varer {tidsstempel(varighet)}. Skriv én sammenhengende beskrivelse "
             "av hele videoen på norsk bokmål, i kronologisk rekkefølge, uten å gjenta deg. "
             "Start med en setning som oppsummerer hva videoen handler om.\n\n" + deler)
    return chat(backend, url, token, modell, SYSTEM_PROMPT, tekst, [], timeout)


# ---------------------------------------------------------------------------
# Hovedløp
# ---------------------------------------------------------------------------

def grupper(liste, storrelse):
    return [liste[i:i + storrelse] for i in range(0, len(liste), storrelse)]


def main() -> None:
    p = argparse.ArgumentParser(description="Beskriv en video med gemma4 via NBs inferensserver.")
    p.add_argument("video", type=Path, help="videofil (mp4, mov, mkv, ...)")
    p.add_argument("--antall", type=int, default=8,
                   help="antall stillbilder som trekkes ut, jevnt fordelt (standard 8)")
    p.add_argument("--bredde", type=int, default=768,
                   help="bredde bildene skaleres til i piksler (standard 768)")
    p.add_argument("--url", default=os.environ.get("NB_INFERENS_URL", STANDARD_URL),
                   help=f"Ollama-base-URL (standard {STANDARD_URL}, eller NB_INFERENS_URL)")
    p.add_argument("--token", default=os.environ.get("NB_INFERENS_TOKEN"),
                   help="Bearer-token (standard fra NB_INFERENS_TOKEN)")
    p.add_argument("--modell", default=None,
                   help=f"modellnavn (standard {STANDARD_MODELL} på Ollama, første modell i lista på LM Studio/vLLM)")
    p.add_argument("--backend", choices=["auto", "ollama", "openai"], default="auto",
                   help="auto (standard), ollama (/api/chat) eller openai (/v1/chat/completions for LM Studio/vLLM)")
    p.add_argument("--per-kall", type=int, default=BILDER_PER_KALL,
                   help=f"bilder per modellkall før oppsummering (standard {BILDER_PER_KALL})")
    p.add_argument("--timeout", type=int, default=600, help="sekunder per modellkall (standard 600)")
    p.add_argument("--ut", type=Path, help="skriv resultat som JSON til denne fila")
    p.add_argument("--behold-bilder", type=Path, metavar="MAPPE",
                   help="lagre stillbildene i denne mappa i stedet for å slette dem")
    a = p.parse_args()

    if not a.video.is_file():
        sys.exit(f"Fant ikke videofila {a.video}")
    sjekk_verktoy()

    backend = finn_backend(a.url, a.token) if a.backend == "auto" else a.backend
    if a.modell is None:
        a.modell = STANDARD_MODELL if backend == "ollama" else forste_modell(a.url, a.token)
    print(f"Backend: {backend} på {a.url}, modell {a.modell}", file=sys.stderr)

    if a.behold_bilder:
        a.behold_bilder.mkdir(parents=True, exist_ok=True)
        mappe = a.behold_bilder
        tmp = None
    else:
        tmp = tempfile.TemporaryDirectory(prefix="videobeskrivelse_")
        mappe = Path(tmp.name)

    try:
        print(f"Trekker ut {a.antall} bilder fra {a.video.name} ...", file=sys.stderr)
        varighet = varighet_sekunder(a.video)
        bilder = trekk_ut_bilder(a.video, a.antall, a.bredde, mappe)
        print(f"  {len(bilder)} bilder klare, varighet {tidsstempel(varighet)}", file=sys.stderr)

        deler = grupper(bilder, max(1, a.per_kall))
        delbeskrivelser = []
        for i, gruppe in enumerate(deler, 1):
            print(f"Beskriver del {i}/{len(deler)} med {a.modell} ...", file=sys.stderr)
            delbeskrivelser.append(
                beskriv_gruppe(backend, a.url, a.token, a.modell, gruppe, i, len(deler), varighet, a.timeout)
            )

        if len(delbeskrivelser) == 1:
            beskrivelse = delbeskrivelser[0]
        else:
            print("Oppsummerer delene ...", file=sys.stderr)
            beskrivelse = oppsummer(backend, a.url, a.token, a.modell, delbeskrivelser, varighet, a.timeout)

        print(beskrivelse)

        if a.ut:
            resultat = {
                "video": str(a.video),
                "varighet_sekunder": round(varighet, 3),
                "modell": a.modell,
                "backend": backend,
                "url": a.url,
                "bilder": [{"sekund": round(t, 3), "tid": tidsstempel(t), "fil": f.name}
                           for t, f in bilder],
                "delbeskrivelser": delbeskrivelser,
                "beskrivelse": beskrivelse,
            }
            a.ut.write_text(json.dumps(resultat, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Skrev {a.ut}", file=sys.stderr)
    finally:
        if tmp:
            tmp.cleanup()


if __name__ == "__main__":
    main()
