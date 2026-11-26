// Jobs page: postings open in a dialog; status and work-mode buttons are sent in the background.
(function () {
var dlg = document.getElementById("job_dlg"), body = document.getElementById("job_dlg_body"), live = document.getElementById("jw_live");
var changed = false, cur = null, curCard = null;
var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
// ---- updating the list in place
function swapCounts(doc) {
  [".stats", ".listhead"].forEach(function (sel) {
    var a = document.querySelector(sel), b = doc.querySelector(sel);
    if (a && b) a.replaceWith(document.importNode(b, true));
  });
  var A = document.querySelectorAll(".ui-filters .row-chips"), B = doc.querySelectorAll(".ui-filters .row-chips");
  if (A.length === B.length) A.forEach(function (x, i) { x.replaceWith(document.importNode(B[i], true)); });
}
function bucketOf(el) {
  while (el && !(el.classList && el.classList.contains("bucket"))) el = el.previousElementSibling;
  return el;
}
function fixBucket(b) {
  if (!b) return;
  var n = 0, x = b.nextElementSibling;
  while (x && !x.classList.contains("bucket")) { if (x.matches("article.job")) n++; x = x.nextElementSibling; }
  if (!n) b.remove(); else { var sp = b.querySelector("span"); if (sp) sp.textContent = "\u00b7 " + n; }
}
function leave(card) {
  var b = bucketOf(card);
  function done() { card.remove(); fixBucket(b); }
  if (reduce) return done();
  card.style.height = card.offsetHeight + "px";
  void card.offsetHeight;
  card.classList.add("leaving");
  setTimeout(done, 420);
}
function refreshCard(card, what) {
  if (!card || !card.isConnected) return;
  var href = card.dataset.href, title = (card.querySelector(".jt a") || {}).textContent || "";
  card.classList.add("busy");
  fetch(location.href, {cache: "no-store"}).then(function (r) { return r.text(); }).then(function (h) {
    var doc = new DOMParser().parseFromString(h, "text/html");
    swapCounts(doc);
    var fresh = doc.querySelector('article.job[data-href="' + href + '"]');
    if (fresh) {
      fresh = document.importNode(fresh, true);
      card.replaceWith(fresh);
      if (!reduce) { fresh.classList.add("updated"); setTimeout(function () { fresh.classList.remove("updated"); }, 900); }
    } else leave(card);
    if (what) live.textContent = title + ": " + what;
  }).catch(function () { location.reload(); });
}
var LABEL = {hidden: "hidden", seen: "marked seen", saved: "saved", applied: "marked applied", "new": "marked new"};
function post(form, submitter) {
  var fd = new FormData(form, submitter);
  return fetch(form.action, {method: "POST", body: new URLSearchParams(fd)}).then(function () { return fd; });
}
// buttons on the cards
document.addEventListener("submit", function (ev) {
  var form = ev.target, card = form.closest && form.closest("article.job[data-href]");
  if (!card || dlg.contains(form) || (form.method || "").toLowerCase() !== "post") return;
  ev.preventDefault();
  var d = form.closest("details"); if (d) d.open = false;
  post(form, ev.submitter).then(function (fd) {
    refreshCard(card, LABEL[fd.get("status")] || (fd.get("work_mode") ? "work mode set" : ""));
  });
}, true);
// ---- the dialog
function load(href) {
  cur = href;
  return fetch(href + "?partial=1").then(function (r) { if (!r.ok) throw r; return r.text(); })
    .then(function (h) { body.innerHTML = h; if (window.jdLoadPreview) window.jdLoadPreview(body); });
}
function open(href, card) {
  curCard = card; changed = !!(card && card.classList.contains("st-new"));   // opening marks it seen
  body.innerHTML = '<p class="muted" style="padding:20px">Loading\u2026</p>';
  dlg.showModal();
  load(href).catch(function () { location.href = href; });
}
var lastStatus = "";
dlg.addEventListener("close", function () {
  body.innerHTML = "";
  if (changed) { changed = false; refreshCard(curCard, LABEL[lastStatus] || ""); }
  lastStatus = "";
});
body.addEventListener("submit", function (ev) {
  var f = ev.target;
  if (!f.action || (f.method || "").toLowerCase() !== "post") return;
  ev.preventDefault();
  post(f, ev.submitter).then(function (fd) {
    changed = true;
    if (fd.has("status")) { lastStatus = fd.get("status"); dlg.close(); } else load(cur);   // status: done; work mode: show it
  });
});
function cardOf(ev) {
  var a = ev.target.closest('.jt a[href^="/job/"]');
  if (a) return {card: a.closest("article.job"), link: true};
  var card = ev.target.closest("article.job[data-href]");
  if (!card || ev.target.closest("a,button,summary,details,form,input,select,textarea,label")) return null;
  if (String(window.getSelection && window.getSelection()).trim()) return null;   // selecting text, not clicking
  return {card: card, link: false};
}
document.addEventListener("click", function (ev) {
  if (ev.button !== 0 || dlg.contains(ev.target)) return;
  var t = cardOf(ev);
  if (!t || !t.card) return;
  if (ev.ctrlKey || ev.metaKey || ev.shiftKey) { if (!t.link) window.open(t.card.dataset.href, "_blank"); return; }
  ev.preventDefault();
  open(t.card.dataset.href, t.card);
});
document.addEventListener("auxclick", function (ev) {
  var t = cardOf(ev);
  if (t && t.card && !t.link && ev.button === 1) { ev.preventDefault(); window.open(t.card.dataset.href, "_blank"); }
});
})();
