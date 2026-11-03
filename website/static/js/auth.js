/* Fachhändler-Anmeldung. Die Einkaufspreise liegen nur verschlüsselt vor (window.WS_PREISE, AES-256-GCM).
   Kundennummer + Passwort leiten per PBKDF2 einen Schlüssel ab, der den Preisschlüssel des Kunden
   entpackt (window.WS_ZUGAENGE). Ohne gültige Anmeldung bleiben die Preise unlesbar. */
(function () {
  'use strict';
  var D = window.WS_DATEN || { produkte: [] }, ENC = window.WS_PREISE || null, ZUG = window.WS_ZUGAENGE || [];
  var KEY = 'ws_zugang_v1';
  var enc = new TextEncoder(), dec = new TextDecoder();
  var A = window.WS_AUTH = { kunde: null, bereit: false };
  function b64d(s) { var b = atob(s), u = new Uint8Array(b.length); for (var i = 0; i < b.length; i++) u[i] = b.charCodeAt(i); return u; }
  function b64e(u) { var s = ''; u = new Uint8Array(u); for (var i = 0; i < u.length; i++) s += String.fromCharCode(u[i]); return btoa(s); }
  function fmt(n) { return (Math.round(n * 100) / 100).toFixed(2).replace('.', ',') + ' €'; }
  var subtle = (window.crypto || {}).subtle;

  function preiseAnwenden(map) {
    D.produkte.forEach(function (p) { var v = map[p.id]; if (v) { p.listenpreis = v[0]; p.aktion = v[1]; p.aktionspreis = v[2]; } });
  }
  async function entschluesseln(master) {
    if (!ENC) throw new Error('keine Preise');
    var key = await subtle.importKey('raw', master, { name: 'AES-GCM' }, false, ['decrypt']);
    var klar = await subtle.decrypt({ name: 'AES-GCM', iv: b64d(ENC.iv) }, key, b64d(ENC.daten));
    return JSON.parse(dec.decode(klar));
  }
  async function schluesselAusPasswort(kundennummer, passwort) {
    var z = ZUG.filter(function (x) { return x.kundennummer.toLowerCase() === kundennummer.trim().toLowerCase(); })[0];
    if (!z) throw new Error('unbekannt');
    var km = await subtle.importKey('raw', enc.encode(passwort), 'PBKDF2', false, ['deriveKey']);
    var kek = await subtle.deriveKey({ name: 'PBKDF2', salt: b64d(z.salt), iterations: z.iterationen || 250000, hash: 'SHA-256' }, km, { name: 'AES-GCM', length: 256 }, false, ['decrypt']);
    var master = await subtle.decrypt({ name: 'AES-GCM', iv: b64d(z.iv), additionalData: enc.encode(z.kundennummer.toLowerCase()) }, kek, b64d(z.schluessel));
    return { master: master, kunde: { kundennummer: z.kundennummer, firma: z.firma } };
  }
  function setzeZustand(kunde) {
    A.kunde = kunde; A.bereit = true;
    document.body.classList.toggle('angemeldet', !!kunde);
    var n = document.getElementById('loginName'); if (n) n.textContent = kunde ? kunde.firma : '';
    var f = document.getElementById('loginFirma'); if (f) f.textContent = kunde ? kunde.firma + ' · Kd.-Nr. ' + kunde.kundennummer : '';
    A.renderPreise();
    document.dispatchEvent(new CustomEvent('auth:geaendert', { detail: { kunde: kunde } }));
  }
  A.renderPreise = function () {
    var an = !!A.kunde;
    var byId = {}; D.produkte.forEach(function (p) { byId[p.id] = p; });
    Array.prototype.forEach.call(document.querySelectorAll('[data-preis-id]'), function (el) {
      var p = byId[el.getAttribute('data-preis-id')]; var w = el.querySelector('.preis-wert'), a = el.querySelector('.aktion');
      if (!p) return;
      if (an && p.listenpreis != null) { if (w) { w.textContent = fmt(p.listenpreis); w.classList.remove('gesperrt'); } if (a) a.textContent = p.aktion ? p.aktion + (p.aktionspreis ? ' → ' + fmt(p.aktionspreis) : '') : ''; }
      else { if (w) { w.textContent = an ? 'Preis auf Anfrage' : 'Preis nach Anmeldung'; w.classList.add('gesperrt'); } if (a) a.textContent = ''; }
    });
  };
  A.anmelden = async function (kundennummer, passwort, merken) {
    if (!subtle) throw new Error('Dieser Browser unterstützt die sichere Anmeldung nicht.');
    var r = await schluesselAusPasswort(kundennummer, passwort);
    var map = await entschluesseln(r.master);
    preiseAnwenden(map);
    try { var ablage = merken ? localStorage : sessionStorage; ablage.setItem(KEY, JSON.stringify({ k: b64e(r.master), kunde: r.kunde })); (merken ? sessionStorage : localStorage).removeItem(KEY); } catch (e) { }
    setzeZustand(r.kunde);
    return r.kunde;
  };
  A.abmelden = function () {
    try { localStorage.removeItem(KEY); sessionStorage.removeItem(KEY); } catch (e) { }
    D.produkte.forEach(function (p) { delete p.listenpreis; delete p.aktion; delete p.aktionspreis; });
    setzeZustand(null);
  };
  async function wiederherstellen() {
    var roh = null; try { roh = sessionStorage.getItem(KEY) || localStorage.getItem(KEY); } catch (e) { }
    if (!roh || !subtle) { setzeZustand(null); return; }
    try { var s = JSON.parse(roh); var map = await entschluesseln(b64d(s.k)); preiseAnwenden(map); setzeZustand(s.kunde); }
    catch (e) { A.abmelden(); }
  }

  /* UI: Modal, Formulare, Schalter */
  function modal() { return document.getElementById('loginModal'); }
  A.oeffne = function () { var m = modal(); if (!m) return; m.hidden = false; var i = m.querySelector('input[name=kundennummer]'); if (i && !A.kunde) setTimeout(function () { i.focus(); }, 50); };
  A.schliesse = function () { var m = modal(); if (m) m.hidden = true; };
  document.addEventListener('click', function (e) {
    var t = e.target.closest('[data-login]'); if (t) { e.preventDefault(); A.oeffne(); return; }
    if (e.target.id === 'loginZu' || e.target.id === 'loginModal') A.schliesse();
    if (e.target.id === 'logoutBtn') { A.abmelden(); A.schliesse(); if (window.WS && WS.toast) WS.toast('Abgemeldet. Preise sind wieder verborgen.'); }
  });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') A.schliesse(); });
  document.addEventListener('submit', async function (e) {
    var f = e.target.closest('[data-login-form]'); if (!f) return;
    e.preventDefault();
    var kd = f.kundennummer.value.trim(), pw = f.passwort.value, merken = f.merken && f.merken.checked; var fehler = f.querySelector('.fehler'); var btn = f.querySelector('button[type=submit]');
    fehler.classList.remove('show');
    if (!kd || !pw) { fehler.textContent = 'Bitte Kundennummer und Passwort eingeben.'; fehler.classList.add('show'); return; }
    btn.disabled = true; btn.textContent = 'Prüfe …';
    try { var k = await A.anmelden(kd, pw, merken); f.reset(); A.schliesse(); if (window.WS && WS.toast) WS.toast('Willkommen, ' + k.firma + '. Preise freigeschaltet.'); }
    catch (err) { fehler.textContent = err && err.message === 'unbekannt' ? 'Diese Kundennummer ist nicht freigeschaltet.' : (err && err.message && err.message.indexOf('Browser') > -1 ? err.message : 'Kundennummer oder Passwort stimmen nicht.'); fehler.classList.add('show'); }
    finally { btn.disabled = false; btn.textContent = 'Anmelden'; }
  });
  wiederherstellen();
})();
