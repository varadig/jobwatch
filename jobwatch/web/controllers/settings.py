"""The Settings page: saving it, presets, and the test buttons (push, e-mail, own sources, e-mail alerts)."""
from jobwatch.config import config_store
from jobwatch.llm import LlmError, llm_for
from jobwatch.services.notify import notifier
from jobwatch.services.settings import preset_action, save_settings, saved_message
from jobwatch.sources.custom import test_custom_sources
from jobwatch.sources.mail import test_mail_source
from jobwatch.web.controllers.base import Controller
from jobwatch.web.http import Response
from jobwatch.web.views.settings import settings_page


class SettingsController(Controller):
    def register(self, router):
        router.get(r"/settings", self.index)
        router.post(r"/settings", self.save)
        router.post(r"/preset", self.preset)
        router.post(r"/test", self.test)

    def index(self, req):
        return Response.html(settings_page(req.msg))

    def save(self, req):
        msg = saved_message(save_settings(req.form))
        # saved from the "unsaved changes" dialog: continue where you were going
        return Response.redirect(self.safe_next(req.form.get("next", ""), "/settings"), msg)

    def preset(self, req):
        return Response.redirect("/settings", preset_action(req.form) or "Nothing to do.", "presets")

    def test(self, req):
        f = req.form
        if "ntfy_server" in f:   # sent with the settings form: what is on the screen is saved first
            save_settings(f)
        with config_store.lock:
            cfg = config_store.load()
        what = f.get("what")
        if what == "custom":
            return Response.redirect("/settings", test_custom_sources(cfg)[:1500])
        if what == "llm":
            return Response.redirect("/settings", test_llm(cfg), "ai")
        if what == "mail":
            return Response.redirect("/settings", test_mail_source(cfg)[:1500], "mail")
        try:
            if what == "email":
                notifier.email(cfg, "jobwatch test", "E-mail notifications work.", "<p>E-mail notifications work. ✅</p>")
            else:
                notifier.push(cfg, "jobwatch test", "Push notifications work ✅", click=cfg.get("public_url"))
            msg = "Test sent."
        except Exception as ex:
            msg = "Test error: %s" % ex
        return Response.redirect("/settings", msg)


def test_llm(cfg):
    """Asks each configured model a trivial question, so a wrong key or an empty balance shows up here and now."""
    llm = llm_for(cfg)
    if not llm:
        return "No AI key is set: add a DeepSeek or an Anthropic key."
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
    results = []
    for provider in llm.providers:
        try:
            done = provider.json(system="Answer with the word ok.", user="Say ok.", schema=schema, effort="low",
                                 timeout=60.0, max_tokens=200)
            results.append("%s works (%s)" % (provider.label, done.model))
        except LlmError as ex:
            results.append("%s: %s" % (provider.label, ex))
    return "; ".join(results) + "."
