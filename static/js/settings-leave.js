// Settings: asks before leaving with unsaved changes; a picked preset fills in the form.
(function () {
var form = document.getElementById("settings"), dlg = document.getElementById("leave_dlg");
if (!form || !dlg) return;
var SEARCH = ["search", "own"], SKIP = /^(preset_pick|preset_name|preset_do|what|next|save_first)$/;
function snap() {
  var out = {};
  form.querySelectorAll("section.ui-section").forEach(function (sec) {
    var v = [];
    sec.querySelectorAll("input,select,textarea").forEach(function (el) {
      if (!el.name || SKIP.test(el.name) || el.type === "button" || el.type === "submit") return;
      v.push(el.name + "=" + (el.type === "checkbox" || el.type === "radio" ? el.checked : el.value));
    });
    out[sec.id] = v.join("\u0001");
  });
  return out;
}
var base = snap(), leaving = false, pending = null;
function dirty() {
  var now = snap(), out = [];
  Object.keys(now).forEach(function (k) { if (now[k] !== base[k]) out.push(k); });
  return out;
}
function title(id) { var h = document.getElementById(id + "-h"); return h ? h.textContent : id; }
var note = document.getElementById("savebar_note"), note0 = note.textContent;
var PF = /^(search_terms|search_locations|min_score|sf_.*|sc_.*)$/;
function psnap() {
  var v = [];
  form.querySelectorAll("input,textarea").forEach(function (el) {
    if (el.name && PF.test(el.name)) v.push(el.name + "=" + (el.type === "checkbox" ? el.checked : el.value)); });
  return v.join("\u0001");
}
var pbase = psnap();
function newPreset() {
  if (psnap() === pbase) return "";
  var n = (document.getElementById("preset_name").value || "").trim() || document.getElementById("preset_name").placeholder;
  return " Saving keeps them as the preset \u201c" + n + "\u201d.";
}
function update() {
  var d = dirty();
  if (!d.length) { note.textContent = note0; return; }
  note.textContent = "Unsaved changes: " + d.map(title).join(", ") +
    (d.some(function (x) { return SEARCH.indexOf(x) >= 0; }) ? ". The search changed: saving clears the found postings and searches again." : ".") +
    newPreset();
}
form.addEventListener("input", update); form.addEventListener("change", update); document.addEventListener("chips", update);
form.addEventListener("submit", function () { leaving = true; });
window.addEventListener("beforeunload", function (ev) { if (!leaving && dirty().length) { ev.preventDefault(); ev.returnValue = ""; } });
function ask(onSave, onDiscard, saveLabel, discardLabel) {
  var d = dirty();
  document.getElementById("leave_what").textContent = "You changed: " + d.map(title).join(", ") + "." + newPreset();
  document.getElementById("leave_search").hidden = !d.some(function (x) { return SEARCH.indexOf(x) >= 0; });
  document.getElementById("leave_save").textContent = saveLabel;
  document.getElementById("leave_discard").textContent = discardLabel;
  pending = {save: onSave, discard: onDiscard};
  dlg.showModal();
}
document.getElementById("leave_stay").onclick = function () { pending = null; dlg.close(); };
document.getElementById("leave_save").onclick = function () { var p = pending; dlg.close(); if (p) { leaving = true; p.save(); } };
document.getElementById("leave_discard").onclick = function () { var p = pending; dlg.close(); if (p) { leaving = true; p.discard(); } };
// links and other forms that leave the page
document.addEventListener("click", function (ev) {
  var a = ev.target.closest("a[href]");
  if (!a || ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.shiftKey || a.target === "_blank") return;
  var href = a.getAttribute("href");
  if (!href || href.charAt(0) === "#" || !dirty().length) return;
  ev.preventDefault();
  ask(function () { document.getElementById("settings_next").value = a.pathname + a.search;
                    form.requestSubmit(document.getElementById("settings_save")); },
      function () { location.href = a.href; }, "Save and continue", "Don't save");
}, true);
document.querySelectorAll("form").forEach(function (f) {
  if (f === form || f.method === "dialog") return;
  f.addEventListener("submit", function (ev) {
    if (leaving || !dirty().length) return;
    ev.preventDefault();
    var sub = ev.submitter;
    ask(function () { document.getElementById("settings_next").value = "/settings";
                      form.requestSubmit(document.getElementById("settings_save")); },
        function () { leaving = true; f.requestSubmit(sub || undefined); }, "Save (stay here)", "Don't save, continue");
  });
});
// picking a preset or template fills in the form; it is applied only when you save
var pick = document.getElementById("preset_pick"), data = JSON.parse(document.getElementById("preset_data").textContent || "{}");
pick.addEventListener("change", function () {
  var p = data[pick.value];
  if (!p) return;
  function set(id, v) { var el = document.getElementById(id); if (el && v !== undefined) el.value = v; }
  if (p.search_terms) set("search_terms", p.search_terms.join("\n"));
  if (p.search_locations) set("search_locations", p.search_locations.join("\n"));
  if (p.min_score !== undefined) set("min_score", p.min_score);
  if (p.search_filter) form.querySelectorAll("input[name^=sf_]").forEach(function (c) { c.checked = !!p.search_filter[c.name.slice(3)]; });
  if (p.scoring) Object.keys(p.scoring).forEach(function (k) {
    var ta = document.getElementById("sc_" + k); if (ta && ta.setChips) ta.setChips(p.scoring[k]); });
  var tpl = pick.value.indexOf("tpl:") === 0;
  document.getElementById("active_preset_set").value = pick.value;
  pbase = psnap();   // this is the picked preset; changes from here on make a new one
  document.getElementById("preset_state").innerHTML = "";
  document.getElementById("preset_state").append("Loaded \u201c" + pick.options[pick.selectedIndex].text + "\u201d" +
    (tpl ? " (template)" : "") + " into the settings below. Nothing is applied until you click Save changes.");
  update();
});
})();
