#!/usr/bin/env python3
"""
Bygg et storyboard som én selvstendig HTML-fil.

Siden viser bildene fra videoen i rekkefølge, med tidspunkt, replikkene som
faller på hvert bilde, og beskrivelsen av hver del. Bildene legges inn
base64-kodet, så fila kan flyttes, sendes videre og skrives ut som den er.

Brukes av beskriv_video.analyser() når `storyboard` er satt, og av server.py
for knappen i Chrome-utvidelsen.
"""

from __future__ import annotations

import base64
import html
from datetime import datetime
from pathlib import Path

CSS = """
:root {
  --blaa: #1F4E79; --lys: #EBF3FB; --mid: #2E75B6;
  --graa: #6c757d; --kant: #dde3ef; --bg: #f7f9fc; --kort: #ffffff;
  --tekst: #1a1a1a;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 0 20px 60px; background: var(--bg); color: var(--tekst);
  font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
}
.ramme { max-width: 1100px; margin: 0 auto; }
header { padding: 32px 0 20px; border-bottom: 3px solid var(--blaa); margin-bottom: 28px; }
h1 { margin: 0 0 6px; font-size: 1.9rem; color: var(--blaa); line-height: 1.2; }
.meta { margin: 0; color: var(--graa); font-size: .88rem; }
.meta span + span::before { content: " · "; }
.meta a { color: var(--graa); }
h2 { font-size: 1.05rem; color: var(--blaa); margin: 0 0 10px;
     text-transform: uppercase; letter-spacing: .05em; }
section { margin-bottom: 34px; }
.sammendrag { background: var(--lys); border-left: 4px solid var(--blaa);
              padding: 18px 22px; border-radius: 0 8px 8px 0; }
.sammendrag p { margin: 0 0 10px; }
.sammendrag p:last-child { margin-bottom: 0; }
.del { margin-bottom: 34px; }
.del-topp { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
            border-bottom: 1px solid var(--kant); padding-bottom: 6px; margin-bottom: 12px; }
.del-topp h2 { margin: 0; }
.del-tid { color: var(--graa); font-size: .85rem; font-variant-numeric: tabular-nums; }
.del-tekst { margin: 0 0 16px; }
.del-tekst p { margin: 0 0 10px; }
.ruter { display: grid; gap: 16px;
         grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
figure { margin: 0; background: var(--kort); border: 1px solid var(--kant);
         border-radius: 10px; overflow: hidden; display: flex; flex-direction: column;
         box-shadow: 0 1px 3px rgba(31,78,121,.08); }
figure img { width: 100%; height: auto; display: block; background: #000; }
figcaption { padding: 9px 12px 12px; font-size: .86rem; flex: 1; }
.tid { display: inline-block; background: var(--blaa); color: #fff;
       border-radius: 4px; padding: 1px 7px; font-size: .78rem;
       font-variant-numeric: tabular-nums; margin-bottom: 6px; }
.replikk { margin: 0; color: var(--tekst); }
.replikk .taler { color: var(--mid); font-weight: 600; }
.ingen { color: var(--graa); font-style: italic; }
table { width: 100%; border-collapse: collapse; background: var(--kort);
        border: 1px solid var(--kant); border-radius: 10px; overflow: hidden; }
td { padding: 7px 12px; border-top: 1px solid var(--kant); vertical-align: top; }
tr:first-child td { border-top: 0; }
td.t { color: var(--graa); white-space: nowrap; width: 1%;
       font-variant-numeric: tabular-nums; font-size: .86rem; }
td.s { color: var(--mid); font-weight: 600; white-space: nowrap; width: 1%; font-size: .86rem; }
footer { color: var(--graa); font-size: .8rem; border-top: 1px solid var(--kant);
         padding-top: 14px; }
@media print {
  body { background: #fff; padding: 0; }
  figure, table { box-shadow: none; break-inside: avoid; }
  .del { break-inside: avoid-page; }
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14181d; --kort: #1c2128; --tekst: #e6e9ee; --kant: #2c333c;
    --lys: #1a2733; --blaa: #7bb3e3; --mid: #9ec9ee; --graa: #97a1ad;
  }
}
"""


def _mime(sti: Path) -> str:
    """Bildetype ut fra filendelsen, så både .jpg og .png virker i data-URI-en."""
    return {"png": "png", "gif": "gif", "webp": "webp"}.get(
        sti.suffix.lower().lstrip("."), "jpeg")


def _tid(sek: float | None) -> str:
    if sek is None:
        return ""
    m, s = divmod(int(sek), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _avsnitt(tekst: str) -> str:
    """Tekst med tomme linjer som avsnittsskille, trygt escapet."""
    biter = [b.strip() for b in (tekst or "").split("\n\n") if b.strip()]
    if not biter:
        return '<p class="ingen">(ingen tekst)</p>'
    return "".join(f"<p>{html.escape(b).replace(chr(10), '<br>')}</p>" for b in biter)


def _fordel_replikker(segmenter: list[dict], tider: list[float]) -> dict[int, list[dict]]:
    """
    Gi hver replikk til det bildet den ligger nærmest, målt fra midten av
    replikken. Slik vises hver replikk nøyaktig én gang, også når den strekker
    seg over overgangen mellom to bilder.
    """
    tilordnet: dict[int, list[dict]] = {i: [] for i in range(len(tider))}
    if not tider:
        return tilordnet
    for s in segmenter:
        if not s.get("tekst") or s.get("start") is None:
            continue
        start = float(s["start"])
        slutt = float(s["slutt"]) if s.get("slutt") is not None else start
        midt = (start + slutt) / 2
        i = min(range(len(tider)), key=lambda j: abs(tider[j] - midt))
        tilordnet[i].append(s)
    return tilordnet


def bygg(resultat: dict, bildefiler: list[tuple[float, Path]], *, tittel: str | None = None,
         per_kall: int = 8) -> str:
    """
    Lag storyboardet som HTML-tekst.

    `resultat` er det analyser() returnerer. `bildefiler` er (sekund, sti) slik
    de lå på disk, siden bildene legges inn base64-kodet i sida.
    """
    varighet = float(resultat.get("varighet_sekunder") or 0)
    segmenter = (resultat.get("transkripsjon") or {}).get("segmenter") or []
    delbeskrivelser = resultat.get("delbeskrivelser") or []
    navn = tittel or resultat.get("tittel") or "Videobeskrivelse"
    steg = varighet / len(bildefiler) if bildefiler else 0
    tilordnet = _fordel_replikker(segmenter, [t for t, _ in bildefiler])

    d: list[str] = []
    d.append("<!DOCTYPE html>\n<html lang=\"no\">\n<head>\n<meta charset=\"utf-8\">")
    d.append('<meta name="viewport" content="width=device-width, initial-scale=1">')
    d.append(f"<title>Storyboard – {html.escape(navn)}</title>")
    d.append(f"<style>{CSS}</style>\n</head>\n<body>\n<div class=\"ramme\">")

    kilde = resultat.get("kilde") or ""
    kildevisning = html.escape(kilde if len(kilde) < 90 else kilde[:87] + "…")
    d.append("<header>")
    d.append(f"<h1>{html.escape(navn)}</h1>")
    d.append('<p class="meta">')
    d.append(f"<span>{len(bildefiler)} bilder</span>")
    d.append(f"<span>{_tid(varighet)}</span>")
    d.append(f"<span>{html.escape(str(resultat.get('modell') or ''))}</span>")
    if segmenter:
        d.append(f"<span>{len(segmenter)} replikker</span>")
    d.append(f"<span>{datetime.now().strftime('%d.%m.%Y %H:%M')}</span>")
    d.append("</p>")
    if kilde:
        lenke = f'<a href="{html.escape(kilde, quote=True)}">{kildevisning}</a>' \
            if kilde.startswith("http") else kildevisning
        d.append(f'<p class="meta">{lenke}</p>')
    d.append("</header>")

    d.append('<section class="sammendrag"><h2>Beskrivelse</h2>')
    d.append(_avsnitt(resultat.get("beskrivelse") or ""))
    d.append("</section>")

    d.append("<section>")
    grupper = [bildefiler[i:i + per_kall] for i in range(0, len(bildefiler), max(1, per_kall))]
    for nr, gruppe in enumerate(grupper, 1):
        forste = (nr - 1) * max(1, per_kall)
        fra = 0.0 if nr == 1 else forste * steg
        til = varighet if nr == len(grupper) else (forste + len(gruppe)) * steg
        d.append('<div class="del">')
        d.append('<div class="del-topp">')
        d.append(f"<h2>{'Del ' + str(nr) if len(grupper) > 1 else 'Bilder'}</h2>")
        d.append(f'<span class="del-tid">{_tid(fra)} – {_tid(til)}</span>')
        d.append("</div>")
        if nr <= len(delbeskrivelser) and len(grupper) > 1:
            d.append(f'<div class="del-tekst">{_avsnitt(delbeskrivelser[nr - 1])}</div>')
        d.append('<div class="ruter">')
        for k, (t, fil) in enumerate(gruppe):
            fil = Path(fil)
            try:
                b64 = base64.b64encode(fil.read_bytes()).decode()
            except OSError:
                b64 = ""
            d.append("<figure>")
            if b64:
                d.append(f'<img src="data:image/{_mime(fil)};base64,{b64}" '
                         f'alt="Bilde ved {_tid(t)}">')
            d.append(f'<figcaption><span class="tid">{_tid(t)}</span>')
            nære = tilordnet.get(forste + k, [])
            if nære:
                for s in nære:
                    taler = f'<span class="taler">{html.escape(str(s["taler"]))}:</span> ' \
                        if s.get("taler") else ""
                    d.append(f'<p class="replikk">{taler}{html.escape(s["tekst"])}</p>')
            else:
                d.append('<p class="replikk ingen">ingen replikk</p>')
            d.append("</figcaption></figure>")
        d.append("</div></div>")
    d.append("</section>")

    if segmenter:
        d.append("<section><h2>Transkripsjon</h2><table>")
        for s in segmenter:
            taler = html.escape(str(s["taler"])) if s.get("taler") else ""
            d.append(f'<tr><td class="t">{_tid(s.get("start"))}</td>'
                     f'<td class="s">{taler}</td>'
                     f'<td>{html.escape(s["tekst"])}</td></tr>')
        d.append("</table>")
        t = resultat.get("transkripsjon") or {}
        biter = t.get("antall_biter")
        if biter and biter > 1:
            d.append(f'<p class="meta">Lyden ble sendt til NB-Whisper i {biter} biter. '
                     "Talernavn tildeles per bit, så samme navn i to biter er ikke "
                     "nødvendigvis samme person.</p>")
        if "musikkfilter" in t:
            d.append('<p class="meta">Musikkfilteret i NB-Whisper var '
                     f'{"på" if t["musikkfilter"] else "av"}.</p>')
        d.append("</section>")
    elif resultat.get("transkripsjon_feil"):
        d.append('<section><h2>Transkripsjon</h2>'
                 f'<p class="ingen">Feilet: {html.escape(str(resultat["transkripsjon_feil"]))}</p>'
                 "</section>")

    d.append('<footer>Laget av videobeskrivelse. Bildene er hentet med ffmpeg, '
             'beskrevet med en modell på NBs inferensserver, og talen transkribert '
             'med NB-Whisper. Automatisk generert tekst kan inneholde feil.</footer>')
    d.append("</div>\n</body>\n</html>\n")
    return "\n".join(d)


def skriv(sti: Path, resultat: dict, bildefiler, **kwargs) -> Path:
    sti = Path(sti)
    sti.parent.mkdir(parents=True, exist_ok=True)
    sti.write_text(bygg(resultat, list(bildefiler), **kwargs), encoding="utf-8")
    return sti
