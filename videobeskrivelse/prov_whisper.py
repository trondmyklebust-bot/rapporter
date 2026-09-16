#!/usr/bin/env python3
"""
Finn ut hva som gir mest tale ut av NB-Whisper.

NB-Whisper fjerner musikk automatisk, og på opptak med musikk under talen
forsvinner ofte hele transkripsjonen. Filteret kan slås av med
`music_classifier: false`, og beskriv_video.py gjør det som standard. Dette
skriptet sammenligner det med andre grep, og måler hvor mange ord hver
variant gir:

  1. Lydbehandling  — rå lyd mot filtre som løfter fram taleområdet.
  2. Bitlengde      — hele sporet mot korte biter.
  3. Parametre      — udokumenterte felt i forespørselen, for eksempel
                      filter_music eller vad, for å se om noe biter.
  4. Endepunkt      — andre modellnavn på samme vert, for eksempel en
                      verbatim-variant.

Kjør det på et opptak der du vet at det blir sagt noe, men der du får lite
eller ingenting ut i dag.

    python3 prov_whisper.py film.mp4
    python3 prov_whisper.py "https://wow.nb.no/.../playlist.m3u8" \
        --referer https://www.nb.no/items/URN:NBN:no-nb_video_9314
    python3 prov_whisper.py film.mp4 --fra 0 --til 35 --hopp-endepunkt
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import beskriv_video as bv  # noqa: E402

# Felt å prøve i forespørselen, ett om gangen. De to første virker: tjenesten
# gjentar dem i metadata.parameters, og music_classifier=False slipper gjennom
# tale med musikk under (målt 16.09.2026). Resten er gjetninger som kan
# avvises eller ignoreres.
PARAMETRE: list[dict] = [
    {"music_classifier": False},
    {"speech_bias": 3},
    {"filter_music": False},
    {"music_filter": False},
    {"remove_music": False},
    {"skip_music": False},
    {"vad": False},
    {"vad_filter": False},
    {"no_speech_threshold": 1.0},
    {"condition_on_previous_text": False},
    {"temperature": 0.0},
    {"task": "transcribe"},
    {"verbatim": True},
    {"diarize": False},
    {"chunk_length_s": 10},
    {"return_timestamps": True},
]

# Andre modellnavn på samme vert. KServe serverer modeller etter navn i stien.
MODELLNAVN = [
    "nb-whisper-large",
    "nb-whisper-large-verbatim",
    "nb-whisper-large-semantic",
    "nb-whisper-medium",
]


def send(url: str, lydfil: Path, sprak: str, timeout: int, ekstra: dict | None = None):
    """Returner (status, tekst, antall_segmenter). Status er 'ok' eller en feil."""
    payload = {"file": bv.base64_fil(lydfil), "language": sprak, "response_format": "json"}
    if ekstra:
        payload.update(ekstra)
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as svar:
            data = json.loads(svar.read())
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}", "", 0
    except urllib.error.URLError as e:
        return f"ingen kontakt ({e.reason})", "", 0
    except ValueError:
        return "ugyldig JSON", "", 0

    segmenter = [s for s in (data.get("content") or [])
                 if isinstance(s, dict) and (s.get("text") or "").strip()]
    tekst = (data.get("text") or "").strip()
    if not tekst and segmenter:
        tekst = " ".join((s.get("text") or "").strip() for s in segmenter)
    return "ok", tekst, len(segmenter)


def ord(tekst: str) -> int:
    return len(tekst.split())


def rad(navn: str, status: str, tekst: str, segmenter: int) -> None:
    n = ord(tekst)
    smak = tekst[:58].replace("\n", " ")
    merke = "  " if status != "ok" else ("✓ " if n else "· ")
    print(f"{merke}{navn:<26} {status:<22} {n:>4} ord  {segmenter:>2} seg  {smak}")


def lag_variant(lydfil: Path, mappe: Path, navn: str, kjede: str | None) -> Path | None:
    if not kjede:
        return lydfil
    ut = mappe / f"lyd_{navn}.m4a"
    try:
        bv._kjor(["ffmpeg", "-v", "error", "-y", "-i", str(lydfil), "-af", kjede,
                  "-ac", "1", "-ar", "16000", "-c:a", "aac", "-b:a", "48k", str(ut)])
    except bv.Feil as e:
        print(f"  (klarte ikke lage varianten {navn}: {e})", file=sys.stderr)
        return None
    return ut if ut.exists() and ut.stat().st_size else None


def main() -> None:
    p = argparse.ArgumentParser(description="Prøv ut hva som gir mest tale fra NB-Whisper.")
    p.add_argument("kilde", help="videofil, lydfil eller URL")
    p.add_argument("--whisper-url", default=bv.STANDARD_WHISPER_URL)
    p.add_argument("--sprak", default="no")
    p.add_argument("--fra", type=float, help="start i sekunder, for å prøve bare en bit")
    p.add_argument("--til", type=float, help="slutt i sekunder")
    p.add_argument("--bit-sekunder", type=int, default=10,
                   help="bitlengde i oppdelingstesten (standard 10)")
    p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--referer")
    p.add_argument("--user-agent")
    p.add_argument("--hopp-parametre", action="store_true")
    p.add_argument("--hopp-endepunkt", action="store_true")
    a = p.parse_args()

    try:
        bv.sjekk_verktoy()
    except bv.Feil as e:
        sys.exit(str(e))

    with tempfile.TemporaryDirectory(prefix="provwhisper_") as t:
        mappe = Path(t)
        print(f"Henter lyd fra {a.kilde} ...", file=sys.stderr)
        try:
            ra = bv.trekk_ut_lyd(a.kilde, mappe, a.referer, a.user_agent)
        except bv.Feil as e:
            sys.exit(str(e))

        if a.fra is not None or a.til is not None:
            fra = a.fra or 0.0
            klipp = mappe / "klipp.m4a"
            args = ["ffmpeg", "-v", "error", "-y", "-ss", str(fra)]
            if a.til is not None:
                args += ["-t", str(max(0.1, a.til - fra))]
            args += ["-i", str(ra), "-c", "copy", str(klipp)]
            bv._kjor(args)
            ra = klipp
            print(f"  bruker utsnittet {fra}–{a.til} s", file=sys.stderr)

        kb = ra.stat().st_size // 1024
        print(f"  lyd klar, {kb} kB\n", file=sys.stderr)

        print("=" * 100)
        print("1. LYDBEHANDLING (hele sporet i én forespørsel)")
        print("=" * 100)
        beste = ("rå", 0)
        varianter: dict[str, Path] = {}
        for navn, kjede in bv.LYDFILTRE.items():
            fil = lag_variant(ra, mappe, navn, kjede)
            if fil is None:
                continue
            varianter[navn] = fil
            status, tekst, seg = send(a.whisper_url, fil, a.sprak, a.timeout)
            rad(navn, status, tekst, seg)
            if ord(tekst) > beste[1]:
                beste = (navn, ord(tekst))

        print()
        print("=" * 100)
        print(f"2. OPPDELING I BITER À {a.bit_sekunder} s")
        print("=" * 100)
        varighet = bv.varighet_sekunder(str(ra))
        for navn, fil in varianter.items():
            biter = bv.del_opp_lyd(fil, mappe, a.bit_sekunder, varighet, logg=lambda m: None)
            samlet, segmenter = [], 0
            for start, bit in biter:
                status, tekst, seg = send(a.whisper_url, bit, a.sprak, a.timeout)
                if tekst:
                    samlet.append(tekst)
                segmenter += seg
            rad(f"{navn} i {len(biter)} biter", "ok", " ".join(samlet), segmenter)
            for _, bit in biter:
                bit.unlink(missing_ok=True)

        if not a.hopp_parametre:
            print()
            print("=" * 100)
            print("2b. UDOKUMENTERTE PARAMETRE (rå lyd, ett felt om gangen)")
            print("=" * 100)
            status, grunn, seg0 = send(a.whisper_url, ra, a.sprak, a.timeout)
            rad("uten ekstra felt", status, grunn, seg0)
            for ekstra in PARAMETRE:
                navn = ", ".join(f"{k}={v}" for k, v in ekstra.items())
                status, tekst, seg = send(a.whisper_url, ra, a.sprak, a.timeout, ekstra)
                if status == "ok" and tekst == grunn:
                    status = "ok (samme svar)"
                rad(navn, status, tekst, seg)

        if not a.hopp_endepunkt:
            print()
            print("=" * 100)
            print("3. ANDRE MODELLNAVN PÅ SAMME VERT")
            print("=" * 100)
            base = a.whisper_url
            for navn in MODELLNAVN:
                url = base
                for kjent in MODELLNAVN:
                    url = url.replace(f"/models/{kjent}:predict", f"/models/{navn}:predict")
                if url == base and f"/models/{navn}:" not in base:
                    continue
                status, tekst, seg = send(url, ra, a.sprak, a.timeout)
                rad(navn, status, tekst, seg)

        print()
        print(f"Mest tale fra lydbehandlingen: {beste[0]} ({beste[1]} ord).")
        print("Gir en variant tydelig mer, bruk den med --lydfilter i beskriv_video.py.")


if __name__ == "__main__":
    main()
