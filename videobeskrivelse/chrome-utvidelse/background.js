// Fanger opp adressen til videostrømmen (HLS-spilleliste fra Wowza, eller mp4)
// per fane, slik at innholdsskriptet kan sende den til den lokale serveren.
//
// Wowza leverer HLS som .../playlist.m3u8 (master) og .../chunklist_*.m3u8
// (varianter). Vi foretrekker master-spillelista, men tar vare på det siste
// av alt som ser ut som en strøm.

const STROMMER = new Map(); // tabId → { url, tid, type }

function erStrom(url) {
  const u = url.toLowerCase();
  return u.includes(".m3u8") || u.includes(".mpd") || /\.mp4(\?|$)/.test(u);
}

function erMaster(url) {
  const u = url.toLowerCase();
  return u.includes("playlist.m3u8") || u.includes("master.m3u8") || u.includes(".mpd");
}

function erVariant(url) {
  return url.toLowerCase().includes("chunklist");
}

async function lagre(tabId, url) {
  const gammel = STROMMER.get(tabId);
  // Ikke la en chunklist overskrive en master-spilleliste vi allerede har.
  if (gammel && erMaster(gammel.url) && erVariant(url)) return;
  const post = { url, tid: Date.now(), type: erMaster(url) ? "master" : erVariant(url) ? "variant" : "fil" };
  STROMMER.set(tabId, post);
  try {
    await chrome.storage.session.set({ ["strom_" + tabId]: post });
  } catch (_) { /* session-lagring kan mangle i eldre Chrome */ }
}

chrome.webRequest.onBeforeRequest.addListener(
  (detaljer) => {
    if (detaljer.tabId < 0 || !erStrom(detaljer.url)) return;
    lagre(detaljer.tabId, detaljer.url);
  },
  { urls: ["<all_urls>"], types: ["xmlhttprequest", "media", "other"] }
);

chrome.tabs.onRemoved.addListener((tabId) => {
  STROMMER.delete(tabId);
  chrome.storage.session.remove("strom_" + tabId).catch(() => {});
});

// Ved navigasjon til en ny side i samme fane glemmer vi gammel strøm.
chrome.webNavigation?.onCommitted?.addListener?.((d) => {
  if (d.frameId === 0) STROMMER.delete(d.tabId);
});

chrome.runtime.onMessage.addListener((melding, avsender, svar) => {
  if (melding?.type !== "hentStrom") return false;
  const tabId = avsender.tab?.id;
  (async () => {
    let post = STROMMER.get(tabId);
    if (!post) {
      try {
        const lagret = await chrome.storage.session.get("strom_" + tabId);
        post = lagret["strom_" + tabId];
      } catch (_) { /* ignorer */ }
    }
    svar(post || null);
  })();
  return true; // asynkront svar
});
