// Settings → Presets: picking a preset in "Add preset…" adds it (the form is saved first); × on a saved preset
// deletes it, the first click only arms the button.
(function () {
var add = document.getElementById("combo_add"), btn = document.getElementById("combo_add_btn");
if (add && btn) add.addEventListener("change", function () { if (add.value) add.form.requestSubmit(btn); });
document.querySelectorAll("#saved_presets button[name=preset_del]").forEach(function (b) {
  b.addEventListener("click", function (ev) {
    if (b.dataset.armed) return;
    ev.preventDefault(); b.dataset.armed = "1"; b.textContent = "delete?";
    setTimeout(function () { delete b.dataset.armed; b.textContent = "×"; }, 3000);
  });
});
})();
