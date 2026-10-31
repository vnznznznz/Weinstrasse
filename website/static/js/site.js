/* Weinstrasse Adolph · gemeinsames Verhalten aller Seiten: Navigation, Effekte, Bestellkorb, Weinberater. */
(function () {
  'use strict';
  var root = document.body.getAttribute('data-root') || './';
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  document.documentElement.classList.add('js');
  var DATEN = window.WS_DATEN || { produkte: [], weingueter: [] };
  var PROD = {}; DATEN.produkte.forEach(function (p) { PROD[p.id] = p; });
  var WG = {}; DATEN.weingueter.forEach(function (w) { WG[w.slug] = w; });

  function $(s, el) { return (el || document).querySelector(s); }
  function $$(s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); }
  function fmt(n) { return (Math.round(n * 100) / 100).toFixed(2).replace('.', ',') + ' €'; }
  function toast(msg) { var t = $('#toast'); if (!t) return; t.textContent = msg; t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(function () { t.classList.remove('show'); }, 2600); }

  /* ---------- Bestellkorb (geteilt über alle Seiten, im Browser gespeichert) ---------- */
  var KEY = 'ws_bestellung_v1';
  function lade() { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (e) { return {}; } }
  function speichere(c) { try { localStorage.setItem(KEY, JSON.stringify(c)); } catch (e) { } }
  var korb = lade();
  var WS = window.WS = {
    fmt: fmt, toast: toast, root: root, produkte: PROD, weingueter: WG, daten: DATEN,
    korb: function () { return korb; },
    menge: function (id) { return korb[id] || 0; },
    setze: function (id, qty, still) {
      qty = Math.max(0, Math.round(qty));
      if (qty === 0) delete korb[id]; else korb[id] = qty;
      speichere(korb); WS.aktualisiere(); if (!still) WS.badgeBounce();
      document.dispatchEvent(new CustomEvent('korb:geaendert', { detail: { id: id, qty: qty } }));
    },
    lege: function (id, qty) { var p = PROD[id]; if (!p) return; var neu = (korb[id] || 0) + qty; WS.setze(id, neu); toast(qty + ' × ' + p.name + ' in der Bestellung (' + neu + ' Fl.)'); WS.oeffneKorb(true); },
    leere: function () { korb = {}; speichere(korb); WS.aktualisiere(); document.dispatchEvent(new CustomEvent('korb:geaendert', { detail: {} })); },
    anzahl: function () { var n = 0; Object.keys(korb).forEach(function (k) { n += korb[k]; }); return n; },
    summe: function () { var s = 0; Object.keys(korb).forEach(function (k) { var p = PROD[k]; if (p && p.listenpreis) s += p.listenpreis * korb[k]; }); return s; },
    badgeBounce: function () { var z = $('#korbZahl'); if (!z) return; z.classList.remove('bounce'); void z.offsetWidth; z.classList.add('bounce'); },
    oeffneKorb: function (kurz) { var k = $('#korb'), s = $('#schleier'); if (!k) return; k.classList.add('open'); k.setAttribute('aria-hidden', 'false'); s.classList.add('show'); if (kurz) { clearTimeout(WS._auto); WS._auto = setTimeout(WS.schliesseKorb, 2600); } },
    schliesseKorb: function () { var k = $('#korb'), s = $('#schleier'); if (!k) return; k.classList.remove('open'); k.setAttribute('aria-hidden', 'true'); s.classList.remove('show'); },
    aktualisiere: function () {
      var z = $('#korbZahl'); if (z) z.textContent = WS.anzahl();
      var l = $('#korbListe'); if (l) {
        var ids = Object.keys(korb);
        if (!ids.length) l.innerHTML = '<div class="korb-leer">Noch leer. Legen Sie Weine aus dem Katalog oder von den Weingut-Seiten hinein.</div>';
        else l.innerHTML = ids.map(function (id) { var p = PROD[id]; if (!p) return ''; return '<div class="kz"><b>' + esc(p.name) + '</b><span class="r">' + fmt((p.listenpreis || 0) * korb[id]) + '</span><span>' + korb[id] + ' Fl. × ' + fmt(p.listenpreis || 0) + ' · ' + esc(p.weingut_name || '') + '</span><span></span></div>'; }).join('');
      }
      var s = $('#korbSumme'); if (s) s.textContent = fmt(WS.summe());
      $$('[data-id]').forEach(function (el) { var q = korb[el.getAttribute('data-id')] || 0; if (el.classList.contains('prow') || el.classList.contains('pk')) { el.classList.toggle('im-korb', q > 0); var inp = $('.pmenge input', el); if (inp && document.activeElement !== inp) inp.value = q || 0; } });
    }
  };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  WS.esc = esc;

  /* Mengenfelder und "In die Bestellung" überall per Delegation */
  document.addEventListener('click', function (e) {
    var b = e.target.closest('.qty-plus, .qty-minus'); if (b) { var inp = $('input', b.parentElement); var ve = +inp.getAttribute('data-ve') || 6; var v = (+inp.value || 0) + (b.classList.contains('qty-plus') ? ve : -ve); inp.value = Math.max(0, v); var row = b.closest('[data-id]'); if (row && row.classList.contains('brow')) WS.setze(row.getAttribute('data-id'), +inp.value, true); return; }
    var a = e.target.closest('.add'); if (a) { var row2 = a.closest('.prow, .pk, .brow') || a; var id = a.getAttribute('data-id') || row2.getAttribute('data-id'); var inp2 = row2 ? $('.pmenge input', row2) : null; var q = inp2 ? (+inp2.value || 0) : 0; var ve2 = inp2 ? (+inp2.getAttribute('data-ve') || 6) : 6; if (q <= 0) q = ve2; if (row2 && row2.classList.contains('pk') || row2 && row2.classList.contains('prow')) { WS.setze(id, q); var p = PROD[id]; toast(q + ' Fl. ' + (p ? p.name : '') + ' in der Bestellung'); WS.oeffneKorb(true); } else WS.lege(id, q); return; }
  });
  document.addEventListener('change', function (e) { var inp = e.target; if (inp.matches('.brow .pmenge input')) { var row = inp.closest('[data-id]'); WS.setze(row.getAttribute('data-id'), +inp.value || 0, true); } });
  var kb = $('#korbBtn'); if (kb) kb.addEventListener('click', function () { WS.oeffneKorb(); });
  var kz = $('#korbZu'); if (kz) kz.addEventListener('click', WS.schliesseKorb);
  var sch = $('#schleier'); if (sch) sch.addEventListener('click', WS.schliesseKorb);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') { WS.schliesseKorb(); var lb = $('#lightbox'); if (lb) lb.hidden = true; } });
  WS.aktualisiere();

  /* ---------- Navigation ---------- */
  var nav = $('#nav'), burger = $('#burger'), mnav = $('#mnav'), totop = $('#totop'), fort = $('#fortschritt');
  if (burger) burger.addEventListener('click', function () { var o = mnav.classList.toggle('open'); burger.setAttribute('aria-expanded', o); document.body.style.overflow = o ? 'hidden' : ''; });
  if (mnav) mnav.addEventListener('click', function (e) { if (e.target.tagName === 'A') { mnav.classList.remove('open'); burger.setAttribute('aria-expanded', 'false'); document.body.style.overflow = ''; } });
  var mega = $('.hat-mega');
  if (mega) {
    var timer; var link = $('a', mega);
    function offen(o) { clearTimeout(timer); mega.classList.toggle('offen', o); }
    mega.addEventListener('mouseenter', function () { offen(true); });
    mega.addEventListener('mouseleave', function () { timer = setTimeout(function () { offen(false); }, 180); });
    link.addEventListener('click', function (e) { if (matchMedia('(hover: none)').matches && !mega.classList.contains('offen')) { e.preventDefault(); offen(true); } });
    var vor = $('#megaVorschau'); if (vor) { var vimg = $('img', vor), vb = $('b', vor), vs = $('span', vor);
      $$('.mega-land a', mega).forEach(function (a) { a.addEventListener('mouseenter', function () { if (a.dataset.hero && vimg.getAttribute('src') !== a.dataset.hero) { vimg.style.opacity = 0; setTimeout(function () { vimg.src = a.dataset.hero; vimg.onload = function () { vimg.style.opacity = 1; }; }, 150); } vb.textContent = a.dataset.name; vs.textContent = a.dataset.ort || ''; }); }); }
    document.addEventListener('click', function (e) { if (!mega.contains(e.target)) offen(false); });
  }

  /* ---------- Scroll-Effekte ---------- */
  var heroBg = $('#heroBg'), heroCopy = $('#heroCopy'), parallax = $$('[data-parallax]');
  var pour = $('#weine.pour');
  var P = pour ? { bottle: $('#bottle'), cap: $('#cap'), burst: $('#burst'), mouth: $('#mouth'), rim: $('#rim'), stream: $('#stream'), scene: $('#scene'), glass: $('#glass'), fill: $('#fill'), foam: $('#foam'), sante: $('#sante') } : null;
  var ease = function (t) { return t < 0 ? 0 : t > 1 ? 1 : 1 - Math.pow(1 - t, 3); }, seg = function (p, a, b) { return Math.min(1, Math.max(0, (p - a) / (b - a))); };
  var ticking = false, lastP = -1;
  function frame() {
    ticking = false;
    var y = window.scrollY || 0, vh = window.innerHeight, h = document.documentElement.scrollHeight - vh;
    if (nav) nav.classList.toggle('glass', y > 30);
    if (totop) totop.classList.toggle('show', y > vh);
    if (fort) fort.style.width = (h > 0 ? (y / h) * 100 : 0) + '%';
    if (!reduce) {
      if (heroBg && heroCopy) { var hp = Math.min(1, y / (vh * 0.9)); heroCopy.style.transform = 'translateY(' + (hp * -40) + 'px)'; heroCopy.style.opacity = String(1 - hp * 1.1); }
      parallax.forEach(function (el) { var r = el.parentElement.getBoundingClientRect(); if (r.bottom > 0 && r.top < vh) { var wp = (vh - r.top) / (vh + r.height); el.style.transform = 'translateY(' + ((wp - 0.5) * 90) + 'px)'; } });
    }
    if (P) {
      var r = pour.getBoundingClientRect(), total = r.height - vh; var p = total > 0 ? Math.min(1, Math.max(0, (-r.top) / total)) : 1;
      if (reduce) p = 1;
      if (Math.abs(p - lastP) > 0.0004 || p === 1 || p === 0) {
        lastP = p;
        var enter = ease(seg(p, 0, .14)), shake = seg(p, .14, .26), popT = ease(seg(p, .26, .46)), tilt = ease(seg(p, .30, .56)), pourT = seg(p, .50, .90), back = ease(seg(p, .90, 1));
        var wobble = (shake > 0 && shake < 1) ? Math.sin(shake * 40) * 2.2 * shake : 0;
        var rot = tilt * 62 - back * 34, lift = tilt * -230 + back * 90;
        P.bottle.style.transform = 'translateY(' + ((1 - enter) * 120 + lift) + 'px) rotate(' + (rot + wobble) + 'deg)'; P.bottle.style.opacity = String(Math.min(1, enter * 1.4));
        P.cap.style.transform = 'translate(' + (popT * 70) + 'px,' + (-popT * 300) + 'px) rotate(' + (popT * 400) + 'deg)'; P.cap.style.opacity = String(popT >= 1 ? 0 : 1 - popT * 1.05);
        var b = seg(p, .26, .36); $$('i', P.burst).forEach(function (d, i) { var ang = -100 + i * 26, dist = b * 60; d.style.transform = 'translate(' + (Math.cos(ang * Math.PI / 180) * dist) + 'px,' + (Math.sin(ang * Math.PI / 180) * dist) + 'px) scale(' + (1 - b) + ')'; d.style.opacity = String(b > 0 && b < 1 ? 1 - b : 0); });
        var on = pourT > 0 && pourT < 1 && tilt > .6;
        if (on) { var sr = P.scene.getBoundingClientRect(), m = P.mouth.getBoundingClientRect(), g = P.rim.getBoundingClientRect(); var x1 = m.left - sr.left, y1 = m.top - sr.top, x2 = g.left - sr.left, y2 = g.top - sr.top + 6; var dx = x2 - x1, dy = y2 - y1, len = Math.sqrt(dx * dx + dy * dy), ang2 = Math.atan2(dy, dx) * 180 / Math.PI - 90; var k = Math.min(1, pourT * 6, (1 - pourT) * 6); P.stream.style.left = x1 + 'px'; P.stream.style.top = y1 + 'px'; P.stream.style.height = (len * k) + 'px'; P.stream.style.transform = 'rotate(' + ang2 + 'deg)'; P.stream.style.opacity = String(Math.min(1, k * 1.2)); }
        else P.stream.style.opacity = '0';
        var level = ease(pourT) * 150; P.fill.setAttribute('y', String(212 - level)); P.fill.setAttribute('height', String(level)); P.foam.setAttribute('y', String(212 - level - 2)); P.foam.setAttribute('opacity', String(level > 4 ? .9 : 0));
        P.glass.classList.toggle('bubbling', level > 8 && !reduce); P.sante.classList.toggle('show', p > .93);
      }
    }
  }
  function onScroll() { if (!ticking) { ticking = true; requestAnimationFrame(frame); } }
  addEventListener('scroll', onScroll, { passive: true }); addEventListener('resize', onScroll); frame();

  /* Reveals, Zähler, Kiste */
  var io = new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } }); }, { rootMargin: '0px 0px -8% 0px', threshold: .08 });
  $$('.rv, .kiste-wrap').forEach(function (el) { io.observe(el); });
  function count(el) { var to = +el.getAttribute('data-count'); if (reduce) { el.textContent = to; return; } var t0 = null, dur = 1300; function f(t) { if (!t0) t0 = t; var p = Math.min(1, (t - t0) / dur); p = 1 - Math.pow(1 - p, 3); el.textContent = Math.round(to * p); if (p < 1) requestAnimationFrame(f); } requestAnimationFrame(f); }
  var cio = new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { count(e.target); cio.unobserve(e.target); } }); }, { threshold: .5 });
  $$('[data-count]').forEach(function (el) { cio.observe(el); });

  /* 3D-Neigung für Karten */
  if (!reduce && matchMedia('(hover: hover)').matches) {
    $$('[data-tilt]').forEach(function (el) {
      el.addEventListener('mousemove', function (e) { var r = el.getBoundingClientRect(); var x = (e.clientX - r.left) / r.width - .5, y = (e.clientY - r.top) / r.height - .5; el.style.transform = 'perspective(900px) rotateX(' + (-y * 7) + 'deg) rotateY(' + (x * 9) + 'deg) translateY(-4px)'; el.style.transition = 'transform .08s'; });
      el.addEventListener('mouseleave', function () { el.style.transform = ''; el.style.transition = 'transform .6s var(--ease)'; });
    });
  }

  /* Lightbox */
  var lb = $('#lightbox'), lbImg = $('#lbBild');
  document.addEventListener('click', function (e) { var a = e.target.closest('[data-lightbox]'); if (a && lb) { e.preventDefault(); lbImg.src = a.getAttribute('href'); lb.hidden = false; } if (e.target === lb || e.target.id === 'lbZu') lb.hidden = true; });

  /* Störer: Korken knallt, dann Katalog */
  var st = $('#stoerer'); if (st) st.addEventListener('click', function () { if (reduce) { location.href = root + 'katalog/'; return; } st.classList.remove('pop'); void st.offsetWidth; st.classList.add('pop'); setTimeout(function () { location.href = root + 'katalog/'; }, 650); });

  /* Kontaktformular (Entwurf: kein Versand) */
  var kf = $('#kontaktForm'); if (kf) kf.addEventListener('submit', function (e) { e.preventDefault(); var ok = $('#kontaktOk'); if (!$('#k-ds').checked) { ok.textContent = 'Bitte bestätigen Sie zuerst den Datenschutzhinweis.'; ok.classList.add('show'); return; } ok.textContent = 'Vielen Dank. Im Entwurf wird noch nichts versendet. Auf der echten Seite landet Ihre Nachricht bei info@weinstrasseadolph.de.'; ok.classList.add('show'); });

  /* ---------- Weinberater ---------- */
  var wb = $('#wb');
  if (wb && DATEN.produkte.length) {
    var wahl = {}; var schritte = $$('.wb-schritt', wb); schritte[0].classList.add('aktiv');
    wb.addEventListener('click', function (e) {
      var b = e.target.closest('.wb-opt button'); if (!b) return;
      var k = b.dataset.k, v = b.dataset.v; wahl[k] = v;
      $$('button', b.parentElement).forEach(function (x) { x.setAttribute('aria-pressed', x === b); });
      var i = schritte.indexOf(b.closest('.wb-schritt')); schritte[i].classList.add('fertig');
      if (schritte[i + 1]) { schritte[i + 1].classList.add('aktiv'); schritte[i + 1].scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'nearest' }); }
      if (wahl.anlass && wahl.farbe && wahl.preis) empfehle();
    });
    function empfehle() {
      var pr = wahl.preis.split('-').map(Number); var alle = DATEN.produkte.filter(function (p) { return p.listenpreis; });
      function score(p) {
        var s = 0;
        if (wahl.farbe !== 'egal') { if (p.kategorie === wahl.farbe) s += 5; else return -1; }
        if (p.listenpreis >= pr[0] && p.listenpreis <= pr[1]) s += 4; else s -= 3;
        var aw = (p.auszeichnungen || []).length, h = (p.hinweis || '').toLowerCase();
        if (wahl.anlass === 'aperitif') s += (p.kategorie === 'Schaumwein' ? 4 : p.kategorie === 'Weisswein' || p.kategorie === 'Rosé' ? 2 : 0);
        if (wahl.anlass === 'alltag') s += (p.listenpreis <= 6 ? 3 : 0) + (p.aktion ? 1 : 0);
        if (wahl.anlass === 'premium') s += Math.min(aw, 3) + (p.listenpreis >= 8 ? 2 : 0);
        if (wahl.anlass === 'geschenk') s += (/holzkiste|geschenk|limitiert|magnum|einzelkiste/.test(h) ? 4 : 0) + (p.uvp && p.uvp >= 20 ? 2 : 0) + Math.min(aw, 2);
        if (wahl.farbe === 'egal' && wahl.anlass === 'aperitif' && p.kategorie === 'Rotwein') s -= 2;
        return s + Math.random() * 1.5;
      }
      var liste = alle.map(function (p) { return [score(p), p]; }).filter(function (x) { return x[0] >= 0; }).sort(function (a, b) { return b[0] - a[0]; }).slice(0, 3).map(function (x) { return x[1]; });
      var box = $('#wbErgebnis'); box.hidden = false;
      if (!liste.length) { box.innerHTML = '<h3>Da finden wir nichts Passendes.</h3><p>Versuchen Sie eine andere Preisklasse.</p>'; return; }
      box.innerHTML = '<h3>Unsere drei Vorschläge für Sie</h3><div class="wb-tipps">' + liste.map(function (p) { var w = WG[p.weingut_slug] || {}; return '<article class="pk" data-id="' + p.id + '"><div class="pk-kopf"><img src="' + root + 'static/img/' + (w.logo || '') + '" alt=""><a href="' + root + (p.weingut_url || 'weingueter/') + '">' + esc(p.weingut_name || '') + '</a><span class="kat-dot kat-' + esc(p.kategorie) + '"></span></div><h3>' + esc(p.name) + '</h3><div class="pmeta">' + [p.jahrgang, p.groesse_cl + ' cl', p.rebsorte].filter(Boolean).map(esc).join(' · ') + '</div>' + (p.beschreibung ? '<p class="pk-desc">' + esc(p.beschreibung) + '</p>' : '') + ((p.auszeichnungen || []).length ? '<ul class="awards">' + p.auszeichnungen.slice(0, 2).map(function (a) { return '<li>🏅 ' + esc(a) + '</li>'; }).join('') + '</ul>' : '') + '<div class="pk-fuss"><div class="ppreis"><b>' + fmt(p.listenpreis) + '</b>' + (p.uvp ? '<span class="uvp">UVP ' + fmt(p.uvp) + '</span>' : '') + '</div><div class="pmenge"><button type="button" class="qty-minus">−</button><input type="number" min="0" step="' + (p.ve || 6) + '" value="' + (p.ve || 6) + '" data-ve="' + (p.ve || 6) + '" aria-label="Flaschen"><button type="button" class="qty-plus">+</button></div><button type="button" class="btn klein add" data-id="' + p.id + '">In die Bestellung</button></div></article>'; }).join('') + '</div><button type="button" class="btn ghost klein wb-reset" id="wbReset">Noch einmal</button>';
      $('#wbReset').addEventListener('click', function () { wahl = {}; box.hidden = true; schritte.forEach(function (s, i) { s.classList.toggle('aktiv', i === 0); s.classList.remove('fertig'); $$('button', s).forEach(function (x) { x.setAttribute('aria-pressed', 'false'); }); }); });
      konfetti(); WS.aktualisiere();
    }
    function konfetti() {
      var c = $('#konfetti'); if (!c || reduce) return; var ctx = c.getContext('2d'); c.width = wb.offsetWidth; c.height = wb.offsetHeight;
      var teile = []; for (var i = 0; i < 90; i++) teile.push({ x: c.width / 2, y: c.height * .6, vx: (Math.random() - .5) * 14, vy: -Math.random() * 14 - 4, r: Math.random() * 5 + 3, col: ['#5c161e', '#d5b78e', '#f3dfc0', '#eadfd9', '#9b2c22'][i % 5], rot: Math.random() * 6 });
      var t0 = performance.now();
      (function tick(t) { var dt = (t - t0) / 1000; ctx.clearRect(0, 0, c.width, c.height); teile.forEach(function (k) { k.vy += .45; k.x += k.vx; k.y += k.vy; k.rot += .1; ctx.save(); ctx.translate(k.x, k.y); ctx.rotate(k.rot); ctx.fillStyle = k.col; ctx.globalAlpha = Math.max(0, 1 - dt / 1.8); ctx.fillRect(-k.r / 2, -k.r / 2, k.r, k.r * 1.6); ctx.restore(); }); if (dt < 1.9) requestAnimationFrame(tick); else ctx.clearRect(0, 0, c.width, c.height); })(t0);
    }
  }
})();
