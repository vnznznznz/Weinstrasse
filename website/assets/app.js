/* Weinstrasse – Laden Köln-Süd. Frontend: Effekte, Shop, Checkout, Concierge.
   Daten: data/produkte.json und data/lieferung.json (erzeugt von `auftragsautomat webshop-export`).
   Bestellung: POST /api/bestellung, Anfrage: POST /api/anfrage (auftragsautomat webshop-server). */
(function () {
"use strict";

/* ===================== Hilfsfunktionen ===================== */
const eur = n => Number(n).toLocaleString("de-DE", { style: "currency", currency: "EUR" });
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let toastT;
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(toastT); toastT = setTimeout(() => t.classList.remove("show"), 2600); }
const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
const fein = matchMedia("(hover: hover) and (pointer: fine)").matches;

/* ===================== Daten ===================== */
let PRODUKTE = [];
let datenFehler = false;
let LIEFERUNG = { mindestflaschen_lastenrad: 12, bestellschluss_uhr: 12, tage_voraus: 4, versandkosten: 6.9, versandkostenfrei_ab: 120, zonen: [], zeitfenster: [], laden: { email: "laden@weinstrasseadolph.de", strasse: "Sachsenring 38", plz: "50677", ort: "Köln", oeffnungszeiten: {} } };

async function ladeDaten() {
  try {
    const [p, l] = await Promise.all([fetch("data/produkte.json").then(r => r.json()), fetch("data/lieferung.json").then(r => r.json())]);
    PRODUKTE = p.produkte || [];
    LIEFERUNG = { ...LIEFERUNG, ...l, laden: { ...LIEFERUNG.laden, ...(l.laden || {}) } };
    const stand = $("#preisStand");
    if (stand) stand.textContent = new Date(p.stand).toLocaleDateString("de-DE");
    if (PRODUKTE.some(x => x.beispielpreis)) $("#shopNote").insertAdjacentHTML("beforeend", " <b>Preise sind Beispielpreise, bis der Artikelstamm angebunden ist.</b>");
  } catch (e) {
    datenFehler = true;
    console.error("Shop-Daten", e);
  }
  const min = $("#minFlaschen"); if (min) min.textContent = LIEFERUNG.mindestflaschen_lastenrad;
}

/* ===================== Navigation + Fortschritt ===================== */
const nav = $("#nav"), progressTop = $("#progressTop");
function navFx() {
  nav.classList.toggle("scrolled", scrollY > 40);
  const h = document.documentElement.scrollHeight - innerHeight;
  progressTop.style.width = (h > 0 ? scrollY / h * 100 : 0) + "%";
}
addEventListener("scroll", navFx, { passive: true }); navFx();
$("#burger").onclick = () => $("#mobileMenu").classList.toggle("open");
$$("#mobileMenu a").forEach(a => a.onclick = () => $("#mobileMenu").classList.remove("open"));

/* ===================== Parallax-Bilder ===================== */
const parallaxImgs = [["#heroImg", 0.12], ["#bikeImg", 0.08], ["#praesentImg", 0.1]].map(([s, f]) => [$(s), f]).filter(([el]) => el);
function parallax() {
  if (reduced) return;
  for (const [img, f] of parallaxImgs) {
    const r = img.parentElement.getBoundingClientRect();
    if (r.bottom < 0 || r.top > innerHeight) continue;
    const p = (r.top + r.height / 2 - innerHeight / 2) / innerHeight;
    img.style.transform = `scale(1.12) translateY(${(-p * f * 100).toFixed(2)}%)`;
  }
}
addEventListener("scroll", () => requestAnimationFrame(parallax), { passive: true }); parallax();

/* ===================== Wort-für-Wort-Reveal ===================== */
$$("[data-words]").forEach(el => {
  const nodes = [...el.childNodes];
  el.innerHTML = "";
  nodes.forEach(n => {
    if (n.nodeType === 3) n.textContent.split(/(\s+)/).forEach(t => { if (!t) return; if (/^\s+$/.test(t)) el.appendChild(document.createTextNode(t)); else { const s = document.createElement("span"); s.className = "w"; s.textContent = t; el.appendChild(s); } });
    else { const wrap = n.cloneNode(false); n.textContent.split(/(\s+)/).forEach(t => { if (!t) return; if (/^\s+$/.test(t)) wrap.appendChild(document.createTextNode(t)); else { const s = document.createElement("span"); s.className = "w"; s.textContent = t; wrap.appendChild(s); } }); el.appendChild(wrap); }
  });
  [...el.querySelectorAll(".w")].forEach((w, i) => w.style.transitionDelay = (i * 0.07) + "s");
});

/* ===================== Reveal beim Scrollen ===================== */
const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } }), { threshold: .15, rootMargin: "0px 0px -8% 0px" });
$$(".reveal,.stagger,.words,.friday").forEach(el => io.observe(el));

const cio = new IntersectionObserver(es => es.forEach(e => {
  if (!e.isIntersecting) return; cio.unobserve(e.target);
  const el = e.target, end = +el.dataset.count, suf = el.dataset.suffix || "", plain = el.dataset.plain, t0 = performance.now(), d = 1400;
  (function tick(t) { const p = Math.min(1, (t - t0) / d), v = Math.round(end * (1 - Math.pow(1 - p, 3))); el.textContent = (plain ? v : v.toLocaleString("de-DE")) + suf; if (p < 1) requestAnimationFrame(tick); })(t0);
}), { threshold: .6 });
$$("[data-count]").forEach(el => cio.observe(el));

/* ===================== Magnet-Buttons, Tilt, Spotlight ===================== */
function magnetic(btn) {
  if (!fein || reduced) return;
  btn.addEventListener("mousemove", e => { const r = btn.getBoundingClientRect(); const x = e.clientX - r.left - r.width / 2, y = e.clientY - r.top - r.height / 2; btn.style.transform = `translate(${x * 0.18}px,${y * 0.25}px)`; });
  btn.addEventListener("mouseleave", () => btn.style.transform = "");
}
$$(".magnetic").forEach(magnetic);
function tilt(card) {
  if (!fein || reduced) return;
  card.addEventListener("mousemove", e => { const r = card.getBoundingClientRect(); const x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5; card.style.transform = `perspective(900px) rotateX(${(-y * 10).toFixed(2)}deg) rotateY(${(x * 12).toFixed(2)}deg) translateY(-6px)`; });
  card.addEventListener("mouseleave", () => card.style.transform = "");
}
$$(".tilt").forEach(tilt);
function spotlight(el) {
  if (!fein) return;
  el.addEventListener("mousemove", e => { const r = el.getBoundingClientRect(); el.style.setProperty("--mx", (e.clientX - r.left) + "px"); el.style.setProperty("--my", (e.clientY - r.top) + "px"); });
}
$$(".spot").forEach(spotlight);

/* ===================== Galerie (Laufband) ===================== */
(function () {
  const track = $("#galleryTrack"); if (!track) return;
  const fotos = [30, 38, 41, 47, 54, 58, 70, 72, 75, 80, 81, 82].map(n => `assets/img/laden/laden-${n}.jpg`);
  const html = fotos.map(f => `<img src="${f}" alt="Im Laden der Weinstrasse" loading="lazy" decoding="async">`).join("");
  track.innerHTML = html + html; // zweimal für nahtloses Laufband
})();

/* ===================== Friday Uncorked: Countdown ===================== */
(function () {
  const el = $("#countdown"); if (!el) return;
  const felder = el.querySelectorAll("b");
  function naechsterFreitag() {
    const jetzt = new Date(); const d = new Date(jetzt); d.setHours(14, 0, 0, 0);
    let diff = (5 - d.getDay() + 7) % 7; // 5 = Freitag
    if (diff === 0 && jetzt.getHours() >= 17) diff = 7;
    d.setDate(d.getDate() + diff);
    return d;
  }
  function tick() {
    const ziel = naechsterFreitag(), jetzt = new Date();
    if (jetzt.getDay() === 5 && jetzt.getHours() >= 14 && jetzt.getHours() < 17) { felder[0].textContent = "Jetzt"; felder[1].textContent = "🍾"; felder[2].textContent = "offen"; return; }
    let s = Math.max(0, Math.floor((ziel - jetzt) / 1000));
    const t = Math.floor(s / 86400); s -= t * 86400; const h = Math.floor(s / 3600); s -= h * 3600; const m = Math.floor(s / 60);
    felder[0].textContent = t; felder[1].textContent = h; felder[2].textContent = m;
  }
  tick(); setInterval(tick, 30000);
})();

/* ===================== Scrollytelling Lastenrad ===================== */
const road = $("#road"), trace = $("#trace"), bike = $("#bike"), steps = $$(".step"), stepsWrap = $("#steps");
const roadLen = road.getTotalLength();
function storyFx() {
  const r = stepsWrap.getBoundingClientRect(), vh = innerHeight;
  const total = r.height - vh * 0.4; const p = Math.min(1, Math.max(0, (vh * 0.6 - r.top) / total));
  const pt = road.getPointAtLength(p * roadLen), ahead = road.getPointAtLength(Math.min(roadLen, p * roadLen + 3));
  const ang = Math.atan2(ahead.y - pt.y, ahead.x - pt.x) * 180 / Math.PI;
  bike.setAttribute("transform", `translate(${pt.x},${pt.y - 10}) rotate(${(ang * 0.5).toFixed(1)})`);
  trace.style.strokeDashoffset = 1 - p;
  const spin = p * roadLen * 4; $("#w1").setAttribute("transform", `translate(10,32) rotate(${spin})`); $("#w2").setAttribute("transform", `translate(72,32) rotate(${spin})`);
  [0, .33, .66, 1].forEach((s, i) => $("#pin" + i).classList.toggle("on", p >= s - 0.02));
  let active = 0; steps.forEach((s, i) => { if (s.getBoundingClientRect().top < vh * 0.55) active = i; }); steps.forEach((s, i) => s.classList.toggle("active", i === active));
}
addEventListener("scroll", () => requestAnimationFrame(storyFx), { passive: true }); addEventListener("resize", storyFx); storyFx();

/* ===================== Konfetti ===================== */
function konfetti() {
  if (reduced) return;
  const box = document.createElement("div"); box.className = "confetti";
  const farben = ["#6b1a24", "#c9a24a", "#2f6b3a", "#f3c8a6", "#9a2b3a"];
  for (let i = 0; i < 80; i++) { const k = document.createElement("i"); k.style.left = Math.random() * 100 + "vw"; k.style.background = farben[i % farben.length]; k.style.animationDelay = (Math.random() * .8) + "s"; k.style.animationDuration = (2 + Math.random() * 1.5) + "s"; box.appendChild(k); }
  document.body.appendChild(box); setTimeout(() => box.remove(), 4500);
}

/* ===================== Shop ===================== */
const grid = $("#grid"); let filter = "alle";
const bottleSvg = f => `<svg viewBox="0 0 60 200" aria-hidden="true"><use href="#bottle-${f}"/></svg>`;
const media = p => p.bild ? `<div class="pic"><img src="${esc(p.bild)}" alt="${esc(p.name)}" loading="lazy" decoding="async"></div>` : bottleSvg(p.farbe);
const thumb = p => p.bild ? `<img src="${esc(p.bild)}" alt="" loading="lazy">` : bottleSvg(p.farbe);
function renderGrid() {
  grid.classList.remove("in");
  if (datenFehler) { grid.innerHTML = `<p class="empty">Das Sortiment konnte nicht geladen werden. Bitte später erneut versuchen oder per E-Mail bestellen.</p>`; return; }
  const liste = PRODUKTE.filter(p => filter === "alle" || p.typ === filter);
  grid.innerHTML = liste.length ? liste.map(p => `
    <article class="card" data-id="${esc(p.id)}">
      <div class="card-media"><div class="bg" style="background:${esc(p.bg)}"></div>${p.badge ? `<span class="badge">${esc(p.badge)}</span>` : ""}${media(p)}</div>
      <div class="card-body">
        <span class="meta">${esc(p.land)} · ${Number(p.liter).toLocaleString("de-DE")} l${p.jahrgang ? " · " + p.jahrgang : ""}</span>
        <h3>${esc(p.name)}</h3>
        <p class="desc">${esc(p.untertitel)}. ${esc(p.text)}</p>
        <div class="price-row">
          <div class="price">${eur(p.preis)}<small>${eur(p.grundpreis_liter)} / l · inkl. MwSt.</small></div>
          <button class="btn btn-dark btn-sm add" data-id="${esc(p.id)}">In den Korb</button>
        </div>
      </div>
    </article>`).join("") : `<p class="empty">In dieser Kategorie ist gerade nichts im Shop.</p>`;
  requestAnimationFrame(() => requestAnimationFrame(() => grid.classList.add("in")));
  $$("#grid .card").forEach(c => { c.classList.add("spot"); spotlight(c); });
}
$("#filters").addEventListener("click", e => { const b = e.target.closest(".chip-btn"); if (!b) return; $$("#filters .chip-btn").forEach(x => x.classList.remove("active")); b.classList.add("active"); filter = b.dataset.f; renderGrid(); });
grid.addEventListener("click", e => { const b = e.target.closest(".add"); if (!b) return; add(b.dataset.id); b.textContent = "✓ Im Korb"; setTimeout(() => b.textContent = "In den Korb", 1200); });

/* ===================== Warenkorb ===================== */
let cart = {}; try { cart = JSON.parse(localStorage.getItem("ws_cart") || "{}"); } catch { cart = {}; }
const save = () => { try { localStorage.setItem("ws_cart", JSON.stringify(cart)); } catch { /* privat */ } };
const produkt = id => PRODUKTE.find(p => p.id === id);
const flaschen = () => Object.values(cart).reduce((a, n) => a + n, 0);
const subtotal = () => Object.entries(cart).reduce((a, [id, n]) => a + (produkt(id)?.preis || 0) * n, 0);
function bereinigeKorb() { for (const id of Object.keys(cart)) if (!produkt(id)) delete cart[id]; }
function add(id, n = 1) {
  if (!produkt(id)) return;
  cart[id] = (cart[id] || 0) + n; if (cart[id] <= 0) delete cart[id]; save(); renderCart();
  if (n > 0) { toast("In den Warenkorb gelegt"); const c = $("#cartCount"); c.classList.remove("bump"); void c.offsetWidth; c.classList.add("bump"); }
}
function renderCart() {
  const n = flaschen(); $("#cartCount").textContent = n;
  const lines = $("#cartLines"), MIN = LIEFERUNG.mindestflaschen_lastenrad;
  if (!n) lines.innerHTML = `<div class="empty"><p style="font-size:2.4rem">🍷</p><p>Noch nichts im Korb.</p></div>`;
  else lines.innerHTML = Object.entries(cart).map(([id, q]) => { const p = produkt(id); return `
    <div class="line"><div class="thumb">${thumb(p)}</div>
      <div><h4>${esc(p.name)}</h4><div class="sub">${esc(p.untertitel)} · ${eur(p.preis)}</div>
        <div class="qty" style="margin-top:.4rem"><button data-d="-1" data-id="${esc(id)}" aria-label="weniger">−</button><span>${q}</span><button data-d="1" data-id="${esc(id)}" aria-label="mehr">+</button></div></div>
      <div class="lp">${eur(p.preis * q)}</div></div>`; }).join("");
  $("#cartSub").textContent = eur(subtotal());
  const rest = Math.max(0, MIN - n);
  $("#cartProgress").style.width = Math.min(100, n / MIN * 100) + "%";
  $("#cartProgressText").innerHTML = rest ? `Noch <b>${rest} Flasche${rest > 1 ? "n" : ""}</b> bis zur Lastenrad-Lieferung (ab ${MIN}).` : `🚲 <b>Lastenrad-Lieferung möglich</b> – ${n} Flaschen im Korb.`;
  $("#toCheckout").disabled = !n;
}
$("#cartLines").addEventListener("click", e => { const b = e.target.closest("[data-d]"); if (b) add(b.dataset.id, +b.dataset.d); });
const drawer = $("#drawer"), overlay = $("#overlay");
function openDrawer() { drawer.classList.add("show"); overlay.classList.add("show"); drawer.setAttribute("aria-hidden", "false"); }
function closeAll() { drawer.classList.remove("show"); overlay.classList.remove("show"); $("#modal").classList.remove("show"); drawer.setAttribute("aria-hidden", "true"); }
$("#cartBtn").onclick = openDrawer; $("#closeDrawer").onclick = closeAll; overlay.onclick = closeAll; $("#closeModal").onclick = closeAll;
addEventListener("keydown", e => { if (e.key === "Escape") closeAll(); });

/* ===================== Termine (wie im Backend) ===================== */
const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const isoTag = d => d.getDay() === 0 ? 7 : d.getDay();
function angeboteneTage() {
  const jetzt = new Date(); const d = new Date(jetzt); d.setHours(0, 0, 0, 0);
  d.setDate(d.getDate() + (jetzt.getHours() < LIEFERUNG.bestellschluss_uhr ? 1 : 2));
  const out = [];
  while (out.length < LIEFERUNG.tage_voraus) { if (isoTag(d) <= 5) out.push(new Date(d)); d.setDate(d.getDate() + 1); }
  return out;
}
function abholTage() {
  const tage = angeboteneTage(); const d = new Date(tage[0]); const out = [];
  while (out.length < 5 && out.length < 10) { if (LIEFERUNG.laden.oeffnungszeiten[isoTag(d)]) out.push(new Date(d)); d.setDate(d.getDate() + 1); if (out.length === 0 && d - tage[0] > 14 * 864e5) break; }
  return out;
}
const fmtTag = d => d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" });
const uhr = t => t.replace(/:00$/, "").replace(":", ".");

/* ===================== Checkout ===================== */
const order = { art: null, plz: "", zone: null, slot: null, slotLabel: "", name: "", adresse: "", plzVersand: "", ort: "", email: "", tel: "", notiz: "", alter: false, agb: false };
let step = 0;
const body = $("#modalBody");
$("#toCheckout").onclick = () => { drawer.classList.remove("show"); step = 0; renderCheckout(); $("#modal").classList.add("show"); };

const zoneFor = plz => LIEFERUNG.zonen.find(z => z.plz.includes(plz)) || null;
function lieferkosten() {
  if (order.art === "lastenrad") return order.zone ? order.zone.gebuehr : 0;
  if (order.art === "versand") return subtotal() >= LIEFERUNG.versandkostenfrei_ab ? 0 : LIEFERUNG.versandkosten;
  return 0;
}
const canNext1 = () => order.art === "lastenrad" ? !!order.zone && !!order.slot : order.art === "abholung" ? !!order.slot : true;
const bar = () => `<div class="steps-bar">${[0, 1, 2, 3].map(i => `<i class="${i <= step ? "done" : ""}"></i>`).join("")}</div>`;

function renderCheckout() {
  const n = flaschen(), MIN = LIEFERUNG.mindestflaschen_lastenrad;
  if (step === 0) {
    body.innerHTML = bar() + `
      <h3 style="font-size:1.5rem;margin-bottom:.4rem">Wie möchten Sie Ihren Wein bekommen?</h3>
      <p style="color:var(--ink-2);font-size:.92rem;margin-bottom:1.2rem">${n} Flasche${n !== 1 ? "n" : ""} im Korb · ${eur(subtotal())}</p>
      <div class="opt-grid">
        <div class="opt ${order.art === "lastenrad" ? "sel" : ""}" data-art="lastenrad"><span class="ico">🚲</span><b>Lastenrad-Lieferung</b><span>Emissionsfrei im Kölner Süden. Ab ${MIN} Flaschen, Kerngebiet kostenlos.</span>${n < MIN ? `<span style="color:#9b1c1c;font-weight:600">Noch ${MIN - n} Flaschen bis zur Mindestmenge</span>` : ""}</div>
        <div class="opt ${order.art === "abholung" ? "sel" : ""}" data-art="abholung"><span class="ico">🏬</span><b>Abholung im Laden</b><span>${esc(LIEFERUNG.laden.strasse)}. Wir stellen alles zusammen, Sie holen ab. Kostenlos.</span></div>
        <div class="opt ${order.art === "versand" ? "sel" : ""}" data-art="versand"><span class="ico">📦</span><b>Paketversand</b><span>Deutschlandweit, ${eur(LIEFERUNG.versandkosten)} pauschal, ab ${eur(LIEFERUNG.versandkostenfrei_ab)} kostenlos.</span></div>
      </div>
      <div class="modal-foot"><span></span><button class="btn btn-primary" id="next" ${order.art ? "" : "disabled"}>Weiter</button></div>`;
    $$(".opt").forEach(o => o.onclick = () => { if (o.dataset.art === "lastenrad" && n < MIN) { toast(`Lastenrad erst ab ${MIN} Flaschen`); return; } if (order.art !== o.dataset.art) { order.slot = null; order.slotLabel = ""; } order.art = o.dataset.art; renderCheckout(); });
    $("#next").onclick = () => { step = 1; renderCheckout(); };
  }
  else if (step === 1) {
    const tage = angeboteneTage();
    const slotsHtml = tage.map(d => LIEFERUNG.zeitfenster.filter(f => f.tage.includes(isoTag(d))).map(f => {
      const key = `${iso(d)}|${f.id}`, label = `${fmtTag(d)}, ${uhr(f.von)} – ${uhr(f.bis)} Uhr`;
      return `<div class="slot ${order.slot === key ? "sel" : ""}" data-slot="${key}" data-label="${esc(label)}"><b>${fmtTag(d)}</b>${uhr(f.von)} – ${uhr(f.bis)} Uhr</div>`; }).join("")).join("");
    const abholHtml = abholTage().map(d => { const [von, bis] = LIEFERUNG.laden.oeffnungszeiten[isoTag(d)]; const key = iso(d), label = `${fmtTag(d)}, ${uhr(von)} – ${uhr(bis)} Uhr`;
      return `<div class="slot ${order.slot === key ? "sel" : ""}" data-slot="${key}" data-label="${esc(label)}"><b>${fmtTag(d)}</b>${uhr(von)} – ${uhr(bis)} Uhr</div>`; }).join("");
    body.innerHTML = bar() + (order.art === "lastenrad" ? `
      <h3 style="font-size:1.5rem;margin-bottom:.4rem">Liefergebiet &amp; Zeitfenster</h3>
      <p style="color:var(--ink-2);font-size:.92rem">Das Wein-Taxi fährt im Kölner Süden. Prüfen Sie Ihre Postleitzahl.</p>
      <div class="field"><label for="plz">Postleitzahl</label><input id="plz" inputmode="numeric" maxlength="5" placeholder="50677" value="${esc(order.plz)}" autocomplete="postal-code"></div>
      <div class="plz-result" id="plzRes"></div>
      <div id="slotWrap" style="display:${order.zone ? "block" : "none"};margin-top:1.2rem">
        <label style="font-size:.82rem;font-weight:600;color:var(--ink-2)">Wunsch-Zeitfenster</label>
        <div class="slots" id="slots">${slotsHtml}</div>
        <p style="font-size:.8rem;color:var(--ink-3);margin-top:.6rem">Bestellungen bis ${LIEFERUNG.bestellschluss_uhr} Uhr liefern wir am nächsten Werktag. Übergabe nur an Personen ab 18 Jahren.</p>
      </div>`
    : order.art === "abholung" ? `
      <h3 style="font-size:1.5rem;margin-bottom:.4rem">Abholung im Laden</h3>
      <p style="color:var(--ink-2);font-size:.92rem">${esc(LIEFERUNG.laden.strasse)}, ${esc(LIEFERUNG.laden.plz)} ${esc(LIEFERUNG.laden.ort)}. Wir packen Ihre Bestellung und melden uns, sobald sie bereitsteht.</p>
      <label style="font-size:.82rem;font-weight:600;color:var(--ink-2);display:block;margin-top:1.2rem">Wunschtag</label>
      <div class="slots" id="slots">${abholHtml}</div>`
    : `
      <h3 style="font-size:1.5rem;margin-bottom:.4rem">Paketversand</h3>
      <p style="color:var(--ink-2);font-size:.92rem">Sicher verpacktes Weinpaket, in der Regel 2 bis 3 Werktage, Zustellung nur an Volljährige. ${subtotal() >= LIEFERUNG.versandkostenfrei_ab ? "Ihr Versand ist kostenlos." : `Noch ${eur(LIEFERUNG.versandkostenfrei_ab - subtotal())} bis zum kostenlosen Versand.`}</p>
      <div class="summary"><div class="sumrow"><span>Versandkosten</span><span>${eur(lieferkosten())}</span></div></div>`)
    + `<div class="modal-foot"><button class="btn btn-ghost" id="back">Zurück</button><button class="btn btn-primary" id="next" ${canNext1() ? "" : "disabled"}>Weiter</button></div>`;
    $("#back").onclick = () => { step = 0; renderCheckout(); }; $("#next").onclick = () => { step = 2; renderCheckout(); };
    const plz = $("#plz");
    if (plz) {
      const check = () => {
        order.plz = plz.value.trim(); const res = $("#plzRes"); res.className = "plz-result";
        if (order.plz.length < 5) { order.zone = null; $("#slotWrap").style.display = "none"; $("#next").disabled = true; return; }
        const z = zoneFor(order.plz); order.zone = z;
        if (z) { res.classList.add(z.gebuehr ? "warn" : "ok"); res.textContent = z.gebuehr ? `✓ ${z.name}: Lieferung für ${eur(z.gebuehr)}.` : `✓ ${z.name}: Lieferung kostenlos.`; $("#slotWrap").style.display = "block"; }
        else { res.classList.add("no"); res.textContent = "Leider außerhalb unseres Lastenrad-Gebiets. Wir versenden gern per Paket oder Sie holen im Laden ab."; $("#slotWrap").style.display = "none"; }
        $("#next").disabled = !canNext1();
      };
      plz.addEventListener("input", check); if (order.plz.length === 5) check(); plz.focus();
    }
    const sl = $("#slots"); if (sl) sl.onclick = e => { const s = e.target.closest(".slot"); if (!s) return; order.slot = s.dataset.slot; order.slotLabel = s.dataset.label; $$(".slot").forEach(x => x.classList.toggle("sel", x === s)); $("#next").disabled = !canNext1(); };
  }
  else if (step === 2) {
    body.innerHTML = bar() + `
      <h3 style="font-size:1.5rem;margin-bottom:.4rem">Ihre Daten</h3>
      <div class="row2"><div class="field"><label for="f-name">Vor- und Nachname</label><input id="f-name" value="${esc(order.name)}" autocomplete="name"></div><div class="field"><label for="f-email">E-Mail</label><input id="f-email" type="email" value="${esc(order.email)}" autocomplete="email"></div></div>
      <div class="row2"><div class="field"><label for="f-tel">Telefon (für die Lieferung)</label><input id="f-tel" type="tel" value="${esc(order.tel)}" autocomplete="tel"></div>
      ${order.art !== "abholung" ? `<div class="field"><label for="f-adr">Straße, Hausnummer</label><input id="f-adr" value="${esc(order.adresse)}" autocomplete="street-address"></div>` : "<div></div>"}</div>
      ${order.art === "versand" ? `<div class="row2"><div class="field"><label for="f-plz">Postleitzahl</label><input id="f-plz" inputmode="numeric" maxlength="5" value="${esc(order.plzVersand)}" autocomplete="postal-code"></div><div class="field"><label for="f-ort">Ort</label><input id="f-ort" value="${esc(order.ort)}" autocomplete="address-level2"></div></div>` : ""}
      <div class="field"><label for="f-notiz">Anmerkung (Klingel, Etage, Wunschzeit)</label><textarea id="f-notiz" rows="2">${esc(order.notiz)}</textarea></div>
      <label class="check"><input type="checkbox" id="f-alter" ${order.alter ? "checked" : ""}><span>Ich bin mindestens 18 Jahre alt. Alkoholische Getränke werden nur an Volljährige übergeben, der Ausweis wird bei Lieferung geprüft.</span></label>
      <label class="check"><input type="checkbox" id="f-agb" ${order.agb ? "checked" : ""}><span>Ich habe die <a href="agb.html" target="_blank" style="text-decoration:underline">AGB</a> und die <a href="widerruf.html" target="_blank" style="text-decoration:underline">Widerrufsbelehrung</a> gelesen und akzeptiere sie. Hinweise zur Datenverarbeitung: <a href="datenschutz.html" target="_blank" style="text-decoration:underline">Datenschutz</a>.</span></label>
      <div class="modal-foot"><button class="btn btn-ghost" id="back">Zurück</button><button class="btn btn-primary" id="next">Zur Übersicht</button></div>`;
    $("#back").onclick = () => { step = 1; renderCheckout(); };
    $("#next").onclick = () => {
      order.name = $("#f-name").value.trim(); order.email = $("#f-email").value.trim(); order.tel = $("#f-tel").value.trim(); order.notiz = $("#f-notiz").value.trim(); order.alter = $("#f-alter").checked; order.agb = $("#f-agb").checked;
      if ($("#f-adr")) order.adresse = $("#f-adr").value.trim();
      if ($("#f-plz")) { order.plzVersand = $("#f-plz").value.trim(); order.ort = $("#f-ort").value.trim(); }
      if (order.name.length < 2 || !/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(order.email)) { toast("Bitte Name und gültige E-Mail angeben"); return; }
      if (order.art !== "abholung" && !order.adresse) { toast("Bitte Straße und Hausnummer angeben"); return; }
      if (order.art === "versand" && (!/^\d{5}$/.test(order.plzVersand) || !order.ort)) { toast("Bitte Postleitzahl und Ort angeben"); return; }
      if (!order.alter) { toast("Bitte Volljährigkeit bestätigen"); return; }
      if (!order.agb) { toast("Bitte AGB akzeptieren"); return; }
      step = 3; renderCheckout();
    };
  }
  else if (step === 3) {
    const lk = lieferkosten(), tot = subtotal() + lk;
    const artTxt = { lastenrad: `Lastenrad-Lieferung · ${order.slotLabel}`, abholung: `Abholung im Laden · ${order.slotLabel}`, versand: "Paketversand" }[order.art];
    const adresse = order.art === "abholung" ? "" : order.art === "lastenrad" ? `${order.adresse}, ${order.plz} ${LIEFERUNG.laden.ort}` : `${order.adresse}, ${order.plzVersand} ${order.ort}`;
    body.innerHTML = bar() + `
      <h3 style="font-size:1.5rem;margin-bottom:.4rem">Bestellung prüfen</h3>
      <div class="summary">
        ${Object.entries(cart).map(([id, q]) => { const p = produkt(id); return `<div class="sumrow"><span>${q} × ${esc(p.name)}</span><span>${eur(p.preis * q)}</span></div>`; }).join("")}
        <div class="sumrow" style="border-top:1px solid rgba(29,27,26,.1);padding-top:.5rem;margin-top:.5rem"><span>${esc(artTxt)}</span><span>${lk ? eur(lk) : "kostenlos"}</span></div>
        <div class="sumrow total"><span>Gesamt inkl. MwSt.</span><span>${eur(tot)}</span></div>
      </div>
      <div class="summary" style="background:#fff;border:1px solid rgba(29,27,26,.08)">
        <b>${esc(order.name)}</b><br>${adresse ? esc(adresse) + "<br>" : ""}${esc(order.email)} · ${esc(order.tel || "keine Telefonnummer")}${order.notiz ? "<br><i>" + esc(order.notiz) + "</i>" : ""}
      </div>
      <p style="font-size:.82rem;color:var(--ink-3);margin-top:1rem">Zahlung: ${order.art === "versand" ? "per Rechnung vorab (Bankverbindung in der Bestätigung)" : "bei Übergabe (Karte oder bar) oder vorab per Rechnung"}. Mit Klick auf „Zahlungspflichtig bestellen“ geben Sie eine verbindliche Bestellung ab.</p>
      <div class="modal-foot"><button class="btn btn-ghost" id="back">Zurück</button><button class="btn btn-primary" id="buy">Zahlungspflichtig bestellen</button></div>`;
    $("#back").onclick = () => { step = 2; renderCheckout(); };
    $("#buy").onclick = absenden;
  }
}

function bestellText(nummer, artTxt, tot) {
  const o = order;
  return `Bestellung ${nummer}\n${artTxt}\n` + Object.entries(cart).map(([id, q]) => { const p = produkt(id); return `${q} x ${p.name} (${eur(p.preis)})`; }).join("\n")
    + `\nGesamt: ${eur(tot)}\n\n${o.name}\n${o.art === "abholung" ? "Abholung" : o.art === "lastenrad" ? `${o.adresse}, ${o.plz} ${LIEFERUNG.laden.ort}` : `${o.adresse}, ${o.plzVersand} ${o.ort}`}\n${o.email} ${o.tel}\n${o.notiz}`;
}

async function absenden() {
  const btn = $("#buy"); btn.disabled = true; btn.textContent = "Wird übermittelt …";
  const lk = lieferkosten(), tot = subtotal() + lk;
  const artTxt = { lastenrad: `Lastenrad-Lieferung · ${order.slotLabel}`, abholung: `Abholung im Laden · ${order.slotLabel}`, versand: "Paketversand" }[order.art];
  const anfrage = {
    lieferart: order.art,
    positionen: Object.entries(cart).map(([id, q]) => ({ artikelnummer: id, menge: q })),
    kunde: { name: order.name, email: order.email, telefon: order.tel, strasse: order.art === "abholung" ? "" : order.adresse,
      plz: order.art === "lastenrad" ? order.plz : order.art === "versand" ? order.plzVersand : "", ort: order.art === "versand" ? order.ort : "", anmerkung: order.notiz },
    zeitfenster: order.art === "versand" ? null : order.slot,
    alter_bestaetigt: order.alter, agb_akzeptiert: order.agb,
  };
  let antwort = null, fehler = null, offline = false;
  try {
    const r = await fetch("api/bestellung", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(anfrage) });
    const d = await r.json().catch(() => ({}));
    if (r.ok) antwort = d; else fehler = d.fehler || "Die Bestellung wurde nicht angenommen.";
  } catch { offline = true; }
  if (fehler) { toast(fehler); btn.disabled = false; btn.textContent = "Zahlungspflichtig bestellen"; return; }
  const nummer = antwort ? antwort.nummer : "ENTWURF-" + Date.now().toString(36).toUpperCase();
  const text = bestellText(nummer, artTxt, antwort ? antwort.gesamt : tot);
  const termin = order.art === "lastenrad" ? "Das Wein-Taxi kommt " + order.slotLabel + "." : order.art === "abholung" ? "Wir melden uns, sobald alles bereitsteht." : "Sie erhalten eine Versandbestätigung per E-Mail.";
  const mailto = `mailto:${LIEFERUNG.laden.email}?subject=${encodeURIComponent("Bestellung " + nummer)}&body=${encodeURIComponent(text)}`;
  body.innerHTML = `<div class="success"><div class="ring"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg></div>
    <h3>${offline ? "Fast geschafft" : "Danke, " + esc(order.name) + "!"}</h3>
    <p style="color:var(--ink-2);margin:.6rem 0 1rem">${offline
      ? `Der Bestellserver ist gerade nicht erreichbar. Bitte schicken Sie uns den Bestelltext per E-Mail an <b>${esc(LIEFERUNG.laden.email)}</b>, wir bestätigen umgehend.`
      : `Ihre Bestellung <span class="code">${esc(nummer)}</span> ist eingegangen.<br>${termin}${antwort && antwort.mail_gesendet ? "<br>Eine Bestätigung ist unterwegs an " + esc(order.email) + "." : ""}`}</p>
    <div style="display:flex;gap:.6rem;justify-content:center;flex-wrap:wrap">
      ${offline ? `<a class="btn btn-primary" href="${mailto}">Per E-Mail senden</a>` : ""}
      <button class="btn btn-dark" id="copy">Bestelltext kopieren</button><button class="btn btn-ghost" id="done">Zurück zum Shop</button></div></div>`;
  $("#copy").onclick = () => { (navigator.clipboard ? navigator.clipboard.writeText(text) : Promise.reject()).then(() => toast("Kopiert")).catch(() => toast("Kopieren nicht möglich")); };
  $("#done").onclick = closeAll;
  if (!offline) { cart = {}; save(); renderCart(); konfetti(); }
}

/* ===================== Concierge-Anfrage ===================== */
(function () {
  const form = $("#anfrageForm"); if (!form) return;
  $$("[data-anlass]").forEach(a => a.addEventListener("click", () => { $("#a-anlass").value = a.dataset.anlass; }));
  form.addEventListener("submit", async e => {
    e.preventDefault();
    const daten = {
      anlass: $("#a-anlass").value, name: $("#a-name").value.trim(), firma: $("#a-firma").value.trim(), email: $("#a-email").value.trim(), telefon: $("#a-tel").value.trim(),
      personen: parseInt($("#a-personen").value, 10) || null, datum: $("#a-datum").value.trim(), budget: $("#a-budget").value.trim(), nachricht: $("#a-nachricht").value.trim(), datenschutz: $("#a-ds").checked,
    };
    if (daten.name.length < 2 || !/^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/.test(daten.email)) { toast("Bitte Name und gültige E-Mail angeben"); return; }
    if (!daten.datenschutz) { toast("Bitte Datenschutzhinweis bestätigen"); return; }
    const btn = $("#anfrageSenden"); btn.disabled = true; btn.textContent = "Wird gesendet …";
    const text = `Anfrage: ${$("#a-anlass").selectedOptions[0].textContent}\n${daten.name}${daten.firma ? " (" + daten.firma + ")" : ""}\n${daten.email} ${daten.telefon}\nPersonen: ${daten.personen || "-"} · Termin: ${daten.datum || "-"} · Budget: ${daten.budget || "-"}\n\n${daten.nachricht}`;
    let ok = false, fehler = null;
    try {
      const r = await fetch("api/anfrage", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(daten) });
      const d = await r.json().catch(() => ({}));
      if (r.ok) ok = true; else fehler = d.fehler || "Die Anfrage wurde nicht angenommen.";
    } catch { /* kein Server */ }
    if (fehler) { toast(fehler); btn.disabled = false; btn.textContent = "Anfrage senden"; return; }
    const mailto = `mailto:${LIEFERUNG.laden.email}?subject=${encodeURIComponent("Concierge-Anfrage")}&body=${encodeURIComponent(text)}`;
    form.innerHTML = ok
      ? `<div class="success" style="padding:1rem 0"><div class="ring"><svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 13l4 4L19 7"/></svg></div><h3 style="font-size:1.5rem">Danke, ${esc(daten.name)}!</h3><p style="color:var(--ink-2);margin-top:.5rem">Ihre Anfrage ist bei uns. Wir melden uns innerhalb eines Werktags.</p></div>`
      : `<div class="success" style="padding:1rem 0"><h3 style="font-size:1.4rem">Fast geschafft</h3><p style="color:var(--ink-2);margin:.5rem 0 1rem">Der Server ist gerade nicht erreichbar. Schicken Sie uns die Anfrage per E-Mail an <b>${esc(LIEFERUNG.laden.email)}</b>.</p><a class="btn btn-primary" href="${mailto}">Per E-Mail senden</a></div>`;
  });
})();

/* ===================== Öffnungszeiten live ===================== */
function oeffnungsstatus() {
  const d = new Date(), tag = isoTag(d), m = d.getHours() * 60 + d.getMinutes(); const el = $("#openNow");
  const oz = LIEFERUNG.laden.oeffnungszeiten[tag];
  const toMin = t => { const [h, mi] = t.split(":").map(Number); return h * 60 + mi; };
  const open = !!oz && m >= toMin(oz[0]) && m < toMin(oz[1]);
  el.classList.toggle("closed", !open);
  el.querySelector("span").textContent = open ? (tag === 5 && d.getHours() >= 14 ? "Jetzt geöffnet · Friday Uncorked läuft" : "Jetzt geöffnet") : "Zurzeit geschlossen";
}

/* ===================== Start ===================== */
ladeDaten().then(() => { bereinigeKorb(); renderGrid(); renderCart(); oeffnungsstatus(); });
})();
