# Videobeskrivelse med gemma4

Ollama på `api.inference.nb.no` tar ikke video som input, bare tekst og bilder.
`beskriv_video.py` løser det ved å trekke ut stillbilder jevnt fordelt over
videoen med ffmpeg, sende dem til gemma4 via Ollamas `/api/chat`, og be modellen
beskrive forløpet på norsk.

## Krav

- Python 3.9 eller nyere (bare standardbiblioteket)
- `ffmpeg` og `ffprobe` i PATH
- Tilgang til `api.inference.nb.no` (NB-nett/VPN) og eventuelt et Bearer-token

## Bruk

```bash
export NB_INFERENS_TOKEN=xxx          # hvis gatewayen krever token
python3 beskriv_video.py film.mp4
```

Nyttige valg:

| Valg | Betydning |
|------|-----------|
| `--antall 12` | antall stillbilder (standard 8) |
| `--bredde 768` | bredde bildene skaleres til |
| `--per-kall 8` | bilder per modellkall; flere kall oppsummeres etterpå |
| `--modell gemma4:e4b` | modellnavn på serveren |
| `--url http://localhost:11434` | annen Ollama-server, for eksempel lokal |
| `--ut resultat.json` | lagre bilder-tidspunkt, delbeskrivelser og sluttbeskrivelse som JSON |
| `--behold-bilder MAPPE` | behold stillbildene for kontroll |

Beskrivelsen skrives til stdout, framdrift til stderr.

## Slik virker det

1. `ffprobe` finner varigheten.
2. `ffmpeg` henter ett bilde midt i hvert av N like lange intervaller.
3. Bildene sendes base64-kodet i `images`-feltet, med tidsstempel i prompten,
   og `think: false` slik skillen for inferensserveren anbefaler.
4. Er det flere bilder enn `--per-kall`, beskrives hver gruppe for seg, og et
   siste kall syr delbeskrivelsene sammen til én tekst.

Lydsporet brukes ikke. Trenger du tale eller dialog, transkriber med
nb-whisper og legg transkripsjonen inn i prompten.
