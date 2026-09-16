// Legger en knapp og et panel på nb.no-sider med video. Ved klikk hentes
// strømadressen fra bakgrunnsskriptet og sendes til den lokale serveren
// (server.py), som henter bilder og lyd, beskriver med gemma4 og
// transkriberer med NB-Whisper. Panelet poller status og viser resultatet.

const STANDARD_SERVER = "http://127.0.0.1:8765";
const POLL_MS = 2000;

let serverUrl = STANDARD_SERVER;
let innstillinger = { antall: 8, transkriber: true, bitSekunder: 20 };
let aktivJobb = null;
let pollTimer = null;

// ---------- hjelpere ----------

function el(tag, attrs = {}, ...barn) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const b of barn) if (b != null) e.append(b);
  return e;
}

function tid(sek) {
  sek = Math.floor(Number(sek) || 0);
  const m = Math.floor(sek / 60), s = sek % 60, h = Math.floor(m / 60);
  const mm = String(m % 60).padStart(2, "0"), ss = String(s).padStart(2, "0");
  return h ? `${String(h).padStart(2, "0")}:${mm}:${ss}` : `${mm}:${ss}`;
}

function urnFraSide() {
  const m = location.href.match(/URN:NBN:no-nb_[A-Za-z0-9_]+/);
  return m ? m[0] : null;
}

function tittelFraSide() {
  const h = document.querySelector("h1");
  return (h && h.textContent.trim()) || document.title;
}

async function hentInnstillinger() {
  try {
    const l = await chrome.storage.sync.get({ serverUrl: STANDARD_SERVER, antall: 8, transkriber: true, bitSekunder: 20 });
    serverUrl = l.serverUrl || STANDARD_SERVER;
    innstillinger = { antall: l.antall, transkriber: l.transkriber, bitSekunder: l.bitSekunder };
  } catch (_) { /* bruk standard */ }
}

function hentStromFraBakgrunn() {
  return new Promise((resolve) => {
    try {
      chrome.runtime.sendMessage({ type: "hentStrom" }, (svar) => {
        if (chrome.runtime.lastError) resolve(null); else resolve(svar);
      });
    } catch (_) { resolve(null); }
  });
}

// Reserve: <video src> hvis spilleren bruker en direkte fil-URL (ikke blob:).
function stromFraVideoElement() {
  for (const v of document.querySelectorAll("video")) {
    const src = v.currentSrc || v.src;
    if (src && /^https?:/.test(src)) return { url: src, type: "fil" };
    for (const s of v.querySelectorAll("source")) {
      if (s.src && /^https?:/.test(s.src)) return { url: s.src, type: "fil" };
    }
  }
  return null;
}

// ---------- panel ----------

let panel, statusEl, feilEl, resultatEl, loggEl, startKnapp, antallInput, transkriberCb, bitInput;

function byggPanel() {
  if (panel) return;
  panel = el("div", { id: "nbvb-panel", hidden: "" });
  const header = el("header", {},
    el("strong", { text: "NB videobeskrivelse" }),
    el("button", { class: "nbvb-lukk", title: "Lukk", text: "×", onclick: () => (panel.hidden = true) })
  );
  antallInput = el("input", { type: "number", min: "1", max: "60", value: String(innstillinger.antall) });
  transkriberCb = el("input", { type: "checkbox" });
  transkriberCb.checked = !!innstillinger.transkriber;
  startKnapp = el("button", { text: "Beskriv denne videoen", onclick: start });
  bitInput = el("input", { type: "number", min: "0", max: "600", value: String(innstillinger.bitSekunder),
                           title: "Sekunder per lydbit til Whisper. 0 sender hele lydsporet i én forespørsel." });
  const rad = el("div", { class: "nbvb-rad" },
    startKnapp,
    el("label", {}, el("span", { text: "Bilder" }), antallInput),
    el("label", {}, transkriberCb, el("span", { text: "Whisper" })),
    el("label", { title: "Sekunder per lydbit. 0 = hele lydsporet i ett." }, el("span", { text: "Bit s" }), bitInput)
  );
  statusEl = el("div", { class: "nbvb-status", text: "Klar." });
  feilEl = el("div", { class: "nbvb-feil", hidden: "" });
  resultatEl = el("div");
  loggEl = el("div", { class: "nbvb-logg", hidden: "" });
  const innhold = el("div", { class: "nbvb-innhold" }, statusEl, feilEl, resultatEl, loggEl);
  panel.append(header, rad, innhold);
  document.documentElement.append(panel);
}

function visFeil(tekst) {
  feilEl.textContent = tekst;
  feilEl.hidden = !tekst;
}

function settStatus(tekst) { statusEl.textContent = tekst; }

function visResultat(jobb) {
  resultatEl.replaceChildren();
  const r = jobb.resultat;
  if (!r) return;

  const verktoy = el("div", { class: "nbvb-rad" },
    el("span", { text: `Varighet ${tid(r.varighet_sekunder)} · ${r.modell}` }),
    el("button", { class: "nbvb-kopier", text: "Kopier beskrivelse", onclick: () => kopier(r.beskrivelse) }),
    el("button", { text: "Last ned JSON", onclick: () => lastNed(jobb) })
  );
  resultatEl.append(verktoy);

  resultatEl.append(el("h4", { text: "Beskrivelse" }), el("p", { class: "nbvb-tekst", text: r.beskrivelse || "(tom)" }));

  if (r.transkripsjon) {
    const biter = r.transkripsjon.antall_biter;
    resultatEl.append(el("h4", { text: biter > 1 ? `Transkripsjon (NB-Whisper, ${biter} biter)` : "Transkripsjon (NB-Whisper)" }));
    const seg = (r.transkripsjon.segmenter || []).filter((s) => s.tekst);
    if (seg.length) {
      for (const s of seg) {
        resultatEl.append(el("div", { class: "nbvb-seg" },
          el("span", { class: "t", text: s.start != null ? tid(s.start) : "" }),
          s.taler ? el("span", { class: "s", text: s.taler }) : null,
          el("span", { text: s.tekst })
        ));
      }
    } else {
      resultatEl.append(el("p", { class: "nbvb-tekst", text: r.transkripsjon.tekst || "(tom)" }));
    }
  } else if (r.transkripsjon_feil) {
    resultatEl.append(el("h4", { text: "Transkripsjon" }), el("p", { class: "nbvb-tekst", text: "Feilet: " + r.transkripsjon_feil }));
  }

  if (r.samtolk) {
    resultatEl.append(el("p", { class: "nbvb-status", text: "Bildene og talen ble tolket sammen i samme modellkall." }));
  }
  if (r.bildebeskrivelse && r.bildebeskrivelse !== r.beskrivelse) {
    resultatEl.append(el("h4", { text: "Bare bildene" }), el("p", { class: "nbvb-tekst", text: r.bildebeskrivelse }));
  }
}

function visLogg(jobb) {
  const linjer = (jobb.logg || []).slice(-30).map((l) => `${l.tid.toFixed(0).padStart(4)} s  ${l.melding}`);
  loggEl.textContent = linjer.join("\n");
  loggEl.hidden = linjer.length === 0;
}

async function kopier(tekst) {
  try { await navigator.clipboard.writeText(tekst || ""); settStatus("Kopiert til utklippstavla."); }
  catch (_) { settStatus("Kunne ikke kopiere."); }
}

function lastNed(jobb) {
  const blob = new Blob([JSON.stringify(jobb, null, 2)], { type: "application/json" });
  const a = el("a", { href: URL.createObjectURL(blob), download: `${jobb.urn || "video"}-beskrivelse.json` });
  document.body.append(a); a.click(); a.remove();
}

// ---------- kjøring ----------

async function start() {
  visFeil("");
  resultatEl.replaceChildren();
  loggEl.hidden = true;

  // 1. Sjekk at den lokale serveren kjører.
  let helse;
  try {
    helse = await (await fetch(serverUrl + "/helse")).json();
  } catch (_) {
    visFeil(`Får ikke kontakt med den lokale serveren på ${serverUrl}. Start den med «python3 server.py» i mappa videobeskrivelse.`);
    return;
  }
  if (helse.ffmpeg === false) {
    visFeil("Serveren kjører, men finner ikke ffmpeg. Installer ffmpeg og start serveren på nytt.");
    return;
  }

  // 2. Finn strømadressen.
  let strom = await hentStromFraBakgrunn();
  if (!strom) strom = stromFraVideoElement();
  if (!strom) {
    visFeil("Fant ingen videostrøm ennå. Trykk play på videoen, vent et par sekunder og prøv igjen.");
    return;
  }

  // 3. Start jobb.
  const antall = Math.max(1, Math.min(60, Number(antallInput.value) || 8));
  const bitSekunder = Math.max(0, Math.min(600, Number(bitInput.value) || 0));
  const body = {
    kilde: strom.url,
    referer: location.href,
    user_agent: navigator.userAgent,
    urn: urnFraSide(),
    tittel: tittelFraSide(),
    antall,
    transkriber: transkriberCb.checked,
    bit_sekunder: bitSekunder,
    samtolk: true,
  };
  try { chrome.storage.sync.set({ antall, transkriber: transkriberCb.checked, bitSekunder }); } catch (_) {}

  startKnapp.disabled = true;
  settStatus(`Sender ${strom.type === "master" ? "HLS-spillelista" : "strømmen"} til serveren …\n${strom.url}`);
  let svar;
  try {
    const res = await fetch(serverUrl + "/jobb", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    svar = await res.json();
    if (!res.ok) throw new Error(svar.feil || res.statusText);
  } catch (e) {
    visFeil("Klarte ikke starte jobben: " + e.message);
    startKnapp.disabled = false;
    return;
  }
  aktivJobb = svar.id;
  poll();
}

async function poll() {
  clearTimeout(pollTimer);
  if (!aktivJobb) return;
  let jobb;
  try {
    jobb = await (await fetch(`${serverUrl}/jobb/${aktivJobb}`)).json();
  } catch (_) {
    settStatus("Mistet kontakt med serveren, prøver igjen …");
    pollTimer = setTimeout(poll, POLL_MS * 2);
    return;
  }
  visLogg(jobb);
  const siste = jobb.logg?.length ? jobb.logg[jobb.logg.length - 1].melding : "";
  if (jobb.status === "kjører") {
    settStatus("Arbeider … " + siste);
    pollTimer = setTimeout(poll, POLL_MS);
    return;
  }
  startKnapp.disabled = false;
  aktivJobb = null;
  if (jobb.status === "feil") {
    settStatus("Feilet.");
    visFeil(jobb.feil || "Ukjent feil");
  } else {
    settStatus("Ferdig.");
    visResultat(jobb);
  }
}

// ---------- oppstart ----------

(async function init() {
  await hentInnstillinger();
  const knapp = el("button", { id: "nbvb-knapp", text: "Beskriv video", onclick: () => {
    byggPanel();
    panel.hidden = !panel.hidden;
  }});
  document.documentElement.append(knapp);
})();
