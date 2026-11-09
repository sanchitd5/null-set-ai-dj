/* /admin/graph: search a song, draw it with its neighbours (cytoscape, vendor/cytoscape.min.js), tap to expand.
   All server text goes in through textContent / cytoscape data, never innerHTML. */
(function () {
  'use strict';
  var COLORS = {measured: '#4ade80', predicted: '#c084fc', yt_related: '#f87171', yt_upnext: '#fb923c',
    played_ok: '#38bdf8', skipped: '#94a3b8', coplay: '#facc15', similar: '#f472b6', artist_of: '#64748b', in_scene: '#64748b'};
  var DEFAULT_KINDS = ['measured', 'predicted', 'yt_related', 'yt_upnext', 'played_ok', 'skipped', 'coplay', 'similar'];
  var $ = function (id) { return document.getElementById(id); };
  var cy = null, centerId = null, expanded = {};

  function el(tag, text, attrs) {
    var n = document.createElement(tag);
    if (text != null) n.textContent = text;
    for (var k in (attrs || {})) n.setAttribute(k, attrs[k]);
    return n;
  }
  function getJSON(url) {
    return fetch(url, {credentials: 'same-origin'}).then(function (r) {
      if (!r.ok) throw new Error(r.status + ' ' + url);
      return r.json();
    });
  }
  function trendText(t) {
    if (!t) return null;
    return '#' + t.rank + ' (' + Object.keys(t.regions).sort().map(function (r) { return r + ' #' + t.regions[r]; }).join(', ') + ')';
  }
  function fmtTime(s) { var n = Number(s); return n ? new Date(n * 1000).toISOString().replace('T', ' ').slice(0, 19) + ' UTC' : '-'; }

  function filters() {
    var kinds = [];
    document.querySelectorAll('#gkinds input:checked').forEach(function (c) { kinds.push(c.value); });
    return {kinds: kinds, minw: parseFloat($('gminw').value) || 0, lang: $('glang').value, k: parseInt($('gk').value, 10) || 12};
  }

  function buildControls(stats) {
    var box = $('gkinds'), leg = $('glegend');
    box.textContent = ''; leg.textContent = '';
    DEFAULT_KINDS.forEach(function (k) {
      var l = el('label'), c = el('input', null, {type: 'checkbox', value: k});
      c.checked = true; c.addEventListener('change', applyFilters);
      l.appendChild(c); l.appendChild(document.createTextNode(k));
      box.appendChild(l);
      var s = el('span'), i = el('i'); i.style.background = COLORS[k];
      s.appendChild(i); s.appendChild(document.createTextNode(k)); leg.appendChild(s);
    });
    var sel = $('glang');
    (stats.langs || []).forEach(function (g) { sel.appendChild(el('option', g, {value: g})); });
  }

  function showStats(s) {
    var box = $('gstats');
    box.textContent = '';
    if (!s || s.available === false) { box.textContent = 'No graph.db yet. Rebuild it from the Jobs page.'; return; }
    var parts = ['rev ' + s.rev, 'built ' + fmtTime(s.built_at), s.nodes + ' nodes', s.edges + ' edges'];
    if (s.trending) parts.push(s.trending + ' trending');
    var ek = s.edge_kinds || {};
    Object.keys(ek).sort().forEach(function (k) { parts.push(k + ' ' + ek[k]); });
    box.textContent = parts.join(' | ');
  }

  function initCy() {
    if (cy) return;
    var style = [
      {selector: 'node', style: {'background-color': '#475569', label: 'data(label)', color: '#e5e7eb', 'font-size': 11,
        'text-wrap': 'ellipsis', 'text-max-width': 110, 'text-valign': 'bottom', 'text-margin-y': 4, width: 26, height: 26}},
      {selector: 'node[?library]', style: {'background-color': '#2563eb'}},
      {selector: 'node[?trending]', style: {'border-width': 3, 'border-color': '#fbbf24'}},
      {selector: 'node.center', style: {'border-width': 4, 'border-color': '#fbbf24', width: 38, height: 38}},
      {selector: 'node.done', style: {'border-width': 2, 'border-color': '#e5e7eb'}},
      {selector: 'node:selected', style: {'overlay-color': '#fbbf24', 'overlay-opacity': 0.25}},
      {selector: 'edge', style: {width: 'mapData(weight, 0, 1, 1, 8)', 'line-color': 'data(color)', opacity: 0.85,
        'curve-style': 'bezier', 'target-arrow-shape': 'triangle', 'target-arrow-color': 'data(color)', 'arrow-scale': 0.8}},
      {selector: 'edge[kind = "skipped"]', style: {'line-style': 'dashed'}},
      {selector: '.hidden', style: {display: 'none'}}
    ];
    cy = cytoscape({container: $('gcy'), style: style, minZoom: 0.15, maxZoom: 4, wheelSensitivity: 0.3});
    cy.on('tap', 'node', function (ev) { var id = ev.target.data('nid'); expand(id); showDetail(id); });
    cy.on('taphold', 'node', function (ev) { showDetail(ev.target.data('nid')); });
    window.djGraph = {cy: cy};
  }

  function addGraph(g) {
    initCy();
    cy.batch(function () {
      g.nodes.forEach(function (n) {
        if (cy.getElementById('n' + n.id).empty())
          cy.add({group: 'nodes', data: {id: 'n' + n.id, nid: n.id, label: n.label, library: n.library, lang: n.lang || '',
            trending: !!n.trend}});
      });
      g.edges.forEach(function (e) {
        var id = 'e' + e.a + '_' + e.b + '_' + e.kind;
        if (cy.getElementById(id).empty())
          cy.add({group: 'edges', data: {id: id, source: 'n' + e.a, target: 'n' + e.b, kind: e.kind, weight: Math.min(1, Math.max(0, e.weight)),
            color: COLORS[e.kind] || '#64748b'}});
      });
    });
    applyFilters();
    cy.layout({name: 'cose', animate: false, fit: true, padding: 30, nodeRepulsion: 9000, idealEdgeLength: 90}).run();
  }

  function applyFilters() {
    if (!cy) return;
    var f = filters();
    cy.batch(function () {
      cy.edges().forEach(function (e) {
        var off = f.kinds.indexOf(e.data('kind')) < 0 || e.data('weight') < f.minw;
        e.toggleClass('hidden', off);
      });
      cy.nodes().forEach(function (n) {
        var off = !!f.lang && n.data('lang') !== f.lang && n.data('nid') !== centerId;
        n.toggleClass('hidden', off);
      });
    });
  }

  function expand(id) {
    var f = filters(), key = id + '|' + f.k + '|' + f.kinds.join();
    if (expanded[key]) return Promise.resolve();
    var url = '/api/admin/graph/neighbours?id=' + id + '&k=' + f.k + '&kinds=' + encodeURIComponent(f.kinds.join()) +
      '&min_weight=' + f.minw + '&lang=' + encodeURIComponent(f.lang);
    return getJSON(url).then(function (g) {
      expanded[key] = true; addGraph(g);
      var n = cy.getElementById('n' + id); n.addClass('done');
    }).catch(function (e) { $('gstats').textContent = String(e.message || e); });
  }

  function showDetail(id) {
    getJSON('/api/admin/graph/node?id=' + id).then(function (d) {
      var p = $('gpanel'), n = d.node;
      p.hidden = false; p.textContent = '';
      p.appendChild(el('h2', n.label));
      var kv = el('table', null, {'class': 'kv'});
      [['Artist', n.artist], ['Title', n.title], ['BPM', n.bpm != null ? Math.round(n.bpm * 10) / 10 : null], ['Key', n.key],
        ['Energy', n.energy], ['Language', n.lang], ['Scene', n.scene], ['Source', n.source],
        ['Library', n.library ? 'yes' : 'no'], ['YouTube id', n.yt_id], ['Trending', trendText(n.trend)]].forEach(function (r) {
        var tr = el('tr'); tr.appendChild(el('th', r[0])); tr.appendChild(el('td', r[1] == null || r[1] === '' ? '-' : String(r[1])));
        kv.appendChild(tr);
      });
      p.appendChild(kv);
      if ((n.trend_history || []).length) {
        p.appendChild(el('h3', 'Chart history (day, region, place)'));
        var hl = el('ul');
        n.trend_history.slice(0, 14).forEach(function (h) { hl.appendChild(el('li', h.day + '  ' + h.region + '  #' + h.rank)); });
        p.appendChild(hl);
      }
      Object.keys(d.edges).sort().forEach(function (k) {
        var g = d.edges[k], h = el('h3', k + ' (out ' + g.out + ', in ' + g.in + ')');
        h.style.borderLeft = '6px solid ' + (COLORS[k] || '#64748b'); h.style.paddingLeft = '8px';
        p.appendChild(h);
        var ul = el('ul');
        g.top.forEach(function (e) {
          var li = el('li', (e.dir === 'out' ? '> ' : '< ') + e.label + '  ' + e.weight.toFixed(2) + (e.n > 1 ? ' (n=' + e.n + ')' : ''));
          ul.appendChild(li);
        });
        p.appendChild(ul);
      });
    }).catch(function (e) { $('gpanel').hidden = false; $('gpanel').textContent = String(e.message || e); });
  }

  function centre(id) {
    centerId = id; expanded = {};
    if (cy) cy.elements().remove();
    initCy();
    getJSON('/api/admin/graph/neighbours?id=' + id + '&k=' + filters().k + '&kinds=' +
      encodeURIComponent(filters().kinds.join()) + '&min_weight=' + filters().minw + '&lang=' + encodeURIComponent(filters().lang))
      .then(function (g) {
        expanded[id + '|' + filters().k + '|' + filters().kinds.join()] = true;
        addGraph(g);
        var c = cy.getElementById('n' + id); c.addClass('center'); c.addClass('done');
      });
    showDetail(id);
  }

  function search(ev) {
    ev.preventDefault();
    var q = $('gq').value.trim(), box = $('gresults'), trend = $('gtrend').checked;
    box.textContent = '';
    if (!q && !trend) return;
    getJSON('/api/admin/graph/node?q=' + encodeURIComponent(q) + (trend ? '&trending=1' : '')).then(function (r) {
      if (!r.results.length) { box.appendChild(el('p', 'No match.', {'class': 'muted'})); return; }
      r.results.forEach(function (n) {
        var b = el('button', n.label + (n.library ? '' : '  (not in library)') + (n.trend ? '  [trending #' + n.trend.rank + ']' : ''), {type: 'button'});
        b.addEventListener('click', function () { box.textContent = ''; centre(n.id); });
        box.appendChild(b);
      });
    }).catch(function (e) { box.textContent = String(e.message || e); });
  }

  function start() {
    $('gsearch').addEventListener('submit', search);
    ['gminw', 'glang'].forEach(function (i) { $(i).addEventListener('change', applyFilters); });
    getJSON('/api/admin/graph/stats').then(function (s) { buildControls(s); showStats(s); })
      .catch(function (e) { $('gstats').textContent = String(e.message || e); });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
