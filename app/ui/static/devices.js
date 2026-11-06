// DEVICES: the signed-in owner's device tokens (phone, sim, scripts) with a revoke button, and SIGN OUT.
// Nothing is fetched until the button is pressed. Server side: app/ui/services/auth.py
// (GET /api/auth/devices, DELETE /api/auth/devices/{id}, /logout).
(function () {
  "use strict";
  function el(tag, attrs, text) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    if (text != null) n.textContent = text;
    return n;
  }
  function when(ts) {
    return ts ? new Date(ts * 1000).toLocaleString() : "never";
  }

  let panel = null;

  async function render() {
    panel.replaceChildren(el("strong", {}, "DEVICES"));
    let res;
    try {
      res = await fetch("/api/auth/devices", { credentials: "same-origin" });
    } catch (e) {
      panel.append(el("p", {}, "could not reach the server"));
      return;
    }
    if (res.status === 404) {
      panel.append(el("p", {}, "Sign-in is off: no owner password set (python -m app.ui.services.auth set-password)."));
      return;
    }
    if (!res.ok) {
      panel.append(el("p", {}, "not signed in"));
      return;
    }
    const { devices } = await res.json();
    if (!devices.length) panel.append(el("p", {}, "No device tokens."));
    for (const d of devices) {
      const row = el("div", { class: "devices-row" });
      row.append(el("span", {}, `${d.name} (${d.id}) - last used ${when(d.last_used)}`));
      const btn = el("button", { type: "button", class: "hw-btn" }, "REVOKE");
      btn.addEventListener("click", async () => {
        if (!window.confirm(`Revoke the token for "${d.name}"? That device stops working at once.`)) return;
        await fetch(`/api/auth/devices/${encodeURIComponent(d.id)}`, { method: "DELETE", credentials: "same-origin" });
        render();
      });
      row.append(btn);
      panel.append(row);
    }
    const out = el("a", { href: "/logout", class: "hw-btn" }, "SIGN OUT");
    panel.append(out);
  }

  function setup() {
    try {
      const open = el("button", { type: "button", class: "hw-btn", id: "devices-btn", title: "Device tokens and sign out" }, "DEVICES");
      open.style.cssText = "position:fixed;left:8px;bottom:8px;z-index:9000;opacity:.6";
      panel = el("div", { id: "devices-panel", hidden: "" });
      panel.style.cssText = "position:fixed;left:8px;bottom:40px;z-index:9001;max-width:28rem;padding:.6rem;"
        + "display:grid;gap:.4rem;background:#111;color:#eee;border:1px solid #444;font:12px system-ui,sans-serif";
      open.addEventListener("click", () => {
        panel.hidden = !panel.hidden;
        if (!panel.hidden) render();
      });
      document.body.append(open, panel);
    } catch (e) {
      /* a DOM without these (headless harness): no panel */
    }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup);
  else setup();
})();
