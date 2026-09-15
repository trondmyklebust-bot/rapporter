# Videobeskrivelse med gemma4 og NB-Whisper

Verken Ollama på `api.inference.nb.no`, LM Studio eller vLLM tar video som
input, bare tekst og bilder. Verktøyene her løser det slik:

1. ffmpeg trekker ut stillbilder jevnt fordelt over videoen.
2. Bildene sendes til en multimodal modell (gemma4 som standard) som beskriver
   det som er synlig.
3. Samtidig trekker ffmpeg ut lydsporet og sender det til NB-Whisper.
4. Et siste modellkall fletter bildebeskrivelse og transkripsjon til én
   beskrivelse av videoen på norsk.

Tre deler:

| Fil | Hva |
|-----|-----|
| `beskriv_video.py` | Kommandolinjeverktøy og kjernen (`analyser()`), kun standardbiblioteket |
| `server.py` | Lokal HTTP-server som Chrome-utvidelsen snakker med, kun standardbiblioteket |
| `chrome-utvidelse/` | Chrome-utvidelse som legger en «Beskriv video»-knapp på nb.no-sider |

## Krav

- Python 3.9 eller nyere
- `ffmpeg` og `ffprobe` i PATH
- Tilgang til `api.inference.nb.no` og `nb-whisper-large-predictor.inference.nb.no`
  (NB-nett eller VPN), og eventuelt et Bearer-token til inferensserveren

## Kommandolinja

```bash
export NB_INFERENS_TOKEN=xxx          # hvis gatewayen krever token
python3 beskriv_video.py film.mp4                                   # NB-inferens, gemma4
python3 beskriv_video.py film.mp4 --transkriber --ut resultat.json  # også lyd via NB-Whisper
python3 beskriv_video.py "https://.../playlist.m3u8" --referer https://www.nb.no/items/...
python3 beskriv_video.py film.mp4 --url http://localhost:1234       # LM Studio, første modell
python3 beskriv_video.py film.mp4 --url http://localhost:1234 --modell qwen/qwen3-vl-8b
```

Kilden kan være en lokal fil eller en URL ffmpeg kan lese, for eksempel en
HLS-spilleliste (`.m3u8`) fra NBs Wowza-strømming. Med `--referer` og
`--user-agent` sender ffmpeg de samme hodene som nettleseren, i tilfelle
strømmen krever det.

| Valg | Betydning |
|------|-----------|
| `--antall 12` | antall stillbilder (standard 8) |
| `--bredde 768` | bredde bildene skaleres til |
| `--per-kall 8` | bilder per modellkall; flere kall oppsummeres etterpå |
| `--transkriber` | send lydsporet til NB-Whisper parallelt og flett inn transkripsjonen |
| `--whisper-url` | annet Whisper-endepunkt (eller `NB_WHISPER_URL`) |
| `--sprak no` | språk for Whisper |
| `--modell gemma4:e4b` | modellnavn på serveren |
| `--url http://localhost:11434` | annen server, for eksempel lokal Ollama eller LM Studio |
| `--backend ollama` / `openai` | overstyr automatisk valg av rute |
| `--ut resultat.json` | lagre alt som JSON: bilder med tidspunkt, delbeskrivelser, transkripsjon, sluttbeskrivelse |
| `--behold-bilder MAPPE` | behold stillbildene og lydfila for kontroll |

Beskrivelsen skrives til stdout, framdrift til stderr.

Backend velges automatisk: svarer serveren på `/api/version` er det Ollama
(`/api/chat` med `think: false`), ellers brukes OpenAI-ruten
(`/v1/chat/completions` med bildene som data-URI, for LM Studio og vLLM). På
LM Studio/vLLM brukes første modell i `/v1/models` hvis du ikke oppgir `--modell`.

Kjører du qwen3-vl, bruk LM Studio. På Ollama tenker qwen3-vl uansett og kan
bruke hele token-budsjettet uten å svare. Gemma 4 tenker ikke og fungerer på begge.

## Chrome-utvidelse mot Nettbiblioteket

Utvidelsen legger en knapp nederst til høyre på `https://www.nb.no/items/...`
(også når du kommer via `urn.nb.no`). Når du trykker på den:

1. Bakgrunnsskriptet har fanget opp adressen til videostrømmen som spilleren
   lastet (Wowza leverer HLS som `.../playlist.m3u8`). Har den ikke sett noen
   strøm ennå, ber panelet deg trykke play og prøve igjen.
2. Innholdsskriptet sender strømadressen, sidens URL som Referer, URN og
   tittel til den lokale serveren.
3. Serveren kjører `analyser()`: bilder til gemma4 og lyd til NB-Whisper
   parallelt, deretter sluttbeskrivelsen.
4. Panelet viser framdrift, beskrivelsen, transkripsjonen med taler og
   tidspunkt, og kan kopiere teksten eller laste ned alt som JSON.

### Oppsett

Start serveren (den må kjøre så lenge du bruker utvidelsen):

```bash
cd videobeskrivelse
export NB_INFERENS_TOKEN=xxx     # om nødvendig
python3 server.py                # lytter på http://127.0.0.1:8765
```

Miljøvariabler serveren leser: `NB_INFERENS_URL`, `NB_INFERENS_TOKEN`,
`NB_INFERENS_MODELL`, `NB_WHISPER_URL`.

Last inn utvidelsen i Chrome:

1. Åpne `chrome://extensions`, slå på «Utviklermodus».
2. Velg «Last inn upakket» og pek på mappa `videobeskrivelse/chrome-utvidelse`.
3. Under utvidelsens innstillinger kan du endre serveradresse, antall bilder
   og om lyd skal sendes til Whisper.

### Ruter på serveren

| Rute | Hva |
|------|-----|
| `GET /helse` | status, hvilken inferens- og Whisper-adresse som brukes, om ffmpeg finnes |
| `POST /jobb` | start en jobb. Body: `{"kilde": url, "referer": ..., "user_agent": ..., "antall": 8, "transkriber": true, "urn": ..., "tittel": ...}` |
| `GET /jobb/<id>` | status (`kjører`, `ferdig`, `feil`), logg og resultat |
| `GET /jobber` | liste over jobber i denne kjøringen |

Serveren svarer med `Access-Control-Allow-Origin: *` slik at utvidelsen
(opphav `chrome-extension://...`) får lov å kalle den. Den lytter bare på
127.0.0.1.

## Kjente forbehold

- **Wowza-strømmen er ikke verifisert.** ffmpeg leser HLS direkte, og
  utvidelsen sender akkurat den adressen nettleseren brukte, med Referer og
  User-Agent. Bruker NB tidsbegrensede tokener i URL-en, må jobben startes
  mens tokenet er gyldig. Er strømmen DRM-beskyttet, virker det ikke.
- **Hvert stillbilde er et eget ffmpeg-kall** med `-ss` mot strømmen. Mot HLS
  betyr det at ffmpeg henter spillelista og ett segment per bilde. Det er
  raskt nok for noen titalls bilder.
- **NB-Whisper filtrerer bort musikk**, og tale over intro-jingler kan bli kuttet.
- Lyden sendes som 16 kHz mono AAC (48 kbit/s) base64-kodet i én JSON-forespørsel.
  Svært lange opptak gir store forespørsler.
