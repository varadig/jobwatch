// Settings: the scoring chips (drag or arrows to reorder; the order is the score) and the try-it field.
(function () {
var P = JSON.parse(document.getElementById("jw_scoring").textContent);
function pts(k, i) { var p = P[k]; return i < p.t.length ? p.t[i] : p.rest; }
function fmt(n) { return (n >= 0 ? "+" : "−") + Math.abs(n); }
function alts(c) { return c.toLowerCase().split(/\s+\/\s+/).map(function (s) { return s.trim(); }).filter(Boolean); }
function esc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
function hit(as, txt) {
  for (var i = 0; i < as.length; i++)
    if (new RegExp("(?<![a-z0-9])" + esc(as[i]) + "(?![a-z0-9#+])").test(txt)) return as[i];
  return null;
}
document.querySelectorAll("textarea.chips-src").forEach(function (ta) {
  var k = ta.dataset.kind, ordered = P[k].t.length > 0, drag = null;
  var items = ta.value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean);
  var box = document.createElement("div"), inp = document.createElement("input");
  box.className = "chips"; box.setAttribute("role", "list");
  inp.type = "text"; inp.id = ta.id + "_in"; inp.placeholder = "Add… (Enter)"; inp.autocomplete = "off";
  var lab = document.querySelector('label[for="' + ta.id + '"]');
  if (lab) { lab.htmlFor = inp.id; inp.setAttribute("aria-label", "Add to: " + lab.textContent); }
  ta.hidden = true; ta.after(box);
  function sync(focus) {
    ta.value = items.join("\n"); render();
    if (focus) { var el = box.querySelector(focus); if (el) el.focus(); }
    document.dispatchEvent(new Event("chips"));
  }
  function button(label, text, fn) {
    var b = document.createElement("button"); b.type = "button"; b.textContent = text;
    b.setAttribute("aria-label", label); b.title = label; b.onclick = fn; return b;
  }
  function render() {
    box.textContent = "";
    items.forEach(function (t, i) {
      var c = document.createElement("span"), p = pts(k, i), b = document.createElement("b");
      c.className = "chip" + (p < 0 ? " neg" : ""); c.setAttribute("role", "listitem"); c.draggable = true;
      b.className = "pts"; b.textContent = fmt(p); c.append(b, document.createTextNode(t));
      if (ordered) {
        if (i > 0) c.append(button("Move " + t + " up", "‹", function () {
          items.splice(i - 1, 0, items.splice(i, 1)[0]); sync(".chip:nth-child(" + i + ") button"); }));
        if (i < items.length - 1) c.append(button("Move " + t + " down", "›", function () {
          items.splice(i + 1, 0, items.splice(i, 1)[0]); sync(".chip:nth-child(" + (i + 2) + ") button:last-of-type"); }));
      }
      c.append(button("Remove " + t, "×", function () { items.splice(i, 1); sync("input"); }));
      c.addEventListener("dragstart", function (ev) { drag = i; c.classList.add("dragging"); ev.dataTransfer.effectAllowed = "move"; ev.dataTransfer.setData("text/plain", t); });
      c.addEventListener("dragend", function () { drag = null; c.classList.remove("dragging"); });
      c.addEventListener("dragover", function (ev) { if (drag !== null) { ev.preventDefault(); c.classList.add("over"); } });
      c.addEventListener("dragleave", function () { c.classList.remove("over"); });
      c.addEventListener("drop", function (ev) {
        ev.preventDefault(); c.classList.remove("over");
        if (drag === null || drag === i) return;
        items.splice(i, 0, items.splice(drag, 1)[0]); drag = null; sync();
      });
      box.append(c);
    });
    box.append(inp);
  }
  function add(focus) {
    var v = inp.value.replace(/,+$/, "").trim();
    if (!v) return;
    if (!items.some(function (x) { return x.toLowerCase() === v.toLowerCase(); })) items.push(v);
    inp.value = ""; sync(focus ? "input" : null);
  }
  inp.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" || ev.key === ",") { ev.preventDefault(); add(true); }
    else if (ev.key === "Backspace" && !inp.value && items.length) { items.pop(); sync("input"); }
  });
  inp.addEventListener("change", function () { add(false); });
  ta.setChips = function (list) { items = (list || []).slice(); sync(); };
  render();
});
var tr = document.getElementById("score_try"), out = document.getElementById("score_try_out");
if (!tr) return;
var hint = out.textContent;
function lines(k) { var t = document.getElementById("sc_" + k); return t ? t.value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean) : []; }
function num(id, d) { var el = document.getElementById(id); return el && el.value !== "" ? +el.value : d; }
function calc() {
  var t = tr.value.toLowerCase().trim();
  if (!t) { out.textContent = hint; return; }
  var s = 0, parts = [];
  ["interests", "less", "never"].forEach(function (k) {
    lines(k).forEach(function (c, i) { var a = hit(alts(c), t); if (a) { var p = pts(k, i); s += p; parts.push(a + " " + fmt(p)); } });
  });
  var min = num("min_score", 4), note = num("notify_min_score", 7);
  out.textContent = (parts.length ? parts.join(", ") : "nothing matches") + "  =  " + s + " points (title only) · " +
    (s >= note ? "listed and notified" : s >= min ? "listed" : "hidden; the description can add up to +" + P.desc.t[0] + "…+4");
}
tr.addEventListener("input", calc); document.addEventListener("chips", calc);
document.querySelectorAll("#min_score,#notify_min_score").forEach(function (el) { el.addEventListener("input", calc); });
})();
