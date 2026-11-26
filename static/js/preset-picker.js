// Settings: the preset list, with an X to delete your own presets (a second click confirms).
(function () {
var sel = document.getElementById("preset_pick"), btn = document.getElementById("pp_btn"), list = document.getElementById("pp_list");
var label = document.getElementById("pp_label"), state = document.getElementById("preset_state");
var X = '<span class="ms" aria-hidden="true">&#xe14c;</span>', data = JSON.parse(document.getElementById("preset_data").textContent || "{}");
function showLabel() { var o = sel.options[sel.selectedIndex]; label.textContent = o ? o.text : ""; }
function build() {
  list.innerHTML = "";
  Array.prototype.forEach.call(sel.children, function (node) {
    var opts = node.tagName === "OPTGROUP" ? node.children : [node];
    if (node.tagName === "OPTGROUP") {
      if (!opts.length) return;
      var g = document.createElement("li"); g.className = "pp-group"; g.setAttribute("role", "presentation");
      g.textContent = node.label; list.appendChild(g);
    }
    Array.prototype.forEach.call(opts, function (o) {
      var li = document.createElement("li"), mine = node.tagName === "OPTGROUP" && o.value && o.value.indexOf("tpl:") !== 0;
      li.setAttribute("role", "option"); li.dataset.value = o.value; li.id = "pp_o" + list.children.length;
      li.setAttribute("aria-selected", o.selected ? "true" : "false");
      var t = document.createElement("span"); t.className = "pp-t"; t.textContent = o.text; li.appendChild(t);
      if (mine) {
        var x = document.createElement("button"); x.type = "button"; x.className = "pp-x"; x.innerHTML = X;
        x.setAttribute("aria-label", "Delete preset " + o.text); x.title = "Delete this preset";
        li.appendChild(x);
      }
      list.appendChild(li);
    });
  });
}
function items() { return Array.prototype.slice.call(list.querySelectorAll("[role=option]")); }
function setActive(li) {
  items().forEach(function (x) { x.classList.toggle("act", x === li); });
  if (li) { list.setAttribute("aria-activedescendant", li.id); li.scrollIntoView({block: "nearest"}); }
}
function open() {
  build(); list.classList.add("open"); btn.setAttribute("aria-expanded", "true");
  setActive(list.querySelector("[aria-selected=true]") || items()[0]); list.focus();
}
function close(focus) {
  list.classList.remove("open"); btn.setAttribute("aria-expanded", "false");
  if (focus) btn.focus();
}
function choose(li) {
  if (!li) return;
  sel.value = li.dataset.value; showLabel(); close(true);
  sel.dispatchEvent(new Event("change", {bubbles: true}));
}
function remove(li, x) {
  if (!x.classList.contains("sure")) {   // first click: ask
    x.classList.add("sure"); x.textContent = "Delete?"; x.setAttribute("aria-label", "Click again to delete");
    setTimeout(function () { if (x.isConnected && x.classList.contains("sure")) { x.classList.remove("sure"); x.innerHTML = X; } }, 3500);
    return;
  }
  var name = li.dataset.value;
  li.classList.add("busy");
  fetch("/preset", {method: "POST", body: new URLSearchParams({preset_do: "delete", preset_pick: name})}).then(function (r) {
    if (!r.ok) throw r;
    var o = Array.prototype.find.call(sel.options, function (op) { return op.value === name; }), was = o && o.selected;
    if (o) { var g = o.parentNode; o.remove(); if (g.tagName === "OPTGROUP" && !g.children.length) g.remove(); }
    delete data[name];
    var act = document.getElementById("active_preset_set");
    if (act && act.value === name) act.value = "";
    if (was) { sel.value = ""; showLabel(); }
    li.remove(); build(); setActive(items()[0]);
    state.textContent = "Preset “" + name + "” deleted. The settings below did not change.";
  }).catch(function () { li.classList.remove("busy"); state.textContent = "Could not delete “" + name + "”; try again."; });
}
btn.addEventListener("click", function () { list.classList.contains("open") ? close(true) : open(); });
btn.addEventListener("keydown", function (ev) { if (ev.key === "ArrowDown" || ev.key === "ArrowUp") { ev.preventDefault(); open(); } });
list.addEventListener("click", function (ev) {
  var li = ev.target.closest("[role=option]"); if (!li) return;
  var x = ev.target.closest(".pp-x");
  if (x) { ev.stopPropagation(); remove(li, x); } else choose(li);
});
list.addEventListener("keydown", function (ev) {
  var all = items(), cur = list.querySelector(".act"), i = all.indexOf(cur);
  if (ev.key === "ArrowDown") { ev.preventDefault(); setActive(all[Math.min(i + 1, all.length - 1)]); }
  else if (ev.key === "ArrowUp") { ev.preventDefault(); setActive(all[Math.max(i - 1, 0)]); }
  else if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); choose(cur); }
  else if (ev.key === "Delete" && cur && cur.querySelector(".pp-x")) { ev.preventDefault(); remove(cur, cur.querySelector(".pp-x")); }
  else if (ev.key === "Escape" || ev.key === "Tab") { if (ev.key === "Escape") ev.preventDefault(); close(ev.key === "Escape"); }
});
document.addEventListener("click", function (ev) { if (!ev.target.closest("#pp")) close(false); });
sel.addEventListener("change", showLabel);
showLabel();
})();
