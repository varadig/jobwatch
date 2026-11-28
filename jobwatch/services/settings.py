"""Saving the Settings form."""
import re

from jobwatch.config import config_store
from jobwatch.domain.scoring import SCORING_KINDS, parse_chips
from jobwatch.models import FETCHED_SOURCES, WORK_MODES
from jobwatch.services.presets import preset_by_key, preset_of
from jobwatch.services.refresh import KEPT_ON_NEW_SEARCH, rescore_all, search_again, search_signature
from jobwatch.sources.custom import parse_custom_sources
from jobwatch.sources.registry import SEARCH_FILTERABLE


class SaveResult:
    """What saving the settings did: truthy when the search changed."""
    def __init__(self, search_changed):
        self.search_changed = search_changed

    def __bool__(self):
        return self.search_changed


def preset_action(f):
    """Settings → Presets buttons (save, delete, combine). Returns the message to show."""
    act = f.get("preset_do", "")
    if f.get("preset_del"):
        name = f["preset_del"]
        with config_store.lock:
            cfg = config_store.load()
            if name not in (cfg.get("presets") or {}):
                return "No such preset."
            del cfg["presets"][name]
            cfg["combined_presets"] = [k for k in cfg.get("combined_presets") or [] if k != name]
            config_store.save(cfg)
        return "Preset “%s” deleted; the current settings did not change." % name
    if act == "save":
        name = re.sub(r"\s+", " ", f.get("preset_name", "")).strip()[:60]
        if not name or name.startswith("tpl:"):
            return "Give the preset a name."
        changed = False
        if "ntfy_server" in f:  # sent with the settings form: what is on the screen counts
            changed = save_settings(f)
        with config_store.lock:
            cfg = config_store.load()
            cfg.setdefault("presets", {})[name] = preset_of(cfg)
            config_store.save(cfg)
        return "Saved as preset “%s”." % name + (" The search changed, so the postings found so far were cleared (%s) and "
                                                 "a new search started." % KEPT_ON_NEW_SEARCH if changed else "")
    if act == "combine":
        with config_store.lock:
            cfg = config_store.load()
            before = search_signature(cfg)
            cfg["combined_presets"] = combo_keys(f, cfg)
            config_store.save(cfg)
            changed = search_signature(cfg) != before
        rescore_all()
        if changed:
            search_again()
        n = len(cfg["combined_presets"])
        return ("Searching and scoring with %d preset%s alongside the current settings." % (n, "" if n == 1 else "s") if n
                else "No combined presets: only the current settings.") + (
            " The search changed, so the postings found so far were cleared (%s) and a new search started." % KEPT_ON_NEW_SEARCH
            if changed else " Postings re-scored.")
    return ""


def combo_keys(f, cfg):
    """The "Also search with" presets (fields combo:<key>), without unknown ones."""
    return [k for k in (k[6:] for k in f if k.startswith("combo:")) if preset_by_key(cfg, k)]


def save_settings(f):
    g = lambda k, d="": f.get(k, d)
    with config_store.lock:
        cfg = config_store.load()
        before = search_signature(cfg)
        cfg["ntfy"].update(enabled="ntfy_enabled" in f, server=g("ntfy_server", "https://ntfy.sh").strip() or "https://ntfy.sh",
                           topic=re.sub(r"[^A-Za-z0-9_-]", "", g("ntfy_topic")) or cfg["ntfy"]["topic"])
        cfg["email"].update(enabled="email_enabled" in f, smtp_host=g("smtp_host").strip(),
                            smtp_port=int(g("smtp_port", "587") or 587), username=g("smtp_user").strip(),
                            to=g("email_to").strip())
        if g("anthropic_api_key").strip():
            cfg["anthropic_api_key"] = g("anthropic_api_key").strip()
        if g("deepseek_api_key").strip():
            cfg["deepseek_api_key"] = g("deepseek_api_key").strip()
        if "llm_primary" in f:   # the AI section is part of the form
            cfg["llm"] = {"primary": g("llm_primary") if g("llm_primary") in ("deepseek", "anthropic") else "deepseek",
                          "deepseek_model": g("deepseek_model") if g("deepseek_model") in ("deepseek-chat", "deepseek-reasoner")
                          else "deepseek-chat"}
        if g("smtp_pass"):
            cfg["email"]["password"] = g("smtp_pass").replace(" ", "")
        cfg["interval_minutes"] = max(15, int(g("interval", "60") or 60))
        if "interval" in f:
            cfg["mode_claude"] = "mode_claude" in f
        cfg["min_score"] = int(g("min_score", "4") or 4)
        cfg["notify_min_score"] = int(g("notify_min_score", "7") or 7)
        cfg["notify_work_modes"] = [k for k in WORK_MODES if ("nwm_" + k) in f]
        cfg["default_work_filter"] = g("default_work_filter", "remote_unknown")
        cfg["public_url"] = g("public_url").strip()
        cfg["sources"] = {k: ("src_" + k) in f for k in FETCHED_SOURCES}
        if "search_terms" in f:
            cfg["search_terms"] = [l.strip() for l in g("search_terms").splitlines() if l.strip().strip("|").strip()]
            cfg["search_locations"] = [l.strip() for l in g("search_locations").splitlines() if l.strip()]
            cfg["search_filter"] = {k: ("sf_" + k) in f for k in SEARCH_FILTERABLE}
        cfg["studios"] = [l.strip() for l in g("studios").splitlines() if l.strip().count("|") >= 1]
        cfg["rss_feeds"] = [l.strip() for l in g("rss_feeds").splitlines() if l.strip().startswith("http")]
        cfg["custom_sources"] = parse_custom_sources(f, cfg.get("custom_sources"))
        if "mail_senders" in f:
            ms = cfg.setdefault("mail_source", {})
            ms.update(senders=[l.strip() for l in g("mail_senders").splitlines() if l.strip()],
                      days=max(1, min(60, int(g("mail_days", "7") or 7))), use_claude="mail_use_claude" in f,
                      imap_host=g("mail_imap_host").strip() or "imap.gmail.com", imap_port=int(g("mail_imap_port", "993") or 993),
                      folder=g("mail_folder").strip() or "INBOX", username=g("mail_user").strip())
            if g("mail_pass"):
                ms["password"] = g("mail_pass").replace(" ", "")
            if not ms["username"]:
                ms["password"] = ""  # no own login: the e-mail account's is used
        if "sc_interests" in f:
            cfg["scoring"] = {k: parse_chips(g("sc_" + k)) for k in SCORING_KINDS}
        if "combo_form" in f:   # the "Also search with" boxes are part of the form
            cfg["combined_presets"] = combo_keys(f, cfg)
        config_store.save(cfg)
        changed = search_signature(cfg) != before
    rescore_all()
    if changed:
        search_again()
    return SaveResult(changed)


def saved_message(res):
    return (("Saved. The search changed, so the postings found so far were cleared (%s) and a new search started: "
             "the list fills up again in a few minutes." % KEPT_ON_NEW_SEARCH) if res else "Saved, postings re-scored.")
