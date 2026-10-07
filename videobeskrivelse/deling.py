"""
Del storyboards mellom en lokal server og sandkassa.

Lista på startsiden bygges av HTML-filene i mappa storyboards/. Å legge en
lokalt kjørt jobb inn i lista i sandkassa betyr derfor å få storyboard-fila
over dit. Det skjer med PUT /storyboard/<id> og en delt nøkkel:

    sandkassa:  NB_VIDEO_IMPORTNOKKEL=<hemmelig>  slår på mottak
    lokalt:     NB_VIDEO_DEL_TIL=<url til sandkassa>
                NB_VIDEO_DEL_NOKKEL=<samme hemmelighet>

Uten NB_VIDEO_IMPORTNOKKEL tar serveren ikke imot noe. Storyboardene serveres
med en innholdspolicy som stopper alle skript, så heller ikke en fil fra en
som har nøkkelen kan kjøre kode i nettleseren til den som åpner den.
"""

from __future__ import annotations

import hmac
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# Lov form på id-er: det jobb-id-ene ser ut som, pluss litt slingringsmonn.
ID_MONSTER = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAKS_BYTES = 40 * 1024 * 1024
NOKKEL_HODE = "X-Importnokkel"
OPPRETTET_HODE = "X-Opprettet"

# Storyboardene har ingen skript. Policyen sørger for at det forblir slik,
# uansett hva fila inneholder: ingen skript, ingen eksterne ressurser,
# bilder bare som data-URI og stil bare inline.
STORYBOARD_CSP = ("default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
                  "base-uri 'none'; form-action 'none'")


class Avvist(Exception):
    """Fila eller forespørselen ble avvist. `status` er HTTP-koden som passer."""

    def __init__(self, status: int, melding: str):
        super().__init__(melding)
        self.status = status


# ---------------------------------------------------------------------------
# Mottak (brukes av server.py i sandkassa)
# ---------------------------------------------------------------------------

def nokkel_ok(oppgitt: str | None, forventet: str | None) -> bool:
    """Sammenlign i konstant tid, så nøkkelen ikke kan gjettes tegn for tegn."""
    if not forventet or not oppgitt:
        return False
    return hmac.compare_digest(oppgitt.encode(), forventet.encode())


def sjekk_id(jobb_id: str) -> str:
    if not ID_MONSTER.match(jobb_id or ""):
        raise Avvist(400, "ugyldig id, bruk bare bokstaver, tall, - og _")
    return jobb_id


def sjekk_storyboard(data: bytes) -> None:
    """Godta bare noe som ser ut som en HTML-side innenfor størrelsesgrensen."""
    if len(data) > MAKS_BYTES:
        raise Avvist(413, f"fila er større enn {MAKS_BYTES // (1024 * 1024)} MB")
    try:
        tekst = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Avvist(400, "fila er ikke UTF-8") from None
    start = tekst.lstrip()[:20].lower()
    if not start.startswith("<!doctype html"):
        raise Avvist(400, "fila er ikke en HTML-side")
    if "<title>" not in tekst[:8000].lower():
        raise Avvist(400, "fila mangler <title>, som lista på startsiden bruker")


def lagre_storyboard(mappe: Path, jobb_id: str, data: bytes, *, erstatt: bool = False,
                     opprettet: float | None = None) -> str:
    """
    Skriv fila trygt til mappa. Returnerer "ny", "uendret" eller "erstattet".

    Finnes id-en fra før med annet innhold, avvises det med 409 med mindre
    `erstatt` er satt. Skrivingen går via en midlertidig fil, så en halvveis
    opplasting aldri blir liggende som en ødelagt side i lista.
    """
    sjekk_id(jobb_id)
    sjekk_storyboard(data)
    mappe.mkdir(parents=True, exist_ok=True)
    fil = mappe / f"{jobb_id}.html"

    status = "ny"
    if fil.exists():
        if fil.read_bytes() == data:
            return "uendret"
        if not erstatt:
            raise Avvist(409, "et annet storyboard med samme id finnes fra før, "
                              "send med erstatt for å skrive over")
        status = "erstattet"

    fd, tmp = tempfile.mkstemp(prefix=f".{jobb_id}.", suffix=".tmp", dir=mappe)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, fil)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise

    # Behold tidspunktet jobben faktisk ble kjørt, så lista sorteres riktig.
    if opprettet and 0 < opprettet <= time.time() + 60:
        os.utime(fil, (opprettet, opprettet))
    return status


# ---------------------------------------------------------------------------
# Sending (brukes lokalt, av server.py og send_til_sandkasse.py)
# ---------------------------------------------------------------------------

def send_storyboard(base_url: str, nokkel: str, fil: Path, *, erstatt: bool = False,
                    timeout: int = 60) -> tuple[int, str]:
    """
    Send én storyboard-fil til en server. Returnerer (HTTP-status, melding).

    Kaster ikke ved HTTP-feil, så den som kaller kan rapportere hver fil for seg.
    """
    jobb_id = fil.stem
    url = f"{base_url.rstrip('/')}/storyboard/{jobb_id}"
    if erstatt:
        url += "?erstatt=1"
    data = fil.read_bytes()
    req = urllib.request.Request(url, data=data, method="PUT", headers={
        "Content-Type": "text/html; charset=utf-8",
        NOKKEL_HODE: nokkel,
        OPPRETTET_HODE: str(int(fil.stat().st_mtime)),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as svar:
            return svar.status, svar.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except urllib.error.URLError as e:
        return 0, f"ingen kontakt: {e.reason}"
