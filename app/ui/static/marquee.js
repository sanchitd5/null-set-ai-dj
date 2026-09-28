// Marquee for long titles (user: "long title break UI, should be marquee effect").
// Watches the elements that show song names (deck titles, NOW / NEXT, status line)
// and, only when the text is wider than its box, wraps it in one inner span that
// scrolls to the end, pauses, and scrolls back (CSS transform animation: no layout
// work per frame). The modules that write these elements keep writing textContent
// / innerHTML as before; the observer re-wraps after each write.
(function (root) {
  "use strict";

  const SPEED_PX_S = 40;       // scroll speed
  const MIN_DUR_S = 8;         // a short overflow still reads calmly
  // Keyframes (fixed stops): hold 0-15 %, scroll 15-35 %, hold 35-65 %, back 65-85 %,
  // hold 85-100 %. Scrolling is 40 % of the cycle, so dur = 2.5 x (there and back).

  // Pure: animation timing for an overflow of `over` px. -> {dur (s)} | null
  function plan(over) {
    if (!(over > 2)) return null;
    const dur = Math.max(MIN_DUR_S, (2 * over / SPEED_PX_S) / 0.4);
    return { dur: Math.round(dur * 100) / 100 };
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { plan, SPEED_PX_S, MIN_DUR_S };
  if (typeof document === "undefined") return;

  const SELECTORS = ["#title-a", "#title-b", "#tid-now", "#tid-next", "#status", "#ap-status"];
  const reduce = root.matchMedia && root.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function apply(el) {
    if (el._mqBusy) return;
    el._mqBusy = true;
    try {
      let inner = el.firstElementChild;
      const wrapped = inner && inner.classList.contains("mq-inner") && el.childNodes.length === 1;
      if (!wrapped) {
        inner = document.createElement("span");
        inner.className = "mq-inner";
        while (el.firstChild) inner.appendChild(el.firstChild);
        el.appendChild(inner);
      }
      el.classList.add("mq-box");
      inner.style.animation = "none";
      const over = inner.scrollWidth - el.clientWidth;
      const p = reduce ? null : plan(over);
      el.classList.toggle("mq-on", !!p);
      if (p) {
        inner.style.setProperty("--mq-shift", `${-(over + 8)}px`);
        inner.style.animation = `mq-scroll ${p.dur}s linear infinite`;
      } else inner.style.animation = "";
      if (!el.title) el.title = el.textContent.trim();
    } finally {
      el._mqBusy = false;
    }
  }

  function watch(el) {
    apply(el);
    new MutationObserver(() => { if (!el._mqBusy) requestAnimationFrame(() => { el.title = ""; apply(el); }); })
      .observe(el, { childList: true, characterData: true, subtree: true });
  }

  const css = document.createElement("style");
  css.textContent = `
    .mq-box { overflow: hidden; white-space: nowrap; text-overflow: clip !important; }
    .mq-box .mq-inner { display: inline-block; white-space: nowrap; will-change: transform; }
    .mq-box:not(.mq-on) .mq-inner { animation: none !important; }
    .mq-on { -webkit-mask-image: linear-gradient(90deg, transparent 0, #000 12px, #000 calc(100% - 12px), transparent 100%);
             mask-image: linear-gradient(90deg, transparent 0, #000 12px, #000 calc(100% - 12px), transparent 100%); }
    .mq-on:hover .mq-inner { animation-play-state: paused; }
    @keyframes mq-scroll {
      0%, 15% { transform: translateX(0); }
      35%, 65% { transform: translateX(var(--mq-shift, 0)); }
      85%, 100% { transform: translateX(0); }
    }`;
  document.head.appendChild(css);

  function init() {
    for (const s of SELECTORS) document.querySelectorAll(s).forEach(watch);
    let t = null;
    root.addEventListener("resize", () => {
      clearTimeout(t);
      t = setTimeout(() => SELECTORS.forEach((s) => document.querySelectorAll(s).forEach(apply)), 200);
    });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
  root.marquee = { plan, apply };
})(typeof window !== "undefined" ? window : globalThis);
