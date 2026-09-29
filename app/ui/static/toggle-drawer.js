// AI Music Brain - side drawers for the autopilot row (user: "checkbox row is overflowing, create it a side
// menu / filter menu") and the AI ACTIONS bar ("MORE ACTIONS").
//
// core  pure and node-testable (app/tests/toggle_drawer_check.js): which group a toggle / action belongs to,
//       filtering, per-group counts, nested LEARNED greying, the saved-state round trip.
// glue  UI only (skipped by the sim, like host-browser.js): MOVES the existing <label><input> elements (ids and
//       listeners stay, nothing is re-created) into the drawer, and picks up ones added later (MutationObserver).
//
// Contract for other modules (no edit here needed):
//   toggle  <label class="hud-label" data-group="GROUP" title="tooltip"><input type="checkbox" id="ap-..."> LABEL</label>
//           inside .ap-btns, or anything marked [data-ai-toggle] anywhere. Unknown groups are created on the fly.
//   action  <button class="hw-btn" data-ai-action="id" data-group="GROUP" title="...">LABEL</button> in #ai-actions:
//           ai-actions.js runs it (ACTIONS / aiActions.register) and this file files it under MORE ACTIONS.
(function (root) {
  "use strict";

  const STORE_KEY = "djAiToggles.v1";          // {id: checked} for the toggles the user changed, nothing else
  const GROUP_ORDER = ["TRANSITIONS", "IN-SONG MOVES", "LEARNED", "AI"];
  const DEFAULT_GROUP = {
    "ap-merge-toggle": "TRANSITIONS", "ap-mashup-toggle": "TRANSITIONS", "ap-riff-toggle": "TRANSITIONS", "ap-peak-toggle": "TRANSITIONS",
    "ap-remix-toggle": "IN-SONG MOVES", "ap-mind-toggle": "IN-SONG MOVES", "ap-grid-toggle": "IN-SONG MOVES",
    "ap-drums-toggle": "IN-SONG MOVES", "ap-sampler-toggle": "IN-SONG MOVES",
    "ap-learned-toggle": "LEARNED",
    "ap-ai-toggle": "AI", "ap-ear-toggle": "AI",
  };
  const LEARNED_PARENT = "ap-learned-toggle";
  // AI ACTIONS: the ones kept on the bar; the rest go under MORE ACTIONS
  const FAV_ACTIONS = ["mix", "merge_hold", "mashup", "riff"];
  const ACTION_GROUP = { mix: "TRANSITIONS", merge_hold: "TRANSITIONS", mashup: "TRANSITIONS", riff: "TRANSITIONS",
    strip: "STEMS", remix: "STEMS", hold: "STEMS", vocals: "STEMS", sample: "SAMPLER" };

  const norm = (s) => String(s == null ? "" : s).replace(/\s+/g, " ").trim();
  // -> {group, parent}: data-group wins (upper-cased), then the known ids, then OTHER
  function classify(id, dataGroup) {
    const learnedKind = /^ap-learned-/.test(id || "") && id !== LEARNED_PARENT;
    const g = norm(dataGroup).toUpperCase();
    return { group: g || (learnedKind ? "LEARNED" : DEFAULT_GROUP[id] || "OTHER"), parent: learnedKind ? LEARNED_PARENT : null };
  }
  function actionGroup(id, dataGroup) {
    const g = norm(dataGroup).toUpperCase();
    return g || (/^learned_/.test(id || "") ? "LEARNED" : ACTION_GROUP[id] || "OTHER");
  }
  // items [{id, label, title, group, parent}] -> [{name, items}] in GROUP_ORDER, new groups in first-seen order, OTHER last
  function groupItems(items) {
    const by = new Map();
    for (const it of items) { if (!by.has(it.group)) by.set(it.group, []); by.get(it.group).push(it); }
    const rank = (n) => (n === "OTHER" ? 1e9 : GROUP_ORDER.includes(n) ? GROUP_ORDER.indexOf(n) : 100);
    const names = [...by.keys()];
    names.sort((a, b) => rank(a) - rank(b) || names.indexOf(a) - names.indexOf(b));
    return names.map((name) => ({ name, items: by.get(name) }));
  }
  // every word of the query in the label or tooltip (case-insensitive); empty query matches all
  function matches(it, q) {
    const words = norm(q).toLowerCase().split(" ").filter(Boolean);
    const hay = `${it.label} ${it.title} ${it.group}`.toLowerCase();
    return words.every((w) => hay.includes(w));
  }
  const filterItems = (items, q) => items.filter((it) => matches(it, q));
  // state {id: checked} -> {group: {on, total}}
  function counts(groups, state) {
    const out = {};
    for (const g of groups) out[g.name] = { on: g.items.filter((it) => !!state[it.id]).length, total: g.items.length };
    return out;
  }
  const greyed = (it, state) => !!(it.parent && state[it.parent] === false);
  // saved state: parse defensively (a bad blob is ignored, never thrown)
  function parseSaved(raw) {
    if (!raw) return {};
    try {
      const o = JSON.parse(raw), out = {};
      if (o && typeof o === "object") for (const [k, v] of Object.entries(o)) if (typeof v === "boolean") out[k] = v;
      return out;
    } catch (e) { return {}; }
  }
  const withSaved = (saved, id, checked) => Object.assign({}, saved, { [id]: !!checked });
  // which toggles to flip on restore: only saved ids that exist and differ -> [[id, checked]]
  function restorePlan(saved, current) {
    return Object.keys(saved).filter((id) => id in current && current[id] !== saved[id]).map((id) => [id, saved[id]]);
  }
  const splitActions = (ids) => ({ bar: ids.filter((id) => FAV_ACTIONS.includes(id)), more: ids.filter((id) => !FAV_ACTIONS.includes(id)) });

  const core = { STORE_KEY, GROUP_ORDER, DEFAULT_GROUP, FAV_ACTIONS, classify, actionGroup, groupItems, matches, filterItems,
    counts, greyed, parseSaved, withSaved, restorePlan, splitActions };
  if (typeof module !== "undefined" && module.exports) module.exports = core;

  // ------------------------------------------------------------------ glue (browser only)
  if (typeof document === "undefined" || !root.document) return;
  const doc = document;
  const storage = (() => { try { return root.localStorage; } catch (e) { return null; } })();
  const load = () => { try { return parseSaved(storage && storage.getItem(STORE_KEY)); } catch (e) { return {}; } };
  const save = (o) => { try { if (storage) storage.setItem(STORE_KEY, JSON.stringify(o)); } catch (e) { /* private mode */ } };
  const mk = (tag, cls, text) => { const n = doc.createElement(tag); if (cls) n.className = cls; if (text != null) n.textContent = text; return n; };

  // one drawer: a button that opens it, a dialog panel with a search box and grouped sections
  function makeDrawer(btn, panel, title, placeholder) {
    panel.classList.add("td-drawer");
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", title);
    panel.hidden = true;
    const head = mk("div", "td-head");
    head.append(mk("span", "hud-label td-title", title));
    const close = mk("button", "hw-btn td-close", "✕");
    close.type = "button"; close.setAttribute("aria-label", `Close ${title}`);
    head.append(close);
    const search = mk("input", "td-search");
    search.type = "search"; search.placeholder = placeholder; search.setAttribute("aria-label", placeholder);
    search.autocomplete = "off"; search.spellcheck = false;
    const body = mk("div", "td-body");
    panel.append(head, search, body);
    btn.setAttribute("aria-expanded", "false");
    btn.setAttribute("aria-controls", panel.id);
    btn.setAttribute("aria-haspopup", "dialog");
    const setOpen = (on) => {
      panel.hidden = !on;
      btn.setAttribute("aria-expanded", String(on));
      if (on) search.focus(); else if (panel.contains(doc.activeElement)) btn.focus();
    };
    btn.addEventListener("click", () => setOpen(panel.hidden));
    close.addEventListener("click", () => setOpen(false));
    doc.addEventListener("keydown", (e) => { if (e.key === "Escape" && !panel.hidden) setOpen(false); });
    doc.addEventListener("pointerdown", (e) => { if (!panel.hidden && !panel.contains(e.target) && !btn.contains(e.target)) setOpen(false); });
    return { search, body };
  }
  // a section per group: header (name, count, all on / all off) + rows
  function section(name) {
    const s = mk("section", "td-group");
    s.dataset.group = name;
    const h = mk("div", "td-group-head");
    const t = mk("span", "hud-label td-group-name", name), n = mk("span", "td-count");
    h.append(t, n);
    const rows = mk("div", "td-rows");
    s.append(h, rows);
    return { el: s, head: h, count: n, rows };
  }

  // ---- AI MOVES: the autopilot toggles
  function toggles() {
    const row = doc.querySelector(".ap-btns"), btn = doc.getElementById("ap-moves-btn"), panel = doc.getElementById("ap-drawer");
    if (!row || !btn || !panel) return;
    const { search, body } = makeDrawer(btn, panel, "AI MOVES", "Filter moves (name or tooltip)");
    const items = new Map();       // id -> {id, label, title, group, parent, el (label), input}
    const secs = new Map();
    let saved = load();
    const state = () => { const o = {}; for (const it of items.values()) o[it.id] = !!it.input.checked; return o; };

    function sectionFor(name) {
      if (secs.has(name)) return secs.get(name);
      const s = section(name);
      for (const [lab, v] of [["all on", true], ["all off", false]]) {
        const b = mk("button", "hw-btn td-all", lab);
        b.type = "button";
        b.addEventListener("click", () => {
          for (const it of items.values()) if (it.group === name && it.input.checked !== v) {
            it.input.checked = v; it.input.dispatchEvent(new Event("change", { bubbles: true }));
          }
        });
        s.head.append(b);
      }
      secs.set(name, s);
      // keep the group order when a new group arrives
      const order = groupItems([...secs.keys()].map((g) => ({ group: g }))).map((g) => g.name);
      for (const g of order) body.append(secs.get(g).el);
      return s;
    }
    function collect(label) {
      const input = label.matches("input[type=checkbox]") ? label : label.querySelector("input[type=checkbox]");
      if (!input || !input.id || items.has(input.id)) return;
      const host = label.matches("input") ? (input.closest("label") || input) : label;
      const kindsSpan = host.closest && host.closest(".ap-learned-kinds");
      const { group, parent } = classify(input.id, host.dataset.group || input.dataset.group);
      const title = norm(host.title || (kindsSpan && kindsSpan.title) || input.title);
      const it = { id: input.id, label: norm(host.textContent), title, group, parent, el: host, input };
      items.set(it.id, it);
      const r = mk("div", "td-row" + (parent ? " td-nested" : ""));
      r.dataset.id = it.id;
      host.classList.add("td-toggle");
      r.append(host);
      if (title) r.append(mk("div", "td-tip", title));
      if (it.id === "ap-drums-toggle") { const lvl = doc.getElementById("ap-drums-level"); if (lvl) { lvl.classList.add("td-level"); host.after(lvl); } }
      it.row = r;
      sectionFor(group).rows.append(r);
      input.addEventListener("change", () => { saved = withSaved(saved, it.id, input.checked); save(saved); paint(); });
      // restore this toggle if the user saved one (a change event so listeners follow)
      if (it.id in saved && input.checked !== saved[it.id]) { input.checked = saved[it.id]; input.dispatchEvent(new Event("change", { bubbles: true })); }
      if (kindsSpan && !kindsSpan.querySelector("input")) kindsSpan.hidden = true;   // the kinds all moved out
    }
    function paint() {
      const st = state(), q = search.value;
      const groups = groupItems([...items.values()]);
      const c = counts(groups, st);
      let on = 0, total = 0;
      for (const g of groups) {
        const s = secs.get(g.name);
        s.count.textContent = `${c[g.name].on}/${c[g.name].total}`;
        on += c[g.name].on; total += c[g.name].total;
        let shown = 0;
        for (const it of g.items) {
          const vis = matches(it, q);
          it.row.hidden = !vis; shown += vis ? 1 : 0;
          const grey = greyed(it, st);
          it.row.classList.toggle("td-greyed", grey);
          it.input.setAttribute("aria-disabled", String(grey));
        }
        s.el.hidden = shown === 0;
      }
      const n = doc.getElementById("ap-moves-count");
      if (n) n.textContent = `(${on} on / ${total})`;
    }
    let obs = null;
    const scan = () => {
      if (obs) obs.disconnect();   // our own moves must not re-trigger the observer (a loop freezes the page)
      try {
        row.querySelectorAll("label").forEach((l) => { if (l.querySelector("input[type=checkbox]")) collect(l); });
        doc.querySelectorAll("[data-ai-toggle]").forEach(collect);
        paint();
      } finally { if (obs) obs.observe(row, { childList: true, subtree: true }); }
    };
    if (typeof MutationObserver === "function") {
      obs = new MutationObserver((ms) => { if (ms.some((m) => m.addedNodes.length)) scan(); });
    }
    scan();
    search.addEventListener("input", paint);
  }

  // ---- MORE ACTIONS: the AI ACTIONS bar keeps its favourites, the rest go in a grouped, searchable menu
  function actions() {
    const bar = doc.getElementById("ai-actions"), btn = doc.getElementById("ai-more-btn"), panel = doc.getElementById("ai-more-drawer");
    if (!bar || !btn || !panel) return;
    const { search, body } = makeDrawer(btn, panel, "MORE ACTIONS", "Search actions");
    const secs = new Map(), seen = new Set();
    let obs = null;
    function scan() {
      // our own moves (favourites re-ordered, buttons moved into the menu) are mutations of `bar`
      // too: pause the observer while scanning, or it re-triggers itself forever and freezes the page
      if (obs) obs.disconnect();
      try { scanBar(); } finally { if (obs) obs.observe(bar, { childList: true }); }
    }
    function scanBar() {
      const aa = root.Engine && root.Engine.mods && root.Engine.mods.aiActions;
      bar.querySelectorAll("[data-ai-action]").forEach((b) => {
        const id = b.dataset.aiAction;
        if (aa && aa.bind) aa.bind(b);          // late buttons: ai-actions binds them too
        if (seen.has(id) || FAV_ACTIONS.includes(id)) return;
        seen.add(id);
        const g = actionGroup(id, b.dataset.group);
        if (!secs.has(g)) {
          secs.set(g, section(g));
          const order = groupItems([...secs.keys()].map((n) => ({ group: n }))).map((x) => x.name);
          for (const n of order) body.append(secs.get(n).el);
        }
        const r = mk("div", "td-row");
        r.dataset.id = id;
        r.append(b);
        if (b.title) r.append(mk("div", "td-tip", norm(b.title)));
        secs.get(g).rows.append(r);
      });
      // favourites keep their order, ahead of the MORE button
      let at = btn;   // walk back from MORE: move a favourite only when it is out of place
      for (let i = FAV_ACTIONS.length - 1; i >= 0; i--) {
        const b = bar.querySelector(`[data-ai-action="${FAV_ACTIONS[i]}"]`);
        if (!b) continue;
        if (b.nextElementSibling !== at) bar.insertBefore(b, at);
        at = b;
      }
      paint();
    }
    function paint() {
      const q = search.value;
      for (const [name, s] of secs) {
        let shown = 0;
        s.rows.querySelectorAll(".td-row").forEach((r) => {
          const b = r.querySelector("[data-ai-action]");
          const vis = !!b && matches({ label: norm(b.textContent), title: norm(b.title), group: name }, q);
          r.hidden = !vis; shown += vis ? 1 : 0;
        });
        s.count.textContent = String(s.rows.children.length);
        s.el.hidden = shown === 0;
      }
    }
    // clicking an action inside the menu closes it (the step log / status line tell the result)
    body.addEventListener("click", (e) => { if (e.target.closest("[data-ai-action]")) btn.click(); });
    search.addEventListener("input", paint);
    if (typeof MutationObserver === "function") {
      obs = new MutationObserver((ms) => { if (ms.some((m) => m.addedNodes.length)) scan(); });
    }
    scan();   // attaches the observer at its end
  }

  toggles();
  actions();
})(typeof window !== "undefined" ? window : globalThis);
