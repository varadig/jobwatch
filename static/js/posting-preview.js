// A posting's detail view: loads the full posting text from its page.
(function () {
window.jdLoadPreview = function (root) {
  var box = root.querySelector("#jd-text[data-preview]"), src = root.querySelector("#jd-src");
  if (!box) return;
  fetch(box.dataset.preview).then(function (r) { return r.ok ? r.text() : ""; }).then(function (h) {
    if (h) { box.innerHTML = h; src.textContent = "From the posting's own page."; }
    else src.textContent = "The full posting could not be read from its page (it may need a login); this is the text jobwatch has. Use \u201cOpen the original \u2197\u201d.";
  }).catch(function () { src.textContent = "The full posting could not be loaded; this is the text jobwatch has."; });
};
})();
