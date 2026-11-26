// Every page: the remembered Jobs filters, mouse-wheel momentum, closing a dialog by clicking outside it.
(function () {  // the Jobs page's filters are remembered in this browser: the Jobs tab opens them again
  var K = "jw_jobs_query", saved = null;
  try {
    if (location.pathname === "/") {
      var p = new URLSearchParams(location.search);
      p.delete("msg");
      if (p.get("reset") === "1") { localStorage.removeItem(K); history.replaceState(null, "", "/"); }
      else if (p.toString()) localStorage.setItem(K, "?" + p.toString());
      else if (!location.search && (saved = localStorage.getItem(K))) { location.replace("/" + saved); return; }
    }
    saved = localStorage.getItem(K);
  } catch (x) { return; }
  if (saved) document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll('header nav a[href="/"]').forEach(function (a) { a.href = "/" + saved; });
  });
})();
(function () {  // mouse wheel: a notch carries further and glides to a stop (momentum with friction)
  if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  var v = 0, raf = 0, BOOST = 0.3, FRICTION = 0.88;   // one notch (100 px) travels about 100 * BOOST / (1 - FRICTION) = 250 px
  function ownScroller(el) {   // inside something that scrolls by itself: leave it to the browser
    for (; el && el.nodeType === 1 && el !== document.body && el !== document.documentElement; el = el.parentElement) {
      if (el.matches("textarea, select, dialog")) return true;
      var oy = getComputedStyle(el).overflowY;
      if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 1) return true;
    }
    return false;
  }
  function step() {
    var before = window.scrollY;
    window.scrollBy(0, v);
    v *= FRICTION;
    if (Math.abs(v) < 0.6 || window.scrollY === before) { v = 0; raf = 0; return; }
    raf = requestAnimationFrame(step);
  }
  addEventListener("wheel", function (ev) {
    if (ev.ctrlKey || ev.shiftKey || ev.defaultPrevented || Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) return;
    var d = ev.deltaY * (ev.deltaMode === 1 ? 40 : ev.deltaMode === 2 ? innerHeight : 1);
    if (ev.deltaMode === 0 && Math.abs(d) < 50) return;   // a touchpad: small steps, it has its own momentum
    if (document.querySelector("dialog[open]") || ownScroller(ev.target)) return;
    ev.preventDefault();
    if (v && (v > 0) !== (d > 0)) v = 0;   // the other way: stop first
    v += d * BOOST;
    if (!raf) raf = requestAnimationFrame(step);
  }, {passive: false});
  ["mousedown", "keydown", "touchstart"].forEach(function (t) { addEventListener(t, function () { v = 0; }, {passive: true}); });
})();
(function () {  // a click outside an open dialog's box closes it (only if the press started outside too: not while selecting text)
  var downOut = false;
  function outside(ev) { var d = ev.target.closest && ev.target.closest("dialog[open]"); return d && !ev.target.closest(".modal-box") ? d : null; }
  document.addEventListener("pointerdown", function (ev) { downOut = !!outside(ev); }, true);
  document.addEventListener("click", function (ev) { var d = outside(ev); if (d && downOut) { ev.preventDefault(); d.close(); } downOut = false; }, true);
})();
