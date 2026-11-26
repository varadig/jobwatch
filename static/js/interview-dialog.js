// Interviews page: an interview opens in a dialog; its buttons and forms are sent in the background.
(function () {
var dlg = document.getElementById("iv_dlg"), body = document.getElementById("iv_dlg_body"), live = document.getElementById("iv_live");
var cur = null, timer = 0, changed = false;
function refreshList() {
  fetch(location.pathname, {cache: "no-store"}).then(function (r) { return r.text(); }).then(function (h) {
    var doc = new DOMParser().parseFromString(h, "text/html"), a = document.getElementById("iv_lists"), b = doc.getElementById("iv_lists");
    if (a && b) a.innerHTML = b.innerHTML;
  });
}
function watch() {
  clearTimeout(timer);
  if (dlg.open && body.querySelector("[data-running]")) timer = setTimeout(function () {
    load(cur).then(function () { if (!body.querySelector("[data-running]")) refreshList(); });
  }, 6000);
}
function load(href) {
  cur = href;
  return fetch(href + "?partial=1", {cache: "no-store"}).then(function (r) { if (!r.ok) throw r; return r.text(); })
    .then(function (h) { body.innerHTML = h; watch(); });
}
function note(msg) {
  if (!msg) return;
  live.textContent = msg;
  var d = document.createElement("div"); d.className = "alert alert-info iv-flash"; d.textContent = msg;
  if (body.firstChild) body.insertBefore(d, body.firstChild);
}
function open(href) {
  body.innerHTML = '<p class="muted" style="padding:20px">Loading…</p>';
  dlg.showModal();
  load(href).catch(function () { location.href = href; });
}
dlg.addEventListener("close", function () {
  clearTimeout(timer); body.innerHTML = "";
  if (changed) { changed = false; refreshList(); }
});
body.addEventListener("submit", function (ev) {
  var f = ev.target;
  if ((f.method || "").toLowerCase() !== "post") return;
  ev.preventDefault();
  var fd = new FormData(f, ev.submitter);
  f.querySelectorAll("button").forEach(function (b) { b.disabled = true; });
  fetch(f.action, {method: "POST", body: new URLSearchParams(fd)}).then(function (r) {
    var u = new URL(r.url), msg = u.searchParams.get("msg") || "";
    changed = true;
    if (!/^[/]interviews[/][0-9]+$/.test(u.pathname)) { dlg.close(); live.textContent = msg; return; }   // deleted
    return load(cur).then(function () { note(msg); });
  }).catch(function () { location.reload(); });
});
function rowOf(ev) {
  var a = ev.target.closest("a"), row = ev.target.closest(".iv[data-href]");
  if (!row) return null;
  if (a) return /^[/]interviews[/][0-9]+$/.test(a.getAttribute("href") || "") ? {row: row, link: true} : null;   // "Open"
  if (ev.target.closest("button,summary,details,form,input,select,textarea,label")) return null;
  if (String(window.getSelection && window.getSelection()).trim()) return null;   // selecting text
  return {row: row, link: false};
}
document.addEventListener("click", function (ev) {
  if (ev.button !== 0 || dlg.contains(ev.target)) return;
  var t = rowOf(ev);
  if (!t) return;
  if (ev.ctrlKey || ev.metaKey || ev.shiftKey) { if (!t.link) window.open(t.row.dataset.href, "_blank"); return; }
  ev.preventDefault();
  open(t.row.dataset.href);
});
document.addEventListener("auxclick", function (ev) {
  var t = rowOf(ev);
  if (t && !t.link && ev.button === 1) { ev.preventDefault(); window.open(t.row.dataset.href, "_blank"); }
});
})();
