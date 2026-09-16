#!/usr/bin/env bash
#
# Start serveren for NB videobeskrivelse, og vær sikker på at det er den nye
# koden som kjører.
#
# Skriptet stopper en server som allerede holder porten, venter til den er
# borte, starter en ny i bakgrunnen, og gir seg ikke før /helse svarer. Til
# slutt skrives hvilken git-versjon som kjører og hvilken modell den bruker,
# slik at det ikke er tvil om hva som står bak porten.
#
#   ./start.sh                 stopp gammel, start ny på port 8170
#   ./start.sh --pull          hent siste kode fra git først
#   ./start.sh --stopp         bare stopp
#   ./start.sh --port 8800     annen port
#   ./start.sh --forgrunn      kjør i dette vinduet i stedet for i bakgrunnen
#
set -uo pipefail

MAPPE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$MAPPE" || exit 1

PORT=8170
PULL=0
BARE_STOPP=0
FORGRUNN=0
LOGG="$MAPPE/server.logg"
PIDFIL="$MAPPE/.server.pid"

while [ $# -gt 0 ]; do
  case "$1" in
    --pull) PULL=1 ;;
    --stopp|--stop) BARE_STOPP=1 ;;
    --forgrunn) FORGRUNN=1 ;;
    --port) PORT="${2:-}"; shift ;;
    -h|--help) sed -n '3,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Ukjent valg: $1"; exit 2 ;;
  esac
  shift
done

blaa() { printf '\033[1;34m%s\033[0m\n' "$1"; }
ok()   { printf '\033[0;32m✓\033[0m %s\n' "$1"; }
feil() { printf '\033[0;31m✗\033[0m %s\n' "$1" >&2; }

# --- 1. Finn og stopp det som holder porten ---------------------------------

pider_paa_port() {
  if command -v lsof > /dev/null 2>&1; then
    lsof -ti "tcp:$PORT" -sTCP:LISTEN 2>/dev/null
  fi
}

stopp_gammel() {
  local pider funnet=0 p navn
  pider="$(pider_paa_port)"

  # Reserve: pid-fila fra forrige start, om lsof ikke finnes eller ikke fant noe.
  if [ -z "$pider" ] && [ -f "$PIDFIL" ]; then
    p="$(cat "$PIDFIL" 2>/dev/null)"
    if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then pider="$p"; fi
  fi

  if [ -z "$pider" ]; then
    ok "Ingen server kjørte på port $PORT"
    rm -f "$PIDFIL"
    return 0
  fi

  for p in $pider; do
    navn="$(ps -p "$p" -o command= 2>/dev/null)"
    case "$navn" in
      *server.py*|*python*)
        echo "  stopper pid $p ($(echo "$navn" | cut -c1-60))"
        kill "$p" 2>/dev/null
        funnet=1
        ;;
      *)
        feil "Port $PORT holdes av noe annet enn serveren: $navn"
        feil "Velg en annen port med --port, eller stopp den prosessen selv."
        exit 1
        ;;
    esac
  done

  # Vent til porten faktisk er ledig. Ta hardere i hvis den henger.
  local i
  for i in $(seq 1 20); do
    sleep 0.25
    [ -z "$(pider_paa_port)" ] && break
    if [ "$i" -eq 12 ]; then
      for p in $pider; do kill -9 "$p" 2>/dev/null; done
    fi
  done

  if [ -n "$(pider_paa_port)" ]; then
    feil "Klarte ikke frigjøre port $PORT"
    exit 1
  fi
  rm -f "$PIDFIL"
  [ "$funnet" -eq 1 ] && ok "Gammel server stoppet"
  return 0
}

blaa "NB videobeskrivelse"
stopp_gammel

if [ "$BARE_STOPP" -eq 1 ]; then
  exit 0
fi

# --- 2. Eventuelt hent siste kode -------------------------------------------

if [ "$PULL" -eq 1 ]; then
  if [ -n "$(git -C "$MAPPE" status --porcelain 2>/dev/null)" ]; then
    feil "Du har lokale endringer. Hopper over git pull."
  else
    echo "  git pull ..."
    git -C "$MAPPE" pull --ff-only 2>&1 | sed 's/^/    /'
  fi
fi

VERSJON="$(git -C "$MAPPE" rev-parse --short HEAD 2>/dev/null || echo ukjent)"

# --- 3. Start ny server ------------------------------------------------------

command -v python3 > /dev/null 2>&1 || { feil "Fant ikke python3"; exit 1; }

if [ "$FORGRUNN" -eq 1 ]; then
  ok "Starter i forgrunnen, versjon $VERSJON. Avslutt med ctrl-C."
  exec python3 "$MAPPE/server.py" --port "$PORT"
fi

: > "$LOGG"
nohup python3 "$MAPPE/server.py" --port "$PORT" >> "$LOGG" 2>&1 &
NY_PID=$!
echo "$NY_PID" > "$PIDFIL"

# --- 4. Vent til den svarer, og vis hva som faktisk kjører -------------------

URL="http://127.0.0.1:$PORT"
SVAR=""
for i in $(seq 1 40); do
  sleep 0.25
  if ! kill -0 "$NY_PID" 2>/dev/null; then
    feil "Serveren stoppet med én gang. Siste linjer fra $LOGG:"
    tail -n 15 "$LOGG" >&2
    rm -f "$PIDFIL"
    exit 1
  fi
  SVAR="$(curl -s --max-time 2 "$URL/helse" 2>/dev/null)"
  [ -n "$SVAR" ] && break
done

if [ -z "$SVAR" ]; then
  feil "Serveren svarer ikke på $URL/helse. Siste linjer fra $LOGG:"
  tail -n 15 "$LOGG" >&2
  exit 1
fi

ok "Ny server svarer på $URL (pid $NY_PID, versjon $VERSJON)"

python3 - "$SVAR" "$URL" <<'PY'
import json, sys, urllib.request
svar, url = sys.argv[1], sys.argv[2]
try:
    d = json.loads(svar)
except ValueError:
    print("  Uventet svar fra /helse:", svar[:200]); sys.exit(0)

print(f"  modell:    {d.get('modell')}")
print(f"  inferens:  {d.get('inferens_url')}")
print(f"  whisper:   {(d.get('whisper_url') or '')[:60]}...")
print(f"  ffmpeg:    {'ja' if d.get('ffmpeg') else 'NEI, installer ffmpeg'}")

# Sjekk at rutene den nye koden trenger faktisk finnes.
mangler = []
for rute in ("/modeller",):
    try:
        with urllib.request.urlopen(url + rute, timeout=5) as r:
            if r.status != 200:
                mangler.append(rute)
    except Exception:
        mangler.append(rute)
if mangler:
    print(f"  \033[0;31m✗\033[0m Ruta {', '.join(mangler)} mangler. Kjører du gammel kode?")
    sys.exit(0)

try:
    with urllib.request.urlopen(url + "/modeller", timeout=30) as r:
        m = json.loads(r.read())
except Exception as e:
    print(f"  modeller:  kunne ikke hentes ({e})"); sys.exit(0)
liste = m.get("modeller") or []
if liste:
    syn = sum(1 for x in liste if x.get("syn"))
    print(f"  modeller:  {len(liste)} tilgjengelig, {syn} kan se bilder")
else:
    print(f"  \033[0;31m✗\033[0m modeller: ingen. {m.get('hint','')}")
PY

echo
echo "  Logg:   tail -f \"$LOGG\""
echo "  Stopp:  \"$MAPPE/start.sh\" --stopp"
