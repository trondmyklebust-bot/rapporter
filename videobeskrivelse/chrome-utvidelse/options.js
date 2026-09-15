const STANDARD = { serverUrl: "http://127.0.0.1:8765", antall: 8, transkriber: true };
const felt = (id) => document.getElementById(id);

chrome.storage.sync.get(STANDARD, (v) => {
  felt("serverUrl").value = v.serverUrl;
  felt("antall").value = v.antall;
  felt("transkriber").checked = !!v.transkriber;
});

felt("lagre").addEventListener("click", () => {
  const v = {
    serverUrl: felt("serverUrl").value.trim().replace(/\/+$/, "") || STANDARD.serverUrl,
    antall: Math.max(1, Math.min(60, Number(felt("antall").value) || STANDARD.antall)),
    transkriber: felt("transkriber").checked,
  };
  chrome.storage.sync.set(v, () => {
    felt("status").textContent = "Lagret";
    setTimeout(() => (felt("status").textContent = ""), 1500);
  });
});
