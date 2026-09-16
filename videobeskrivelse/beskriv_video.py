#!/usr/bin/env python3
"""
Beskriv en video med gemma4 (eller en annen multimodal modell) og NB-Whisper.

Verken Ollama, LM Studio eller vLLM tar video som input, så skriptet trekker ut
stillbilder med ffmpeg, sender dem til modellen og ber den beskrive forløpet.
Med --transkriber trekkes lydsporet ut samtidig og sendes til NB-Whisper, og
modellen får bildene og talen fra samme tidsrom i ett og samme kall.

Lyden sendes i biter (standard 20 sekunder). NB-Whisper filtrerer bort musikk,
og på et opptak med gjennomgående musikk kan hele talen forsvinne når fila
vurderes under ett. Korte biter gir tale i en pause i musikken en sjanse til å
slippe gjennom. Sett --bit-sekunder 0 for å sende hele lydsporet i én forespørsel.

Kilden kan være en lokal fil eller en URL ffmpeg kan lese, for eksempel en
HLS-spilleliste (.m3u8) fra NBs Wowza-strømming. Med --referer sendes samme
Referer som nettleseren brukte, i tilfelle strømmen krever det.

Backend velges automatisk: svarer serveren på /api/version brukes Ollamas /api/chat
(med think:false), ellers OpenAI-ruten /v1/chat/completions (LM Studio, vLLM).

Krever: python3 (kun standardbiblioteket), ffmpeg og ffprobe i PATH.

Eksempler:
    python3 beskriv_video.py film.mp4
    python3 beskriv_video.py film.mp4 --antall 12 --transkriber --ut beskrivelse.json
    python3 beskriv_video.py film.mp4 --transkriber --bit-sekunder 10
    python3 beskriv_video.py film.mp4 --transkriber --storyboard film.html
    python3 beskriv_video.py "https://.../playlist.m3u8" --referer https://www.nb.no/items/...
    NB_INFERENS_TOKEN=xxx python3 beskriv_video.py film.mp4 --modell gemma4:e4b  # mindre og raskere
    python3 beskriv_video.py film.mp4 --url http://localhost:11434   # lokal Ollama
    python3 beskriv_video.py film.mp4 --url http://localhost:1234    # LM Studio, første modell
    python3 beskriv_video.py film.mp4 --url http://localhost:1234 --modell qwen/qwen3-vl-8b
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
import storyboard as sb  # noqa: E402

STANDARD_URL = "https://api.inference.nb.no"
STANDARD_MODELL = "gemma4:26b-a4b-it-q8_0"
STANDARD_WHISPER_URL = (
    "https://nb-whisper-large-predictor.inference.nb.no/v1/models/nb-whisper-large:predict"
)

# Hvor mange bilder som sendes i ett kall. Flere bilder gir bedre sammenheng,
# men øker kontekstbruken. 8 bilder er trygt innenfor num_ctx=32768.
BILDER_PER_KALL = 8

# Lengden på hver lydbit til Whisper. 0 = send hele lydsporet i én forespørsel.
STANDARD_BIT_SEKUNDER = 20
# Hvor mange lydbiter som sendes samtidig.
SAMTIDIGE_BITER = 3

SYSTEM_PROMPT = (
    "Du er en assistent som beskriver videoer for et bibliotek. Du får en serie "
    "stillbilder hentet fra en video i kronologisk rekkefølge, hvert merket med "
    "tidspunkt. Noen ganger får du i tillegg en transkripsjon av det som blir sagt "
    "i det samme tidsrommet. Tolk da bildene og talen i sammenheng. "
    "Beskriv hva som skjer i videoen på norsk bokmål: sted, personer, "
    "objekter, handlinger, tekst som er synlig, og hvordan innholdet utvikler seg "
    "over tid. Vær konkret og nøktern. Ikke gjett på ting du ikke kan se eller høre. "
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

Logg = Callable[[str], None]


def _stderr(melding: str) -> None:
    print(melding, file=sys.stderr, flush=True)


class Feil(RuntimeError):
    """Feil som skal vises til brukeren uten traceback."""


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------

def sjekk_verktoy() -> None:
    mangler = [v for v in ("ffmpeg", "ffprobe") if shutil.which(v) is None]
    if mangler:
        raise Feil(f"Fant ikke {', '.join(mangler)} i PATH. Installer ffmpeg først.")


def er_url(kilde: str) -> bool:
    return kilde.startswith(("http://", "https://"))


def _inn_args(kilde: str, referer: str | None, user_agent: str | None) -> list[str]:
    """Argumenter foran -i: HTTP-hoder når kilden er en URL."""
    args: list[str] = []
    if er_url(kilde):
        hoder = []
        if referer:
            hoder.append(f"Referer: {referer}")
        if user_agent:
            hoder.append(f"User-Agent: {user_agent}")
        if hoder:
            args += ["-headers", "\r\n".join(hoder) + "\r\n"]
    return args + ["-i", kilde]


def _kjor(kommando: list[str]) -> subprocess.CompletedProcess:
    r = subprocess.run(kommando, capture_output=True, text=True)
    if r.returncode != 0:
        raise Feil(f"{kommando[0]} feilet: {r.stderr.strip()[-800:]}")
    return r


def varighet_sekunder(kilde: str, referer: str | None = None, user_agent: str | None = None) -> float:
    hoder = _inn_args(kilde, referer, user_agent)[:-2]  # ffprobe tar -headers, men ikke -i
    r = _kjor(["ffprobe", "-v", "error", *hoder, "-show_entries", "format=duration",
               "-of", "default=noprint_wrappers=1:nokey=1", kilde])
    try:
        return float(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise Feil(f"Klarte ikke lese varighet fra ffprobe: {r.stdout!r}")


def trekk_ut_bilder(kilde: str, antall: int, bredde: int, mappe: Path, varighet: float,
                    referer: str | None = None, user_agent: str | None = None,
                    logg: Logg = _stderr) -> list[tuple[float, Path]]:
    """Hent `antall` bilder jevnt fordelt over videoen. Returnerer (sekund, fil)."""
    if varighet <= 0:
        raise Feil("Videoen har ingen varighet.")
    antall = max(1, min(antall, int(varighet) or 1))
    bilder: list[tuple[float, Path]] = []
    for i in range(antall):
        t = varighet * (i + 0.5) / antall
        ut = mappe / f"bilde_{i + 1:03d}.jpg"
        _kjor(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}",
               *_inn_args(kilde, referer, user_agent),
               "-frames:v", "1", "-vf", f"scale={bredde}:-2", "-q:v", "3", str(ut)])
        if ut.exists():
            bilder.append((t, ut))
            logg(f"  bilde {i + 1}/{antall} ved {tidsstempel(t)}")
    if not bilder:
        raise Feil("ffmpeg produserte ingen bilder.")
    return bilder


def trekk_ut_lyd(kilde: str, mappe: Path, referer: str | None = None,
                 user_agent: str | None = None) -> Path:
    """Lydsporet som 16 kHz mono AAC (m4a), lite nok til å sendes base64-kodet."""
    ut = mappe / "lyd.m4a"
    _kjor(["ffmpeg", "-v", "error", "-y", *_inn_args(kilde, referer, user_agent),
           "-vn", "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k", str(ut)])
    if not ut.exists() or ut.stat().st_size == 0:
        raise Feil("ffmpeg produserte ingen lydfil. Har videoen lydspor?")
    return ut


def del_opp_lyd(lydfil: Path, mappe: Path, bit_sekunder: int, varighet: float,
                logg: Logg = _stderr) -> list[tuple[float, Path]]:
    """
    Klipp lydfila i biter på `bit_sekunder`. Returnerer (startsekund, fil).

    Hver bit sendes til Whisper for seg. Det gjør at musikkfilteret vurderer en
    kort snutt om gangen, slik at tale i en pause i musikken kan slippe gjennom
    selv om hele opptaket ellers ville blitt klassifisert som musikk.
    """
    if bit_sekunder <= 0:
        return [(0.0, lydfil)]
    antall = max(1, math.ceil(varighet / bit_sekunder))
    if antall == 1:
        return [(0.0, lydfil)]
    biter: list[tuple[float, Path]] = []
    for i in range(antall):
        start = i * bit_sekunder
        ut = mappe / f"bit_{i + 1:03d}.m4a"
        _kjor(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(bit_sekunder),
               "-i", str(lydfil), "-c", "copy", str(ut)])
        if ut.exists() and ut.stat().st_size > 0:
            biter.append((float(start), ut))
    if not biter:
        raise Feil("Klarte ikke dele opp lydfila.")
    logg(f"  delte lyden i {len(biter)} biter à {bit_sekunder} s")
    return biter


def tidsstempel(sek: float) -> str:
    m, s = divmod(int(sek), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ---------------------------------------------------------------------------
# HTTP
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
        raise Feil(f"HTTP {e.code} fra {url}: {kropp[:500]}")
    except urllib.error.URLError as e:
        raise Feil(f"Fikk ikke kontakt med {url}: {e.reason}")


def _get_json(url: str, token: str | None, timeout: int = 10) -> dict | None:
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as svar:
            return json.loads(svar.read())
    except (urllib.error.URLError, ValueError, OSError):
        return None


# ---------------------------------------------------------------------------
# Språkmodell (Ollama eller OpenAI-rute)
# ---------------------------------------------------------------------------

def finn_backend(url: str, token: str | None) -> str:
    """'ollama' hvis serveren svarer på /api/version, ellers 'openai'."""
    if _get_json(f"{url.rstrip('/')}/api/version", token) is not None:
        return "ollama"
    return "openai"


def tilgjengelige_modeller(url: str, token: str | None) -> list[str]:
    """Modellnavn serveren tilbyr. Prøver Ollamas /api/tags, så OpenAI-ruten."""
    base = url.rstrip("/")
    data = _get_json(f"{base}/api/tags", token)
    if data and data.get("models"):
        return [m["name"] for m in data["models"] if m.get("name")]
    data = _get_json(f"{base}/v1/models", token)
    if data and data.get("data"):
        return [m["id"] for m in data["data"] if m.get("id")]
    return []


def _berik_modellfeil(e: Feil, url: str, token: str | None, modell: str) -> Feil:
    """Gjør «model not found» om til en feil som viser hvilke navn som finnes."""
    if "not found" not in str(e).lower():
        return e
    navn = tilgjengelige_modeller(url, token)
    if not navn:
        return e
    return Feil(
        f"Modellen '{modell}' finnes ikke på {url.rstrip('/')}. "
        f"Tilgjengelige modeller: {', '.join(navn)}. "
        "Velg en med --modell, eller sett NB_INFERENS_MODELL før du starter server.py."
    )


def forste_modell(url: str, token: str | None) -> str:
    data = _get_json(f"{url.rstrip('/')}/v1/models", token)
    modeller = [m.get("id") for m in (data or {}).get("data", []) if m.get("id")]
    if not modeller:
        raise Feil(f"Fant ingen modeller på {url}/v1/models. Oppgi --modell.")
    return modeller[0]


def base64_fil(sti: Path) -> str:
    return base64.b64encode(sti.read_bytes()).decode()


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
        try:
            data = _post_json(f"{base}/api/chat", token, payload, timeout)
        except Feil as e:
            raise _berik_modellfeil(e, base, token, modell) from None
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
    try:
        data = _post_json(f"{base}/v1/chat/completions", token, payload, timeout)
    except Feil as e:
        raise _berik_modellfeil(e, base, token, modell) from None
    valg = data.get("choices") or [{}]
    return (valg[0].get("message") or {}).get("content", "").strip()


def beskriv_gruppe(backend, url, token, modell, gruppe, del_nr, antall_deler, varighet, timeout,
                   tale: str | None = None) -> str:
    """Beskriv én gruppe bilder, eventuelt sammen med talen fra samme tidsrom."""
    tider = ", ".join(tidsstempel(t) for t, _ in gruppe)
    if antall_deler > 1:
        innledning = (f"Dette er del {del_nr} av {antall_deler} av en video som varer "
                      f"{tidsstempel(varighet)}. ")
    else:
        innledning = f"Videoen varer {tidsstempel(varighet)}. "
    tekst = innledning + f"Bildene er tatt ved {tider}. "
    if tale:
        tekst += (
            "Under følger det som blir sagt i det samme tidsrommet, automatisk "
            "transkribert. Transkripsjonen kan ha feil i navn og enkeltord, og "
            "musikk er filtrert bort, så den kan være ufullstendig. Se bildene og "
            "talen i sammenheng: bruk talen til å forstå hva som skjer og hva "
            "videoen handler om, men ikke gjengi den ordrett.\n\n"
            f"TALE I DETTE TIDSROMMET:\n{tale}\n\n"
        )
        tekst += "Beskriv hva som skjer i denne delen av videoen, ut fra bildene og talen."
    else:
        tekst += "Beskriv hva som skjer i denne delen av videoen."
    return chat(backend, url, token, modell, SYSTEM_PROMPT, tekst, [f for _, f in gruppe], timeout)


def oppsummer(backend, url, token, modell, delbeskrivelser: list[str], varighet, timeout) -> str:
    deler = "\n\n".join(f"Del {i + 1}:\n{d}" for i, d in enumerate(delbeskrivelser))
    tekst = (f"Under følger beskrivelser av {len(delbeskrivelser)} påfølgende deler av en "
             f"video som varer {tidsstempel(varighet)}. Skriv én sammenhengende beskrivelse "
             "av hele videoen på norsk bokmål, i kronologisk rekkefølge, uten å gjenta deg. "
             "Start med en setning som oppsummerer hva videoen handler om.\n\n" + deler)
    return chat(backend, url, token, modell, SYSTEM_PROMPT, tekst, [], timeout)


def flett_inn_transkripsjon(backend, url, token, modell, visuell: str, transkripsjon: str,
                            varighet, timeout) -> str:
    """Kombiner en ferdig bildebeskrivelse med talen. Brukes når --ikke-samtolk er satt."""
    tekst = (f"En video som varer {tidsstempel(varighet)} er beskrevet på to måter. "
             "Først en beskrivelse av det som er synlig, laget ut fra stillbilder. "
             "Deretter en transkripsjon av det som blir sagt, laget av en talegjenkjenner "
             "som kan ha feil i navn og enkeltord. Skriv én samlet beskrivelse av videoen "
             "på norsk bokmål: hva den handler om, hva som vises, og hva som sies. "
             "Bruk transkripsjonen til å forstå tema, personer og hendelser, men ikke "
             "gjengi den ordrett. Vær nøktern og ikke dikt opp noe.\n\n"
             f"BILDEBESKRIVELSE:\n{visuell}\n\nTRANSKRIPSJON:\n{transkripsjon}")
    return chat(backend, url, token, modell, SYSTEM_PROMPT, tekst, [], timeout)


# ---------------------------------------------------------------------------
# NB-Whisper
# ---------------------------------------------------------------------------

def transkriber_fil(lydfil: Path, whisper_url: str = STANDARD_WHISPER_URL, sprak: str = "no",
                    timeout: int = 900) -> dict:
    """Send én lydfil til NB-Whisper. Returnerer {"tekst": str, "segmenter": [...]}."""
    payload = {"file": base64_fil(lydfil), "language": sprak, "response_format": "json"}
    data = _post_json(whisper_url, None, payload, timeout)
    segmenter = []
    for seg in data.get("content") or []:
        if not isinstance(seg, dict):
            continue
        segmenter.append({
            "start": seg.get("start"),
            "slutt": seg.get("end"),
            "taler": seg.get("speaker"),
            "tekst": (seg.get("text") or "").strip(),
        })
    tekst = (data.get("text") or "").strip()
    if not tekst and segmenter:
        tekst = " ".join(s["tekst"] for s in segmenter if s["tekst"])
    return {"tekst": tekst, "segmenter": segmenter}


def transkriber(biter: list[tuple[float, Path]], whisper_url: str = STANDARD_WHISPER_URL,
                sprak: str = "no", timeout: int = 900, logg: Logg = _stderr) -> dict:
    """
    Transkriber alle lydbitene og sett dem sammen til én transkripsjon med
    tidspunkt regnet fra starten av videoen.

    Merk: talernavn (SPEAKER_00 og så videre) tildeles per forespørsel, så samme
    navn i to ulike biter er ikke nødvendigvis samme person. Hvert segment har
    derfor også feltet `bit`.
    """
    resultater: list[dict | None] = [None] * len(biter)

    def jobb(i: int) -> None:
        start, fil = biter[i]
        r = transkriber_fil(fil, whisper_url, sprak, timeout)
        segmenter = []
        for s in r["segmenter"]:
            if s["start"] is not None:
                s["start"] = float(s["start"]) + start
            if s["slutt"] is not None:
                s["slutt"] = float(s["slutt"]) + start
            s["bit"] = i + 1
            segmenter.append(s)
        # Noen svar har bare `text` uten segmenter. Lag da ett segment for hele biten.
        if not segmenter and r["tekst"]:
            segmenter = [{"start": start, "slutt": None, "taler": None,
                          "tekst": r["tekst"], "bit": i + 1}]
        resultater[i] = {"tekst": r["tekst"], "segmenter": segmenter}

    if len(biter) == 1:
        jobb(0)
    else:
        ferdige = [0]
        las = threading.Lock()

        def med_logg(i: int) -> None:
            jobb(i)
            with las:
                ferdige[0] += 1
                logg(f"  bit {ferdige[0]}/{len(biter)} transkribert")

        with ThreadPoolExecutor(max_workers=min(SAMTIDIGE_BITER, len(biter))) as pool:
            list(pool.map(med_logg, range(len(biter))))

    segmenter = [s for r in resultater if r for s in r["segmenter"] if s["tekst"]]
    segmenter.sort(key=lambda s: (s["start"] if s["start"] is not None else 0.0))
    return {
        "tekst": " ".join(s["tekst"] for s in segmenter),
        "segmenter": segmenter,
        "antall_biter": len(biter),
    }


def transkripsjon_som_tekst(t: dict, fra: float | None = None, til: float | None = None) -> str:
    """
    Transkripsjon med taler og tidspunkt per segment, egnet som prompt-innhold.
    Med `fra` og `til` tas bare segmenter som overlapper det tidsrommet med.
    """
    linjer = []
    for s in t.get("segmenter") or []:
        if not s.get("tekst"):
            continue
        start = s.get("start")
        slutt = s.get("slutt") if s.get("slutt") is not None else start
        if fra is not None and start is not None and slutt is not None:
            if slutt < fra or start > til:
                continue
        prefiks = ""
        if start is not None:
            prefiks += f"[{tidsstempel(float(start))}] "
        if s.get("taler"):
            prefiks += f"{s['taler']}: "
        linjer.append(prefiks + s["tekst"])
    return "\n".join(linjer) if linjer else (t.get("tekst", "") if fra is None else "")


# ---------------------------------------------------------------------------
# Hovedløp, brukt av både kommandolinja og server.py
# ---------------------------------------------------------------------------

def grupper(liste, storrelse):
    return [liste[i:i + storrelse] for i in range(0, len(liste), storrelse)]


def analyser(kilde: str, *, antall: int = 8, bredde: int = 768, url: str = STANDARD_URL,
             token: str | None = None, modell: str | None = None, backend: str = "auto",
             per_kall: int = BILDER_PER_KALL, timeout: int = 600, transkriber_lyd: bool = False,
             whisper_url: str = STANDARD_WHISPER_URL, sprak: str = "no",
             bit_sekunder: int = STANDARD_BIT_SEKUNDER, samtolk: bool = True,
             referer: str | None = None, user_agent: str | None = None,
             mappe: Path | None = None, storyboard: Path | None = None,
             tittel: str | None = None, logg: Logg = _stderr) -> dict:
    """
    Kjør hele løpet: lyd → Whisper i biter, bilder → modell, og til slutt én
    beskrivelse. Med samtolk=True får modellen bildene og talen fra samme
    tidsrom i ett kall. Returnerer et resultat-dict (samme som --ut).
    """
    if not er_url(kilde) and not Path(kilde).is_file():
        raise Feil(f"Fant ikke videofila {kilde}")
    sjekk_verktoy()

    backend = finn_backend(url, token) if backend == "auto" else backend
    if modell is None:
        modell = STANDARD_MODELL if backend == "ollama" else forste_modell(url, token)
    logg(f"Backend: {backend} på {url}, modell {modell}")

    tmp = None
    if mappe is None:
        tmp = tempfile.TemporaryDirectory(prefix="videobeskrivelse_")
        mappe = Path(tmp.name)
    mappe.mkdir(parents=True, exist_ok=True)

    resultat: dict = {"kilde": kilde, "modell": modell, "backend": backend, "url": url,
                      "samtolk": bool(transkriber_lyd and samtolk)}
    if tittel:
        resultat["tittel"] = tittel
    try:
        varighet = varighet_sekunder(kilde, referer, user_agent)
        resultat["varighet_sekunder"] = round(varighet, 3)
        logg(f"Varighet {tidsstempel(varighet)}")

        # Lyd → Whisper i egen tråd, samtidig med at bildene hentes.
        lydresultat: dict = {}

        def lydjobb():
            try:
                logg("Trekker ut lydspor ...")
                lydfil = trekk_ut_lyd(kilde, mappe, referer, user_agent)
                biter = del_opp_lyd(lydfil, mappe, bit_sekunder, varighet, logg)
                kb = sum(f.stat().st_size for _, f in biter) // 1024
                logg(f"Sender {kb} kB lyd til NB-Whisper i {len(biter)} "
                     f"{'bit' if len(biter) == 1 else 'biter'} ...")
                lydresultat["transkripsjon"] = transkriber(biter, whisper_url, sprak, timeout, logg)
                logg("Transkripsjon klar")
            except Exception as e:  # noqa: BLE001 - feilen rapporteres i resultatet
                lydresultat["feil"] = str(e)
                logg(f"Transkripsjon feilet: {e}")

        trad = None
        if transkriber_lyd:
            trad = threading.Thread(target=lydjobb, daemon=True)
            trad.start()

        logg(f"Trekker ut {antall} bilder ...")
        bilder = trekk_ut_bilder(kilde, antall, bredde, mappe, varighet, referer, user_agent, logg)
        resultat["bilder"] = [{"sekund": round(t, 3), "tid": tidsstempel(t), "fil": f.name}
                              for t, f in bilder]

        # Skal bilder og tale tolkes sammen, trengs transkripsjonen før modellkallene.
        transkripsjon = None
        if trad is not None and samtolk:
            if trad.is_alive():
                logg("Venter på transkripsjonen før bildene tolkes ...")
            trad.join()
            if "transkripsjon" in lydresultat:
                transkripsjon = lydresultat["transkripsjon"]
                resultat["transkripsjon"] = transkripsjon
                if not transkripsjon.get("segmenter"):
                    logg("Transkripsjonen var tom, beskriver bare bildene")
                    transkripsjon = None
            else:
                resultat["transkripsjon_feil"] = lydresultat.get("feil", "ukjent feil")

        steg = varighet / len(bilder)
        deler = grupper(bilder, max(1, per_kall))
        delbeskrivelser = []
        for i, gruppe in enumerate(deler, 1):
            fra = 0.0 if i == 1 else (i - 1) * max(1, per_kall) * steg
            til = float("inf") if i == len(deler) else fra + len(gruppe) * steg
            tale = transkripsjon_som_tekst(transkripsjon, fra, til) if transkripsjon else None
            if tale:
                logg(f"Beskriver del {i}/{len(deler)} med {modell}, bilder og tale ...")
            else:
                logg(f"Beskriver del {i}/{len(deler)} med {modell} ...")
            delbeskrivelser.append(
                beskriv_gruppe(backend, url, token, modell, gruppe, i, len(deler), varighet,
                               timeout, tale)
            )
        resultat["delbeskrivelser"] = delbeskrivelser

        if len(delbeskrivelser) == 1:
            beskrivelse = delbeskrivelser[0]
        else:
            logg("Oppsummerer delene ...")
            beskrivelse = oppsummer(backend, url, token, modell, delbeskrivelser, varighet, timeout)

        # Uten samtolk kjøres transkripsjonen inn til slutt, som før.
        if trad is not None and not samtolk:
            resultat["bildebeskrivelse"] = beskrivelse
            if trad.is_alive():
                logg("Venter på transkripsjonen ...")
            trad.join()
            if "transkripsjon" in lydresultat:
                t = lydresultat["transkripsjon"]
                resultat["transkripsjon"] = t
                if t.get("tekst"):
                    logg("Fletter inn transkripsjonen ...")
                    beskrivelse = flett_inn_transkripsjon(
                        backend, url, token, modell, beskrivelse, transkripsjon_som_tekst(t),
                        varighet, timeout)
                else:
                    logg("Transkripsjonen var tom, bruker bare bildebeskrivelsen")
            else:
                resultat["transkripsjon_feil"] = lydresultat.get("feil", "ukjent feil")

        resultat["beskrivelse"] = beskrivelse

        # Storyboardet må lages mens bildefilene fortsatt finnes.
        if storyboard is not None:
            sti = sb.skriv(storyboard, resultat, bilder, tittel=tittel, per_kall=max(1, per_kall))
            resultat["storyboard"] = str(sti)
            logg(f"Skrev storyboard: {sti}")

        return resultat
    finally:
        if tmp:
            tmp.cleanup()


def main() -> None:
    p = argparse.ArgumentParser(description="Beskriv en video med gemma4 via NBs inferensserver.")
    p.add_argument("video", nargs="?",
                   help="videofil (mp4, mov, mkv, ...) eller URL, f.eks. en .m3u8-spilleliste")
    p.add_argument("--modeller", action="store_true",
                   help="list modellene serveren tilbyr, og avslutt")
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
    p.add_argument("--transkriber", action="store_true",
                   help="send lydsporet til NB-Whisper og tolk talen sammen med bildene")
    p.add_argument("--bit-sekunder", type=int, default=STANDARD_BIT_SEKUNDER,
                   help=f"lengden på hver lydbit til Whisper, 0 = hele sporet i ett (standard {STANDARD_BIT_SEKUNDER})")
    p.add_argument("--ikke-samtolk", action="store_true",
                   help="beskriv bildene først og flett inn talen til slutt, i stedet for å tolke dem sammen")
    p.add_argument("--whisper-url", default=os.environ.get("NB_WHISPER_URL", STANDARD_WHISPER_URL),
                   help="NB-Whisper-endepunkt (standard fra NB_WHISPER_URL)")
    p.add_argument("--sprak", default="no", help="språk for Whisper (standard no)")
    p.add_argument("--referer", help="Referer-hode ffmpeg sender når kilden er en URL")
    p.add_argument("--user-agent", help="User-Agent-hode ffmpeg sender når kilden er en URL")
    p.add_argument("--ut", type=Path, help="skriv resultat som JSON til denne fila")
    p.add_argument("--storyboard", type=Path, metavar="FIL.html",
                   help="lag et storyboard: én HTML-fil med bilder, beskrivelser og replikker")
    p.add_argument("--tittel", help="tittel på storyboardet")
    p.add_argument("--behold-bilder", type=Path, metavar="MAPPE",
                   help="lagre stillbildene (og lydfila) i denne mappa i stedet for å slette dem")
    a = p.parse_args()

    if a.modeller:
        navn = tilgjengelige_modeller(a.url, a.token)
        if not navn:
            sys.exit(f"Fikk ingen modelliste fra {a.url}. Er adressen riktig, og er du på NB-nett?")
        print(f"Modeller på {a.url.rstrip('/')}:")
        for n in navn:
            print(f"  {n}")
        return
    if not a.video:
        p.error("oppgi en videofil eller URL (eller bruk --modeller)")

    try:
        resultat = analyser(
            a.video, antall=a.antall, bredde=a.bredde, url=a.url, token=a.token,
            modell=a.modell, backend=a.backend, per_kall=a.per_kall, timeout=a.timeout,
            transkriber_lyd=a.transkriber, whisper_url=a.whisper_url, sprak=a.sprak,
            bit_sekunder=a.bit_sekunder, samtolk=not a.ikke_samtolk,
            referer=a.referer, user_agent=a.user_agent, mappe=a.behold_bilder,
            storyboard=a.storyboard, tittel=a.tittel,
        )
    except Feil as e:
        sys.exit(str(e))

    print(resultat["beskrivelse"])
    if a.ut:
        a.ut.write_text(json.dumps(resultat, ensure_ascii=False, indent=2), encoding="utf-8")
        _stderr(f"Skrev {a.ut}")


if __name__ == "__main__":
    main()
