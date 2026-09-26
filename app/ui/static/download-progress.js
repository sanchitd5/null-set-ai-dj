// AI Music Brain — download progress bars (autopilot pre-downloads).
//
// window.dlJobs.run(url, label) starts a background job on the server
// (POST /api/download/jobs), shows a row with a progress bar in #ap-downloads,
// polls GET /api/download/jobs/{id} and resolves with the registered tracks
// (each with duration / bpm, already analyzed) or rejects with the job error.
//
// The bar is determinate while bytes are flowing (yt-dlp reports a byte
// ratio) and indeterminate for search / convert / analyze steps.

(function () {
  const box = document.getElementById("ap-downloads");
  const POLL_MS = 700;
  const KEEP_DONE_MS = 15000;
  const KEEP_ERROR_MS = 30000;

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const POLL_TIMEOUT_MS = 15000;
  const MAX_POLL_ERRORS = 8;      // ~ a server restart's worth of failed polls
  const JOB_DEADLINE_MS = 6 * 60 * 1000;

  async function fetchT(url, opts = {}, ms = POLL_TIMEOUT_MS) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), ms);
    try { return await fetch(url, Object.assign({}, opts, { signal: ctl.signal })); }
    finally { clearTimeout(timer); }
  }

  function makeRow(label) {
    if (!box) return { update() {}, done() {}, fail() {} };
    const row = document.createElement("div");
    row.className = "dl-row";
    const name = document.createElement("span");
    name.className = "dl-name";
    name.textContent = label;
    const stage = document.createElement("span");
    stage.className = "dl-stage";
    stage.textContent = "queued";
    const bar = document.createElement("div");
    bar.className = "dl-bar";
    bar.setAttribute("role", "progressbar");
    bar.setAttribute("aria-label", `Download ${label}`);
    const fill = document.createElement("div");
    fill.className = "dl-fill indeterminate";
    bar.appendChild(fill);
    row.append(name, stage, bar);
    box.prepend(row);

    const remove = (ms) => setTimeout(() => row.remove(), ms);
    return {
      update(job) {
        stage.textContent = job.stage || job.state;
        if (typeof job.percent === "number") {
          fill.classList.remove("indeterminate");
          fill.style.width = `${Math.max(2, Math.min(100, job.percent)).toFixed(0)}%`;
          bar.setAttribute("aria-valuenow", String(Math.round(job.percent)));
        } else {
          fill.classList.add("indeterminate");
          fill.style.width = "";
          bar.removeAttribute("aria-valuenow");
        }
      },
      done(tracks) {
        row.classList.add("done");
        fill.classList.remove("indeterminate");
        fill.style.width = "100%";
        const t = tracks && tracks[0];
        const mins = t && t.duration ? ` · ${Math.floor(t.duration / 60)}:${String(Math.round(t.duration % 60)).padStart(2, "0")}` : "";
        stage.textContent = `ready${mins}`;
        remove(KEEP_DONE_MS);
      },
      fail(msg) {
        row.classList.add("err");
        fill.classList.remove("indeterminate");
        fill.style.width = "100%";
        stage.textContent = "skipped";
        row.title = msg || "";
        const why = document.createElement("span");
        why.className = "dl-why";
        why.textContent = (msg || "failed").slice(0, 140);
        row.appendChild(why);
        remove(KEEP_ERROR_MS);
      },
    };
  }

  async function run(url, label) {
    const res = await fetchT("/api/download/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, label: label || "" }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    const row = makeRow(label || url);
    const began = Date.now();
    let errors = 0;
    for (;;) {
      await sleep(POLL_MS);
      if (Date.now() - began > JOB_DEADLINE_MS) {
        row.fail("gave up after 6 min");
        throw new Error("download job timed out");
      }
      let job;
      try {
        const r = await fetchT(`/api/download/jobs/${data.job_id}`);
        if (r.status === 404) throw new Error("job lost (server restarted)");
        if (!r.ok) throw new Error(`job ${r.status}`);
        job = await r.json();
        errors = 0;
      } catch (e) {
        // transient (timeout / connection refused during a restart): retry;
        // a 404 means the server forgot the job, so stop right away
        if (++errors < MAX_POLL_ERRORS && !String(e.message).includes("job lost")) continue;
        row.fail(e.message);
        throw e;
      }
      row.update(job);
      if (job.state === "done") { row.done(job.tracks); return job.tracks || []; }
      if (job.state === "error") { row.fail(job.error); throw new Error(job.error || "download failed"); }
    }
  }

  window.dlJobs = { run };
})();
