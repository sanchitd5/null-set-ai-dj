/* /admin/jobs: save a job's schedule through PUT /api/admin/jobs/{name}/schedule, and refresh the page every 5 s
   unless a form field has focus or was edited. Server text only goes in through textContent. */
(function () {
  'use strict';
  var dirty = false;
  document.querySelectorAll('form.sched').forEach(function (f) {
    var msg = f.querySelector('.msg');
    f.addEventListener('input', function () { dirty = true; });
    f.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var q = function (n) { return f.elements[n]; };
      var body = {
        schedule: q('schedule').value.trim() || null,
        enabled: q('enabled').checked,
        idle_only: q('idle_only').checked,
        max_runtime_s: Math.max(0, parseInt(q('max_min').value, 10) || 0) * 60
      };
      msg.textContent = 'saving';
      fetch('/api/admin/jobs/' + encodeURIComponent(f.getAttribute('data-job')) + '/schedule', {
        method: 'PUT', credentials: 'same-origin', headers: {'content-type': 'application/json'}, body: JSON.stringify(body)
      }).then(function (r) {
        return r.json().then(function (j) { return {ok: r.ok, j: j}; });
      }).then(function (res) {
        if (res.ok) { dirty = false; location.reload(); } else { msg.textContent = String(res.j.detail || 'rejected'); }
      }).catch(function () { msg.textContent = 'network error'; });
    });
  });
  setInterval(function () {
    var a = document.activeElement;
    if (!dirty && !(a && a.form && a.form.classList.contains('sched'))) location.reload();
  }, 5000);
})();
