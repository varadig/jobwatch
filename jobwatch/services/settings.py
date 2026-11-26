"""Saving the Settings form."""
import copy
import re

from jobwatch.config import config_store
from jobwatch.domain.scoring import SCORING_KINDS, parse_chips
from jobwatch.models import FETCHED_SOURCES, WORK_MODES
from jobwatch.services.presets import (
    apply_preset, clean_preset_name, free_preset_name, preset_by_key, preset_of, same_as_preset, template_presets
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


def preset_action(f):
    """Settings → Presets buttons. Returns the message to show."""
    act, pick = f.get("preset_do", ""), f.get("preset_pick", "")
    if act == "save":
        name = re.sub(r"\s+", " ", f.get("preset_name", "")).strip()[:60]
        if not name:
            return "Give the preset a name."
        changed = False
        if "ntfy_server" in f:  # sent with the settings form: what is on the screen counts
            with config_store.lock:
                old = config_store.load()
            changed = save_settings(f)
        with config_store.lock:
            cfg = config_store.load()
            if "ntfy_server" in f and old.get("active_preset") in (old.get("presets") or {}) and old["active_preset"] != name:
                cfg["presets"][old["active_preset"]] = old["presets"][old["active_preset"]]  # the edits go to the new one only
            cfg.setdefault("presets", {})[name] = preset_of(cfg)
            cfg["active_preset"] = name
            config_store.save(cfg)
        return "Saved as preset “%s”." % name + (" The search changed, so the postings found so far were cleared (%s) and "
                                                 "a new search started." % KEPT_ON_NEW_SEARCH if changed else "")
    if act == "load":
        pre = ""
        if f.get("save_first") and "ntfy_server" in f:  # "Save, then switch" from the unsaved-changes dialog
            save_settings(f)
            pre = "Your changes were saved. "
        if pick.startswith("tpl:"):
            preset, name = template_presets().get(pick[4:]), "%s (template)" % pick[4:]
        else:
            with config_store.lock:
                preset, name = (config_store.load().get("presets") or {}).get(pick), pick
        if not preset:
            return "Pick a preset first."
        with config_store.lock:
            cfg = config_store.load()
            before = search_signature(cfg)
            apply_preset(cfg, preset)
            cfg["active_preset"] = pick
            config_store.save(cfg)
            changed = search_signature(cfg) != before
        rescore_all()
        if changed:
            search_again()
        return (pre + "Switched to “%s”. " % name
                + ("The search changed, so the postings found so far were cleared (%s) and a new search started." % KEPT_ON_NEW_SEARCH
                   if changed else "Postings re-scored.")
                )
    if act == "rename":
        new = clean_preset_name(f.get("preset_name"))
        with config_store.lock:
            cfg = config_store.load()
            old = cfg.get("active_preset") or ""
            if old not in (cfg.get("presets") or {}):
                return "Only your own presets can be renamed (pick or save one first)."
            if not new or new.startswith("tpl:"):
                return "Type the new name in the name field."
            if new != old and new in cfg["presets"]:
                return "There is already a preset called “%s”." % new
            cfg["presets"] = {(new if k == old else k): v for k, v in cfg["presets"].items()}
            cfg["active_preset"] = new
            config_store.save(cfg)
        return "Preset “%s” renamed to “%s”." % (old, new)
    if act == "delete":
        with config_store.lock:
            cfg = config_store.load()
            if pick not in (cfg.get("presets") or {}):
                return "Pick one of your presets to delete (templates cannot be deleted)."
            del cfg["presets"][pick]
            if cfg.get("active_preset") == pick:
                cfg["active_preset"] = ""
            config_store.save(cfg)
        return "Preset “%s” deleted; the current settings did not change." % pick
    return ""


def save_settings(f):
    g = lambda k, d="": f.get(k, d)
    with config_store.lock:
        cfg = config_store.load()
        old_cfg = copy.deepcopy(cfg)
        before = search_signature(cfg)
        cfg["ntfy"].update(enabled="ntfy_enabled" in f, server=g("ntfy_server", "https://ntfy.sh").strip() or "https://ntfy.sh",
                           topic=re.sub(r"[^A-Za-z0-9_-]", "", g("ntfy_topic")) or cfg["ntfy"]["topic"])
        cfg["email"].update(enabled="email_enabled" in f, smtp_host=g("smtp_host").strip(),
                            smtp_port=int(g("smtp_port", "587") or 587), username=g("smtp_user").strip(),
                            to=g("email_to").strip())
        if g("anthropic_api_key").strip():
            cfg["anthropic_api_key"] = g("anthropic_api_key").strip()
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
        note = ""
        if "sc_interests" in f:  # the whole form: decide which preset these settings are
            pick = g("active_preset_set", cfg.get("active_preset") or "")
            base = preset_by_key(cfg, pick)
            if same_as_preset(cfg, base):
                cfg["active_preset"] = pick                      # exactly the picked preset or template
            elif base is None and not pick and same_as_preset(cfg, preset_of(old_cfg)):
                pass                                             # no preset yet and the search did not change
            else:  # changed: saved as a preset of its own, never over the picked one unless you name it
                name = clean_preset_name(g("preset_name"))
                if not name or name.startswith("tpl:") or name == pick and pick.startswith("tpl:"):
                    name = free_preset_name(cfg)
                existed = name in (cfg.get("presets") or {})
                cfg.setdefault("presets", {})[name] = preset_of(cfg)
                cfg["active_preset"] = name
                note = ("Preset “%s” updated." if existed else
                        "Your changes were saved as a new preset: “%s” (rename it under Presets).") % name
        config_store.save(cfg)
        changed = search_signature(cfg) != before
    rescore_all()
    if changed:
        search_again()
    return SaveResult(changed, note)


def saved_message(res):
    return (("Saved. The search changed, so the postings found so far were cleared (%s) and a new search started: "
             "the list fills up again in a few minutes." % KEPT_ON_NEW_SEARCH) if res else "Saved, postings re-scored.") + (
        " " + res.preset_note if getattr(res, "preset_note", "") else "")
