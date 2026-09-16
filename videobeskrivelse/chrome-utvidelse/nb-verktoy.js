/**
 * nb-verktoy — felles dokk for utvidelsene som legger seg på nb.no.
 *
 * Bakgrunnen: åtte utvidelser tegnet hver sin knapp i hvert sitt hjørne, og
 * hjørnene tok slutt. Her er avtalen i stedet: et verktøy eier aldri en
 * plassering. Det melder seg inn med navn, farge og nivå, og dokken bestemmer
 * hvor raden havner. Da kan ikke to utvidelser ta samme plass — heller ikke de
 * som ennå ikke er skrevet.
 *
 * ---------------------------------------------------------------------------
 * Det som styrer hele designet: utvidelser deler DOM, men IKKE JS-globaler.
 * Hver av dem kjører i sin egen isolerte verden, så `NBVerktoy` nederst er ikke
 * ett objekt de deler — det er én kopi per utvidelse. All samordning må derfor
 * gå gjennom det de faktisk deler: DOM-en på sida.
 *
 *   Registeret   <nb-verktoy-register> med én <nb-verktoy-post> per verktøy.
 *                Hver verden skriver sine egne poster og leser alles.
 *   Verten       Den første som laster bygger <nb-verktoy-dokk> og tegner
 *                menyene. De andre nøyer seg med å registrere seg.
 *   Meldinger    CustomEvent på document. `detail` er alltid en JSON-STRENG,
 *                aldri et objekt: objekter krysser ikke isolerte verdener
 *                pålitelig, primitiver gjør det.
 *   Puls         Hver verden stempler postene sine hvert 5. sekund. Verten
 *                fjerner poster som er blitt kalde (utvidelsen er skrudd av
 *                eller lastet på nytt), og en annen tar over vertsrollen om
 *                dokken slutter å slå.
 * ---------------------------------------------------------------------------
 *
 * Brukes slik, i utvidelsens eget content script:
 *
 *   NBVerktoy.registrer({
 *     id: 'katalogisering',
 *     navn: 'Katalogisering',
 *     hint: 'Send objektet til NB Katalogisering',
 *     farge: '#550029',
 *     nivaa: 'objekt',            // objekt | sok | side
 *     rang: 10,                   // fast rekkefølge, uavhengig av lasterekkefølge
 *     gjelder: (ktx) => ktx.type === 'objekt',   // gjelder RADEN I DOKKEN
 *     paaObjekt: (ktx) => send(ktx.itemId),      // klikk i dokken
 *     paaKort:   (ktx) => send(ktx.itemId),      // klikk i kortmenyen (valgfri)
 *     kortNaar:  'digibok_|digifoto_',           // valgfritt filter, se under
 *   });
 *
 * `gjelder` styrer bare dokkraden. Har verktøyet en `paaKort`, får det rad i
 * kortmenyen overalt hvor det finnes treffkort — også der `gjelder` er usann,
 * som på trefflista.
 *
 * `kortNaar` er et regulæruttrykk som TEKST (funksjoner krysser ikke isolerte
 * verdener — verten som tegner menyen er gjerne en annen utvidelse). Verten
 * prøver det mot URN-en til kortet og tar bort raden hvis den ikke treffer.
 * Kortlenka gir bare en sesam-id, så URN-en hentes fra miniatyren; finner
 * verten ingen URN, blir raden stående — vi skjuler bare når vi vet.
 *
 * Fila kopieres uendret inn i hver utvidelse (se synk.sh). Rett den her, ikke
 * i kopiene.
 */

(() => {
  'use strict';

  if (globalThis.NBVerktoy) return;   // allerede lastet i denne verdenen

  const VERSJON = 2;

  const PULS_MS = 5000;        // hvor ofte en verden stempler postene sine
  const KALD_MS = 16000;       // uten stempel så lenge regnes en post som død
  const VERT_KALD_MS = 16000;  // ... og dokken som forlatt, klar for ny vert

  /* Nivåene er også rekkefølgen i dokken. */
  const NIVAA = {
    objekt: { rang: 0, tittel: 'Dette objektet' },
    sok:    { rang: 1, tittel: 'Hele søket' },
    side:   { rang: 2, tittel: 'Denne sida' },
  };

  /* Alt tegnes i shadow DOM. nb.no har egne stilark som ellers smitter inn
     (og våre ut), og med ett delt, konstruerbart stilark slipper vi å legge
     CSS-tekst inn i hvert eneste treffkort.
     NB: konstanten heter STIL, ikke CSS — `CSS` er nettleserens eget objekt,
     og vi bruker CSS.escape lenger nede. */
  const STIL = `
    :host { all: initial; display: contents; }
    * { box-sizing: border-box; }

    .lag {
      position: fixed;
      inset: 0;
      z-index: 2147483000;
      pointer-events: none;
      font-family: "STK Bureau Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
      color-scheme: light;
    }
    .lag > * { pointer-events: auto; }

    /* ------------------------------------------------------------- dokken */
    .dokk {
      position: absolute;
      right: 20px;
      bottom: 20px;
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 8px;
    }
    /* På objektsider ligger nb.no sin sidenavigering langs bunnen. */
    .dokk[data-loft="1"] { bottom: 88px; }

    .dokk-knapp {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 10px 16px;
      border: 0;
      border-radius: 999px;
      background: #550029;
      color: #fff;
      font: 500 14px/1.2 inherit;
      cursor: pointer;
      box-shadow: 0 3px 14px rgba(0, 0, 0, .32);
    }
    .dokk-knapp:hover { background: #6b0034; }
    .dokk-knapp:focus-visible { outline: 3px solid #fe91b8; outline-offset: 2px; }

    .antall {
      display: grid;
      place-items: center;
      min-width: 19px;
      height: 19px;
      padding: 0 5px;
      border-radius: 999px;
      background: rgba(255, 255, 255, .22);
      font: 500 12px/1 ui-monospace, monospace;
      font-variant-numeric: tabular-nums;
    }

    /* --------------------------------------------------------------- meny */
    .meny {
      min-width: 232px;
      max-width: min(300px, calc(100vw - 40px));
      max-height: min(70vh, 560px);
      overflow-y: auto;
      background: #fff;
      color: #17181b;
      border: 1px solid #d4d4d4;
      border-radius: 10px;
      box-shadow: 0 10px 32px rgba(0, 0, 0, .28);
      padding: 4px 0;
    }
    .meny[hidden] { display: none; }

    .meny-tittel {
      padding: 9px 14px 5px;
      font: 500 10px/1 ui-monospace, monospace;
      text-transform: uppercase;
      letter-spacing: .1em;
      color: #5b5c62;
    }
    .meny-tittel + .rad { border-top: 0; }

    .rad {
      display: flex;
      align-items: flex-start;
      gap: 9px;
      width: 100%;
      padding: 9px 14px;
      border: 0;
      border-top: 1px solid #f0eeee;
      background: none;
      text-align: left;
      font: 400 14px/1.35 inherit;
      color: inherit;
      cursor: pointer;
    }
    .rad:hover { background: #f4f4f4; }
    .rad:focus-visible { outline: 2px solid #550029; outline-offset: -2px; }
    .rad[aria-busy="true"] { opacity: .6; cursor: progress; }

    .prikk {
      flex: none;
      width: 9px;
      height: 9px;
      margin-top: 5px;
      border-radius: 50%;
      background: #17181b;
    }
    .rad[aria-busy="true"] .prikk { animation: puls 1s ease-in-out infinite; }
    @keyframes puls { 50% { opacity: .25; } }
    @media (prefers-reduced-motion: reduce) {
      .rad[aria-busy="true"] .prikk { animation: none; }
    }

    .navn { display: block; font-weight: 500; }
    .hint { display: block; margin-top: 1px; font-size: 12px; line-height: 1.35; color: #5b5c62; }

    /* ---------------------------------------------------------- kortknapp */
    .kortknapp {
      position: absolute;
      top: 8px;
      right: 8px;
      width: 26px;
      height: 26px;
      display: grid;
      place-items: center;
      border: 0;
      border-radius: 50%;
      background: rgba(23, 24, 27, .78);
      color: #fff;
      font: 600 14px/1 inherit;
      cursor: pointer;
      box-shadow: 0 1px 6px rgba(0, 0, 0, .38);
    }
    .kortknapp:hover { background: #550029; }
    .kortknapp:focus-visible { outline: 3px solid #fe91b8; outline-offset: 2px; }

    /* Kortmenyen tegnes i overleggslaget, ikke inni kortet: kortet klipper. */
    .kortmeny { position: absolute; }

    /* -------------------------------------------------------------- varsel */
    .varsel {
      position: absolute;
      left: 50%;
      bottom: 24px;
      transform: translateX(-50%);
      max-width: min(560px, calc(100vw - 32px));
      padding: 12px 18px;
      border-radius: 8px;
      background: #17181b;
      color: #fff;
      font: 400 14px/1.45 inherit;
      box-shadow: 0 4px 20px rgba(0, 0, 0, .4);
    }
    .varsel[data-feil="1"] { background: #8b1a1a; }
  `;

  const ARK = new CSSStyleSheet();
  ARK.replaceSync(STIL);

  /* ====================================================== kontekst =========
     Hvor er vi, og hvilket objekt gjelder det? Ett oppslag, delt av alle —
     i dag gjør fem utvidelser hver sin variant av akkurat dette. */

  /* nb.no bruker to id-former i /items/-URL-er:
       /items/38330b20f94519d7d472d5afbab6dea8        (sesamid)
       /items/URN:NBN:no-nb_digibok_2011072508049     (URN, med kolon)
     Vi tar derfor hele stisegmentet framfor å anta et tegnsett. */
  const ITEM_STI = /^\/items\/([^/?#]{8,})/;

  function itemIdFraSti(sti) {
    const treff = ITEM_STI.exec(sti || '');
    if (!treff) return null;
    try {
      return decodeURIComponent(treff[1]);
    } catch (_) {
      return treff[1];
    }
  }

  function itemIdFraLenke(href) {
    if (!href) return null;
    const sti = href.startsWith('http') ? new URL(href).pathname : href.split('?')[0];
    return itemIdFraSti(sti);
  }

  function kontekst() {
    const sti = location.pathname;
    const itemId = itemIdFraSti(sti);
    if (itemId) return { type: 'objekt', itemId, sti, url: location.href };
    // Treffkort dukker også opp utenfor /search (samlinger, temasider), så
    // vi spør DOM-en i tillegg til å se på adressa.
    const harKort = /^\/search/.test(sti) || !!document.querySelector('nb-item-card');
    return { type: harKort ? 'sok' : 'side', itemId: null, sti, url: location.href };
  }

  /* ====================================================== meldinger ========
     detail er alltid en JSON-streng — se toppkommentaren. */

  function meld(navn, data) {
    document.dispatchEvent(new CustomEvent(navn, { detail: JSON.stringify(data || {}) }));
  }

  function lytt(navn, fn) {
    document.addEventListener(navn, (h) => {
      let data;
      try {
        data = JSON.parse(typeof h.detail === 'string' ? h.detail : '{}');
      } catch (_) {
        return;
      }
      fn(data);
    });
  }

  /* ====================================================== registeret =======
     Én <nb-verktoy-post> per verktøy, med JSON i data-post. Verten leser
     alles, hver verden skriver bare sine egne. */

  function register() {
    let el = document.querySelector('nb-verktoy-register');
    if (!el) {
      el = document.createElement('nb-verktoy-register');
      el.hidden = true;
      el.style.display = 'none';
      document.documentElement.appendChild(el);
    }
    return el;
  }

  /** Mine egne verktøy i denne verdenen: id -> definisjon (med funksjoner). */
  const mine = new Map();

  function skrivPost(def, aktiv) {
    const reg = register();
    let post = reg.querySelector(`nb-verktoy-post[data-id="${CSS.escape(def.id)}"]`);
    if (!post) {
      post = document.createElement('nb-verktoy-post');
      post.dataset.id = def.id;
      reg.appendChild(post);
    }
    post.dataset.post = JSON.stringify({
      versjon: VERSJON,
      id: def.id,
      navn: def.navn,
      hint: def.hint || '',
      farge: def.farge || '#17181b',
      nivaa: NIVAA[def.nivaa] ? def.nivaa : 'side',
      rang: Number.isFinite(def.rang) ? def.rang : 500,
      // aktiv = rad i dokken her og nå. kort = rad i kortmenyen, uavhengig
      // av `gjelder`: på trefflista er dokkraden gjerne av mens kortraden er på.
      aktiv: !!aktiv,
      kort: typeof def.paaKort === 'function',
      // Regulæruttrykk som tekst — se toppkommentaren.
      kortNaar: typeof def.kortNaar === 'string' ? def.kortNaar : '',
    });
    post.dataset.puls = String(Date.now());
  }

  /** Les hele registeret, kast kalde poster, sorter. Kalles av verten. */
  function lesRegister() {
    const naa = Date.now();
    const poster = [];
    register().querySelectorAll('nb-verktoy-post').forEach((post) => {
      const stempel = Number(post.dataset.puls || 0);
      if (naa - stempel > KALD_MS) {
        // Utvidelsen er skrudd av eller lastet på nytt; posten er foreldreløs.
        post.remove();
        return;
      }
      try {
        poster.push(JSON.parse(post.dataset.post || '{}'));
      } catch (_) { /* ødelagt post — hopp over den */ }
    });

    poster.sort((a, b) =>
      (NIVAA[a.nivaa]?.rang ?? 9) - (NIVAA[b.nivaa]?.rang ?? 9)
      || a.rang - b.rang
      || String(a.navn).localeCompare(String(b.navn), 'no'));

    return poster;
  }

  /* ====================================================== verten ===========
     Bygger dokken, kortknappene og menyene. Bare én verden gjør dette. */

  let vert = null;      // { el, rot, lag, dokk, knapp, meny, ... } når vi er vert
  let lyttere = false;  // document/window-lytterne er satt opp i denne verdenen

  function dokkErForlatt() {
    const el = document.querySelector('nb-verktoy-dokk');
    if (!el) return true;
    return Date.now() - Number(el.dataset.puls || 0) > VERT_KALD_MS;
  }

  /* Utvidelsene deles ut enkeltvis, så folk kan ha ulike utgaver av denne fila
     installert samtidig. Da skal den nyeste tegne — ellers ville en gammel dokk
     ikke kjent til det en nyere post ber om. */
  function dokkErEldre() {
    const el = document.querySelector('nb-verktoy-dokk');
    return !!el && Number(el.dataset.versjon || 0) < VERSJON;
  }

  function skygge(vertselement) {
    const rot = vertselement.attachShadow({ mode: 'open' });
    rot.adoptedStyleSheets = [ARK];
    return rot;
  }

  function bliVert() {
    // Er dokken der, men kald, har utvidelsen som eide den blitt skrudd av.
    document.querySelectorAll('nb-verktoy-dokk').forEach((el) => el.remove());

    const el = document.createElement('nb-verktoy-dokk');
    el.dataset.versjon = String(VERSJON);
    el.dataset.puls = String(Date.now());
    document.documentElement.appendChild(el);

    // To content scripts kan i teorien komme hit i samme runde. Chrome kjører
    // dem én om gangen på hovedtråden, så sjekken over holder — men taper vi
    // et kappløp likevel, viker den som ikke står først i DOM-en.
    const alle = document.querySelectorAll('nb-verktoy-dokk');
    if (alle.length > 1 && alle[0] !== el) {
      el.remove();
      return;
    }

    /* Kortknappene fra en tidligere vert henger i en skygge som ikke lenger
       er koblet til sida — klikk på dem ville falt død ned. Løs merket, så
       tegner vi dem opp igjen som våre egne. */
    document.querySelectorAll(`nb-item-card[${KORT_MERKE}]`).forEach((kort) => {
      kort.querySelectorAll('nb-verktoy-kort').forEach((holder) => holder.remove());
      kort.removeAttribute(KORT_MERKE);
    });

    const rot = skygge(el);
    const lag = document.createElement('div');
    lag.className = 'lag';
    rot.appendChild(lag);

    const dokk = document.createElement('div');
    dokk.className = 'dokk';

    const meny = document.createElement('div');
    meny.className = 'meny';
    meny.hidden = true;
    meny.setAttribute('role', 'menu');

    const knapp = document.createElement('button');
    knapp.type = 'button';
    knapp.className = 'dokk-knapp';
    knapp.setAttribute('aria-haspopup', 'menu');
    knapp.setAttribute('aria-expanded', 'false');
    knapp.addEventListener('click', () => visDokkmeny(meny.hidden));

    dokk.append(meny, knapp);
    lag.appendChild(dokk);

    vert = { el, rot, lag, dokk, knapp, meny, kortmeny: null, varsel: null, signatur: null };

    /* Lytterne henger på document og window, ikke på dokken, så de settes
       opp én gang per verden — vi kan miste og ta tilbake vertsrollen. */
    if (lyttere) return void tegn();
    lyttere = true;

    /* Klikk utenfor lukker menyene. Hendelser fra shadow DOM peker på
       vertselementet når de ses herfra, så egne klikk kjenner vi igjen på
       taggnavnet — uten dette ville capture-fasen rive bort raden før
       klikket rakk fram til den. */
    document.addEventListener('click', (h) => {
      const tagg = h.target?.tagName;
      if (tagg === 'NB-VERKTOY-DOKK' || tagg === 'NB-VERKTOY-KORT') return;
      lukkMenyer();
    }, true);

    // Kortmenyen henger sammen med et kort som virtual scroll kan gjenbruke.
    window.addEventListener('scroll', lukkKortmeny, true);
    window.addEventListener('resize', lukkKortmeny);
    document.addEventListener('keydown', (h) => {
      if (h.key === 'Escape') lukkMenyer();
    });

    tegn();
  }

  function visDokkmeny(aapne) {
    if (!vert) return;
    lukkKortmeny();
    vert.meny.hidden = !aapne;
    vert.knapp.setAttribute('aria-expanded', String(!!aapne));
    if (aapne) vert.meny.querySelector('.rad')?.focus();
  }

  function lukkKortmeny() {
    if (vert?.kortmeny) {
      vert.kortmeny.remove();
      vert.kortmeny = null;
    }
  }

  function lukkMenyer() {
    lukkKortmeny();
    if (vert && !vert.meny.hidden) visDokkmeny(false);
  }

  /**
   * @param finnKtx  Funksjon, ikke verdi. Virtual scroll gjenbruker kortene
   *   til andre objekter, så konteksten må leses i det brukeren trykker.
   */
  function lagRad(post, finnKtx, kilde) {
    const rad = document.createElement('button');
    rad.type = 'button';
    rad.className = 'rad';
    rad.dataset.id = post.id;
    rad.setAttribute('role', 'menuitem');

    const prikk = document.createElement('span');
    prikk.className = 'prikk';
    prikk.style.background = post.farge;

    const tekst = document.createElement('span');
    const navn = document.createElement('span');
    navn.className = 'navn';
    navn.textContent = post.navn;
    tekst.appendChild(navn);
    if (post.hint) {
      const hint = document.createElement('span');
      hint.className = 'hint';
      hint.textContent = post.hint;
      tekst.appendChild(hint);
    }

    rad.append(prikk, tekst);
    rad.addEventListener('click', (h) => {
      h.preventDefault();
      h.stopPropagation();
      lukkMenyer();
      meld('nbv:kjor', { id: post.id, kilde, ktx: finnKtx() });
    });
    return rad;
  }

  /** Tegner dokken på nytt ut fra registeret. */
  function tegn() {
    if (!vert) return;
    const ktx = kontekst();
    const poster = lesRegister().filter((p) => p.aktiv);

    vert.el.dataset.puls = String(Date.now());
    vert.dokk.dataset.loft = ktx.type === 'objekt' ? '1' : '0';

    // nb.no endrer DOM-en støtt. Uten denne sperren ville menyen blitt bygget
    // på nytt midt i at noen leser den — og mistet både rulling og fokus.
    const signatur = `${ktx.type}|${poster.map((p) => p.id).join(',')}`;
    if (signatur === vert.signatur) return;
    vert.signatur = signatur;

    // Ingen verktøy passer på denne sida — da skal det heller ikke stå noe.
    vert.dokk.style.display = poster.length ? '' : 'none';
    if (!poster.length) {
      visDokkmeny(false);
      return;
    }

    vert.knapp.textContent = 'NB-verktøy ';
    const antall = document.createElement('span');
    antall.className = 'antall';
    antall.textContent = String(poster.length);
    vert.knapp.appendChild(antall);
    vert.knapp.title = poster.map((p) => p.navn).join(', ');

    vert.meny.textContent = '';
    let forrigeNivaa = null;
    poster.forEach((post) => {
      if (post.nivaa !== forrigeNivaa) {
        forrigeNivaa = post.nivaa;
        const tittel = document.createElement('div');
        tittel.className = 'meny-tittel';
        tittel.textContent = NIVAA[post.nivaa]?.tittel || '';
        vert.meny.appendChild(tittel);
      }
      vert.meny.appendChild(lagRad(post, () => kontekst(), 'dokk'));
    });
  }

  /* ------------------------------------------------------------ kortknapp */

  const KORT_MERKE = 'data-nbv-kort';

  function tegnKortknapper() {
    if (!vert) return;
    // `:not([MERKE])` lar CSS-motoren filtrere bort kortene vi alt har vært
    // innom, så vi itererer ikke over flere tusen noder ved hver DOM-endring.
    document.querySelectorAll(`nb-item-card:not([${KORT_MERKE}])`).forEach((kort) => {
      kort.setAttribute(KORT_MERKE, '1');
      if (getComputedStyle(kort).position === 'static') kort.style.position = 'relative';

      const holder = document.createElement('nb-verktoy-kort');
      const rot = skygge(holder);

      const knapp = document.createElement('button');
      knapp.type = 'button';
      knapp.className = 'kortknapp';
      knapp.textContent = '⋯';
      knapp.title = 'NB-verktøy for dette treffet';
      knapp.setAttribute('aria-label', knapp.title);
      knapp.setAttribute('aria-haspopup', 'menu');

      knapp.addEventListener('click', (h) => {
        h.preventDefault();
        h.stopPropagation();
        aapneKortmeny(kort, knapp);
      });
      // Treffkortene er pakket i en <a>. Uten dette navigerer nb.no i stedet.
      ['mousedown', 'mouseup', 'pointerdown', 'pointerup'].forEach((type) => {
        knapp.addEventListener(type, (h) => {
          h.preventDefault();
          h.stopPropagation();
        });
      });

      rot.appendChild(knapp);
      kort.appendChild(holder);
    });
  }

  /* Kortlenka gir bare en sesam-id, men miniatyren peker på bildeleveransen —
     og den har URN-en i seg, med mediatypen som prefiks:
     .../image/resolver/URN:NBN:no-nb_digitidsskrift_2025072480003_025_0001/... */
  function urnFraKort(kort) {
    for (const img of kort.querySelectorAll('img')) {
      const treff = /URN:NBN:no-nb_[A-Za-z0-9_.-]+/.exec(img.getAttribute('src') || '');
      if (treff) return treff[0];
    }
    return null;
  }

  /** Skjul en kortrad bare når vi vet at den ikke gjelder. */
  function passerKort(post, urn) {
    if (!post.kortNaar || !urn) return true;
    try {
      return new RegExp(post.kortNaar).test(urn);
    } catch (_) {
      return true;   // ødelagt uttrykk skal ikke gjemme bort verktøyet
    }
  }

  function aapneKortmeny(kort, knapp) {
    const varAapenHer = vert.kortmeny?._kort === kort;
    lukkMenyer();
    if (varAapenHer) return;   // andre klikk på samme kort lukker

    const urn = urnFraKort(kort);
    const poster = lesRegister().filter((p) => p.kort && passerKort(p, urn));
    if (!poster.length) {
      // Knappen tegnes for alle kort, men et verktøy kan ha filtrert seg bort.
      // Si fra i stedet for å la knappen se ødelagt ut.
      visVarsel('Ingen verktøy passer dette treffet');
      return;
    }

    const meny = document.createElement('div');
    meny.className = 'meny kortmeny';
    meny.setAttribute('role', 'menu');
    meny._kort = kort;

    const finnKtx = () => ({
      type: 'kort',
      itemId: itemIdFraLenke(kort.querySelector('a[href*="/items/"]')?.getAttribute('href')),
      urn: urnFraKort(kort),
      sti: location.pathname,
      url: location.href,
    });

    poster.forEach((post) => meny.appendChild(lagRad(post, finnKtx, 'kort')));

    // Menyen ligger i overleggslaget, ikke i kortet — kortet klipper innholdet.
    // .lag er position:fixed, så koordinatene fra rect passer rett inn.
    const boks = knapp.getBoundingClientRect();
    meny.style.top = `${Math.round(boks.bottom + 6)}px`;
    meny.style.left = `${Math.round(Math.max(8, Math.min(boks.left, window.innerWidth - 252)))}px`;
    vert.lag.appendChild(meny);
    vert.kortmeny = meny;
    meny.querySelector('.rad')?.focus();
  }

  /* --------------------------------------------------------------- varsler */

  function visVarsel(melding, erFeil) {
    if (!vert) return;
    vert.varsel?.remove();
    const el = document.createElement('div');
    el.className = 'varsel';
    if (erFeil) el.dataset.feil = '1';
    el.setAttribute('role', 'status');
    el.textContent = melding;
    vert.lag.appendChild(el);
    vert.varsel = el;
    setTimeout(() => {
      if (vert?.varsel === el) vert.varsel = null;
      el.remove();
    }, erFeil ? 9000 : 5000);
  }

  function settJobber(id, jobber) {
    if (!vert) return;
    vert.rot.querySelectorAll(`.rad[data-id="${CSS.escape(id)}"]`).forEach((rad) => {
      rad.setAttribute('aria-busy', String(!!jobber));
    });
  }

  /* ====================================================== hver verden ======
     Dette kjører i alle utvidelsene, vert eller ei. */

  lytt('nbv:kjor', (m) => {
    const def = mine.get(m.id);
    if (!def) return;   // et annet verktøy — ikke vårt
    try {
      if (m.kilde === 'kort' && typeof def.paaKort === 'function') def.paaKort(m.ktx);
      else if (typeof def.paaObjekt === 'function') def.paaObjekt(m.ktx);
    } catch (error) {
      console.error('[nb-verktoy]', def.id, error);
      globalThis.NBVerktoy.varsle(`${def.navn}: ${error.message || 'ukjent feil'}`, true);
    }
  });

  lytt('nbv:varsel', (m) => visVarsel(m.melding, m.erFeil));
  lytt('nbv:status', (m) => settJobber(m.id, m.jobber));
  lytt('nbv:endret', () => tegn());

  /** Regn ut på nytt hva som gjelder her, og be verten tegne. */
  let planlagt = false;
  function oppdater() {
    if (planlagt) return;
    planlagt = true;
    requestAnimationFrame(() => {
      planlagt = false;
      try {
        // Er dokken vår revet bort — av en annen vert, eller av nb.no selv —
        // må vi slippe taket. Ellers tegner vi videre inn i en løsrevet skygge
        // som ingen ser, og kortknappene våre slutter å svare.
        if (vert && !vert.el.isConnected) vert = null;
        if (!vert && (dokkErForlatt() || dokkErEldre())) bliVert();

        const ktx = kontekst();
        let endret = false;
        mine.forEach((def) => {
          const aktiv = typeof def.gjelder === 'function' ? !!def.gjelder(ktx) : true;
          if (aktiv !== def._aktiv) {
            def._aktiv = aktiv;
            endret = true;
          }
          skrivPost(def, aktiv);
        });

        if (vert) {
          tegn();
          tegnKortknapper();
        } else if (endret) {
          meld('nbv:endret', {});
        }
      } catch (error) {
        console.error('[nb-verktoy]', error);
      }
    });
  }

  /* SPA-navigasjon skjer uten sidelast, så History API-kallene må fanges.
     Hver verden lapper sin egen kopi av history — det er greit, de kaller
     bare videre til den forrige. */
  ['pushState', 'replaceState'].forEach((metode) => {
    const opprinnelig = history[metode];
    history[metode] = function nbvLappet(...args) {
      const resultat = opprinnelig.apply(this, args);
      window.dispatchEvent(new Event('nbv:navigasjon'));
      return resultat;
    };
  });
  window.addEventListener('popstate', oppdater);
  window.addEventListener('nbv:navigasjon', oppdater);

  /* Pulsen holder registeret varmt og lar en annen ta over dokken hvis
     verts-utvidelsen blir skrudd av eller lastet på nytt midt i økta. */
  setInterval(() => {
    if (!mine.size) return;
    mine.forEach((def) => skrivPost(def, !!def._aktiv));
    if (vert) vert.el.dataset.puls = String(Date.now());
    else if (dokkErForlatt() || dokkErEldre()) oppdater();
  }, PULS_MS);

  /* ====================================================== API ============== */

  globalThis.NBVerktoy = {
    versjon: VERSJON,

    /** Meld inn et verktøy. Kan kalles flere ganger for samme id. */
    registrer(def) {
      if (!def?.id || !def.navn) {
        console.error('[nb-verktoy] registrer() krever id og navn', def);
        return;
      }
      mine.set(def.id, def);
      oppdater();
    },

    /** Kortvarig melding til brukeren. Verten tegner den. */
    varsle(melding, erFeil = false) {
      meld('nbv:varsel', { melding, erFeil: !!erFeil });
    },

    /** Marker raden som opptatt mens et kall er underveis. */
    jobber(id, jobber = true) {
      meld('nbv:status', { id, jobber: !!jobber });
    },

    /** Hjelpere verktøyene ellers ville skrevet hver for seg. */
    kontekst,
    itemIdFraSti,
    itemIdFraLenke,
  };

  new MutationObserver(oppdater).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  oppdater();
})();
