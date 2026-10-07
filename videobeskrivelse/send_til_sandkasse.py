#!/usr/bin/env python3
"""
Legg lokalt kjørte jobber inn i lista i sandkassa.

Lista på startsiden i sandkassa bygges av storyboard-filene der. Skriptet
sender storyboardene fra den lokale mappa storyboards/ dit, med PUT og den
delte nøkkelen. Det som finnes fra før med samme innhold hoppes over.

    export NB_VIDEO_DEL_NOKKEL=<nøkkelen sandkassa er startet med>
    python3 send_til_sandkasse.py                 # alle lokale storyboards
    python3 send_til_sandkasse.py 9277e217c765    # bare én jobb
    python3 send_til_sandkasse.py --tørrkjøring   # vis hva som ville blitt sendt

Vil du at hver ny lokal jobb sendes automatisk, start den lokale serveren med
NB_VIDEO_DEL_TIL og NB_VIDEO_DEL_NOKKEL satt, så skjer dette etter hver jobb.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import deling  # noqa: E402

STANDARD_SANDKASSE = "https://sandkasse.nb.no/nb-videobeskrivelse"
MAPPE = Path(__file__).resolve().parent / "storyboards"


def helse(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{url}/helse", timeout=15) as svar:
            return json.loads(svar.read())
    except (urllib.error.URLError, ValueError, OSError):
        return None


def main() -> None:
    p = argparse.ArgumentParser(description="Send lokale storyboards til lista i sandkassa.")
    p.add_argument("id", nargs="*", help="jobb-id-er å sende (standard: alle i mappa)")
    p.add_argument("--url", default=os.environ.get("NB_VIDEO_DEL_TIL") or STANDARD_SANDKASSE,
                   help=f"adressen til sandkassa (standard {STANDARD_SANDKASSE})")
    p.add_argument("--nokkel", default=os.environ.get("NB_VIDEO_DEL_NOKKEL"),
                   help="delt nøkkel (standard fra NB_VIDEO_DEL_NOKKEL)")
    p.add_argument("--mappe", type=Path, default=MAPPE, help="mappa med lokale storyboards")
    p.add_argument("--erstatt", action="store_true",
                   help="skriv over storyboards som finnes med annet innhold")
    p.add_argument("--tørrkjøring", "--torrkjoring", dest="torr", action="store_true",
                   help="vis hva som ville blitt sendt, uten å sende")
    a = p.parse_args()
    url = a.url.rstrip("/")

    if a.id:
        filer = [a.mappe / f"{i.removesuffix('.html')}.html" for i in a.id]
        mangler = [f.name for f in filer if not f.is_file()]
        if mangler:
            sys.exit(f"Fant ikke i {a.mappe}: {', '.join(mangler)}")
    else:
        filer = sorted(a.mappe.glob("*.html"), key=lambda f: f.stat().st_mtime)
    if not filer:
        sys.exit(f"Ingen storyboards i {a.mappe}. Kjør en jobb lokalt først.")

    print(f"{len(filer)} storyboard{'s' if len(filer) != 1 else ''} fra {a.mappe}")
    if a.torr:
        for f in filer:
            dato = time.strftime("%d.%m.%Y %H:%M", time.localtime(f.stat().st_mtime))
            print(f"  {dato}  {f.stem}  {f.stat().st_size // 1024} kB")
        print(f"Ville blitt sendt til {url}")
        return

    if not a.nokkel:
        sys.exit("Mangler nøkkel. Sett NB_VIDEO_DEL_NOKKEL, eller bruk --nokkel.")

    h = helse(url)
    if h is None:
        sys.exit(f"Får ikke kontakt med {url}/helse. Er du på NB-nett?")
    if "import" not in h:
        sys.exit(f"Sandkassa kjører versjon {h.get('versjon', 'ukjent')}, som ikke kan ta imot "
                 "storyboards ennå. Den må oppdateres med koden som har deling.py.")
    if not h.get("import"):
        sys.exit("Sandkassa har ikke slått på mottak. Den må startes med NB_VIDEO_IMPORTNOKKEL satt.")

    teller = {"ny": 0, "uendret": 0, "erstattet": 0, "feil": 0}
    for f in filer:
        status, svar = deling.send_storyboard(url, a.nokkel, f, erstatt=a.erstatt)
        try:
            data = json.loads(svar)
        except ValueError:
            data = {"feil": svar[:120]}
        if status in (200, 201):
            utfall = data.get("status", "ok")
            teller[utfall] = teller.get(utfall, 0) + 1
            merke = "✓" if utfall != "uendret" else "·"
            print(f"  {merke} {f.stem}  {utfall}")
        else:
            teller["feil"] += 1
            print(f"  ✗ {f.stem}  HTTP {status}: {data.get('feil', svar[:120])}")
            if status == 403:
                sys.exit("Nøkkelen ble avvist. Sjekk at den er lik NB_VIDEO_IMPORTNOKKEL i sandkassa.")

    print(f"\nNye: {teller['ny']}, uendret: {teller['uendret']}, "
          f"erstattet: {teller['erstattet']}, feil: {teller['feil']}")
    print(f"Lista: {url}/")
    sys.exit(1 if teller["feil"] else 0)


if __name__ == "__main__":
    main()
