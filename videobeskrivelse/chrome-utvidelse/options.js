const STANDARD = { serverUrl: "http://127.0.0.1:8170", antall: 8, transkriber: true, bitSekunder: 20, musikkfilter: false };
const felt = (id) => document.getElementById(id);

chrome.storage.sync.get(STANDARD, (v) => {
  felt("serverUrl").value = v.serverUrl;
  felt("antall").value = v.antall;
  felt("transkriber").checked = !!v.transkriber;
  felt("bitSekunder").value = v.bitSekunder;
  felt("musikkfilter").checked = !!v.musikkfilter;
});

felt("lagre").addEventListener("click", () => {
  const v = {
    serverUrl: felt("serverUrl").value.trim().replace(/\/+$/, "") || STANDARD.serverUrl,
    antall: Math.max(1, Math.min(60, Number(felt("antall").value) || STANDARD.antall)),
    transkriber: felt("transkriber").checked,
    bitSekunder: Math.max(0, Math.min(600, Number(felt("bitSekunder").value) || 0)),
    musikkfilter: felt("musikkfilter").checked,
  };
  chrome.storage.sync.set(v, () => {
    felt("status").textContent = "Lagret";
    setTimeout(() => (felt("status").textContent = ""), 1500);
  });
});
