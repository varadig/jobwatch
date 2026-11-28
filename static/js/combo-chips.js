// Settings → Presets: the combined presets as chips. Pick one from the list to add it, × removes it.
(function () {
var box = document.getElementById("combo_chips"), add = document.getElementById("combo_add");
// × on a saved preset deletes it; the first click only arms the button
document.querySelectorAll("#saved_presets button[name=preset_del]").forEach(function (b) {
  b.addEventListener("click", function (ev) {
    if (b.dataset.armed) return;
    ev.preventDefault(); b.dataset.armed = "1"; b.textContent = "delete?";
    setTimeout(function () { delete b.dataset.armed; b.textContent = "×"; }, 3000);
  });
});
if (!box || !add) return;
function label(key) { return JSON.parse(box.dataset.labels)[key] || key; }
function chip(key) {
  var c = document.createElement("span"), h = document.createElement("input"), b = document.createElement("button");
  c.className = "chip"; c.dataset.key = key; c.setAttribute("role", "listitem");
  h.type = "hidden"; h.name = "combo:" + key; h.value = "on";
  b.type = "button"; b.textContent = "×"; b.title = b.ariaLabel = "Remove " + label(key);
  b.onclick = function () {
    var o = document.createElement("option"); o.value = key; o.textContent = label(key);
    add.append(o); c.remove(); update(); box.dispatchEvent(new Event("change", { bubbles: true }));
    add.focus();
  };
  c.append(h, document.createTextNode(label(key)), b);
  return c;
}
function update() { add.hidden = add.options.length < 2; }   // the first option is the "Add preset…" prompt
box.querySelectorAll(".chip").forEach(function (c) {
  var key = c.dataset.key, fresh = chip(key);
  c.replaceWith(fresh);
});
add.addEventListener("change", function () {
  var key = add.value; if (!key) return;
  add.options[add.selectedIndex].remove(); add.selectedIndex = 0;
  box.insertBefore(chip(key), add); update();
  box.dispatchEvent(new Event("change", { bubbles: true }));
});
update();
})();
