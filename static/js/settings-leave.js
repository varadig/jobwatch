// Settings: asks before leaving with unsaved changes; a picked preset fills in the form.
(function () {
var form = document.getElementById("settings"), dlg = document.getElementById("leave_dlg");
if (!form || !dlg) return;
var SEARCH = ["search", "own"], SKIP = /^(preset_name|preset_do|preset_del|preset_add|what|next|save_first)$/;
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
function update() {
  var d = dirty();
  if (!d.length) { note.textContent = note0; return; }
  note.textContent = "Unsaved changes: " + d.map(title).join(", ") +
    (d.some(function (x) { return SEARCH.indexOf(x) >= 0; }) ? ". The search changed: saving clears the found postings and searches again." : ".");
}
form.addEventListener("input", update); form.addEventListener("change", update); document.addEventListener("chips", update);
form.addEventListener("submit", function () { leaving = true; });
window.addEventListener("beforeunload", function (ev) { if (!leaving && dirty().length) { ev.preventDefault(); ev.returnValue = ""; } });
function ask(onSave, onDiscard, saveLabel, discardLabel) {
  var d = dirty();
  document.getElementById("leave_what").textContent = "You changed: " + d.map(title).join(", ") + ".";
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
})();
