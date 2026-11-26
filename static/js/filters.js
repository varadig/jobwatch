// Jobs page: the folded filter panel (opens with an animation; whether it is open is remembered in the browser).
(function () {
var sec = document.querySelector(".ui-filters"), btn = document.getElementById("flt_btn"), panel = document.getElementById("flt_panel");
if (!sec || !btn) return;
function set(open, animate) {
  if (!animate) sec.classList.add("no-anim");
  sec.classList.toggle("open", open);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) panel.removeAttribute("inert"); else panel.setAttribute("inert", "");
  if (!animate) { void sec.offsetHeight; sec.classList.remove("no-anim"); }
}
var saved = null;
try { saved = localStorage.getItem("jw_filters_open"); } catch (x) {}
set(saved === "1", false);
btn.addEventListener("click", function () {
  var open = !sec.classList.contains("open");
  set(open, true);
  try { localStorage.setItem("jw_filters_open", open ? "1" : "0"); } catch (x) {}
});
})();
