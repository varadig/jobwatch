"""Saving the Settings form."""
import re

from jobwatch.config import config_store
from jobwatch.domain.scoring import SCORING_KINDS, parse_chips
from jobwatch.models import FETCHED_SOURCES, WORK_MODES
from jobwatch.services.presets import (
    active_set, apply_preset, free_preset_name, preset_by_key, preset_label, preset_of, same_as_preset
)
from jobwatch.services.refresh import KEPT_ON_NEW_SEARCH, rescore_all, search_again, search_signature
from jobwatch.sources.custom import parse_custom_sources
from jobwatch.sources.registry import SEARCH_FILTERABLE


class SaveResult:
    """What saving the settings did: truthy when the search changed."""
    def __init__(self, search_changed, preset_note=""):
        self.search_changed, self.preset_note = search_changed, preset_note

    def __bool__(self):
        return self.search_changed


def _new_search_note(changed):
    return (" The search changed, so the postings found so far were cleared (%s) and a new search started." % KEPT_ON_NEW_SEARCH
            if changed else " Postings re-scored.")


def _finish(cfg, before, msg):
    """Saves cfg (the lock is held by the caller), then re-scores, and searches again if the search changed."""
    config_store.save(cfg)
    return msg, search_signature(cfg) != before


def preset_action(f):
    """Settings → Presets buttons: edit, only, remove, add, save as new, delete. The settings form is saved first
    (into the preset being edited), so nothing typed is lost. Returns the message to show."""
    keys = ("preset_edit", "preset_only", "preset_remove", "preset_add", "preset_del")
    key = next((k for k in keys if f.get(k)), "")
    act = f.get("preset_do", "")
    if not key and act != "save":
        return ""
    note = ""
    if "ntfy_server" in f and key != "preset_del":   # sent with the settings form: what is on the screen counts
        res = save_settings(f, sync=(act != "save"))
        note = (" " + res.preset_note) if res.preset_note else ""
    if key == "preset_del":
        name = f["preset_del"]
        with config_store.lock:
            cfg = config_store.load()
            if name == cfg.get("active_preset"):
                return "Switch to another preset first: this one is being edited."
            if name not in (cfg.get("presets") or {}):
                return "No such preset."
            del cfg["presets"][name]
            cfg["combined_presets"] = [k for k in cfg.get("combined_presets") or [] if k != name]
            config_store.save(cfg)
        return "Preset “%s” deleted." % name
    with config_store.lock:
        cfg = config_store.load()
        before = search_signature(cfg)
        active, combined = cfg.get("active_preset") or "", list(cfg.get("combined_presets") or [])
        if act == "save":
            name = re.sub(r"\s+", " ", f.get("preset_name", "")).strip()[:60]
            if not name or name.startswith("tpl:"):
                return "Give the preset a name."
            cfg.setdefault("presets", {})[name] = preset_of(cfg)
            cfg["active_preset"], cfg["combined_presets"] = name, [k for k in combined if k != name]
            msg = "Saved as preset “%s”, now being edited." % name
        elif key == "preset_edit":
            k = f["preset_edit"]
            preset = preset_by_key(cfg, k)
            if preset is None:
                return "No such preset."
            if k != active:
                apply_preset(cfg, preset)
                cfg["active_preset"] = k
                cfg["combined_presets"] = ([active] if active else []) + [c for c in combined if c != k]
            msg = "Editing “%s”." % preset_label(k) + (" It is a template: saving makes your own copy." if k.startswith("tpl:") else "")
        elif key == "preset_only":
            k = f["preset_only"]
            preset = preset_by_key(cfg, k)
            if preset is None:
                return "No such preset."
            apply_preset(cfg, preset)
            cfg["active_preset"], cfg["combined_presets"] = k, []
            msg = "Searching and scoring with only “%s”." % preset_label(k)
        elif key == "preset_remove":
            k = f["preset_remove"]
            rest = [x for x in active_set(cfg) if x != k]
            if not rest:
                return "At least one preset must stay active."
            if k == active:
                apply_preset(cfg, preset_by_key(cfg, rest[0]))
                cfg["active_preset"], cfg["combined_presets"] = rest[0], rest[1:]
            else:
                cfg["combined_presets"] = [c for c in combined if c != k]
            msg = "“%s” is no longer searched." % preset_label(k)
        else:   # preset_add
            k = f["preset_add"]
            if preset_by_key(cfg, k) is None:
                return "No such preset."
            if k not in active_set(cfg):
                cfg["combined_presets"] = combined + [k]
            msg = "“%s” is searched too." % preset_label(k)
        msg, changed = _finish(cfg, before, msg)
    rescore_all()
    if changed:
        search_again()
    return msg + note + _new_search_note(changed)


def sync_active_preset(cfg):
    """The settings on the page belong to the preset being edited: an own one is updated, a changed template
    becomes your own copy. Returns a note for the user."""
    active = cfg.get("active_preset") or ""
    if not active:
        return ""
    if active.startswith("tpl:"):
        if same_as_preset(cfg, preset_by_key(cfg, active)):
            return ""
        name = free_preset_name(cfg, active[4:])
        cfg.setdefault("presets", {})[name] = preset_of(cfg)
        cfg["active_preset"] = name
        return "The template stays as it was; your changes were saved as your own preset “%s”." % name
    cfg.setdefault("presets", {})[active] = preset_of(cfg)
    return ""


def save_settings(f, sync=True):
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
        note = sync_active_preset(cfg) if sync and "sc_interests" in f else ""
        config_store.save(cfg)
        changed = search_signature(cfg) != before
    rescore_all()
    if changed:
        search_again()
    return SaveResult(changed, note)


def saved_message(res):
    return (("Saved. The search changed, so the postings found so far were cleared (%s) and a new search started: "
             "the list fills up again in a few minutes." % KEPT_ON_NEW_SEARCH) if res else "Saved, postings re-scored.") + (
        " " + res.preset_note if res.preset_note else "")
