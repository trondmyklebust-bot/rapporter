# Videobeskrivelse med gemma4 eller qwen3-vl

Verken Ollama på `api.inference.nb.no`, LM Studio eller vLLM tar video som
input, bare tekst og bilder. `beskriv_video.py` løser det ved å trekke ut
stillbilder jevnt fordelt over videoen med ffmpeg, sende dem til en
multimodal modell og be den beskrive forløpet på norsk.

Skriptet snakker med to typer server:

| Backend | Rute | Typisk bruk |
|---------|------|-------------|
| Ollama | `/api/chat` med `think: false` | `api.inference.nb.no`, standardmodell `gemma4:e4b` |
| OpenAI-kompatibel | `/v1/chat/completions` med bilder som data-URI | LM Studio på `localhost:1234`, vLLM |

Backend velges automatisk: svarer serveren på `/api/version` er det Ollama,
ellers brukes OpenAI-ruten. På LM Studio/vLLM brukes første modell i
`/v1/models` hvis du ikke oppgir `--modell`.

## Krav

- Python 3.9 eller nyere (bare standardbiblioteket)
- `ffmpeg` og `ffprobe` i PATH
- Tilgang til `api.inference.nb.no` (NB-nett/VPN) og eventuelt et Bearer-token

## Bruk

```bash
export NB_INFERENS_TOKEN=xxx          # hvis gatewayen krever token
python3 beskriv_video.py film.mp4                                   # NB-inferens, gemma4
python3 beskriv_video.py film.mp4 --url http://localhost:1234       # LM Studio, første modell
python3 beskriv_video.py film.mp4 --url http://localhost:1234 --modell qwen/qwen3-vl-8b
```

Kjører du qwen3-vl, bruk LM Studio. På Ollama tenker qwen3-vl uansett og kan
bruke hele token-budsjettet uten å svare. Gemma 4 tenker ikke og fungerer på begge.

Nyttige valg:

| Valg | Betydning |
|------|-----------|
| `--antall 12` | antall stillbilder (standard 8) |
| `--bredde 768` | bredde bildene skaleres til |
| `--per-kall 8` | bilder per modellkall; flere kall oppsummeres etterpå |
| `--modell gemma4:e4b` | modellnavn på serveren |
| `--url http://localhost:11434` | annen server, for eksempel lokal Ollama eller LM Studio |
| `--backend ollama` / `openai` | overstyr automatisk valg av rute |
| `--ut resultat.json` | lagre bilder-tidspunkt, delbeskrivelser og sluttbeskrivelse som JSON |
| `--behold-bilder MAPPE` | behold stillbildene for kontroll |

Beskrivelsen skrives til stdout, framdrift til stderr.

## Slik virker det

1. `ffprobe` finner varigheten.
2. `ffmpeg` henter ett bilde midt i hvert av N like lange intervaller.
3. Bildene sendes base64-kodet med tidsstempel i prompten: i `images`-feltet
   og med `think: false` på Ollama, som `image_url`-data-URI på OpenAI-ruten.
4. Er det flere bilder enn `--per-kall`, beskrives hver gruppe for seg, og et
   siste kall syr delbeskrivelsene sammen til én tekst.

Lydsporet brukes ikke. Trenger du tale eller dialog, transkriber med
nb-whisper og legg transkripsjonen inn i prompten.
