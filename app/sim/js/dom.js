// A small DOM for the headless console: enough of Element / Document for the console scripts
// to run unmodified against the real index.html (ids, classes, data-* attributes, form values,
// selectors, events with bubbling). It renders nothing. Anything the scripts call that is not
// modelled here resolves to a harmless no-op (see `inert()`), and is counted in dom.misses so
// a run reports which browser APIs the console leaned on that the sim did not model.
"use strict";

const VOID = new Set(["input", "br", "img", "hr", "meta", "link", "source", "area", "base", "col", "embed", "track", "wbr"]);
const RAW = new Set(["script", "style"]);

function inert(name, misses) {
  const f = function () { return undefined; };
  return new Proxy(f, {
    get(_t, k) { if (k === Symbol.toPrimitive) return () => 0; if (k === "then") return undefined; misses && misses.add(`${name}.${String(k)}`); return inert(`${name}.${String(k)}`, misses); },
    apply() { misses && misses.add(`${name}()`); return undefined; },
  });
}

// ---- selectors -----------------------------------------------------------------------
function parseSelector(sel) {
  // -> [ [ {compound, comb} ... ] , ... ] one chain per comma part; comb = combinator BEFORE the compound
  return sel.split(",").map((part) => {
    const chain = [];
    const re = /\s*(>|\+|~)?\s*((?:\*|[a-zA-Z][\w-]*)?(?:#[\w-]+|\.[\w-]+|\[[^\]]+\]|:[\w-]+(?:\([^)]*\))?)*)/g;
    let m, last = 0;
    const s = part.trim();
    while (last < s.length && (m = re.exec(s)) && m[0] !== "") {
      last = re.lastIndex;
      chain.push({ comb: m[1] || " ", compound: parseCompound(m[2]) });
    }
    return chain;
  });
}
function parseCompound(c) {
  const out = { tag: null, id: null, classes: [], attrs: [], pseudo: [] };
  const re = /(^\*|^[a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([^\]=~^$*|]+)(?:([~^$*|]?=)"?([^"\]]*)"?)?\]|:([\w-]+)(?:\(([^)]*)\))?/g;
  let m;
  while ((m = re.exec(c))) {
    if (m[1]) out.tag = m[1] === "*" ? null : m[1].toLowerCase();
    else if (m[2]) out.id = m[2];
    else if (m[3]) out.classes.push(m[3]);
    else if (m[4]) out.attrs.push({ name: m[4].trim(), op: m[5] || null, val: m[6] });
    else if (m[7]) out.pseudo.push({ name: m[7], arg: m[8] });
  }
  return out;
}
function matchCompound(el, c) {
  if (c.tag && el.tagName.toLowerCase() !== c.tag) return false;
  if (c.id && el.id !== c.id) return false;
  for (const k of c.classes) if (!el.classList.contains(k)) return false;
  for (const a of c.attrs) {
    const v = el.getAttribute(a.name);
    if (v === null) return false;
    if (a.op === "=" && v !== a.val) return false;
    if (a.op === "^=" && !v.startsWith(a.val)) return false;
    if (a.op === "$=" && !v.endsWith(a.val)) return false;
    if (a.op === "*=" && !v.includes(a.val)) return false;
    if (a.op === "~=" && !v.split(/\s+/).includes(a.val)) return false;
  }
  for (const p of c.pseudo) {
    if (p.name === "first-child" && el.parentNode && el.parentNode.children[0] !== el) return false;
    if (p.name === "last-child" && el.parentNode && el.parentNode.children[el.parentNode.children.length - 1] !== el) return false;
    if (p.name === "not" && matchesSelector(el, p.arg)) return false;
    if (p.name === "checked" && !el.checked) return false;
    if (p.name === "disabled" && !el.disabled) return false;
  }
  return true;
}
function matchChain(el, chain, i) {
  const step = chain[i];
  if (!matchCompound(el, step.compound)) return false;
  if (i === 0) return true;
  if (step.comb === ">") return !!el.parentElement && matchChain(el.parentElement, chain, i - 1);
  if (step.comb === "+" || step.comb === "~") {
    const sibs = el.parentNode ? el.parentNode.children : [];
    const idx = sibs.indexOf(el);
    for (let j = idx - 1; j >= 0; j--) { if (matchChain(sibs[j], chain, i - 1)) return true; if (step.comb === "+") break; }
    return false;
  }
  for (let p = el.parentElement; p; p = p.parentElement) if (matchChain(p, chain, i - 1)) return true;
  return false;
}
const selCache = new Map();
function matchesSelector(el, sel) {
  let chains = selCache.get(sel);
  if (!chains) selCache.set(sel, (chains = parseSelector(sel)));
  return chains.some((ch) => ch.length && matchChain(el, ch, ch.length - 1));
}

// ---- events --------------------------------------------------------------------------------
class Listeners {
  constructor() { this.map = new Map(); }
  add(type, fn, opts) {
    if (!fn) return;
    let l = this.map.get(type); if (!l) this.map.set(type, (l = []));
    if (!l.some((x) => x.fn === fn)) l.push({ fn, once: !!(opts && opts.once) });
  }
  remove(type, fn) { const l = this.map.get(type); if (l) this.map.set(type, l.filter((x) => x.fn !== fn)); }
  fire(target, ev) {
    const l = this.map.get(ev.type);
    if (!l) return;
    for (const x of [...l]) {
      if (x.once) this.remove(ev.type, x.fn);
      try { typeof x.fn === "function" ? x.fn.call(target, ev) : x.fn.handleEvent(ev); } catch (e) { if (target.__onError) target.__onError(e, ev); else throw e; }
    }
  }
}

function makeStyle() {
  const store = {};
  return new Proxy(store, {
    get(t, k) {
      if (k === "setProperty") return (n, v) => { t[n] = String(v); };
      if (k === "removeProperty") return (n) => { delete t[n]; };
      if (k === "getPropertyValue") return (n) => t[n] || "";
      if (k === "cssText") return Object.entries(t).map(([a, b]) => `${a}:${b}`).join(";");
      return t[k] === undefined ? "" : t[k];
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}

class Element {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.tagName = String(tag).toUpperCase();
    this.nodeType = 1;
    this.children = [];
    this.childNodes = this.children;
    this.parentNode = null;
    this._attrs = new Map();
    this._l = new Listeners();
    this.style = makeStyle();
    this._text = "";
    this._value = undefined;
    this._checked = undefined;
    this.hidden = false;
    this.disabled = false;
    this.title = "";
    this.dataset = new Proxy({}, {
      get: (t, k) => (typeof k === "string" ? (this._attrs.has(`data-${dashed(k)}`) ? this._attrs.get(`data-${dashed(k)}`) : undefined) : undefined),
      set: (t, k, v) => { this._attrs.set(`data-${dashed(k)}`, String(v)); return true; },
      deleteProperty: (t, k) => { this._attrs.delete(`data-${dashed(k)}`); return true; },
      has: (t, k) => this._attrs.has(`data-${dashed(k)}`),
      ownKeys: () => [...this._attrs.keys()].filter((k) => k.startsWith("data-")).map((k) => camel(k.slice(5))),
      getOwnPropertyDescriptor: (t, k) => (this._attrs.has(`data-${dashed(String(k))}`) ? { enumerable: true, configurable: true, value: this._attrs.get(`data-${dashed(String(k))}`) } : undefined),
    });
    const self = this;
    this.classList = {
      add(...c) { const s = self._classes(); c.forEach((x) => s.add(x)); self._attrs.set("class", [...s].join(" ")); },
      remove(...c) { const s = self._classes(); c.forEach((x) => s.delete(x)); self._attrs.set("class", [...s].join(" ")); },
      toggle(c, force) { const s = self._classes(); const on = force === undefined ? !s.has(c) : !!force; on ? s.add(c) : s.delete(c); self._attrs.set("class", [...s].join(" ")); return on; },
      contains(c) { return self._classes().has(c); },
      replace(a, b) { const s = self._classes(); if (!s.has(a)) return false; s.delete(a); s.add(b); self._attrs.set("class", [...s].join(" ")); return true; },
      get length() { return self._classes().size; },
      [Symbol.iterator]() { return self._classes()[Symbol.iterator](); },
      toString() { return self._attrs.get("class") || ""; },
    };
  }
  _classes() { return new Set((this._attrs.get("class") || "").split(/\s+/).filter(Boolean)); }
  get id() { return this._attrs.get("id") || ""; }
  set id(v) { const d = this.ownerDocument; if (this.id) d._ids.delete(this.id); this._attrs.set("id", String(v)); if (this.isConnected) d._ids.set(String(v), this); }
  get className() { return this._attrs.get("class") || ""; }
  set className(v) { this._attrs.set("class", String(v)); }
  get parentElement() { return this.parentNode && this.parentNode.nodeType === 1 ? this.parentNode : null; }
  get isConnected() { let p = this; while (p.parentNode) p = p.parentNode; return p === this.ownerDocument.documentElement || p === this.ownerDocument; }
  get firstElementChild() { return this.children[0] || null; }
  get lastElementChild() { return this.children[this.children.length - 1] || null; }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get nextElementSibling() { const s = this.parentNode && this.parentNode.children; return s ? s[s.indexOf(this) + 1] || null : null; }
  get previousElementSibling() { const s = this.parentNode && this.parentNode.children; return s ? s[s.indexOf(this) - 1] || null : null; }
  get textContent() { return this.children.length ? this.children.map((c) => c.textContent).join("") + this._text : this._text; }
  set textContent(v) {
    this._detachChildren(); this._text = String(v == null ? "" : v);
    const d = this.ownerDocument;      // the sim watches a few status elements (the console's own words)
    if (d && d._watch && d._watch.has(this.id) && d.textLog.length < 20000) d.textLog.push({ t: +d._now().toFixed(3), id: this.id, text: this._text.slice(0, 300) });
  }
  get innerText() { return this.textContent; }
  set innerText(v) { this.textContent = v; }
  get innerHTML() { return this._html || ""; }
  set innerHTML(html) { this._detachChildren(); this._text = ""; this._html = String(html); if (html) parseInto(this.ownerDocument, this, String(html)); }
  get outerHTML() { return `<${this.tagName.toLowerCase()}>${this.innerHTML}</${this.tagName.toLowerCase()}>`; }
  _detachChildren() { for (const c of this.children.splice(0)) { c.parentNode = null; c._unregister && c._unregister(); } this._html = ""; }
  _unregister() { const d = this.ownerDocument; if (this.id && d._ids.get(this.id) === this) d._ids.delete(this.id); this.children.forEach((c) => c._unregister()); }
  _register() { const d = this.ownerDocument; if (this.id) d._ids.set(this.id, this); this.children.forEach((c) => c._register()); }

  get value() {
    if (this._value !== undefined) return this._value;
    if (this.tagName === "SELECT") { const o = this.children.find((c) => c.selected) || this.children[0]; return o ? o.value : ""; }
    if (this.tagName === "OPTION") return this._attrs.has("value") ? this._attrs.get("value") : this.textContent;
    const t = (this._attrs.get("type") || "").toLowerCase();
    if (t === "range") { const min = +this._attrs.get("min") || 0, max = this._attrs.has("max") ? +this._attrs.get("max") : 100; const v = this._attrs.has("value") ? +this._attrs.get("value") : min + (max - min) / 2; return String(v); }
    return this._attrs.get("value") || "";
  }
  set value(v) {
    this._value = String(v);
    if (this.tagName === "SELECT") this.children.forEach((c) => { c.selected = c.value === this._value; });
  }
  get valueAsNumber() { return Number(this.value); }
  set valueAsNumber(v) { this.value = v; }
  get checked() { return this._checked !== undefined ? this._checked : this._attrs.has("checked"); }
  set checked(v) { this._checked = !!v; }
  get selected() { return this._sel !== undefined ? this._sel : this._attrs.has("selected"); }
  set selected(v) { this._sel = !!v; }
  get type() { return this._attrs.get("type") || (this.tagName === "INPUT" ? "text" : ""); }
  set type(v) { this._attrs.set("type", v); }
  get name() { return this._attrs.get("name") || ""; }
  get href() { return this._attrs.get("href") || ""; }
  set href(v) { this._attrs.set("href", v); }
  get src() { return this._attrs.get("src") || ""; }
  set src(v) { this._attrs.set("src", v); }
  get offsetWidth() { return 800; } get offsetHeight() { return 100; } get clientWidth() { return 800; } get clientHeight() { return 100; }
  get scrollWidth() { return 800; } get scrollHeight() { return 100; } get scrollTop() { return this._st || 0; } set scrollTop(v) { this._st = v; }
  get scrollLeft() { return this._sl || 0; } set scrollLeft(v) { this._sl = v; }
  get width() { return this._w !== undefined ? this._w : (+this._attrs.get("width") || 300); } set width(v) { this._w = v; }
  get height() { return this._h !== undefined ? this._h : (+this._attrs.get("height") || 150); } set height(v) { this._h = v; }

  getAttribute(n) { const v = this._attrs.get(n); return v === undefined ? null : v; }
  setAttribute(n, v) { this._attrs.set(n, String(v)); if (n === "id") this.id = v; }
  hasAttribute(n) { return this._attrs.has(n); }
  removeAttribute(n) { this._attrs.delete(n); }
  toggleAttribute(n, f) { const on = f === undefined ? !this._attrs.has(n) : !!f; on ? this._attrs.set(n, "") : this._attrs.delete(n); return on; }
  get attributes() { return [...this._attrs].map(([name, value]) => ({ name, value })); }

  appendChild(c) { return this.insertBefore(c, null); }
  append(...cs) { cs.forEach((c) => this.appendChild(typeof c === "string" ? this.ownerDocument.createTextNode(c) : c)); }
  prepend(...cs) { cs.reverse().forEach((c) => this.insertBefore(c, this.children[0] || null)); }
  insertBefore(c, ref) {
    if (!c) return c;
    if (c.nodeType === 11) { for (const k of [...c.children]) this.insertBefore(k, ref); return c; }
    if (c.parentNode) c.parentNode.removeChild(c);
    if (c.nodeType === 3) { this._text += c.data; return c; }
    c.parentNode = this;
    const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
    if (this.isConnected) c._register();
    return c;
  }
  removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) { this.children.splice(i, 1); c.parentNode = null; c._unregister && c._unregister(); } return c; }
  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceChildren(...cs) { this._detachChildren(); this.append(...cs); }
  replaceWith(n) { const p = this.parentNode; if (p) { p.insertBefore(n, this); p.removeChild(this); } }
  after(n) { const p = this.parentNode; if (p) p.insertBefore(n, this.nextElementSibling); }
  before(n) { const p = this.parentNode; if (p) p.insertBefore(n, this); }
  contains(o) { for (let p = o; p; p = p.parentNode) if (p === this) return true; return false; }
  cloneNode(deep) { const c = new Element(this.ownerDocument, this.tagName); this._attrs.forEach((v, k) => c._attrs.set(k, v)); c._text = this._text; if (deep) this.children.forEach((k) => c.appendChild(k.cloneNode(true))); return c; }
  insertAdjacentHTML(pos, html) { const tmp = new Element(this.ownerDocument, "div"); tmp.innerHTML = html; for (const k of [...tmp.children]) this.appendChild(k); }
  insertAdjacentElement(pos, el) { return this.appendChild(el); }

  _walk(fn) { for (const c of this.children) { if (fn(c) === true) return true; if (c._walk(fn)) return true; } return false; }
  querySelector(sel) { let hit = null; const chains = parseSel(sel); this._walk((c) => { if (chains.some((ch) => ch.length && matchChain(c, ch, ch.length - 1))) { hit = c; return true; } return false; }); return hit; }
  querySelectorAll(sel) { const out = []; const chains = parseSel(sel); this._walk((c) => { if (chains.some((ch) => ch.length && matchChain(c, ch, ch.length - 1))) out.push(c); return false; }); return out; }
  getElementsByClassName(c) { return this.querySelectorAll("." + c.split(/\s+/).join(".")); }
  getElementsByTagName(t) { return t === "*" ? this.querySelectorAll("*") : this.querySelectorAll(t); }
  matches(sel) { return matchesSelector(this, sel); }
  closest(sel) { for (let p = this; p && p.nodeType === 1; p = p.parentNode) if (matchesSelector(p, sel)) return p; return null; }

  addEventListener(t, fn, o) { this._l.add(t, fn, o); }
  removeEventListener(t, fn) { this._l.remove(t, fn); }
  dispatchEvent(ev) {
    ev.target = ev.target || this;
    ev.currentTarget = this;
    let stop = false;
    const origStop = ev.stopPropagation && ev.stopPropagation.bind(ev);
    ev.stopPropagation = () => { stop = true; origStop && origStop(); };
    for (let n = this; n && !stop; n = n.parentNode || (n === this.ownerDocument.documentElement ? this.ownerDocument : null)) {
      n._l && n._l.fire(n, ev);
      if (!ev.bubbles) break;
    }
    if (ev.bubbles && !stop) this.ownerDocument.defaultView && this.ownerDocument.defaultView.__windowListeners && this.ownerDocument.defaultView.__windowListeners.fire(this.ownerDocument.defaultView, ev);
    return !ev.defaultPrevented;
  }
  click() { const ev = new Event("click", { bubbles: true, cancelable: true }); if (this.tagName === "INPUT" && this.type === "checkbox") this.checked = !this.checked; return this.dispatchEvent(ev); }
  focus() {} blur() {} select() {} scrollIntoView() {} setPointerCapture() {} releasePointerCapture() {} requestFullscreen() {}
  getBoundingClientRect() { return { x: 0, y: 0, top: 0, left: 0, right: 800, bottom: 100, width: 800, height: 100 }; }
  getClientRects() { return []; }
  animate() { return { cancel() {}, finish() {}, onfinish: null, play() {}, pause() {} }; }
  getContext() { return this._ctx || (this._ctx = inertCtx()); }
  toDataURL() { return "data:image/png;base64,"; }
  toBlob(cb) { cb && cb(new Blob([])); }
  play() { return Promise.resolve(); } pause() {} load() {}
  captureStream() { return {}; }
  get files() { return this._files || []; }
  showModal() {} close() {}
}
function dashed(k) { return String(k).replace(/[A-Z]/g, (m) => "-" + m.toLowerCase()); }
function camel(k) { return String(k).replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }
const parseSelCache = new Map();
function parseSel(sel) { let c = parseSelCache.get(sel); if (!c) parseSelCache.set(sel, (c = parseSelector(sel))); return c; }
function inertCtx() {
  const store = {};
  return new Proxy(store, {
    get(t, k) {
      if (k in t) return t[k];
      if (k === "measureText") return () => ({ width: 10 });
      if (k === "createLinearGradient" || k === "createRadialGradient") return () => ({ addColorStop() {} });
      if (k === "getImageData") return () => ({ data: new Uint8ClampedArray(4) });
      if (k === "canvas") return {};
      return () => undefined;
    },
    set(t, k, v) { t[k] = v; return true; },
  });
}

class TextNode { constructor(d) { this.nodeType = 3; this.data = d; this.textContent = d; } }

// ---- HTML parsing ---------------------------------------------------------------------------
const ATTR_RE = /([^\s"'<>\/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
function parseAttrs(s) {
  const out = [];
  let m;
  ATTR_RE.lastIndex = 0;
  while ((m = ATTR_RE.exec(s))) out.push([m[1], m[2] !== undefined ? m[2] : m[3] !== undefined ? m[3] : m[4] !== undefined ? m[4] : ""]);
  return out;
}
function decode(s) { return s.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&amp;/g, "&"); }
function parseInto(doc, root, html) {
  const src = html.replace(/<!--[\s\S]*?-->/g, "").replace(/<!doctype[^>]*>/i, "");
  const re = /<(\/?)([a-zA-Z][\w-]*)((?:"[^"]*"|'[^']*'|[^>"'])*)>|([^<]+)/g;
  const stack = [root];
  let m;
  while ((m = re.exec(src))) {
    const top = stack[stack.length - 1];
    if (m[4] !== undefined) { if (m[4].trim()) top._text += decode(m[4]); continue; }
    const closing = m[1] === "/", tag = m[2].toLowerCase();
    if (closing) {
      for (let i = stack.length - 1; i > 0; i--) if (stack[i].tagName.toLowerCase() === tag) { stack.length = i; break; }
      continue;
    }
    const el = new Element(doc, tag);
    let attrStr = m[3] || "";
    const selfClose = /\/\s*$/.test(attrStr);
    for (const [k, v] of parseAttrs(attrStr.replace(/\/\s*$/, ""))) el._attrs.set(k.toLowerCase(), decode(v));
    if (el.tagName === "SELECT" || el.tagName === "OPTION") { /* selected handled by attr */ }
    top.children.push(el); el.parentNode = top;
    if (RAW.has(tag)) { const end = src.toLowerCase().indexOf(`</${tag}`, re.lastIndex); if (end >= 0) { re.lastIndex = src.indexOf(">", end) + 1; } continue; }
    if (!VOID.has(tag) && !selfClose) stack.push(el);
  }
  root._register && root._register();
}

class Document extends Element {
  constructor() {
    super(null, "#document");
    this.ownerDocument = this;
    this.nodeType = 9;
    this._ids = new Map();
    this.misses = new Set();
    this.documentElement = null;
    this.body = null;
    this.head = null;
    this.hidden = false;
    this.visibilityState = "visible";
    this.readyState = "complete";
    this.cookie = "";
    this.activeElement = null;
    this.fonts = { ready: Promise.resolve(), load() { return Promise.resolve(); } };
    this._watch = new Set(["ap-status"]); this.textLog = []; this._now = () => 0;
  }
  load(html) {
    const root = new Element(this, "html");
    root.parentNode = this;
    this.children.length = 0; this.children.push(root);
    parseInto(this, root, html);
    this.documentElement = root;
    this.body = root.querySelector("body") || (() => { const b = new Element(this, "body"); root.appendChild(b); return b; })();
    this.head = root.querySelector("head") || (() => { const h = new Element(this, "head"); root.insertBefore(h, root.children[0] || null); return h; })();
    this._ids.clear(); root._register();
  }
  createElement(t) { return new Element(this, t); }
  createElementNS(ns, t) { return new Element(this, t); }
  createTextNode(t) { return new TextNode(String(t)); }
  createDocumentFragment() { const f = new Element(this, "#fragment"); f.nodeType = 11; return f; }
  createEvent(t) { return new Event(t); }
  getElementById(id) { return this._ids.get(id) || null; }
  get isConnected() { return true; }
  hasFocus() { return true; }
  exitFullscreen() {}
  get defaultView() { return this._view; }
}

module.exports = { Element, Document, parseInto, matchesSelector, inert, TextNode };
