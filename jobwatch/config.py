"""The settings (config.json): defaults, loading with migrations, saving."""
import copy
import json
import os
import secrets
import threading

from jobwatch.env import CFG_PATH, PORT

DEFAULT_CONFIG = {
    # Generic defaults. The concrete job type (keywords, searches, sources) comes from
    # templates/: tools/new_profile.py <name> --template <template>.
    "interval_minutes": 60,
    "min_score": 4,          # below this score a posting is hidden from the list by default
    "notify_min_score": 7,   # from this score on, a notification is sent
    "notify_work_modes": ["remote", "unknown"],   # no notification for hybrid/on-site postings
    "mode_claude": True,     # Claude reads the postings whose work mode the page and the rules cannot settle
    "default_work_filter": "remote_unknown",
    "public_url": "http://localhost:%d/" % PORT,
    "anthropic_api_key": "",
    "deepseek_api_key": "",
    # which model answers first: DeepSeek (cheap) or Claude; the other is the fallback when a key is set
    "llm": {"primary": "deepseek", "deepseek_model": "deepseek-chat"},
    "ntfy": {"enabled": True, "server": "https://ntfy.sh", "topic": ""},
    "email": {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
              "username": "", "password": "", "to": ""},
    "sources": {"linkedin": True, "meetfrank": True, "4dayweek": True, "flexa": True,
                "remotive": True, "studios": True, "rss": True, "custom": True, "mail": False},
    # job alert e-mails read from a mailbox (IMAP, read-only). Empty username/password: the e-mail (SMTP) login is used.
    "mail_source": {"imap_host": "imap.gmail.com", "imap_port": 993, "username": "", "password": "",
                    "folder": "INBOX", "senders": ["jackandjill.ai", "jobalerts-noreply@linkedin.com"], "days": 7,
                    "use_claude": True},
    # One search for every source: "keywords" or "keywords | location" per line. LinkedIn, Remotive and Flexa search
    # with them; the sources that list everything keep only the postings that match a term (see search_filter).
    "search_terms": [],        # e.g. "project manager | Hungary"
    "search_locations": ["European Union"],  # LinkedIn locations for the terms without their own
    "search_filter": {"meetfrank": True, "4dayweek": True, "studios": False, "rss": False, "custom": False},
    "active_preset": "",       # the preset being edited: its search and scoring are the settings above (see ensure_active)
    "combined_presets": [],    # the other active presets, searched and scored alongside (own names or "tpl:<template>")
    "rss_feeds": [],
    "custom_sources": [],      # own sources from Settings, see CUSTOM_TYPES
    "studios": [],            # "rendszer | azonosito | nev", pl. "ashby | acme | Acme"
    # Scoring as ordered chips (see SCORING_KINDS): in the ordered lists the position is the score.
    "scoring": {"interests": [], "desc": [], "less": [], "never": ["intern", "internship", "junior", "trainee", "student"],
                "loc_good": ["remote", "europe"], "loc_bad": []},
}


class ConfigStore:
    """config.json: read with the defaults filled in and the old formats migrated, written atomically and readable by
    the owner only (it holds passwords). Hold `lock` around a load-change-save, so two requests do not overwrite
    each other."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def load(self):
        cfg = copy.deepcopy(DEFAULT_CONFIG)
        saved = {}
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                saved = json.load(f)
            for k, v in saved.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict) and k not in OLD_SCORING:
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        changed = migrate_search(cfg)
        changed = migrate_scoring(cfg, saved) or changed
        from jobwatch.services.presets import ensure_active   # late: presets import this module
        changed = ensure_active(cfg) or changed
        if not cfg["ntfy"].get("topic"):
            cfg["ntfy"]["topic"] = "jobwatch-" + secrets.token_hex(6)
            changed = True
        if changed:
            self.save(cfg)
        return cfg

    def save(self, cfg):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


def migrate_search(cfg):
    """The old per-source lists (linkedin_searches, remotive_queries) become the shared search_terms.
    The LinkedIn lines keep their location; the work-mode part is dropped (LinkedIn ignores it)."""
    if "linkedin_searches" not in cfg and "remotive_queries" not in cfg:
        return False
    old_li, old_q = cfg.pop("linkedin_searches", None) or [], cfg.pop("remotive_queries", None) or []
    if not cfg.get("search_terms"):
        terms, seen = [], set()
        for line in old_li:
            parts = [x.strip() for x in line.split("|")]
            if parts[0]:
                terms.append(parts[0] + (" | " + parts[1] if len(parts) > 1 and parts[1] else ""))
                seen.add(parts[0].lower())
        for q in old_q:
            if q.strip() and q.strip().lower() not in seen:
                terms.append(q.strip())
                seen.add(q.strip().lower())
        cfg["search_terms"] = terms
    return True


OLD_SCORING = ("title_keywords", "body_keywords", "negative_keywords", "location_keywords")


def migrate_scoring(cfg, saved):
    """The old "term = points" lists become ordered chips: sorted by their points, so the order keeps the old weights.
    Negative title terms: -5 or lower -> "not for me", the milder ones -> "less relevant"."""
    if not any(k in saved for k in OLD_SCORING):
        return False
    if "scoring" not in saved:
        by_pts = lambda d, sign: [k for k, v in sorted(d.items(), key=lambda kv: -kv[1] * sign) if v * sign > 0]
        title = cfg.get("title_keywords") or {}
        negs = {k: v for k, v in title.items() if v < 0}
        negs.update(cfg.get("negative_keywords") or {})
        loc = cfg.get("location_keywords") or {}
        cfg["scoring"] = {"interests": by_pts(title, 1), "desc": by_pts(cfg.get("body_keywords") or {}, 1),
                          "never": [k for k in by_pts(negs, -1) if negs[k] <= -5],
                          "less": [k for k in by_pts(negs, -1) if negs[k] > -5],
                          "loc_good": by_pts(loc, 1), "loc_bad": by_pts(loc, -1)}
    for k in OLD_SCORING:
        cfg.pop(k, None)
    return True


config_store = ConfigStore(CFG_PATH)
