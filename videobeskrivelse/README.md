# Videobeskrivelse med gemma4 og NB-Whisper

Verken Ollama på `api.inference.nb.no`, LM Studio eller vLLM tar video som
input, bare tekst og bilder. Verktøyene her løser det slik:

1. ffmpeg trekker ut stillbilder jevnt fordelt over videoen.
2. Samtidig trekker ffmpeg ut lydsporet, deler det i biter og sender bitene til
   NB-Whisper.
3. Modellen (`gemma4:26b-a4b-it-q8_0` som standard) får bildene og talen fra samme tidsrom i
   ett og samme kall, og tolker dem i sammenheng.
4. Er videoen delt i flere deler, syr et siste kall delbeskrivelsene sammen til
   én beskrivelse på norsk.

Tre deler:

| Fil | Hva |
|-----|-----|
| `beskriv_video.py` | Kommandolinjeverktøy og kjernen (`analyser()`), kun standardbiblioteket |
| `storyboard.py` | Bygger storyboardet som én selvstendig HTML-fil |
| `prov_whisper.py` | Prøver ut hva som gir mest tale ut av NB-Whisper |
| `server.py` | Lokal HTTP-server som Chrome-utvidelsen snakker med, kun standardbiblioteket |
| `start.sh` | Stopper gammel server, starter ny, og bekrefter at riktig versjon svarer |
| `chrome-utvidelse/` | Chrome-utvidelse som legger en «Beskriv video»-knapp på nb.no-sider |

### Hvorfor lyden deles i biter

NB-Whisper filtrerer bort musikk, og tale over en jingel blir gjerne kuttet. På
et opptak med gjennomgående musikk, for eksempel en reklamefilm, kan hele talen
forsvinne når fila vurderes under ett. Sendes lyden i korte biter, vurderes hver
bit for seg, og tale i en pause i musikken får en sjanse til å slippe gjennom.
Standard bitlengde er 20 sekunder, og `--bit-sekunder 0` sender hele lydsporet i
én forespørsel som før.

Tidspunktene regnes om til posisjon i videoen, så segmentene stemmer selv om de
kommer fra hver sin forespørsel. Talernavn som `SPEAKER_00` tildeles derimot per
forespørsel, så samme navn i to ulike biter er ikke nødvendigvis samme person.
Hvert segment har feltet `bit` slik at du ser hvor det kom fra.

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
python3 beskriv_video.py film.mp4 --transkriber --bit-sekunder 10   # kortere lydbiter
python3 beskriv_video.py film.mp4 --transkriber --storyboard film.html
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
| `--transkriber` | send lydsporet til NB-Whisper og tolk talen sammen med bildene |
| `--bit-sekunder 10` | lengden på hver lydbit til Whisper, 0 sender hele sporet i ett (standard 20) |
| `--ikke-samtolk` | beskriv bildene først og flett inn talen til slutt, som før |
| `--whisper-url` | annet Whisper-endepunkt (eller `NB_WHISPER_URL`) |
| `--sprak no` | språk for Whisper |
| `--lydfilter tale` | behandle lyden før Whisper: `ingen`, `tale` eller `kraftig` |
| `--modell gemma4:e4b` | modellnavn på serveren, for eksempel en mindre og raskere gemma4 |
| `--modeller` | list modellene serveren tilbyr, og avslutt |
| `--url http://localhost:11434` | annen server, for eksempel lokal Ollama eller LM Studio |
| `--backend ollama` / `openai` | overstyr automatisk valg av rute |
| `--storyboard film.html` | lag et storyboard: bilder, beskrivelser og replikker i én HTML-fil |
| `--tittel "..."` | overskrift på storyboardet |
| `--ut resultat.json` | lagre alt som JSON: bilder med tidspunkt, delbeskrivelser, transkripsjon, sluttbeskrivelse |
| `--behold-bilder MAPPE` | behold stillbildene og lydfila for kontroll |

Beskrivelsen skrives til stdout, framdrift til stderr.

### Når Whisper ikke finner talen

Musikkfilteret i NB-Whisper er ikke dokumentert som noe man kan skru av, og på
en reklamefilm med musikk hele veien kan nesten all tale forsvinne. Tre ting
kan prøves, i denne rekkefølgen:

1. **Kortere biter.** `--bit-sekunder 10` eller lavere. Hver bit vurderes for
   seg, så tale i en pause i musikken får en sjanse.
2. **Lydfilter.** `--lydfilter tale` løfter fram taleområdet og jevner ut
   nivået. `--lydfilter kraftig` gjør det samme hardere, med kompresjon.
   Filtrene endrer bare det som sendes til Whisper, ikke det som vises.
3. **Vokalseparasjon.** Skill ut stemmen med Demucs først, og send bare
   vokalsporet. Krever en tung modell lokalt, men fjerner jingelen før
   Whisper ser den.

`prov_whisper.py` prøver alt dette systematisk på ett opptak og teller hvor
mange ord hver variant gir:

```bash
python3 prov_whisper.py film.mp4
python3 prov_whisper.py "https://wow.nb.no/.../playlist.m3u8" \
    --referer https://www.nb.no/items/URN:NBN:no-nb_video_9314
```

Den tester fire ting: lydbehandling, bitlengde, udokumenterte felt i
forespørselen som `filter_music` og `vad`, og andre modellnavn på samme vert,
for eksempel en verbatim-variant. Resultatet er en tabell over hvor mange ord
hver variant ga, så du ser hva som virker i stedet for å gjette.

### Storyboard

`--storyboard fil.html` lager en side som presenterer filmen: bildene i
rekkefølge med tidspunkt, replikken som hører til hvert bilde, beskrivelsen av
hver del, sammendraget øverst og hele transkripsjonen nederst. Bildene ligger
base64-kodet i sida, så fila kan sendes videre, arkiveres og skrives ut som den
er. Hver replikk vises ved det bildet den ligger nærmest, målt fra midten av
replikken, så ingenting dukker opp to ganger.

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
3. Serveren kjører `analyser()`: bilder og lyd hentes parallelt, lyden går til
   NB-Whisper i biter, og modellen tolker bilder og tale sammen.
4. Panelet viser framdrift, beskrivelsen, transkripsjonen med taler og
   tidspunkt. Knappen «Åpne storyboard» åpner presentasjonen i en ny fane, og
   du kan kopiere teksten eller laste ned alt som JSON.

### Oppsett

Start serveren (den må kjøre så lenge du bruker utvidelsen):

```bash
cd videobeskrivelse
./start.sh
```

`start.sh` stopper først en server som allerede holder porten, venter til den
er borte, starter en ny i bakgrunnen, og gir seg ikke før `/helse` svarer.
Deretter skriver den hvilken git-versjon som kjører, hvilken modell den bruker,
om ffmpeg finnes, og hvor mange modeller inferensserveren tilbyr. Da er det
ingen tvil om hva som står bak porten.

| Valg | Betydning |
|------|-----------|
| `--pull` | hent siste kode med `git pull` før start |
| `--stopp` | bare stopp den som kjører |
| `--port 8800` | annen port |
| `--forgrunn` | kjør i dette vinduet i stedet for i bakgrunnen |

Holder noe annet enn serveren porten, stopper skriptet og sier fra i stedet for
å drepe prosessen. Starter serveren og dør med én gang, vises de siste linjene
fra `server.logg`.

Vil du heller styre det selv, virker `python3 server.py` som før.

Miljøvariabler serveren leser: `NB_INFERENS_URL`, `NB_INFERENS_TOKEN`,
`NB_INFERENS_MODELL`, `NB_WHISPER_URL`. Sett dem i samme vindu som du kjører
`start.sh` fra, eller foran kommandoen:

```bash
NB_INFERENS_MODELL=gemma4:e4b ./start.sh
```

Last inn utvidelsen i Chrome:

1. Åpne `chrome://extensions`, slå på «Utviklermodus».
2. Velg «Last inn upakket» og pek på mappa `videobeskrivelse/chrome-utvidelse`.
3. Under utvidelsens innstillinger kan du endre serveradresse, antall bilder,
   bitlengden for lyden og om lyd skal sendes til Whisper. De to siste kan også
   settes rett i panelet.

### Velge modell i panelet

Panelet har en nedtrekksliste over modellene på inferensserveren. Den henter
lista fra serveren, og deler den i to: modeller som kan se bilder, og resten.
Velger du en modell uten syn, sier panelet fra om at den bare får
transkripsjonen å gå på. Valget huskes til neste gang. «Standard» bruker det
`server.py` er satt opp med, altså `NB_INFERENS_MODELL` eller
`gemma4:26b-a4b-it-q8_0`.

Hvilke modeller som kan se bilder leses fra Ollamas `/api/show`, der `vision`
i `capabilities` avgjør. Lista mellomlagres i fem minutter, siden den krever
ett kall per modell.

### Ruter på serveren

| Rute | Hva |
|------|-----|
| `GET /helse` | status, git-versjon, rutene som finnes, inferens- og Whisper-adresse, om ffmpeg finnes |
| `POST /jobb` | start en jobb. Body: `{"kilde": url, "referer": ..., "user_agent": ..., "antall": 8, "transkriber": true, "bit_sekunder": 20, "samtolk": true, "urn": ..., "tittel": ...}` |
| `GET /jobb/<id>` | status (`kjører`, `ferdig`, `feil`), logg og resultat |
| `GET /modeller` | modellene inferensserveren tilbyr, hver med om den kan se bilder. `?frisk` hopper over mellomlagringen |
| `GET /storyboard/<id>` | storyboardet for jobben som ferdig HTML-side |
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
  Det finnes ingen dokumentert bryter for å slå det av. Se avsnittet under.
- **Standardmodellen er `gemma4:26b-a4b-it-q8_0`.** Heter modellen noe annet på serveren din,
  se lista med `python3 beskriv_video.py --modeller`, og sett riktig navn med
  `--modell` eller miljøvariabelen `NB_INFERENS_MODELL`. Treffer du et navn som
  ikke finnes, viser feilmeldingen hvilke som gjør det.
- Lyden sendes som 16 kHz mono AAC (48 kbit/s) base64-kodet. Med oppdeling blir
  hver forespørsel liten, uansett hvor langt opptaket er.
