/* Katalog (Filter, Suche, Ansicht) und Bestellportal (Gruppen je Weingut, Mindestmengen, Palette, Abschluss). */
(function () {
  'use strict';
  var WS = window.WS; if (!WS) return;
  var D = WS.daten, PROD = WS.produkte, WG = WS.weingueter, root = WS.root, fmt = WS.fmt, esc = WS.esc; var IX = document.querySelector('a[href$="index.html"]') ? 'index.html' : '';
  var $ = function (s, el) { return (el || document).querySelector(s); }, $$ = function (s, el) { return Array.prototype.slice.call((el || document).querySelectorAll(s)); };
  var LAENDER = ['Italien', 'Frankreich', 'Spanien', 'Portugal', 'Bulgarien', 'England', 'Argentinien', 'Australien'];
  var KATS = ['Schaumwein', 'Weisswein', 'Rosé', 'Rotwein', 'Süsswein', 'Portwein', 'Spirituose', 'Alkoholfrei', 'Olivenöl', 'Cidre', 'Sonstiges'];
  var KAT_LABEL = { Weisswein: 'Weißwein', Süsswein: 'Süßwein' };

  function karte(p, mitBeschreibung) {
    var w = WG[p.weingut_slug] || {}; var q = WS.menge(p.id); var ve = p.ve || 6;
    return '<article class="pk' + (q ? ' im-korb' : '') + '" data-id="' + p.id + '">' +
      '<div class="pk-kopf"><img src="' + root + 'static/img/' + (w.logo || '') + '" alt="" loading="lazy"><a href="' + root + (p.weingut_url || 'weingueter/') + '">' + esc(p.weingut_name || '') + ' · ' + esc(p.land || '') + '</a><span class="kat-dot kat-' + esc(p.kategorie) + '" title="' + esc(KAT_LABEL[p.kategorie] || p.kategorie) + '"></span></div>' +
      '<h3>' + esc(p.name) + '</h3><div class="pmeta">' + [p.jahrgang, p.groesse_cl ? p.groesse_cl + ' cl' : null, p.region, p.rebsorte].filter(Boolean).map(esc).join(' · ') + (p.bio ? ' · <i class="bio">BIO</i>' : '') + (p.hinweis ? ' · ' + esc(p.hinweis) : '') + '</div>' +
      (mitBeschreibung && p.beschreibung ? '<p class="pk-desc">' + esc(p.beschreibung) + '</p>' : '') +
      ((p.auszeichnungen || []).length ? '<ul class="awards">' + p.auszeichnungen.slice(0, 3).map(function (a) { return '<li>🏅 ' + esc(a) + '</li>'; }).join('') + '</ul>' : '') +
      '<div class="pk-fuss"><div class="ppreis"><b>' + fmt(p.listenpreis || 0) + (p.zusatzsteuer ? '<span class="stern" title="' + esc(p.zusatzsteuer) + '">*</span>' : '') + '</b>' + (p.uvp ? '<span class="uvp">UVP ' + fmt(p.uvp) + '</span>' : '') + (p.aktion ? '<span class="aktion">' + esc(p.aktion) + (p.aktionspreis ? ' → ' + fmt(p.aktionspreis) : '') + '</span>' : '') + '</div>' +
      '<div class="pmenge"><button type="button" class="qty-minus" aria-label="weniger">−</button><input type="number" min="0" step="' + ve + '" value="' + (q || 0) + '" data-ve="' + ve + '" aria-label="Flaschen"><button type="button" class="qty-plus" aria-label="mehr">+</button></div>' +
      '<button type="button" class="btn klein add" data-id="' + p.id + '">' + (q ? 'Menge übernehmen' : 'In die Bestellung') + '</button></div></article>';
  }

  /* ---------------- Katalog ---------------- */
  var liste = $('#kListe');
  if (liste) {
    var f = { land: 'alle', kat: 'alle', preis: 100, awards: false, bio: false, aktion: false, q: '', sort: 'weingut' }, seite = 0, SEITE = 48, ansicht = 'karten';
    var hoechster = D.produkte.reduce(function (m, p) { return Math.max(m, p.listenpreis || 0); }, 0); var maxPreis = hoechster > 0 ? Math.min(100, Math.ceil(hoechster / 10) * 10) : 100;
    var fp = $('#fPreis'); fp.max = maxPreis; fp.value = maxPreis; f.preis = maxPreis; $('#fPreisWert').textContent = 'bis ' + maxPreis + ' €';
    function chips(el, werte, key, labels) { el.innerHTML = ['alle'].concat(werte).map(function (v) { return '<button class="chip" type="button" data-v="' + esc(v) + '" aria-pressed="' + (v === 'alle') + '">' + (v === 'alle' ? 'Alle' : esc((labels && labels[v]) || v)) + '</button>'; }).join(''); el.addEventListener('click', function (e) { var b = e.target.closest('.chip'); if (!b) return; $$('.chip', el).forEach(function (x) { x.setAttribute('aria-pressed', x === b); }); f[key] = b.dataset.v; seite = 0; render(); }); }
    var laender = LAENDER.filter(function (l) { return D.produkte.some(function (p) { return p.land === l; }); });
    var kats = KATS.filter(function (k) { return D.produkte.some(function (p) { return p.kategorie === k; }); });
    chips($('#fLand'), laender, 'land'); chips($('#fKat'), kats, 'kat', KAT_LABEL);
    fp.addEventListener('input', function () { f.preis = +fp.value; $('#fPreisWert').textContent = 'bis ' + fp.value + ' €'; seite = 0; render(); });
    ['fAwards', 'fBio', 'fAktion'].forEach(function (id, i) { $('#' + id).addEventListener('change', function (e) { f[['awards', 'bio', 'aktion'][i]] = e.target.checked; seite = 0; render(); }); });
    $('#kSuche').addEventListener('input', function (e) { f.q = e.target.value.trim().toLowerCase(); seite = 0; render(); });
    $('#fSort').addEventListener('change', function (e) { f.sort = e.target.value; render(); });
    $('#fReset').addEventListener('click', function () { f = { land: 'alle', kat: 'alle', preis: maxPreis, awards: false, bio: false, aktion: false, q: '', sort: 'weingut' }; fp.value = maxPreis; $('#fPreisWert').textContent = 'bis ' + maxPreis + ' €'; $('#kSuche').value = ''; ['fAwards', 'fBio', 'fAktion'].forEach(function (id) { $('#' + id).checked = false; }); $$('#fLand .chip, #fKat .chip').forEach(function (c) { c.setAttribute('aria-pressed', c.dataset.v === 'alle'); }); seite = 0; render(); });
    $$('.ansicht button').forEach(function (b) { b.addEventListener('click', function () { ansicht = b.dataset.ansicht; $$('.ansicht button').forEach(function (x) { x.setAttribute('aria-pressed', x === b); }); liste.classList.toggle('liste', ansicht === 'liste'); render(); }); });
    $('#kMehr').addEventListener('click', function () { seite++; render(true); });
    var params = new URLSearchParams(location.search); if (params.get('weingut')) f.q = params.get('weingut').toLowerCase();
    function gefiltert() {
      var r = D.produkte.filter(function (p) {
        if (f.land !== 'alle' && p.land !== f.land) return false;
        if (f.kat !== 'alle' && p.kategorie !== f.kat) return false;
        if ((p.listenpreis || 0) > f.preis) return false;
        if (f.awards && !(p.auszeichnungen || []).length) return false;
        if (f.bio && !p.bio) return false;
        if (f.aktion && !p.aktion) return false;
        if (f.q) { var h = [p.name, p.weingut_name, p.region, p.rebsorte, p.kategorie, p.land, p.jahrgang, p.hinweis].join(' ').toLowerCase(); if (h.indexOf(f.q) < 0) return false; }
        return true;
      });
      var s = f.sort;
      r.sort(function (a, b) { if (s === 'preis-auf') return (a.listenpreis || 0) - (b.listenpreis || 0); if (s === 'preis-ab') return (b.listenpreis || 0) - (a.listenpreis || 0); if (s === 'name') return a.name.localeCompare(b.name, 'de'); return 0; });
      return r;
    }
    function render(anhaengen) {
      var r = gefiltert(); $('#kAnzahl').textContent = r.length + ' von ' + D.produkte.length + ' Artikeln';
      var teil = r.slice(0, (seite + 1) * SEITE);
      var html = teil.map(function (p) { return karte(p, ansicht === 'karten'); }).join('');
      liste.innerHTML = html || '<div class="leer">Nichts gefunden. Filter anpassen oder zurücksetzen.</div>';
      $('#kMehr').hidden = r.length <= teil.length;
    }
    render();
    document.addEventListener('korb:geaendert', function () { $$('.pk', liste).forEach(function (el) { var q = WS.menge(el.dataset.id); el.classList.toggle('im-korb', q > 0); var b = $('.add', el); if (b) b.textContent = q ? 'Menge übernehmen' : 'In die Bestellung'; }); });
  }

  /* ---------------- Bestellportal ---------------- */
  var gruppen = $('#bestellGruppen');
  if (gruppen) {
    var palette = $('#palette'); for (var i = 0; i < 100; i++) { var c = document.createElement('i'); palette.appendChild(c); }
    function fracht(w, menge) {
      var staffeln = (w.frachtkosten || []).map(function (t) { var m = /ab\s*(\d[\d.]*)\s*Fl[^0-9]*?([\d]+,[\d]+)\s*€/i.exec(t); return m ? [parseInt(m[1].replace('.', ''), 10), parseFloat(m[2].replace(',', '.'))] : null; }).filter(Boolean).sort(function (a, b) { return a[0] - b[0]; });
      if (!staffeln.length) return null;
      var satz = staffeln[0][1]; staffeln.forEach(function (s) { if (menge >= s[0]) satz = s[1]; });
      return satz * menge;
    }
    function render() {
      var korb = WS.korb(), ids = Object.keys(korb);
      $('#bestellLeer').hidden = ids.length > 0; $('#kunde').hidden = ids.length === 0;
      var nachWg = {};
      ids.forEach(function (id) { var p = PROD[id]; if (!p) return; (nachWg[p.weingut_slug] = nachWg[p.weingut_slug] || []).push(p); });
      var gesamtFl = 0, waren = 0, frachtSumme = 0, frachtUnbekannt = false, minHtml = '';
      gruppen.innerHTML = Object.keys(nachWg).map(function (slug) {
        var w = WG[slug] || { name: slug, mindestmenge: 120, logo: '' }; var ps = nachWg[slug];
        var fl = ps.reduce(function (s, p) { return s + korb[p.id]; }, 0); var summe = ps.reduce(function (s, p) { return s + (p.listenpreis || 0) * korb[p.id]; }, 0);
        var min = w.mindestmenge || 120, ok = fl >= min; gesamtFl += fl; waren += summe;
        var fr = fracht(w, fl); if (fr == null) frachtUnbekannt = true; else frachtSumme += fr;
        minHtml += '<div><span>' + esc(w.name) + '</span><span class="' + (ok ? 'ok' : 'fehlt') + '">' + fl + ' / ' + min + ' Fl.</span></div>';
        return '<section class="bgrp"><header><img src="' + root + 'static/img/' + (w.logo || '') + '" alt=""><div><b>' + esc(w.name) + '</b><span>' + fl + ' Flaschen · Mindestmenge ' + min + (w.abwicklung === 'WIK' ? ' · Abwicklung Wein-Import-Kreis' : '') + '</span><div class="min-balken"><i style="width:' + Math.min(100, fl / min * 100) + '%"></i></div></div><span class="min-status ' + (ok ? 'ok' : 'fehlt') + '">' + (ok ? 'Mindestmenge erreicht' : 'noch ' + (min - fl) + ' Fl.') + '</span></header>' +
          ps.map(function (p) { return '<div class="brow" data-id="' + p.id + '"><div class="bname"><b>' + esc(p.name) + '</b><span>' + [p.jahrgang, p.groesse_cl + ' cl', p.region].filter(Boolean).map(esc).join(' · ') + (p.zusatzsteuer ? ' · ' + esc(p.zusatzsteuer) : '') + '</span></div><div class="pmenge"><button type="button" class="qty-minus" aria-label="weniger">−</button><input type="number" min="0" step="' + (p.ve || 6) + '" value="' + korb[p.id] + '" data-ve="' + (p.ve || 6) + '" aria-label="Flaschen"><button type="button" class="qty-plus" aria-label="mehr">+</button></div><div class="bpreis">à ' + fmt(p.listenpreis || 0) + '</div><div class="bsumme">' + fmt((p.listenpreis || 0) * korb[p.id]) + '</div><button type="button" class="entfernen" data-remove="' + p.id + '" aria-label="Entfernen">✕</button></div>'; }).join('') +
          '<footer><span>Warenwert ' + fmt(summe) + '</span><span>' + (fr == null ? 'Fracht auf Anfrage' : 'Fracht ca. ' + fmt(fr) + ' (' + (w.frachtkosten || []).join(', ') + ')') + '</span></footer></section>';
      }).join('');
      var voll = Math.floor(gesamtFl / 600), rest = gesamtFl % 600; var zellen = $$('i', palette);
      var anteil = gesamtFl === 0 ? 0 : (rest === 0 ? 100 : Math.round(rest / 6));
      zellen.forEach(function (z, i) { z.className = i < anteil ? 'voll' : ''; if (i === anteil && gesamtFl > 0 && anteil < 100) z.className = 'aktiv'; });
      $('#pFlaschen').textContent = gesamtFl; $('#pPaletten').textContent = (gesamtFl / 600).toFixed(1).replace('.', ',');
      $('#minCheck').innerHTML = minHtml || '<div><span>Noch keine Weingüter in der Bestellung.</span></div>';
      $('#sWaren').textContent = fmt(waren); $('#sFracht').textContent = frachtSumme ? '≈ ' + fmt(frachtSumme) + (frachtUnbekannt ? ' +' : '') : (ids.length ? 'auf Anfrage' : '–'); $('#sGesamt').textContent = fmt(waren + frachtSumme);
      WS.aktualisiere();
    }
    gruppen.addEventListener('click', function (e) { var b = e.target.closest('[data-remove]'); if (b) { WS.setze(b.dataset.remove, 0, true); render(); } });
    document.addEventListener('korb:geaendert', function () { if (!gruppen._busy) render(); });
    gruppen.addEventListener('input', function (e) { if (e.target.matches('.pmenge input')) { gruppen._busy = true; var row = e.target.closest('.brow'); WS.setze(row.dataset.id, +e.target.value || 0, true); gruppen._busy = false; var p = PROD[row.dataset.id]; $('.bsumme', row).textContent = fmt((p.listenpreis || 0) * (+e.target.value || 0)); } });
    gruppen.addEventListener('change', function () { render(); });
    $('#bLeeren').addEventListener('click', function () { if ($('#bLeeren').dataset.sicher) { WS.leere(); render(); $('#bLeeren').textContent = 'Bestellung leeren'; delete $('#bLeeren').dataset.sicher; } else { $('#bLeeren').dataset.sicher = '1'; $('#bLeeren').textContent = 'Wirklich leeren?'; setTimeout(function () { delete $('#bLeeren').dataset.sicher; $('#bLeeren').textContent = 'Bestellung leeren'; }, 3000); } });
    $('#bestellForm').addEventListener('submit', function (e) {
      e.preventDefault();
      var pflicht = ['b-firma', 'b-name', 'b-mail', 'b-adresse']; var fehlt = pflicht.filter(function (id) { return !$('#' + id).value.trim(); });
      if (fehlt.length) { $('#' + fehlt[0]).focus(); WS.toast('Bitte füllen Sie die Pflichtfelder aus.'); return; }
      if (!$('#b-agb').checked) { $('#b-agb').focus(); WS.toast('Bitte bestätigen Sie die Konditionen.'); return; }
      var korb = WS.korb(), ids = Object.keys(korb); if (!ids.length) { WS.toast('Die Bestellung ist leer.'); return; }
      var kunde = { firma: $('#b-firma').value.trim(), kundennummer: $('#b-kdnr').value.trim(), ansprechpartner: $('#b-name').value.trim(), email: $('#b-mail').value.trim(), telefon: $('#b-tel').value.trim(), lieferadresse: $('#b-adresse').value.trim(), wunschtermin: $('#b-termin').value, bemerkung: $('#b-bem').value.trim() };
      var nachWg = {}; ids.forEach(function (id) { var p = PROD[id]; if (p) (nachWg[p.weingut_slug] = nachWg[p.weingut_slug] || []).push(p); });
      var datum = new Date().toLocaleDateString('de-DE'); var zeilen = ['Bestellung Weinstrasse Adolph GmbH · ' + datum, '', 'Kunde: ' + kunde.firma + (kunde.kundennummer ? ' (Kd.-Nr. ' + kunde.kundennummer + ')' : ''), 'Ansprechpartner: ' + kunde.ansprechpartner + ' · ' + kunde.email + (kunde.telefon ? ' · ' + kunde.telefon : ''), 'Lieferadresse: ' + kunde.lieferadresse, kunde.wunschtermin ? 'Wunschtermin: ' + kunde.wunschtermin : null, kunde.bemerkung ? 'Bemerkung: ' + kunde.bemerkung : null, ''].filter(function (x) { return x !== null; });
      var waren = 0, positionen = [];
      Object.keys(nachWg).forEach(function (slug) { var w = WG[slug] || { name: slug }; var fl = nachWg[slug].reduce(function (s, p) { return s + korb[p.id]; }, 0); zeilen.push(w.name + ' (' + fl + ' Flaschen, Mindestmenge ' + (w.mindestmenge || 120) + (w.abwicklung === 'WIK' ? ', Abwicklung WIK' : '') + ')'); nachWg[slug].forEach(function (p) { var s = (p.listenpreis || 0) * korb[p.id]; waren += s; zeilen.push('  ' + korb[p.id] + ' Fl. × ' + p.name + (p.jahrgang ? ' ' + p.jahrgang : '') + ' ' + p.groesse_cl + ' cl à ' + fmt(p.listenpreis || 0) + ' = ' + fmt(s) + (p.zusatzsteuer ? ' (' + p.zusatzsteuer + ')' : '')); positionen.push({ artikel_id: p.id, bezeichnung: p.name, jahrgang: p.jahrgang, groesse_cl: p.groesse_cl, menge: korb[p.id], einzelpreis: p.listenpreis, lieferant: w.name, lieferant_slug: slug }); }); zeilen.push(''); });
      zeilen.push('Warenwert netto ab Keller: ' + fmt(waren), 'Zzgl. Fracht, ggf. Schaumwein-/Branntweinsteuer und optionale Services laut Katalog 2026.', '', 'Verbindlich bestellt zu den Konditionen des Katalogs 2026. Bitte Auftragsbestätigung per E-Mail.');
      var text = zeilen.join('\n'); $('#bText').value = text; $('#bestaetigung').hidden = false; $('#bestaetigung').scrollIntoView({ behavior: 'smooth', block: 'start' });
      $('#bMail').href = 'mailto:info@weinstrasseadolph.de?subject=' + encodeURIComponent('Bestellung ' + kunde.firma + ' · ' + datum) + '&body=' + encodeURIComponent(text);
      var json = { quelle: 'weinstrasseadolph.de/bestellen', datum: new Date().toISOString(), kunde: kunde, positionen: positionen, warenwert_netto: Math.round(waren * 100) / 100 };
      $('#bKopieren').onclick = function () { kopiere(text, 'Bestelltext kopiert.'); };
      $('#bJson').onclick = function () { kopiere(JSON.stringify(json, null, 1), 'JSON für den Auftragsautomaten kopiert.'); };
      WS.toast('Bestellung vorbereitet.');
    });
    function kopiere(t, msg) { var ta = $('#bText'); if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(function () { WS.toast(msg); }, function () { ta.select(); WS.toast('Bitte mit Strg+C kopieren.'); }); else { ta.select(); WS.toast('Bitte mit Strg+C kopieren.'); } }
    render();
  }
})();
