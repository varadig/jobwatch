#!/usr/bin/env python3
"""jobwatch - watches job postings, scores them against the CV,
sends ntfy push / e-mail notifications, and serves a web interface.
Only the Python standard library. Run: python3 app.py  (port: JOBWATCH_PORT, default 8090)
"""
import copy, hashlib, html, json, shutil, logging, os, re, secrets, smtplib, sqlite3, ssl, threading, time
import urllib.error, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
import sys
if BASE not in sys.path:
    sys.path.insert(0, BASE)  # cvgen modul (python -I mellett is)
DATA = os.environ.get("JOBWATCH_DATA", os.path.join(BASE, "data"))
PORT = int(os.environ.get("JOBWATCH_PORT", "8090"))
CFG_PATH = os.path.join(DATA, "config.json")
PROFILE_PATH = os.path.join(DATA, "profile.json")
CV_DIR = os.path.join(DATA, "cv")
LOGO_DIR = os.path.join(DATA, "logos")  # company logos, downloaded once and served from here
FONT_DIR = os.path.join(BASE, "fonts")
PHOTO_PATH = os.path.join(DATA, "photo.png")
# Generalt dokumentumok cegenkent; /mnt/media a Samba "Media" megosztas, igy a Macrol is elerheto.
# Folder for generated documents (CV / resume / cover letter). If the file system is a network share,
# set JOBWATCH_SMB_PREFIX (local path prefix) and JOBWATCH_SMB_BASE (smb:// address) to get a Finder link too.
DOCS_ROOT = os.environ.get("JOBWATCH_DOCS") or os.path.join(DATA, "docs")
SMB_PREFIX = os.environ.get("JOBWATCH_SMB_PREFIX", "")
SMB_BASE = os.environ.get("JOBWATCH_SMB_BASE", "")
DB_PATH = os.path.join(DATA, "jobs.db")
UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jobwatch")

SOURCES_INFO = {
    "linkedin": ("LinkedIn", "https://www.linkedin.com/jobs/"),
    "meetfrank": ("MeetFrank", "https://meetfrank.com/latest-remote-jobs-in-meetfrankcom"),
    "4dayweek": ("4dayweek.io", "https://4dayweek.io/"),
    "flexa": ("Flexa", "https://flexa.careers/"),
    "remotive": ("Remotive (extra)", "https://remotive.com/"),
    "studios": ("Company job boards", ""),
    "rss": ("Own RSS feeds", ""),
    "custom": ("Own sources", ""),
    "mail": ("E-mail alerts", ""),
    "manual": ("Added by hand", ""),
}
FETCHED_SOURCES = [k for k in SOURCES_INFO if k != "manual"]
SEARCH_FILTERABLE = ["meetfrank", "4dayweek", "studios", "rss", "custom"]  # they list everything; the others search  # "manual" is never fetched, only added from the Jobs page
UNSUPPORTED = [
    ("HiringCafe", "https://hiringcafe.com/", "Cloudflare blocks automated fetching (403)"),
    ("JobLeads", "https://www.jobleads.com/", "Cloudflare blocks automated fetching (403)"),
    ("Cord", "https://cord.com/", "JavaScript app, cannot be read without a browser"),
    ("Jack & Jill", "https://app.jackandjill.ai/", "Login required"),
    ("Company career pages", "", "No public API: check manually, or add the company's board to the Studios list"),
]

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
    "rss_feeds": [],
    "custom_sources": [],      # own sources from Settings, see CUSTOM_TYPES
    "studios": [],            # "rendszer | azonosito | nev", pl. "ashby | acme | Acme"
    # Scoring as ordered chips (see SCORING_KINDS): in the ordered lists the position is the score.
    "scoring": {"interests": [], "desc": [], "less": [], "never": ["intern", "internship", "junior", "trainee", "student"],
                "loc_good": ["remote", "europe"], "loc_bad": []},
}

CFG_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()
STATE = {"running": False, "last_run": None, "next_run": None, "progress": None, "enrich": None}
REQ = threading.local()  # current request path (active menu item)


# ---------------------------------------------------------------- config / db
def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    saved = {}
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict) and k not in OLD_SCORING:
                cfg[k].update(v)
            else:
                cfg[k] = v
    changed = migrate_search(cfg)
    changed = migrate_scoring(cfg, saved) or changed
    if not cfg["ntfy"].get("topic"):
        cfg["ntfy"]["topic"] = "jobwatch-" + secrets.token_hex(6)
        changed = True
    if changed:
        save_config(cfg)
    return cfg


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


# ---------------------------------------------------------------- presets
# A preset is a named "what am I looking for": the search and the scoring. Switching re-scores every posting.
PRESET_KEYS = ("search_terms", "search_locations", "search_filter", "scoring", "min_score")
TEMPLATE_DIR = os.path.join(BASE, "templates")


def template_presets():
    """{name: preset} from templates/*.json (the job-type templates, read-only)."""
    out = {}
    for fn in sorted(os.listdir(TEMPLATE_DIR)) if os.path.isdir(TEMPLATE_DIR) else []:
        if not fn.endswith(".json") or fn.startswith("profile"):
            continue
        try:
            with open(os.path.join(TEMPLATE_DIR, fn), encoding="utf-8") as f:
                t = json.load(f)
        except (OSError, ValueError):
            continue
        if "search_terms" not in t and "linkedin_searches" not in t:
            continue
        t = dict(t)
        migrate_search(t)
        migrate_scoring(t, t)
        out[fn[:-5]] = {k: copy.deepcopy(t[k]) for k in PRESET_KEYS if k in t}
    return out


def preset_of(cfg):
    return {k: copy.deepcopy(cfg.get(k)) for k in PRESET_KEYS if k in cfg}


def apply_preset(cfg, preset):
    for k in PRESET_KEYS:
        if k in preset:
            cfg[k] = copy.deepcopy(preset[k])


NEW_PRESET = "New preset"


def clean_preset_name(name):
    return re.sub(r"\s+", " ", name or "").strip()[:60]


def preset_by_key(cfg, key):
    """The preset behind a Presets-box value: one of yours, or "tpl:<template>"."""
    if (key or "").startswith("tpl:"):
        return template_presets().get(key[4:])
    return (cfg.get("presets") or {}).get(key)


def same_as_preset(cfg, preset):
    """True if the current search + scoring equal the preset (the keys the preset has)."""
    if not preset:
        return False

    def eq(a, b):  # a missing list / flag counts as an empty one
        if not a and not b:
            return True
        if isinstance(a, dict) or isinstance(b, dict):
            a, b = a or {}, b or {}
            return isinstance(a, dict) and isinstance(b, dict) and all(eq(a.get(k), b.get(k)) for k in set(a) | set(b))
        return a == b
    return all(eq(cfg.get(k), v) for k, v in preset.items() if k in PRESET_KEYS)


def free_preset_name(cfg, base=NEW_PRESET):
    have = set(cfg.get("presets") or {})
    if base not in have:
        return base
    return next("%s %d" % (base, i) for i in range(2, 1000) if "%s %d" % (base, i) not in have)


def preset_label(key):
    return "%s (template)" % key[4:] if (key or "").startswith("tpl:") else key


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
            with CFG_LOCK:
                old = load_config()
            changed = save_settings(f)
        with CFG_LOCK:
            cfg = load_config()
            if "ntfy_server" in f and old.get("active_preset") in (old.get("presets") or {}) and old["active_preset"] != name:
                cfg["presets"][old["active_preset"]] = old["presets"][old["active_preset"]]  # the edits go to the new one only
            cfg.setdefault("presets", {})[name] = preset_of(cfg)
            cfg["active_preset"] = name
            save_config(cfg)
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
            with CFG_LOCK:
                preset, name = (load_config().get("presets") or {}).get(pick), pick
        if not preset:
            return "Pick a preset first."
        with CFG_LOCK:
            cfg = load_config()
            before = search_signature(cfg)
            apply_preset(cfg, preset)
            cfg["active_preset"] = pick
            save_config(cfg)
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
        with CFG_LOCK:
            cfg = load_config()
            old = cfg.get("active_preset") or ""
            if old not in (cfg.get("presets") or {}):
                return "Only your own presets can be renamed (pick or save one first)."
            if not new or new.startswith("tpl:"):
                return "Type the new name in the name field."
            if new != old and new in cfg["presets"]:
                return "There is already a preset called “%s”." % new
            cfg["presets"] = {(new if k == old else k): v for k, v in cfg["presets"].items()}
            cfg["active_preset"] = new
            save_config(cfg)
        return "Preset “%s” renamed to “%s”." % (old, new)
    if act == "delete":
        with CFG_LOCK:
            cfg = load_config()
            if pick not in (cfg.get("presets") or {}):
                return "Pick one of your presets to delete (templates cannot be deleted)."
            del cfg["presets"][pick]
            if cfg.get("active_preset") == pick:
                cfg["active_preset"] = ""
            save_config(cfg)
        return "Preset “%s” deleted; the current settings did not change." % pick
    return ""


def search_entries(cfg):
    """[(keywords, location or "")] from the search terms."""
    out = []
    for line in cfg.get("search_terms") or []:
        parts = [x.strip() for x in line.split("|")]
        if parts[0]:
            out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


def search_keywords(cfg):
    """The distinct keywords (case-insensitive), in order."""
    out, seen = [], set()
    for kw, _ in search_entries(cfg):
        if kw.lower() not in seen:
            seen.add(kw.lower())
            out.append(kw)
    return out


def linkedin_plan(cfg):
    """[(keywords, location)]: a term with its own location is searched there, the others in each default location."""
    locs = [l.strip() for l in cfg.get("search_locations") or [] if l.strip()] or ["European Union"]
    out, seen = [], set()
    for kw, loc in search_entries(cfg):
        for l in ([loc] if loc else locs):
            if (kw.lower(), l.lower()) not in seen:
                seen.add((kw.lower(), l.lower()))
                out.append((kw, l))
    return out


def matches_search(keywords, it):
    """True if the title or description contains every word of at least one search term."""
    text = " ".join((it.get("title") or "", it.get("description") or "")).lower()
    return any(all(term_re(w).search(text) for w in kw.lower().split()) for kw in keywords)


def save_config(cfg):
    os.makedirs(DATA, exist_ok=True)
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CFG_PATH)


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(DATA, exist_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT, location TEXT, url TEXT,
          posted TEXT, description TEXT, score INTEGER, matched TEXT, dupkey TEXT,
          first_seen TEXT, last_seen TEXT, status TEXT DEFAULT 'new', notified INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS jobs_dup ON jobs(dupkey);
        CREATE TABLE IF NOT EXISTS cvs(
          job_id TEXT PRIMARY KEY, status TEXT, error TEXT, data TEXT, description TEXT,
          created TEXT, sent_to TEXT, sent_at TEXT);
        CREATE TABLE IF NOT EXISTS outreach(
          id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, name TEXT, title TEXT, contact TEXT,
          channel TEXT, language TEXT, job_id TEXT, note TEXT, subject TEXT, message TEXT,
          status TEXT, error TEXT, sent_at TEXT, model TEXT, via TEXT, kind TEXT,
          profile TEXT, role_ref TEXT, role_text TEXT);
        CREATE TABLE IF NOT EXISTS mail_seen(
          msgid TEXT PRIMARY KEY, processed_at TEXT, found INTEGER);
        CREATE TABLE IF NOT EXISTS company_logos(
          ckey TEXT PRIMARY KEY, url TEXT, file TEXT, tried TEXT);
        CREATE TABLE IF NOT EXISTS source_state(
          source TEXT PRIMARY KEY, last_run TEXT, last_ok TEXT, found INTEGER, new INTEGER,
          error TEXT, seeded INTEGER DEFAULT 0);
        """)


def migrate_db():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        v = con.execute("SELECT v FROM meta WHERE k='mode_rules'").fetchone()
        if not v or v["v"] != "2":
            # new rules: the LinkedIn filter is unreliable -> re-classify from the text
            if "work_mode" in [r["name"] for r in con.execute("PRAGMA table_info(jobs)")]:
                for r in con.execute("SELECT id, source, title, location, description FROM jobs WHERE source IN ('linkedin','4dayweek','flexa','rss')").fetchall():
                    con.execute("UPDATE jobs SET work_mode=? WHERE id=?",
                                (classify_mode(r["title"], r["location"], r["description"]), r["id"]))
            con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('mode_rules','2')")
        ccols = [r["name"] for r in con.execute("PRAGMA table_info(cvs)")]
        if ccols and "doc_dir" not in ccols:
            con.execute("ALTER TABLE cvs ADD COLUMN doc_dir TEXT")
        ocols = [r["name"] for r in con.execute("PRAGMA table_info(outreach)")]
        for col in ("kind", "profile", "role_ref", "role_text"):
            if ocols and col not in ocols:
                con.execute("ALTER TABLE outreach ADD COLUMN %s TEXT" % col)
        cols = [r["name"] for r in con.execute("PRAGMA table_info(jobs)")]
        if "work_mode" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN work_mode TEXT DEFAULT 'unknown'")
            for r in con.execute("SELECT id, source, title, location, description FROM jobs").fetchall():
                m = "remote" if r["source"] == "remotive" else classify_mode(r["title"], r["location"], r["description"])
                con.execute("UPDATE jobs SET work_mode=? WHERE id=?", (m, r["id"]))
        # mode_src: where the work mode comes from (source / page / text / claude / manual); mode_note: the evidence
        for col in ("urlkey", "also_sources", "dup_of", "mode_src", "mode_note", "mode_checked"):
            if col not in cols:
                con.execute("ALTER TABLE jobs ADD COLUMN %s TEXT" % col)
        con.execute("CREATE INDEX IF NOT EXISTS jobs_url ON jobs(urlkey)")
        v = con.execute("SELECT v FROM meta WHERE k='dedupe'").fetchone()
        if not v or v["v"] != "1":
            # new duplicate rules: recompute the keys, then hide the duplicates already in the list
            for r in con.execute("SELECT id, title, company, url FROM jobs").fetchall():
                con.execute("UPDATE jobs SET dupkey=?, urlkey=? WHERE id=?",
                            (dup_key(r["title"], r["company"], r["url"]), url_key(r["url"]), r["id"]))
            n = dedupe_existing(con)
            if n:
                log.info("Duplicates: %d posting(s) hidden", n)
            con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('dedupe','1')")


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- scoring
_re_cache = {}


def term_re(term):
    t = term.lower().strip()
    if t not in _re_cache:
        _re_cache[t] = re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9#+])")
    return _re_cache[t]


# Scoring: what interests you, as chips. In the ordered lists the position is the score (first = most points);
# the other lists give the same points to every chip. "unity / unity3d": words that count as one.
SCORING_KINDS = {  # kind: (points by position, points after the table / for every chip)
    "interests": ((6, 5, 4, 4, 3, 3, 2, 2, 2), 1),   # in the job title
    "desc": ((3, 2), 1),                              # in the description, at most DESC_MAX together
    "less": ((), -3),                                 # in the title: less relevant
    "never": ((), -8),                                # in the title: hides the posting
    "loc_good": ((3, 2), 1),                          # in the location, only for a fitting title
    "loc_bad": ((), -5),                              # in the location
}
DESC_MAX = 4


def chip_points(kind, rank):
    table, rest = SCORING_KINDS[kind]
    return table[rank] if rank < len(table) else rest


def chip_alts(chip):
    return [a.strip().lower() for a in re.split(r"\s+/\s+", chip or "") if a.strip()]


def scoring_rules(cfg):
    """{kind: [(alternatives, points)]} from the chips, or from the old term = points lists (templates, old configs)."""
    sc = cfg.get("scoring")
    if sc is not None:
        return {k: [(chip_alts(c), chip_points(k, i)) for i, c in enumerate(sc.get(k) or []) if chip_alts(c)]
                for k in SCORING_KINDS}
    d = lambda k: [([t.lower()], int(w)) for t, w in (cfg.get(k) or {}).items()]
    title, loc = d("title_keywords"), d("location_keywords")
    return {"interests": [x for x in title if x[1] > 0], "desc": d("body_keywords"),
            "less": [x for x in title if x[1] < 0] + d("negative_keywords"), "never": [],
            "loc_good": [x for x in loc if x[1] > 0], "loc_bad": [x for x in loc if x[1] < 0]}


def _hit(alts, text):
    for a in alts:
        if term_re(a).search(text):
            return a
    return None


def score_job(cfg, j):
    rules = scoring_rules(cfg)
    title = (j.get("title") or "").lower()
    body = " ".join([(j.get("description") or ""), (j.get("company") or "")]).lower()
    score, hits, seen = 0, [], set()
    for alts, w in rules["interests"]:
        a = _hit(alts, title)
        if a:
            score += w
            hits.append(a)
            seen.update(alts)
    title_pts = score
    body_pts = 0
    for alts, w in rules["desc"]:
        a = None if seen.intersection(alts) else _hit(alts, body)
        if a:
            body_pts += w
            hits.append(a)
    score += min(body_pts, DESC_MAX)  # the description can give at most DESC_MAX points
    for alts, w in rules["less"] + rules["never"]:
        a = _hit(alts, title)
        if a:
            score += w
            hits.append("%s(%d)" % (a, w))
    loc = (j.get("location") or "").lower()
    for alts, w in rules["loc_good"] + rules["loc_bad"]:
        a = _hit(alts, loc)
        if a:
            if w > 0 and title_pts < 4:
                continue  # location bonus only for a relevant position
            score += w
            hits.append("📍%s(%+d)" % (a, w))
    if re.search(r",\s*[A-Z]{2}$", j.get("location") or ""):  # US allam (pl. "Louisville, CO")
        score -= 4
        hits.append("📍USA(-4)")
    score += j.get("bonus", 0)
    return score, hits


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# ---------------------------------------------------------------- duplicates across sources
# The same posting often comes from several places (a LinkedIn search and a LinkedIn alert e-mail, a company
# board and Flexa…). Two postings are the same when their link points to the same posting, or when the title
# and the company match after the noise is removed (legal forms, gender tags).
LEGAL_FORMS = re.compile(r"\b(gmbh|ag|ltd|limited|inc|llc|plc|corp|corporation|co|sas|sa|sarl|srl|bv|nv|ab|oy|as|aps|"
                         r"kft|zrt|nyrt|bt|spa|sl|pty|pte|kk|se)\b\.?", re.I)
GENDER_TAGS = re.compile(r"\(\s*(?:[mwfdx]\s*/\s*){1,3}[mwfdx]\s*\)|\(\s*(?:all genders?|alle geschlechter|gn\*?|w\*m\*d)\s*\)|"
                         r"\b[mwf]\s*/\s*[mwf]\s*/\s*[dx]\b", re.I)


def norm_company(c):
    return norm(LEGAL_FORMS.sub(" ", c or ""))


def url_key(url):
    """One key per posting link: the LinkedIn posting id, else the address without query and fragment."""
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})", url or "")
    if m:
        return "li:" + m.group(1)
    return re.sub(r"[?#].*$", "", (url or "").strip().lower()).rstrip("/")


def dup_key(title, company, url):
    return norm(GENDER_TAGS.sub(" ", title or "")) + "|" + (norm_company(company) or url_key(url))


def find_duplicate(con, title, company, url):
    """The posting already in the list that is the same as this one (or None)."""
    return con.execute("SELECT id, source, company, description, also_sources FROM jobs WHERE (dupkey=? OR urlkey=?) "
                       "AND dup_of IS NULL LIMIT 1", (dup_key(title, company, url), url_key(url))).fetchone()


def merge_into(con, row, source, it):
    """Notes that the posting also came from another source, and fills in what the first one lacked."""
    also = [s for s in (row["also_sources"] or "").split(",") if s]
    if source != row["source"] and source not in also:
        also.append(source)
    con.execute("UPDATE jobs SET also_sources=?, company=CASE WHEN COALESCE(company,'')='' THEN ? ELSE company END, "
                "description=CASE WHEN COALESCE(description,'')='' THEN ? ELSE description END WHERE id=?",
                (",".join(also), it.get("company") or "", it.get("description") or "", row["id"]))
    # an exact work mode from the other source (e.g. "(Hybrid)" in a LinkedIn alert e-mail) settles an unknown one
    if it.get("work_mode") in ("remote", "hybrid", "onsite"):
        con.execute("UPDATE jobs SET work_mode=?, mode_src='source', mode_note=? WHERE id=? AND "
                    "COALESCE(work_mode,'unknown')='unknown' AND COALESCE(mode_src,'')!='manual'",
                    (it["work_mode"], "from " + SOURCES_INFO.get(source, (source,))[0], row["id"]))


def dedupe_existing(con):
    """Hides the duplicates already in the list (dup_of = the one kept; nothing is deleted). The one kept is the
    one you worked on most: a CV, then applied > saved > seen > new, then the oldest."""
    rank = {"applied": 4, "saved": 3, "seen": 2, "new": 1}
    with_cv = {r["job_id"] for r in con.execute("SELECT job_id FROM cvs")}
    rows = [dict(r) for r in con.execute("SELECT id, source, status, first_seen, dupkey, urlkey, also_sources FROM jobs WHERE dup_of IS NULL")]
    groups = {}
    for r in rows:
        for k in ("d:" + (r["dupkey"] or ""), "u:" + (r["urlkey"] or "")):
            if len(k) > 2:
                groups.setdefault(k, []).append(r)
    hidden = 0
    for members in groups.values():
        members = [m for m in members if not m.get("_gone")]
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (m["id"] not in with_cv, -rank.get(m["status"], 0), m["first_seen"] or ""))
        keep = members[0]
        also = [s for s in (keep["also_sources"] or "").split(",") if s]
        for m in members[1:]:
            m["_gone"] = True
            con.execute("UPDATE jobs SET dup_of=? WHERE id=?", (keep["id"], m["id"]))
            hidden += 1
            if m["source"] != keep["source"] and m["source"] not in also:
                also.append(m["source"])
        keep["also_sources"] = ",".join(also)
        con.execute("UPDATE jobs SET also_sources=? WHERE id=?", (keep["also_sources"], keep["id"]))
    return hidden


WORK_MODES = {"remote": "🏠 Remote", "hybrid": "🏢🏠 Hybrid", "onsite": "🏢 On-site", "unknown": "❔ Not stated"}
WT_LINKEDIN = {"2": "remote", "3": "hybrid", "1": "onsite"}


# Work-mode wording in the languages the postings come in (en, hu, de, fr, es, it, pl, sv, nl).
RE_HYB = re.compile(
    r"\bhybrid\w*|\bhibrid\w*|\bhybride\b|\bhíbrid[oa]\b|\bibrid[oa]\b|\bhybrydow\w*|teilweise remote|"
    r"\b\d\s*(?:-\s*\d\s*)?days?\s*(?:a|per|each)\s*week\s*(?:in|at|from)\s*(?:the|our)?\s*(?:office|studio)|"
    r"(?:in|at|from)\s*(?:the|our)\s*(?:office|studio)\s*\d\s*(?:-\s*\d\s*)?days|"
    r"\d\s*(?:-\s*\d\s*)?(?:jours?|days?)\s*(?:de\s*)?(?:télétravail|remote|home ?office)|télétravail\s*(?:partiel|possible|\d)|"
    r"possibilité de télétravail|\d\s*tage?\s*(?:pro woche\s*)?(?:im büro|vor ort|homeoffice|home office)|homeoffice[- ]möglichkeit|"
    r"home office lehetőség|heti\s*\d\s*nap|teletrabajo parcial|smart working|flexible (?:office|home) (?:days|working)", re.I)
RE_ONS = re.compile(
    r"\bon-?site\b|\bonsite\b|\bin-office\b|office-based|studio-based|fully in[- ]office|"
    r"\birodai\b|irodában|helyszíni munka|vor ort|präsenz\w*|\bsur site\b|\ben présentiel\b|présentiel|"
    r"\bpresencial\b|in sede|stacjonarn\w*|w biurze|på plats|på kontoret|op kantoor|"
    r"work(?:ing)? from (?:our|the) (?:office|studio)|relocat(?:e|ion) to|"
    r"\b(?:no|not a|not)[- ]remote\b|remote (?:work )?(?:is )?not (?:possible|available|an option|offered)|"
    r"this (?:role|position) is not remote", re.I)
RE_REM_STRONG = re.compile(
    r"fully remote|full[- ]remote|100\s*%\s*remote|remote[- ]first|\bfully distributed|work from anywhere|"
    r"remote work is possible|remote (?:position|role|job|opportunity)|work(?:ing)? remotely|"
    r"távmunka|teljesen remote|100\s*%\s*(?:télétravail|homeoffice|home office)|full télétravail|télétravail complet|"
    r"vollständig remote|komplett remote|praca zdalna|w pełni zdaln\w*|totalmente remoto|en remoto|100\s*%\s*remoto|"
    r"helt på distans|distansarbete", re.I)
RE_REM_TL = re.compile(r"\bremote\b|anywhere|home office|\bwfh\b|télétravail|zdaln\w*|remoto", re.I)
RE_NOT_REMOTE = re.compile(r"\b(?:no|not a|not)[- ]remote\b|remote (?:work )?(?:is )?not (?:possible|available|an option|offered)|"
                           r"this (?:role|position) is not remote", re.I)


def _evidence(text, m):
    a, b = max(0, m.start() - 50), min(len(text), m.end() + 50)
    return ("…" if a else "") + re.sub(r"\s+", " ", text[a:b]).strip() + ("…" if b < len(text) else "")


def classify_mode_ex(title="", location="", description=""):
    """(work mode, evidence quote). Title/location are strong signals; in the description only unambiguous
    phrases count; "no remote" style negations mean on-site."""
    tl = " ".join(x or "" for x in (title, location))
    for rx, mode in ((RE_HYB, "hybrid"), (RE_NOT_REMOTE, "onsite"), (re.compile(r"\bon-?site\b|in-office|\bonsite\b", re.I), "onsite"),
                     (RE_REM_TL, "remote")):
        m = rx.search(tl)
        if m:
            return mode, _evidence(tl, m)
    d = description or ""
    neg = RE_NOT_REMOTE.search(d)
    if neg:
        return "onsite", _evidence(d, neg)
    rem, hyb, ons = RE_REM_STRONG.search(d), RE_HYB.search(d), RE_ONS.search(d)
    if rem and not hyb:
        return "remote", _evidence(d, rem)
    if hyb:
        return "hybrid", _evidence(d, hyb)
    if ons:
        return "onsite", _evidence(d, ons)
    return "unknown", ""


def classify_mode(title="", location="", description=""):
    return classify_mode_ex(title, location, description)[0]


def norm_mode(v):
    v = (v or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    return {"remote": "remote", "fully": "remote", "hybrid": "hybrid", "partially": "hybrid",
            "onsite": "onsite", "office": "onsite", "inoffice": "onsite"}.get(v)


# ---------------------------------------------------------------- fetching
def fetch(url, data=None, headers=None, timeout=30):
    h = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def strip_tags(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def src_linkedin(cfg):
    out = []
    for kw, loc in linkedin_plan(cfg):
        done_short = set()
        wts = [""]  # the guest API ignores the work-type filter (f_WT); the mode is taken from the text
        for wt, start in [(w, st) for w in wts for st in (0, 25)]:
            if start and wt in done_short:
                continue
            q = {"keywords": kw, "location": loc, "f_TPR": "r604800", "start": str(start)}
            if wt:
                q["f_WT"] = wt  # only narrows the search; the LinkedIn guest filter is unreliable
            url = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?" + urllib.parse.urlencode(q)
            page = None
            for wait in (0, 30, 90):
                time.sleep(wait)
                try:
                    page = fetch(url)
                    break
                except urllib.error.HTTPError as ex:
                    if ex.code != 429:
                        raise
            if page is None:
                if out:
                    log.warning("LinkedIn 429, continuing with partial results (%d)", len(out))
                    return out
                raise RuntimeError("LinkedIn 429 Too Many Requests (will retry later)")
            cards = page.split("<li")
            got = 0
            for c in cards:
                m = re.search(r"urn:li:jobPosting:(\d+)", c)
                if not m:
                    continue
                jid = m.group(1)
                t = re.search(r'base-search-card__title">(.*?)</h3>', c, re.S)
                co = re.search(r'base-search-card__subtitle">(.*?)</h4>', c, re.S)
                lo = re.search(r'job-search-card__location">(.*?)</span>', c, re.S)
                dt = re.search(r'datetime="([^"]+)"', c)
                out.append({
                    "ext_id": jid, "title": strip_tags(t.group(1)) if t else "",
                    "company": strip_tags(co.group(1)) if co else "",
                    "location": strip_tags(lo.group(1)) if lo else loc,
                    "url": "https://www.linkedin.com/jobs/view/%s/" % jid,
                    "posted": dt.group(1) if dt else "", "description": "", "bonus": 1,
                    "logo": (lambda m: html.unescape(m.group(1)) if m else "")(
                        re.search(r'data-delayed-url="(https://media\.licdn\.com/[^"]*company-logo[^"]*)"', c)),
                })
                got += 1
            time.sleep(8)
            if got < 20:
                done_short.add(wt)
    return out


def src_meetfrank(cfg):
    out, errs = [], []
    for page_url in ("https://meetfrank.com/latest-remote-jobs-in-meetfrankcom",
                     "https://meetfrank.com/fully-remote-jobs-in-meetfrankcom"):
        try:
            s = fetch(page_url)
        except Exception as ex:
            errs.append("%s: %s" % (page_url.rsplit("/", 1)[-1], ex))
            continue
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', s, re.S)
        if not m:
            errs.append("no __NEXT_DATA__ (has the page changed?)")
            continue
        cache = json.loads(m.group(1))["props"]["pageProps"]["apollo"]["cache"]
        for key, v in cache.items():
            if "Opening" not in v.get("__typename", "") or not v.get("title") or not v.get("urlHandle"):
                continue
            comp = cache.get((v.get("company") or {}).get("__ref", ""), {})
            ch = comp.get("urlHandle", "")
            locs = []
            for ref in v.get("positionLocations") or []:
                L = cache.get(ref.get("__ref", ""), {})
                locs.append(L.get("name") or L.get("fullName") or L.get("city") or "")
            remote = (v.get("remote") or {}).get("type", "")
            snippets = " ".join((sn.get("title", "") + " " + sn.get("text", "")) for sn in v.get("snippets") or [])
            out.append({
                "ext_id": v.get("id") or key, "title": v["title"],
                "company": comp.get("name") or ch,
                "location": ", ".join([x for x in locs if x] + (["Remote"] if remote == "FULLY" else [])),
                "url": "https://meetfrank.com/jobs/%s/%s" % (ch, v["urlHandle"]),
                "posted": v.get("publishedAt", ""), "description": strip_tags(snippets)[:8000],
                "work_mode": "remote" if remote == "FULLY" else ("hybrid" if remote else None),
            })
        time.sleep(2)
    if not out and errs:
        raise RuntimeError("; ".join(errs))
    return out


def parse_rss(xml_text, default_company=""):
    out = []
    root = ET.fromstring(xml_text)
    items = root.iter("item")
    for it in items:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        company = (it.findtext("author") or default_company).strip()
        if " at " in title and not company:
            title, company = title.rsplit(" at ", 1)
        elif company and title.endswith(" at " + company):
            title = title[: -len(" at " + company)]
        pd = it.findtext("pubDate") or ""
        try:
            pd = parsedate_to_datetime(pd).isoformat()
        except Exception:
            pass
        out.append({"ext_id": it.findtext("guid") or link, "title": title, "company": company,
                    "location": "", "url": link, "posted": pd,
                    "description": strip_tags(it.findtext("description") or "")[:8000]})
    return out


def src_4dayweek(cfg):
    return parse_rss(fetch("https://4dayweek.io/feed"))


def src_flexa(cfg):
    out, seen = [], set()
    for q in search_keywords(cfg):
        s = fetch("https://flexa.careers/jobs?" + urllib.parse.urlencode({"search": q}))
        for m in re.finditer(r'<a[^>]+href="/jobs/([a-z0-9-]+)"[^>]*>(.*?)</a>', s, re.S):
            slug, title = m.group(1), strip_tags(m.group(2))
            if slug in seen or not title:
                continue
            seen.add(slug)
            idm = re.search(r"-([0-9a-f]{24})$", slug)
            base = slug[: idm.start()] if idm else slug
            tslug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            company = base[: -len(tslug)].strip("-").replace("-", " ").title() if tslug and base.endswith(tslug) else ""
            out.append({"ext_id": slug, "title": title, "company": company, "location": "",
                        "url": "https://flexa.careers/jobs/" + slug, "posted": "", "description": ""})
        time.sleep(2)
    return out


def src_remotive(cfg):
    out = []
    for q in search_keywords(cfg):
        data = json.loads(fetch("https://remotive.com/api/remote-jobs?" + urllib.parse.urlencode({"search": q})))
        for j in data.get("jobs", []):
            out.append({"ext_id": str(j.get("id")), "title": j.get("title", ""),
                        "company": j.get("company_name", ""),
                        "location": j.get("candidate_required_location", ""), "url": j.get("url", ""),
                        "posted": j.get("publication_date", ""),
                        "description": strip_tags(j.get("description", ""))[:8000], "work_mode": "remote",
                        "logo": j.get("company_logo") or ""})
    return out


def src_studios(cfg):
    out, errs = [], []
    for line in cfg.get("studios") or []:
        parts = [x.strip() for x in line.split("|")]
        if len(parts) < 2:
            continue
        ats, slug = parts[0].lower(), parts[1]
        name = parts[2] if len(parts) > 2 else slug
        try:
            if ats == "ashby":
                d = json.loads(fetch("https://api.ashbyhq.com/posting-api/job-board/%s" % slug))
                for j in d.get("jobs", []):
                    if j.get("isListed") is False:
                        continue
                    loc = j.get("location") or ""
                    if j.get("isRemote") and "remote" not in loc.lower():
                        loc += " (Remote)"
                    out.append({"ext_id": j["id"], "title": j.get("title", ""), "company": name, "location": loc,
                                "work_mode": norm_mode(j.get("workplaceType")) or ("remote" if j.get("isRemote") else None),
                                "url": j.get("jobUrl", ""), "posted": j.get("publishedAt", ""),
                                "description": (j.get("descriptionPlain") or strip_tags(j.get("descriptionHtml", "")))[:8000]})
            elif ats == "lever":
                for j in json.loads(fetch("https://api.lever.co/v0/postings/%s?mode=json" % slug)):
                    c = j.get("categories") or {}
                    ts = j.get("createdAt")
                    out.append({"ext_id": j["id"], "title": j.get("text", ""), "company": name,
                                "location": " ".join(x for x in (c.get("location"), j.get("workplaceType")) if x),
                                "work_mode": norm_mode(j.get("workplaceType")),
                                "url": j.get("hostedUrl", ""),
                                "posted": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else "",
                                "description": (j.get("descriptionPlain") or "")[:8000]})
            elif ats == "greenhouse":
                d = json.loads(fetch("https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % slug))
                for j in d.get("jobs", []):
                    out.append({"ext_id": str(j["id"]), "title": j.get("title", ""), "company": name,
                                "location": (j.get("location") or {}).get("name", ""), "url": j.get("absolute_url", ""),
                                "posted": j.get("updated_at", ""),
                                "description": strip_tags(html.unescape(j.get("content", "")))[:8000]})
            elif ats == "bamboohr":
                d = json.loads(fetch("https://%s.bamboohr.com/careers/list" % slug, headers={"Accept": "application/json"}))
                for j in d.get("result", []):
                    L = j.get("location") or {}
                    loc = ", ".join(x for x in (L.get("city"), L.get("state")) if x)
                    bm = {"0": "onsite", "1": "remote", "2": "hybrid"}.get(str(j.get("locationType")))
                    if j.get("isRemote"):
                        bm = "remote"
                    if bm == "remote":
                        loc = (loc + " Remote").strip()
                    out.append({"ext_id": str(j["id"]), "title": (j.get("jobOpeningName") or "").strip(), "company": name,
                                "location": loc, "work_mode": bm,
                                "url": "https://%s.bamboohr.com/careers/%s" % (slug, j["id"]),
                                "posted": "", "description": j.get("departmentLabel") or ""})
            elif ats == "smartrecruiters":
                d = json.loads(fetch("https://api.smartrecruiters.com/v1/companies/%s/postings" % slug))
                for j in d.get("content", []):
                    L = j.get("location") or {}
                    loc = ", ".join(x for x in (L.get("city"), L.get("country")) if x) + (" Remote" if L.get("remote") else "")
                    out.append({"ext_id": str(j["id"]), "title": j.get("name", ""), "company": name, "location": loc,
                                "work_mode": "remote" if L.get("remote") else ("hybrid" if L.get("hybrid") else None),
                                "url": "https://jobs.smartrecruiters.com/%s/%s" % (slug, j["id"]),
                                "posted": j.get("releasedDate", ""), "description": ""})
            else:
                errs.append("%s: unknown platform '%s'" % (name, ats))
        except Exception as ex:
            errs.append("%s: %s" % (name, ex))
        time.sleep(1)
    if errs:
        log.warning("Company board errors: %s", "; ".join(errs))
        if not out:
            raise RuntimeError("; ".join(errs))
    return out


def src_rss(cfg):
    out = []
    for u in cfg.get("rss_feeds") or []:
        u = u.strip()
        if u:
            out += parse_rss(fetch(u))
    return out


# ---------------------------------------------------------------- own sources (added in Settings)
# Each entry of cfg["custom_sources"]: {"id", "enabled", "type", "name", "url", "pattern",
# "login_url", "username", "password"}. A source with a username + password logs in first, through the
# site's own login form (the form that has a password field), and keeps the cookies for that run only.
CUSTOM_TYPES = {
    "vmp": "munka.hu VMP (search result URL)",
    "page": "Any web page (links matching a pattern)",
    "rss": "RSS / Atom feed",
}
VMP_BASE = "https://vmp.munka.hu"
CUSTOM_DETAIL_LIMIT = 8   # per source and run: new postings whose detail page is fetched
CUSTOM_DELAY = 3          # seconds between requests to the same site (government sites block fast clients)


class WebSession:
    """Cookie-keeping HTTP client, so a source can log in and then read pages as that user."""

    def __init__(self):
        import http.cookiejar
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, url, data=None, referer=None, timeout=30):
        """GET (or POST when data is given). Returns (final url, text)."""
        h = {"User-Agent": UA, "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8"}
        if referer:
            h["Referer"] = referer
        if isinstance(data, dict):
            data = urllib.parse.urlencode(data).encode()
        with self.opener.open(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
            return r.geturl(), r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


def html_attr(attrs, name):
    m = re.search(r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % name, attrs, re.I)
    return html.unescape(next(g for g in m.groups() if g is not None)) if m else None


def form_login(sess, login_url, username, password):
    """Logs in through the first form on login_url that has a password field. Hidden fields are sent back,
    the text field before the password field gets the username. Raises RuntimeError if it did not work."""
    url, pg = sess.get(login_url)
    for fm in re.finditer(r"<form\b([^>]*)>(.*?)</form>", pg, re.S | re.I):
        inputs = [m.group(1) for m in re.finditer(r"<input\b([^>]*)>", fm.group(2), re.I)]
        if not any((html_attr(a, "type") or "").lower() == "password" for a in inputs):
            continue
        fields, user_f, pass_f, submit = {}, None, None, None
        for a in inputs:
            name, typ = html_attr(a, "name"), (html_attr(a, "type") or "text").lower()
            if not name:
                continue
            if typ == "password":
                pass_f = pass_f or name
            elif typ in ("text", "email") and pass_f is None:
                user_f = name  # the last text field before the password field
            elif typ == "hidden":
                fields[name] = html_attr(a, "value") or ""
            elif typ == "submit" and submit is None:
                submit = (name, html_attr(a, "value") or "")
        if not user_f:
            raise RuntimeError("login form on %s has no username field" % login_url)
        fields[user_f], fields[pass_f] = username, password
        if submit:
            fields[submit[0]] = submit[1]
        action = urllib.parse.urljoin(url, html_attr(fm.group(1), "action") or url)
        _, resp = sess.get(action, data=fields, referer=url)
        if re.search(r"""name\s*=\s*["']?%s["'\s>]""" % re.escape(pass_f), resp, re.I):
            raise RuntimeError("login failed (wrong username or password?)")
        return
    raise RuntimeError("no login form (with a password field) on %s" % login_url)


def with_query(url, **params):
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    q.update({k: str(v) for k, v in params.items()})
    return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))


def custom_vmp(sess, src, is_new):
    """munka.hu Virtual Labour Market: the URL is a search result page (vmp.munka.hu/allas/talalatok?...).
    The employer is shown only to logged-in users; descriptions come from the detail pages of new postings."""
    out, seen = [], set()
    for n in range(1, 4):  # at most 3 result pages per run
        _, pg = sess.get(with_query(src["url"], oldal=n))
        added = 0
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", pg, re.S | re.I):
            m = re.search(r'href="(/allas/reszletek/(\d+))"', row)
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S | re.I)
            if not m or len(cells) < 3 or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            added += 1
            employer = strip_tags(cells[3]) if len(cells) > 3 else ""
            if "bejelentkez" in employer.lower():
                employer = ""
            out.append({"ext_id": m.group(2), "url": urllib.parse.urljoin(src["url"], m.group(1)), "posted": "",
                        "title": re.sub(r"^\d{3,4}\s*-\s*", "", strip_tags(cells[0])),  # drop the FEOR code
                        "company": employer, "location": strip_tags(cells[2]),
                        "description": "Végzettség: " + strip_tags(cells[1])})
        if not added:
            break
        time.sleep(CUSTOM_DELAY)
    fetched = 0
    for it in out:
        if fetched >= CUSTOM_DETAIL_LIMIT or not is_new(it["ext_id"]):
            continue
        fetched += 1
        time.sleep(CUSTOM_DELAY)
        try:
            _, pg = sess.get(it["url"])
            text = strip_tags(pg.replace("</td>", " </td>").replace("<br", " <br"))
            a, b = text.find("Munkakör (FEOR)"), text.find("TÁMOP")
            if a < 0:
                continue  # not a posting page (e.g. "Nincs ilyen állásajánlat"): keep the list data
            it["description"] = text[a: b if b > a else None][:8000]
            if not it["company"]:
                m = re.search(r"Foglalkoztató neve\s+(.{2,80}?)\s{2,}|Foglalkoztató neve\s+(\S.{1,80}?)\s+(?:Cím|Székhely)", text)
                if m:
                    it["company"] = (m.group(1) or m.group(2)).strip()
        except Exception as ex:
            log.warning("VMP detail %s: %s", it["ext_id"], ex)
    return out


def custom_page(sess, src, is_new):
    """Any page: every link whose address contains the pattern (or matches it as a regex) is a posting."""
    url, pg = sess.get(src["url"])
    pat = (src.get("pattern") or "").strip()
    try:
        rx = re.compile(pat, re.I) if pat else None
    except re.error:
        rx = re.compile(re.escape(pat), re.I)
    out, seen = [], set()
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", pg, re.S | re.I):
        href = html_attr(m.group(1), "href")
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urllib.parse.urljoin(url, href)
        if (rx and not rx.search(full)) or full in seen:
            continue
        title = strip_tags(m.group(2)) or (html_attr(m.group(1), "title") or "").strip()
        if len(title) < 3:
            continue
        seen.add(full)
        out.append({"ext_id": hashlib.sha1(full.encode()).hexdigest()[:16], "title": title[:200],
                    "company": "", "location": "", "url": full, "posted": "", "description": ""})
    return out


def custom_rss(sess, src, is_new):
    return parse_rss(sess.get(src["url"])[1])


CUSTOM_FUNCS = {"vmp": custom_vmp, "page": custom_page, "rss": custom_rss}


def custom_login_url(src):
    return (src.get("login_url") or "").strip() or (VMP_BASE + "/belepes" if src.get("type") == "vmp" else "")


def run_custom_source(src, is_new=lambda ext_id: True):
    """Fetches one own source (logging in first when it has a username and password)."""
    if src.get("type") not in CUSTOM_FUNCS:
        raise RuntimeError("unknown type '%s'" % src.get("type"))
    sess = WebSession()
    if src.get("username") and src.get("password"):
        if not custom_login_url(src):
            raise RuntimeError("username given but no login page URL")
        form_login(sess, custom_login_url(src), src["username"], src["password"])
    return CUSTOM_FUNCS[src["type"]](sess, src, is_new)


def src_custom(cfg):
    out, errs = [], []
    for src in cfg.get("custom_sources") or []:
        if not src.get("enabled", True) or not src.get("url"):
            continue
        prefix = src["id"] + ":"

        def is_new(ext_id, prefix=prefix):
            with db() as con:
                return not con.execute("SELECT 1 FROM jobs WHERE id=?", ("custom:" + prefix + ext_id,)).fetchone()
        try:
            for it in run_custom_source(src, is_new):
                it["ext_id"] = prefix + it["ext_id"]
                it["via"] = src.get("name") or src["type"]
                out.append(it)
        except Exception as ex:
            errs.append("%s: %s" % (src.get("name") or src["type"], ex))
    if errs:
        log.warning("Own source errors: %s", "; ".join(errs))
        if not out:
            raise RuntimeError("; ".join(errs))
    return out


# ---------------------------------------------------------------- job alert e-mails (IMAP, read-only)
MAIL_SKIP_TEXT = re.compile(r"unsubscribe|leiratkoz|preferences|settings|privacy|help|view (it )?in (your )?browser|"
                            r"see all|show all|manage|terms|sign in|log ?in|download the app|feedback|^here$|^click", re.I)
MAIL_JOB_URL = re.compile(r"/(jobs?|careers?|positions?|vacanc\w*|openings?|roles?|apply|opportunit\w*|allas\w*)(/|\?|$|-)", re.I)


def canonical_job_url(url):
    """LinkedIn alert links carry tracking parameters; one posting should be one address."""
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})", url)
    return "https://www.linkedin.com/jobs/view/%s/" % m.group(1) if m else url


def mail_body(msg):
    """(plain text, [(link text, url)]) of an e-mail; the HTML part is preferred for the links."""
    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    links, text = [], ""
    if html_part is not None:
        h = html_part.get_content()
        for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", h, re.S | re.I):
            href = html_attr(m.group(1), "href") or ""
            if href.startswith("http"):
                links.append((strip_tags(m.group(2)) or (html_attr(m.group(1), "title") or ""), href))
        text = strip_tags(h.replace("</tr>", "\n").replace("<br", "\n<br").replace("</p>", "\n"))
    if text_part is not None and not text:
        text = text_part.get_content()
    return text, links


def jobs_from_links(links):
    """Without Claude: links that look like postings, their text as the title."""
    out, seen = [], set()
    for t, u in links:
        t = re.sub(r"\s+", " ", t).strip()
        u = canonical_job_url(html.unescape(u))
        if not (6 <= len(t) <= 160) or MAIL_SKIP_TEXT.search(t) or u in seen:
            continue
        if not (MAIL_JOB_URL.search(urllib.parse.urlparse(u).path) or "linkedin.com/jobs/view/" in u):
            continue
        seen.add(u)
        out.append({"title": t, "company": "", "location": "", "url": u})
    return out


def mail_login(cfg):
    """(IMAP host, port, user, password): the alert settings, else the e-mail (SMTP) login."""
    ms, em = cfg.get("mail_source") or {}, cfg["email"]
    user = ms.get("username") or em.get("username")
    pw = ms.get("password") or em.get("password")
    if not (user and pw):
        raise RuntimeError("no mailbox login: set the e-mail account in Settings → Notifications, or one for e-mail alerts")
    return ms.get("imap_host") or "imap.gmail.com", int(ms.get("imap_port") or 993), user, pw


def read_alert_mails(cfg, only_new=True, limit=40):
    """The alert e-mails of the last days from the listed senders, newest first: [(msgid, sender, subject, msg)].
    The mailbox is opened read-only and messages are fetched with PEEK, so nothing is marked as read."""
    import email as email_lib
    import email.policy
    import imaplib
    ms = cfg.get("mail_source") or {}
    senders = [s.strip() for s in ms.get("senders") or [] if s.strip()]
    if not senders:
        raise RuntimeError("no senders listed for e-mail alerts")
    host, port, user, pw = mail_login(cfg)
    since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - 86400 * int(ms.get("days") or 7)))
    out = []
    with imaplib.IMAP4_SSL(host, port, timeout=30) as M:
        M.login(user, pw)
        typ, _ = M.select('"%s"' % (ms.get("folder") or "INBOX").replace('"', ""), readonly=True)
        if typ != "OK":
            raise RuntimeError("cannot open the mail folder %s" % ms.get("folder"))
        ids = set()
        for s in senders:
            typ, data = M.search(None, "SINCE", since, "FROM", '"%s"' % s.replace('"', ""))
            if typ == "OK" and data and data[0]:
                ids.update(data[0].split())
        with db() as con:
            done = {r["msgid"] for r in con.execute("SELECT msgid FROM mail_seen")} if only_new else set()
        for num in sorted(ids, key=int, reverse=True)[:limit * 3]:
            typ, data = M.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email_lib.message_from_bytes(data[0][1], policy=email.policy.default)
            msgid = (msg.get("Message-ID") or "").strip() or hashlib.sha1(data[0][1][:2000]).hexdigest()
            if msgid in done:
                continue
            out.append((msgid, str(msg.get("From", "")), str(msg.get("Subject", "")), msg))
            if len(out) >= limit:
                break
    return out


def jobs_from_mail(cfg, sender, subject, msg):
    """The postings in one alert e-mail: Claude reads it when that is turned on (and a key is set), else the links."""
    import cvgen
    text, links = mail_body(msg)
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if (cfg.get("mail_source") or {}).get("use_claude", True) and key:
        found = cvgen.jobs_from_email(key, subject, sender, text, links)
        for j in found:
            j["url"] = canonical_job_url(html.unescape(j.get("url") or ""))
        return found
    return jobs_from_links(links)


MAIL_PER_RUN = 15   # newest first; the rest waits for the next run, so one run stays short
MAIL_WORKERS = 4    # e-mails read by Claude at the same time


def src_mail(cfg):
    """The postings of the new alert e-mails. The e-mails are marked as done only after their postings are
    saved (run_all calls mark_mails_done), so a restart in the middle loses nothing."""
    from concurrent.futures import ThreadPoolExecutor
    mails = read_alert_mails(cfg, limit=MAIL_PER_RUN)
    prog = (STATE.get("progress") or {}).get("sources")
    done_n = [0]

    def one(m):
        msgid, sender, subject, msg = m
        try:
            return msgid, sender, msg, jobs_from_mail(cfg, sender, subject, msg), None
        except Exception as ex:
            return msgid, sender, msg, [], "%s: %s" % (subject[:60], ex)
        finally:
            done_n[0] += 1
            if prog is not None and "mail" in prog:
                prog["mail"] = "fetching %d / %d e-mails" % (done_n[0], len(mails))

    out, errs, done = [], [], []
    with ThreadPoolExecutor(max_workers=MAIL_WORKERS) as pool:
        for msgid, sender, msg, jobs, err in pool.map(one, mails):
            if err:
                errs.append(err)
                continue  # not marked as done: tried again next time
            via = re.sub(r".*@", "", re.sub(r"[<>\s]", "", sender.rsplit(" ", 1)[-1]))
            for j in jobs:
                url = j.get("url") or ""
                if not url.startswith("http"):
                    continue
                out.append({"ext_id": hashlib.sha1(url.encode()).hexdigest()[:16], "title": j["title"][:200],
                            "company": j.get("company", "")[:120], "location": j.get("location", "")[:120] or via,
                            "url": url, "posted": parsedate_iso(msg.get("Date")), "description": "",
                            "work_mode": j.get("work_mode") if j.get("work_mode") in ("remote", "hybrid", "onsite") else None})
            done.append((msgid, len(jobs)))
    cfg.setdefault("_mail_done", []).extend(done)
    if errs:
        log.warning("E-mail alert errors: %s", "; ".join(errs))
        if not out:
            raise RuntimeError("; ".join(errs))
    return out


# Settings that decide WHAT is fetched. When one changes, the postings found so far are cleared and fetched again.
SEARCH_SETTINGS = ("sources", "search_terms", "search_locations", "search_filter", "studios", "rss_feeds", "custom_sources")
KEPT_ON_NEW_SEARCH = "kept: saved, applied and hidden postings, the ones with a CV or a recruiter message, e-mail alerts and the ones added by hand"


def search_signature(cfg):
    return json.dumps({k: cfg.get(k) for k in SEARCH_SETTINGS}, sort_keys=True, ensure_ascii=False)


def clear_for_new_search():
    """Deletes the postings the old search found, except what you worked with, and forgets the source state
    (so the first run after it seeds without a flood of notifications). Returns how many were deleted."""
    with db() as con:
        n = con.execute(
            "DELETE FROM jobs WHERE source NOT IN ('mail','manual') AND COALESCE(status,'new') IN ('new','seen') "
            "AND id NOT IN (SELECT job_id FROM cvs WHERE job_id IS NOT NULL) "
            "AND id NOT IN (SELECT job_id FROM outreach WHERE job_id IS NOT NULL)").rowcount
        con.execute("UPDATE jobs SET dup_of=NULL WHERE dup_of IS NOT NULL AND dup_of NOT IN (SELECT id FROM jobs)")
        con.execute("DELETE FROM source_state WHERE source NOT IN ('mail')")
    return n


def search_again():
    """Clears the old results and fetches every source again, in the background (after a running refresh)."""
    n = clear_for_new_search()

    def go():
        while RUN_LOCK.locked():
            time.sleep(2)
        run_all(force=True)
    threading.Thread(target=go, daemon=True).start()
    return n


def reset_job_cache():
    """Deletes the found postings and the source status, and forgets which alert e-mails were read, so the
    next refresh reads those e-mails again (otherwise their postings would not come back)."""
    with db() as con:
        con.execute("DELETE FROM jobs")
        con.execute("DELETE FROM source_state")
        con.execute("DELETE FROM mail_seen")


def mark_mails_done(done):
    with db() as con:
        con.executemany("INSERT OR REPLACE INTO mail_seen(msgid, processed_at, found) VALUES(?,?,?)",
                        [(m, now_iso(), n) for m, n in done])


# ---------------------------------------------------------------- a posting added by its link
def job_from_page(url):
    """Title, company, location, description (and logo) of any job page: from its JobPosting structured data
    (schema.org, what Google for Jobs reads) when it has one, else from the page title and text."""
    m = LINKEDIN_JOB_RE.search(url)
    if m:
        j = linkedin_job(m.group(1))
        return dict(j, url="https://www.linkedin.com/jobs/view/%s/" % m.group(1), ext_id="linkedin-" + m.group(1), logo="", posted="")
    pg = fetch(url, timeout=25)
    job = {"url": url, "ext_id": hashlib.sha1(url.encode()).hexdigest()[:16], "title": "", "company": "", "location": "",
           "description": "", "logo": "", "posted": ""}
    for blk in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', pg, re.S | re.I):
        try:
            data = json.loads(blk.strip())
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for n in nodes:
            if not isinstance(n, dict) or "JobPosting" not in str(n.get("@type")):
                continue
            org = n.get("hiringOrganization") or {}
            org = org if isinstance(org, dict) else {"name": str(org)}
            locs = n.get("jobLocation") or []
            locs = locs if isinstance(locs, list) else [locs]
            place = []
            for L in locs:
                a = (L or {}).get("address") or {} if isinstance(L, dict) else {}
                a = a if isinstance(a, dict) else {}
                place.append(", ".join(x for x in (a.get("addressLocality"), a.get("addressCountry") if isinstance(a.get("addressCountry"), str)
                                                    else (a.get("addressCountry") or {}).get("name")) if x))
            remote = "Remote" if str(n.get("jobLocationType", "")).upper() == "TELECOMMUTE" else ""
            logo = org.get("logo")
            job.update(title=strip_tags(n.get("title") or ""), company=org.get("name") or "",
                       location=", ".join(x for x in place + [remote] if x),
                       description=strip_tags(html.unescape(n.get("description") or ""))[:8000],
                       logo=(logo.get("url") if isinstance(logo, dict) else logo) or "", posted=n.get("datePosted") or "",
                       work_mode="remote" if remote else None)
            return job
    meta = lambda prop: (lambda mm: html.unescape(mm.group(1)).strip() if mm else "")(
        re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % prop, pg, re.I))
    t = re.search(r"<title[^>]*>(.*?)</title>", pg, re.S | re.I)
    job.update(title=meta("og:title") or (strip_tags(t.group(1)) if t else ""), company=meta("og:site_name"),
               description=strip_tags(pg)[:8000])
    return job


def add_job_by_link(url):
    """Adds the posting behind url to the list (source "manual"). Returns (job id, message)."""
    url = (url or "").strip()
    if not re.match(r"^https?://", url):
        return None, "Paste the full link of the posting (https://…)."
    with db() as con:
        row = con.execute("SELECT id, dup_of FROM jobs WHERE urlkey=? LIMIT 1", (url_key(url),)).fetchone()
        if row:
            return row["dup_of"] or row["id"], "This posting is already in the list."
    try:
        it = job_from_page(url)
    except Exception as ex:
        return None, "Could not read the page: %s" % ex
    if not it["title"]:
        return None, "The page has no readable posting (it may need a login). Copy its text into the CV page instead."
    with CFG_LOCK:
        cfg = load_config()
    sc, hits = score_job(cfg, it)
    if it.get("work_mode") == "remote":   # the page's structured data says TELECOMMUTE
        wm, msrc, mnote = "remote", "source", "from the posting page (structured data)"
    else:
        wm, mnote = classify_mode_ex(it["title"], it["location"], it["description"])
        msrc = "text" if wm != "unknown" else None
    jid, ts = "manual:" + it["ext_id"], now_iso()
    with db() as con:
        remember_logo(con, it["company"], it.get("logo"))
        same = find_duplicate(con, it["title"], it["company"], it["url"])
        if same:
            merge_into(con, same, "manual", it)
            return same["id"], "This posting is already in the list (from %s)." % SOURCES_INFO.get(same["source"], (same["source"],))[0]
        con.execute("INSERT INTO jobs(id,source,title,company,location,url,posted,description,score,matched,dupkey,"
                    "first_seen,last_seen,status,notified,work_mode,urlkey,mode_src,mode_note) "
                    "VALUES(?,'manual',?,?,?,?,?,?,?,?,?,?,?,'new',1,?,?,?,?)",
                    (jid, it["title"][:200], it["company"][:120], it["location"][:200], it["url"], it["posted"], it["description"],
                     sc, ", ".join(hits), dup_key(it["title"], it["company"], it["url"]), ts, ts, wm, url_key(it["url"]), msrc, mnote))
    if wm == "unknown":
        threading.Thread(target=enrich_work_modes, args=(cfg,), daemon=True).start()
    threading.Thread(target=fetch_logos, daemon=True).start()
    return jid, "Added: %s (%d points)." % (it["title"][:80], sc)


def parsedate_iso(value):
    try:
        return parsedate_to_datetime(str(value)).isoformat()
    except Exception:
        return ""


def test_mail_source(cfg):
    """Settings → "test": reads the newest alert e-mail (even if already processed) and shows what it finds."""
    try:
        mails = read_alert_mails(cfg, only_new=False, limit=1)
    except Exception as ex:
        return "E-mail alerts: ERROR %s" % ex
    if not mails:
        return "E-mail alerts: mailbox OK, but no e-mail from the listed senders in the last %s days." % (
            (cfg.get("mail_source") or {}).get("days", 7))
    _, sender, subject, msg = mails[0]
    try:
        jobs = jobs_from_mail(cfg, sender, subject, msg)
    except Exception as ex:
        return "E-mail alerts: mailbox OK, newest e-mail “%s”, but reading it failed: %s" % (subject[:80], ex)
    sample = "; ".join("%s (%s)" % (j["title"], j.get("company") or "-") for j in jobs[:3])
    return "E-mail alerts: mailbox OK. Newest e-mail “%s”: %d posting(s)%s" % (subject[:80], len(jobs), (" – e.g. " + sample) if sample else "")


def linkedin_detail(jid):
    """Full posting text from the public jobPosting endpoint (None if it failed)."""
    page = fetch("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/%s" % jid)
    m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', page, re.S)
    if not m:
        return None
    return strip_tags(m.group(1).replace("<br>", "\n").replace("</li>", "\n").replace("</p>", "\n"))[:12000]


LINKEDIN_RETRY_WAITS = (30, 90, 180)  # on 429 (too many requests) wait these many seconds, then retry


def enrich_linkedin(con, items, limit=60):
    """For new / description-less LinkedIn postings, download the text and decide the work mode from it.
    On 429 (too many requests) it waits and retries, so the work mode is not left unknown because of the rate limit."""
    n = 0
    for it in items:
        row = con.execute("SELECT description FROM jobs WHERE id=?", (it.get("row_id") or "linkedin:" + it["ext_id"],)).fetchone()
        if row and row["description"]:
            continue
        if n >= limit:
            break
        d = None
        for wait in (0,) + LINKEDIN_RETRY_WAITS:
            if wait:
                time.sleep(wait)
            try:
                d = linkedin_detail(it["ext_id"])
                break
            except urllib.error.HTTPError as ex:
                if ex.code == 429:
                    log.warning("LinkedIn details: 429, waiting %d s", wait)
                    continue
                break
            except Exception:
                break
        else:
            log.warning("LinkedIn details: still 429, the rest on the next run")
            break
        n += 1
        if d:
            it["description"] = d
            it["work_mode"], it["mode_note"] = classify_mode_ex(it["title"], it["location"], d)
        time.sleep(6)
    return n


SOURCE_FUNCS = {"linkedin": src_linkedin, "meetfrank": src_meetfrank, "4dayweek": src_4dayweek,
                "flexa": src_flexa, "remotive": src_remotive, "studios": src_studios,
                "rss": src_rss, "custom": src_custom, "mail": lambda cfg: src_mail(cfg)}
MIN_INTERVAL = {"remotive": 360,  # perc; a Remotive API max ~4 hivast ker naponta
                "custom": 120}    # own sources (e.g. munka.hu): gentle, so the site does not block us


# ---------------------------------------------------------------- notify
def send_ntfy(cfg, title, message, click=None, priority=3):
    n = cfg["ntfy"]
    body = {"topic": n["topic"], "title": title[:200], "message": message[:3500],
            "tags": ["briefcase"], "priority": priority}
    if click:
        body["click"] = click
    req = urllib.request.Request(n["server"].rstrip("/") + "/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "jobwatch"})
    urllib.request.urlopen(req, timeout=20).read()


def send_email(cfg, subject, text, html_body=None, to=None, attachments=()):
    e = cfg["email"]
    if not (e.get("username") and e.get("password")):
        raise RuntimeError("E-mail (SMTP) is not configured in Settings.")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = e["username"]
    msg["To"] = to or e["to"] or e["username"]
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")
    for fname, data in attachments:
        msg.add_attachment(data, maintype="application", subtype="pdf", filename=fname)
    with smtplib.SMTP(e["smtp_host"], int(e["smtp_port"]), timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(e["username"], e["password"])
        s.send_message(msg)


def notify_jobs(cfg, jobs):
    if not jobs:
        return []
    errors = []
    jobs = sorted(jobs, key=lambda j: -j["score"])
    if cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic"):
        try:
            if len(jobs) <= 5:
                for j in jobs:
                    send_ntfy(cfg, "New job (%d pts): %s" % (j["score"], j["title"]),
                              "%s · %s\n%s" % (j["company"], j["location"] or "-", j["url"]),
                              click=j["url"], priority=4 if j["score"] >= cfg["notify_min_score"] + 3 else 3)
            else:
                lines = ["• %s – %s (%d)" % (j["title"], j["company"], j["score"]) for j in jobs[:15]]
                send_ntfy(cfg, "%d new relevant job(s)" % len(jobs), "\n".join(lines),
                          click=cfg.get("public_url"), priority=4)
        except Exception as ex:
            errors.append("ntfy: %s" % ex)
    if cfg["email"].get("enabled") and cfg["email"].get("username") and cfg["email"].get("password"):
        try:
            rows = "".join(
                '<li><b>%d</b> · <a href="%s">%s</a> – %s <small>(%s, %s)</small></li>' % (
                    j["score"], html.escape(j["url"]), html.escape(j["title"]), html.escape(j["company"]),
                    html.escape(j["location"] or "-"), html.escape(j["source"])) for j in jobs)
            text = "\n".join("[%d] %s – %s\n%s" % (j["score"], j["title"], j["company"], j["url"]) for j in jobs)
            send_email(cfg, "jobwatch: %d new relevant job(s)" % len(jobs), text,
                       '<p>New postings that match your CV:</p><ul>%s</ul><p><a href="%s">Open jobwatch</a></p>'
                       % (rows, html.escape(cfg.get("public_url", ""))))
        except Exception as ex:
            errors.append("e-mail: %s" % ex)
    for e in errors:
        log.error("Notification error: %s", e)
    return errors


# ---------------------------------------------------------------- run
def source_due(con, cfg, name, force):
    if force:
        return True
    row = con.execute("SELECT last_run FROM source_state WHERE source=?", (name,)).fetchone()
    if not row or not row["last_run"]:
        return True
    interval = max(int(cfg["interval_minutes"]), MIN_INTERVAL.get(name, 0))
    last = datetime.fromisoformat(row["last_run"])
    return (datetime.now(timezone.utc) - last).total_seconds() >= interval * 60 - 30


ENRICH_LOCK = threading.Lock()


# ---------------------------------------------------------------- company logos
LOGO_MAX_BYTES = 300 * 1024
# no SVG: served from this origin, an SVG could run scripts
LOGO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}
LOGO_LOCK = threading.Lock()


def remember_logo(con, company, url):
    """Notes the logo address of a company (once per company; a later source may fill in a missing one)."""
    key = norm(company)
    if not key or not url or not url.startswith("https://"):
        return
    row = con.execute("SELECT url, file FROM company_logos WHERE ckey=?", (key,)).fetchone()
    if not row:
        con.execute("INSERT INTO company_logos(ckey, url) VALUES(?, ?)", (key, url))
    elif not row["file"] and row["url"] != url:
        con.execute("UPDATE company_logos SET url=?, tried=NULL WHERE ckey=?", (url, key))


def fetch_logos(limit=200):
    """Background job: downloads the logos not downloaded yet (a failed one is retried after a day)."""
    if not LOGO_LOCK.acquire(blocking=False):
        return
    try:
        day_ago = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat()
        with db() as con:
            rows = con.execute("SELECT ckey, url FROM company_logos WHERE file IS NULL AND (tried IS NULL OR tried < ?) "
                               "LIMIT ?", (day_ago, limit)).fetchall()
        os.makedirs(LOGO_DIR, exist_ok=True)
        done = 0
        for r in rows:
            fname = None
            try:
                req = urllib.request.Request(r["url"], headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    ctype = (resp.headers.get_content_type() or "").lower()
                    data = resp.read(LOGO_MAX_BYTES + 1)
                if ctype in LOGO_TYPES and 0 < len(data) <= LOGO_MAX_BYTES:
                    fname = hashlib.sha1(r["ckey"].encode()).hexdigest()[:16] + LOGO_TYPES[ctype]
                    with open(os.path.join(LOGO_DIR, fname), "wb") as fh:
                        fh.write(data)
                    done += 1
            except Exception as ex:
                log.info("Logo %s: %s", r["ckey"], ex)
            with db() as con:
                con.execute("UPDATE company_logos SET file=?, tried=? WHERE ckey=?", (fname, now_iso(), r["ckey"]))
            time.sleep(0.5)
        if rows:
            log.info("Logos: %d of %d downloaded", done, len(rows))
    finally:
        LOGO_LOCK.release()


def logo_files():
    with db() as con:
        return {r["ckey"]: r["file"] for r in con.execute("SELECT ckey, file FROM company_logos WHERE file IS NOT NULL")}


def logo_html(company, files, size=40):
    """The company's logo, or its initials on a colour picked from the name when there is no logo."""
    key = norm(company)
    if key in files:
        return '<img class="logo" src="/logo/%s" alt="" width="%d" height="%d" loading="lazy">' % (e(key), size, size)
    words = [w for w in re.split(r"[\s\-_.,&/]+", company or "") if w and w[0].isalnum()]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    hue = int(hashlib.md5(key.encode()).hexdigest()[:4], 16) % 360 if key else 0
    return ('<span class="logo ph" style="width:%dpx;height:%dpx;background:hsl(%d 45%% 42%%)" aria-hidden="true">%s</span>'
            % (size, size, hue, e(initials)))


def enrich_pending_linkedin(cfg, limit=60):
    """Background job: fetches the description of LinkedIn postings that still have none,
    then updates their work mode and score in the database. Never blocks a refresh."""
    if not ENRICH_LOCK.acquire(blocking=False):
        return
    try:
        with db() as con:
            # every LinkedIn posting, whichever source brought it (a LinkedIn search, an alert e-mail, a pasted link)
            rows = con.execute("SELECT id, urlkey, title, company, location FROM jobs WHERE (urlkey LIKE 'li:%' OR source='linkedin') "
                               "AND dup_of IS NULL AND (description IS NULL OR description='') ORDER BY first_seen DESC LIMIT ?",
                               (limit,)).fetchall()
            items = [{"ext_id": r["urlkey"][3:] if (r["urlkey"] or "").startswith("li:") else r["id"].split(":", 1)[-1],
                      "row_id": r["id"], "title": r["title"], "company": r["company"], "location": r["location"]} for r in rows]
            if not items:
                return
            STATE["enrich"] = {"total": len(items), "done": 0}
            enrich_linkedin(con, items, limit=limit)
            STATE["enrich"]["done"] = sum(1 for it in items if it.get("description"))
        with db() as con:
            for it in items:
                if not it.get("description"):
                    continue
                jid = it["row_id"]
                sc, hits = score_job(cfg, it)
                con.execute("UPDATE jobs SET description=?, score=?, matched=? WHERE id=?",
                            (it["description"], sc, ", ".join(hits), jid))
                if it.get("work_mode", "unknown") != "unknown":  # the text decides only where nothing exact is known
                    con.execute("UPDATE jobs SET work_mode=?, mode_src='text', mode_note=? WHERE id=? AND "
                                "COALESCE(mode_src,'') NOT IN ('manual','source','page')", (it["work_mode"], it.get("mode_note"), jid))
        log.info("LinkedIn descriptions: %d updated", sum(1 for it in items if it.get("description")))
    except Exception:
        log.exception("LinkedIn background enrichment failed")
    finally:
        STATE["enrich"] = None
        ENRICH_LOCK.release()
    threading.Thread(target=enrich_work_modes, args=(cfg,), daemon=True).start()  # descriptions arrived: settle the rest


# ---------------------------------------------------------------- settling unknown work modes
MODE_LOCK = threading.Lock()
MODE_CLAUDE_PER_RUN = 40
FLEXA_MODE = {"On-site": "onsite", "Hybrid": "hybrid", "Remote-first": "remote", "Remote": "remote", "Field-based": "onsite",
              "Work from anywhere": "remote"}
FOURDAY_MODE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite", "on site": "onsite",
                "office": "onsite", "in-office": "onsite", "in office": "onsite"}


# Why a posting's work mode is not known: code -> (short text on the card, full explanation in the menu)
UNKNOWN_WHY = {
    "pending": ("not checked yet",
                "Not checked yet: it is checked in the background right after the refresh."),
    "li_wait": ("LinkedIn hides it, text not downloaded yet",
                "LinkedIn shows the work mode only to logged-in users, so it is not in what jobwatch can read. "
                "The posting text is still being downloaded (LinkedIn limits how fast); the text is checked once it arrives."),
    "li_hidden": ("LinkedIn hides it, text doesn't say",
                  "LinkedIn shows the work mode only to logged-in users, and the posting text does not state it either "
                  "(checked by the rules%s). Set it here, or let a LinkedIn job alert e-mail bring it: those state the mode."),
    "board_none": ("%s doesn't give it",
                   "%s does not state the work mode for this posting, and the text does not say it either (checked by the rules%s)."),
    "mail_none": ("the e-mail doesn't say",
                  "The alert e-mail did not state the work mode for this posting, and there is no posting text to check."),
    "no_text": ("no posting text",
                "The source gives no posting text, so there is nothing to check."),
    "text_none": ("the text doesn't say",
                  "The posting text does not state the work mode (checked by the rules%s)."),
}


def unknown_reason(r):
    """(short, long) reason why the work mode of r is unknown."""
    note = r.get("mode_note") or ""
    code, _, extra = note[1:].partition("|") if note.startswith("?") else ("", "", "")
    linkedin = (r.get("urlkey") or "").startswith("li:") or r.get("source") == "linkedin"
    if not code:  # not checked yet: say what it is waiting for
        code = "li_wait" if linkedin and not (r.get("description") or "") else "pending"
    by = {"claude": " and Claude", "claude_off": "; Claude is turned off in Settings", "no_key": "; no Claude API key",
          "claude_error": "; Claude could not be reached", "": ""}.get(extra, "")
    board = SOURCES_INFO.get(r.get("source"), (r.get("source") or "",))[0]
    short, long_ = UNKNOWN_WHY.get(code, UNKNOWN_WHY["pending"])
    if code == "board_none":
        return short % board, long_ % (board, by)
    return short, (long_ % by) if "%s" in long_ else long_


def mode_why(r):
    src, note = r.get("mode_src") or "", r.get("mode_note") or ""
    if src == "manual":
        return "Set by you."
    if src in ("source", "page"):
        return "Exact, %s." % (note or "from the source")
    if src == "text":
        return "From the posting text: %s" % note
    if src == "claude":
        return "Claude read the posting: %s" % note
    if (r.get("work_mode") or "unknown") == "unknown":
        long_ = unknown_reason(r)[1]
        return long_ if "Set it here" in long_ else long_ + " You can set it here."
    return "From the posting text."


def mode_picker(r):
    """The work-mode chip on a card: shows where the value comes from, and opens a menu to set it by hand."""
    wm = r.get("work_mode") or "unknown"
    src = r.get("mode_src") or ""
    mark = {"manual": " ✎", "claude": " ✦"}.get(src, "")
    qid = e(urllib.parse.quote(r["id"], safe=""))
    opts = "".join('<button class="btn btn-xs btn-ghost%s" name="work_mode" value="%s">%s</button>' % (
        " btn-active" if k == wm and src == "manual" else "", k, e(WORK_MODES[k])) for k in ("remote", "hybrid", "onsite"))
    why_short = (' <span class="wm-why">· %s</span>' % e(unknown_reason(r)[0])) if wm == "unknown" and src != "manual" else ""
    return ('<details class="wm-pick"><summary class="chip wm-%s" title="%s">%s%s%s ▾</summary>'
            '<form class="wm-menu" method="post" action="/job/%s"><div class="muted">%s</div>%s'
            '<button class="btn btn-xs btn-ghost" name="work_mode" value="auto" title="Let jobwatch decide again">↺ Automatic</button>'
            '</form></details>') % (e(wm), e(mode_why(r)), e(WORK_MODES.get(wm, wm)), mark, why_short, qid, e(mode_why(r)), opts)


def mode_from_page(source, url):
    """The work mode a job board states on the posting's own page (4dayweek, Flexa): (mode, note) or (None, "")."""
    if source == "4dayweek":
        pg = fetch(url, timeout=20)
        m = re.search(r"<span><span>([A-Za-z -]{3,20}?) · ([^<]{1,80})</span>", pg)
        if m and m.group(1).strip().lower() in FOURDAY_MODE:
            return FOURDAY_MODE[m.group(1).strip().lower()], "4dayweek: “%s · %s”" % (m.group(1).strip(), html.unescape(m.group(2)))
    elif source == "flexa":
        pg = fetch(url, timeout=20).replace('\\"', '"')
        m = re.search(r'"size":16\}\],"(%s)"\]' % "|".join(map(re.escape, FLEXA_MODE)), pg)
        if m:
            return FLEXA_MODE[m.group(1)], "Flexa: “%s”" % m.group(1)
    return None, ""


def enrich_work_modes(cfg, limit=80):
    """Background job: settles postings whose work mode is unknown — first from the job board's own page
    (4dayweek, Flexa), then from the text in many languages, then (if turned on) Claude reads the posting.
    A manual choice is never touched. Each posting is checked once (again after 3 days if still unknown)."""
    if not MODE_LOCK.acquire(blocking=False):
        return
    try:
        import cvgen
        old = datetime.fromtimestamp(time.time() - 3 * 86400, timezone.utc).isoformat()
        with db() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT id, source, url, urlkey, title, location, description FROM jobs WHERE dup_of IS NULL AND "
                "COALESCE(work_mode,'unknown')='unknown' AND COALESCE(mode_src,'')!='manual' AND "
                "NOT ((COALESCE(urlkey,'') LIKE 'li:%' OR source='linkedin') AND COALESCE(description,'')='') AND "
                "(mode_checked IS NULL OR mode_checked < ?) ORDER BY first_seen DESC LIMIT ?", (old, limit))]
        key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        use_claude = cfg.get("mode_claude", True) and key
        settled, asked = 0, 0
        for r in rows:
            mode, src, note = None, None, ""
            linkedin = (r["urlkey"] or "").startswith("li:") or r["source"] == "linkedin"
            if linkedin and not (r["description"] or ""):
                continue  # wait for its text (the LinkedIn description download calls this again); not marked as checked
            page_tried = False
            if r["source"] in ("4dayweek", "flexa"):
                page_tried = True
                try:
                    mode, note = mode_from_page(r["source"], r["url"])
                    src = "page" if mode else None
                except Exception as ex:
                    log.info("Work mode page %s: %s", r["url"], ex)
                time.sleep(1)
            if not mode:
                m2, n2 = classify_mode_ex(r["title"], r["location"], r["description"])
                if m2 != "unknown":
                    mode, src, note = m2, "text", n2
            claude_state = "claude_off" if not cfg.get("mode_claude", True) else ("no_key" if not key else "")
            if not mode and use_claude and len(r["description"] or "") > 200 and asked < MODE_CLAUDE_PER_RUN:
                asked += 1
                try:
                    m3, n3 = cvgen.work_mode_from_text(key, r["title"], r["location"], r["description"])
                    claude_state = "claude"
                    if m3 != "unknown":
                        mode, src, note = m3, "claude", n3
                except Exception as ex:
                    claude_state = "claude_error"
                    log.info("Work mode (Claude) %s: %s", r["id"], ex)
            if not mode:  # remember why it stays unknown, to show it on the card
                if linkedin:
                    code = "li_hidden"
                elif page_tried:
                    code = "board_none"
                elif r["source"] == "mail" and not (r["description"] or ""):
                    code = "mail_none"
                elif not (r["description"] or ""):
                    code = "no_text"
                else:
                    code = "text_none"
                note = "?%s|%s" % (code, claude_state)
            with db() as con:
                if mode:
                    settled += 1
                    con.execute("UPDATE jobs SET work_mode=?, mode_src=?, mode_note=?, mode_checked=? WHERE id=? AND "
                                "COALESCE(mode_src,'')!='manual'", (mode, src, note[:300], now_iso(), r["id"]))
                else:
                    con.execute("UPDATE jobs SET mode_checked=?, mode_note=? WHERE id=? AND COALESCE(mode_src,'')!='manual'",
                                (now_iso(), note, r["id"]))
        if rows:
            log.info("Work modes: %d of %d unknown settled (%d read by Claude)", settled, len(rows), asked)
    finally:
        MODE_LOCK.release()


def run_all(force=False, only=None, custom_ids=None):
    """Refreshes the due sources (all of them with force). only: just these source names (forced);
    custom_ids: just these own sources (the "custom" source then runs with only them)."""
    if not RUN_LOCK.acquire(blocking=False):
        return
    STATE["running"] = True
    try:
        with CFG_LOCK:
            cfg = load_config()
        if custom_ids:
            cfg["custom_sources"] = [s for s in cfg.get("custom_sources") or [] if s.get("id") in custom_ids]
            only = set(only or ()) | {"custom"}
        to_notify = []
        # 1) decide which sources are due, 2) fetch them in parallel, 3) save them one by one in order
        due = []
        with db() as con:
            for name, fn in SOURCE_FUNCS.items():
                if only is not None and name not in only:
                    continue
                if cfg["sources"].get(name) and source_due(con, cfg, name, force or only is not None):
                    st = con.execute("SELECT seeded FROM source_state WHERE source=?", (name,)).fetchone()
                    due.append((name, fn, bool(st and st["seeded"])))
        from concurrent.futures import ThreadPoolExecutor, as_completed
        STATE["progress"] = {"total": len(due), "done": 0, "sources": {n: "fetching" for n, _, _ in due}}
        completed = iter(())
        if due:
            pool = ThreadPoolExecutor(max_workers=len(due))
            futs = {pool.submit(fn, cfg): (name, seeded) for name, fn, seeded in due}
            pool.shutdown(wait=False)
            completed = as_completed(futs)
        for fut in completed:
            name, seeded = futs[fut]
            try:
                items, err = fut.result(), None
            except Exception as ex:
                log.exception("Source error: %s", name)
                items, err = [], "%s: %s" % (type(ex).__name__, ex)
            ts = now_iso()
            new = skipped = 0
            kws = search_keywords(cfg) if (cfg.get("search_filter") or {}).get(name) else []
            with db() as con:
                for it in items:
                    if not it.get("title") or not it.get("url", "").startswith("http"):
                        continue
                    jid = "%s:%s" % (name, it["ext_id"])
                    remember_logo(con, it.get("company"), it.get("logo"))
                    sc, hits = score_job(cfg, it)
                    # without an employer the title alone is not enough to call two postings the same
                    dup = dup_key(it["title"], it["company"], it["url"])
                    if it.get("work_mode") in ("remote", "hybrid", "onsite"):   # the source says it: exact
                        wm, msrc, mnote = it["work_mode"], "source", "from " + SOURCES_INFO.get(name, (name,))[0]
                    else:
                        wm, mnote = classify_mode_ex(it["title"], it["location"], it.get("description"))
                        msrc = "text" if wm != "unknown" else None
                    ex_row = con.execute("SELECT id, mode_src FROM jobs WHERE id=?", (jid,)).fetchone()
                    if ex_row:
                        keep = (ex_row["mode_src"] or "") in ("manual", "source", "page")  # never overwrite what is exact
                        if it.get("description") and name == "linkedin":
                            sc2, hits2 = score_job(cfg, it)
                            con.execute("UPDATE jobs SET last_seen=?, description=?, score=?, matched=? WHERE id=?",
                                        (ts, it["description"], sc2, ", ".join(hits2), jid))
                        else:
                            con.execute("UPDATE jobs SET last_seen=? WHERE id=?", (ts, jid))
                        if wm != "unknown" and (msrc == "source" or not keep) and (ex_row["mode_src"] or "") != "manual":
                            con.execute("UPDATE jobs SET work_mode=?, mode_src=?, mode_note=? WHERE id=?", (wm, msrc, mnote, jid))
                        continue
                    if kws and not matches_search(kws, it):
                        skipped += 1  # this source lists everything: keep only what matches a search term
                        continue
                    same = find_duplicate(con, it["title"], it["company"], it["url"])
                    if same:
                        merge_into(con, same, name, it)  # already listed from another source
                        continue
                    notified = 0 if (seeded and sc >= cfg["notify_min_score"]
                                     and wm in cfg.get("notify_work_modes", ["remote", "unknown"])) else 1
                    con.execute(
                        "INSERT INTO jobs(id,source,title,company,location,url,posted,description,score,matched,"
                        "dupkey,first_seen,last_seen,status,notified,work_mode,urlkey,mode_src,mode_note) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?,?,?,?)",
                        (jid, name, it["title"], it["company"], it["location"], it["url"], it["posted"],
                         it.get("description", ""), sc, ", ".join(hits), dup, ts, ts, notified, wm, url_key(it["url"]),
                         msrc, mnote))
                    new += 1
                    if not notified:
                        to_notify.append(dict(id=jid, source=name, score=sc, **{
                            k: it[k] for k in ("title", "company", "location", "url")}))
                con.execute(
                    "INSERT INTO source_state(source,last_run,last_ok,found,new,error,seeded) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run, "
                    "last_ok=COALESCE(excluded.last_ok, source_state.last_ok), found=excluded.found, "
                    "new=excluded.new, error=excluded.error, seeded=MAX(source_state.seeded, excluded.seeded)",
                    (name, ts, None if err else ts, len(items), new, err, 0 if err else 1))
            if name == "mail" and not err:
                mark_mails_done(cfg.pop("_mail_done", []))  # only now that their postings are saved
            prog = STATE["progress"]
            prog["done"] += 1
            prog["sources"][name] = ("error" if err else "done") + " (%d)" % len(items)
            log.info("%s: %d postings, %d new%s%s", name, len(items), new,
                     (", %d not matching the search" % skipped) if skipped else "", " ERROR: " + err if err else "")
        errs = notify_jobs(cfg, to_notify)
        if to_notify and not errs:
            with db() as con:
                con.executemany("UPDATE jobs SET notified=1 WHERE id=?", [(j["id"],) for j in to_notify])
        elif to_notify:
            # if at least one channel worked, do not send again
            ok_channels = (cfg["ntfy"].get("enabled") and not any(e.startswith("ntfy") for e in errs)) or \
                          (cfg["email"].get("enabled") and not any(e.startswith("e-mail") for e in errs))
            if ok_channels:
                with db() as con:
                    con.executemany("UPDATE jobs SET notified=1 WHERE id=?", [(j["id"],) for j in to_notify])
        if any(name in ("linkedin", "mail") for name, _, _ in due):
            # descriptions for LinkedIn postings come later, in the background, so the list is not held up
            threading.Thread(target=enrich_pending_linkedin, args=(cfg,), daemon=True).start()
        threading.Thread(target=fetch_logos, daemon=True).start()
        threading.Thread(target=enrich_work_modes, args=(cfg,), daemon=True).start()
    finally:
        STATE["running"] = False
        STATE["last_run"] = now_iso()
        STATE["progress"] = None
        RUN_LOCK.release()


def rescore_all():
    with CFG_LOCK:
        cfg = load_config()
    with db() as con:
        for r in con.execute("SELECT * FROM jobs").fetchall():
            sc, hits = score_job(cfg, dict(r))
            con.execute("UPDATE jobs SET score=?, matched=? WHERE id=?", (sc, ", ".join(hits), r["id"]))


def scheduler():
    time.sleep(5)
    while True:
        try:
            run_all()
        except Exception:
            log.exception("Scheduler error")
        time.sleep(60)


# ---------------------------------------------------------------- web UI
CSS = """
:root{--bg:var(--color-base-200);--card:var(--color-base-100);--fg:var(--color-base-content);
--muted:color-mix(in oklab,var(--color-base-content) 65%,transparent);--line:var(--color-base-300);
--soft:color-mix(in oklab,var(--color-base-content) 7%,var(--color-base-100));
--acc:var(--color-primary);--acc-soft:color-mix(in oklab,var(--color-primary) 14%,transparent);
--good:var(--color-success);--good-soft:color-mix(in oklab,var(--color-success) 16%,transparent);
--warn:var(--color-warning);--warn-soft:color-mix(in oklab,var(--color-warning) 18%,transparent);
--bad:var(--color-error);--bad-soft:color-mix(in oklab,var(--color-error) 16%,transparent);--radius:var(--radius-box,12px)}
*{box-sizing:border-box}html{background:var(--bg);min-height:100%}body{margin:0;min-height:100vh;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,Segoe UI,Roboto,Inter,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
a.btn-primary,a.btn-primary:hover{color:var(--color-primary-content)}
a.btn-primary:hover{text-decoration:none}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line)}
.bar{max-width:1040px;margin:0 auto;padding:8px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.brand{font-weight:700;font-size:16px;margin-right:6px;white-space:nowrap}
nav.tabs{display:flex;gap:2px;flex-wrap:wrap}nav.tabs a{padding:6px 11px;border-radius:8px;color:var(--muted);font-weight:500}
nav.tabs a:hover{background:var(--soft);text-decoration:none}nav.tabs a.on{background:var(--acc-soft);color:var(--acc)}
.bar .right{margin-left:auto;display:flex;gap:6px;align-items:center}
main{max-width:1040px;margin:0 auto;padding:16px}
textarea{width:100%;min-height:120px;font-family:ui-monospace,Menlo,monospace;font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:10px 14px;color:var(--fg)}
.stat:hover{text-decoration:none;border-color:var(--acc)}.stat b{display:block;font-size:22px;line-height:1.2}.stat span{color:var(--muted);font-size:13px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px;margin-bottom:14px}
.row-chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:2px 0 8px}.row-chips .lbl{color:var(--muted);font-size:13px;width:92px}
a.fchip{padding:4px 11px;border-radius:99px;border:1px solid var(--line);color:var(--fg);font-size:13.5px;background:var(--card)}
a.fchip:hover{text-decoration:none;border-color:var(--acc)}a.fchip.on{background:var(--acc);border-color:var(--acc);color:#fff}
a.fchip small{opacity:.75;margin-left:3px}
details.more summary{cursor:pointer;color:var(--muted);font-size:13.5px;margin-top:4px}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
.listhead{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin:4px 0 6px;color:var(--muted);font-size:13.5px}
.bucket{position:sticky;top:52px;z-index:2;background:var(--bg);font-size:13px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;
color:var(--muted);margin:16px 0 8px;padding:6px 0;border-bottom:1px solid var(--line)}.bucket span{font-weight:400}
.job{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:var(--radius);padding:12px 14px;margin-bottom:10px;
display:grid;grid-template-columns:46px 1fr auto;gap:4px 14px;align-items:start}
.job.st-new{border-left-color:var(--acc)}.job.st-saved{border-left-color:var(--warn)}.job.st-applied{border-left-color:var(--good)}
.job.st-hidden,.job.stale{opacity:.6}
.score{width:46px;height:46px;border-radius:12px;display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:17px;background:var(--soft);color:var(--muted);line-height:1}
.score small{font-size:9.5px;font-weight:500;margin-top:2px}.s-hi{background:var(--good);color:#fff}.s-mid{background:var(--warn-soft);color:var(--warn)}
.jt{font-weight:650;font-size:16px;line-height:1.3}.jt a{color:var(--fg)}.jt a:hover{color:var(--acc)}
.co{margin-top:1px;font-size:14px}.co .src{color:var(--muted);font-size:12.5px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
.chip{display:inline-flex;align-items:center;gap:3px;font-size:12.5px;padding:2px 8px;border-radius:99px;background:var(--soft);color:var(--muted);white-space:nowrap}
.chip.wm-remote{background:var(--good-soft);color:var(--good)}.chip.wm-hybrid{background:var(--warn-soft);color:var(--warn)}.chip.wm-onsite{background:var(--bad-soft);color:var(--bad)}
.chip.age-new{background:var(--acc-soft);color:var(--acc);font-weight:600}.chip.age-old{background:var(--soft);color:var(--muted)}.chip.closed{background:var(--bad-soft);color:var(--bad)}
.chip.st{background:transparent;border:1px solid var(--line)}
details.why{margin-top:6px;font-size:12.5px;color:var(--muted)}details.why summary{cursor:pointer;width:max-content}
.acts{display:flex;flex-direction:column;gap:6px;align-items:stretch;min-width:150px}
.acts .icons{display:flex;gap:4px;justify-content:flex-end}.acts .icons button{padding:4px 8px;font-size:15px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;margin-bottom:14px}
.card h2{font-size:16px;margin:0 0 10px}label{display:block;margin:8px 0 3px;font-weight:600;font-size:13px}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
table{width:100%;border-collapse:collapse}td,th{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;font-size:14px;vertical-align:top}
.meta,.muted{color:var(--muted)}.meta{font-size:13.5px}
.err{color:var(--bad)}.ok{color:var(--good)}.flash{padding:9px 12px;border-radius:8px;background:var(--acc-soft);border:1px solid var(--acc);margin-bottom:12px}
.badge{display:inline-block;font-size:11.5px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.docs-co{margin-bottom:16px}.docs-co h3{margin:0 0 6px;font-size:15px}.docs-job{padding:8px 0;border-top:1px solid var(--line)}
.files{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px}
@media (max-width:700px){.job{grid-template-columns:42px 1fr}.acts{grid-column:1/-1;flex-direction:row;flex-wrap:wrap;min-width:0}
.acts .icons{justify-content:flex-start}.score{width:42px;height:42px}.bucket{top:88px}.row-chips .lbl{width:100%}}
/* shared page building blocks (ui-*) and the Recruiters page (rc-*) */
.ui-table td,.ui-table th{vertical-align:middle;font-size:14px}.ui-table .num{text-align:right}.ui-table td.when{white-space:nowrap;color:var(--muted)}
.ui-table tr.off td{opacity:.55}.ui-err{font-size:12px;color:var(--bad);margin-top:3px;max-width:42ch}
.ui-row{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;padding:14px 18px;border-top:1px solid var(--line)}
.ui-section>header+.ui-row{border-top:0}.ui-row-main{min-width:0;flex:1}.ui-row-title{font-weight:650;font-size:15px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ui-btns{display:flex;gap:6px;flex-wrap:wrap}.ui-empty{color:var(--muted);text-align:center;padding:28px 18px}
.ui-inline{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0 0;font-size:14px}.ui-inline .help{margin:0}
.ui-list{margin:0;padding-left:20px}.ui-list li{margin:3px 0}
.ui-addjob{margin-top:12px;width:100%;max-width:620px}.ui-addjob input{flex:1}
details.wm-pick{display:inline-block;position:relative}.wm-why{opacity:.8;font-weight:400}details.wm-pick>summary{list-style:none;cursor:pointer}
details.wm-pick>summary::-webkit-details-marker{display:none}
.wm-menu{position:absolute;z-index:6;top:calc(100% + 4px);left:0;min-width:230px;max-width:320px;display:flex;flex-direction:column;
  align-items:stretch;gap:2px;padding:8px;background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.18)}
.wm-menu .muted{font-size:12px;line-height:1.35;padding:2px 4px 6px;white-space:normal}.wm-menu .btn{justify-content:flex-start}
.job-head{display:flex;gap:12px;align-items:center}.job-head>div{min-width:0}
.logo{flex:0 0 auto;border-radius:9px;object-fit:contain;background:#fff;border:1px solid var(--line)}
.logo.ph{display:inline-flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:14px;
  letter-spacing:.02em;border:0;user-select:none}
.ui-section>header>div{min-width:0}.ui-desc{margin:3px 0 0;color:var(--muted);font-size:13.5px;max-width:75ch}
.ui-h3{font-size:14px;font-weight:650;margin:18px 0 10px;padding-top:14px;border-top:1px solid var(--line)}
.ui-body>.ui-h3:first-child{margin-top:0;padding-top:0;border-top:0}
.ui-checks{display:flex;flex-wrap:wrap;gap:8px 18px}.ui .ui-checks label{display:inline-flex;align-items:center;gap:7px;font-weight:400;margin:0}
.ui .ui-toggle{display:inline-flex;align-items:center;gap:10px;font-weight:600;font-size:14px;margin:0}
.ui-steps{margin:0 0 6px;padding-left:20px;font-size:13.5px}.ui-steps li{margin:2px 0}
.ui-mono{font-family:ui-monospace,Menlo,Consolas,monospace!important;font-size:13px!important}
.ui-lead{margin:0 0 12px;font-size:13.5px;color:var(--muted)}
.ui-sub{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px;background:var(--soft)}
.ui-subhead{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.ui-subhead .muted{font-size:12.5px}
.ui-layout{display:grid;grid-template-columns:180px minmax(0,1fr);gap:22px;align-items:start}
.ui-side{position:sticky;top:70px;display:flex;flex-direction:column;gap:2px}
.ui-side a{padding:7px 12px;border-radius:8px;color:var(--muted);font-weight:500;font-size:14px}
.ui-side a:hover{background:var(--soft);color:var(--fg);text-decoration:none}
.ui-savebar{position:sticky;bottom:0;z-index:4;display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:12px 18px;margin:0 0 18px;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:0 -6px 18px rgba(0,0,0,.12)}.ui-savebar .muted{font-size:13px}
.ui-danger{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}
.ui-danger .help{display:block}
.chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:7px;min-height:44px;border:1px solid var(--line);
  border-radius:10px;background:var(--card)}.chips:focus-within{border-color:var(--acc)}
.chip{display:inline-flex;align-items:center;gap:2px;padding:2px 3px 2px 9px;border-radius:99px;background:var(--soft);
  border:1px solid var(--line);font-size:13px;cursor:grab;user-select:none;white-space:nowrap}
.chip .pts{font:600 12px ui-monospace,Menlo,Consolas,monospace;color:var(--acc);margin-right:5px}.chip.neg .pts{color:#d14343}
.chip button{border:0;background:none;padding:0 5px;border-radius:99px;cursor:pointer;color:var(--muted);font-size:15px;line-height:20px}
.chip button:hover,.chip button:focus-visible{background:var(--line);color:var(--fg)}.chip.dragging{opacity:.4}
.chip.over{outline:2px solid var(--acc)}
.chips input{flex:1;min-width:150px;border:0;outline:0;background:transparent;font-size:13px;padding:4px;color:inherit}
section.ui-section{scroll-margin-top:70px}
@media (max-width:860px){.ui-layout{grid-template-columns:1fr}.ui-side{position:static;flex-direction:row;flex-wrap:wrap}}
.rc{max-width:900px;margin:0 auto}
.ui-head{margin:4px 0 18px}.ui-head h1{font-size:22px;font-weight:700;margin:0}
.ui-head p{margin:4px 0 0;color:var(--muted);max-width:70ch}
.ui-stats{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.ui-section{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);margin-bottom:18px}
.ui-section>header{position:static;background:none;display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:14px 18px;border-bottom:1px solid var(--line)}
.ui-section>header h2{font-size:16px;font-weight:650;margin:0}
.ui-body{padding:16px 18px}
.ui-step{display:flex;gap:12px;margin-bottom:20px}.ui-step:last-child{margin-bottom:0}
.ui-num{flex:0 0 26px;height:26px;border-radius:99px;background:var(--acc-soft);color:var(--acc);font-weight:700;
  display:flex;align-items:center;justify-content:center;font-size:13px;margin-top:1px}
.ui-step>div{flex:1;min-width:0}.ui-step h3{font-size:14.5px;font-weight:650;margin:2px 0 10px}
.ui-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 14px}.ui-grid .wide{grid-column:1/-1}
.ui-grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px 14px}
.ui label{display:block;font-weight:600;font-size:13px;margin:0 0 4px}
.ui .help{display:block;font-weight:400;color:var(--muted);font-size:12.5px;margin-top:3px}
.ui input.input,.ui select.select{width:100%}
.ui textarea.textarea{width:100%;font-family:inherit;font-size:14.5px;line-height:1.55;min-height:120px}
.rc-people{margin-top:12px;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.rc-people table td,.rc-people table th{font-size:13.5px;vertical-align:middle}
.rc-people .empty{padding:14px;color:var(--muted);font-size:13.5px;text-align:center}
.ui-hint{min-height:20px;font-size:13px;color:var(--muted);margin-top:6px}
.ui-foot{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;
  padding:14px 18px;border-top:1px solid var(--line);background:var(--soft);border-radius:0 0 var(--radius) var(--radius)}
.ui-more summary{cursor:pointer;font-weight:600;font-size:13.5px;color:var(--acc);margin:4px 0}
.ui-more[open] summary{margin-bottom:12px}
.rc-draft{border-top:1px solid var(--line);padding:16px 18px}.rc-draft:first-child{border-top:0}
.rc-who{display:flex;gap:12px;align-items:flex-start}
.rc-avatar{flex:0 0 38px;height:38px;border-radius:99px;background:var(--acc-soft);color:var(--acc);font-weight:700;
  display:flex;align-items:center;justify-content:center;font-size:14px}
.rc-who .name{font-weight:650;font-size:15px;line-height:1.3}.rc-who .sub{color:var(--muted);font-size:13px}
.rc-who .badges{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.rc-role{margin:8px 0 0 50px;font-size:13px;color:var(--muted)}
.rc-fields{margin:12px 0 0 50px}.rc-fields>div{margin-bottom:10px}
.rc-count{text-align:right;font-size:12px;color:var(--muted);margin-top:2px}.rc-count.over{color:var(--bad);font-weight:600}
.rc-actions{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;margin:4px 0 0 50px}
.rc-actions>div{display:flex;gap:6px;flex-wrap:wrap}
.rc-sent tbody tr{cursor:pointer}.rc-sent tbody tr:hover{background:var(--soft)}
.rc-sent td.when{white-space:nowrap;color:var(--muted)}
.rc-modal{max-width:680px;width:92vw}.rc-modal h3{font-size:18px;font-weight:700;margin:0}
.rc-modal .sub{color:var(--muted);font-size:13.5px;margin:2px 0 14px}
.rc-modal .modal-action{justify-content:space-between;flex-wrap:wrap}
.rc-modal .modal-action>div{display:flex;gap:6px;flex-wrap:wrap}
.rc-alert{margin:10px 0 0 50px}
@media (max-width:700px){.ui-grid,.ui-grid3{grid-template-columns:1fr}.rc-role,.rc-fields,.rc-actions,.rc-alert{margin-left:0}
  .rc-who .badges{display:none}.rc-sent .hide-sm{display:none}}
"""


STATUS_LABEL = {"new": "new", "seen": "seen", "saved": "saved", "applied": "applied", "hidden": "hidden"}


def e(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def safe_url(u):
    return u if isinstance(u, str) and u.startswith(("http://", "https://")) else "#"


def fmt_dt(s):
    if not s:
        return ""
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone()
        return d.strftime("%Y-%m-%d %H:%M") if (d.hour or d.minute) else d.strftime("%Y-%m-%d")
    except Exception:
        return s[:16]


# Custom class -> daisyUI component. The markup uses the old class names; this mapping
# gives the daisyUI styles. Classes not in the list keep their own (CSS) style.
DAISY_CLASS = {
    "card": "card bg-base-100 shadow-sm",
    "card muted": "card bg-base-100 shadow-sm muted",
    "card docs-co": "card bg-base-100 shadow-sm docs-co",
    "btn": "btn btn-sm",
    "btn sm": "btn btn-sm",
    "btn primary": "btn btn-primary btn-sm",
    "btn primary sm": "btn btn-primary btn-sm",
    "primary": "btn btn-primary btn-sm",
    "primary sm": "btn btn-primary btn-sm",
    "sm": "btn btn-sm",
    "badge": "badge badge-soft badge-primary",
    "stat": "stat",
    "stats": "stats shadow-sm",
    "flash": "alert alert-info",
}


def daisify(html):
    html = re.sub(r'class="([^"]*)"', lambda m: 'class="%s"' % DAISY_CLASS.get(m.group(1), m.group(1)), html)
    html = re.sub(r'<button(?![^>]*\bclass=)', '<button class="btn btn-sm"', html)
    html = re.sub(r'<select(?![^>]*\bclass=)', '<select class="select select-sm"', html)
    html = re.sub(r'<textarea(?![^>]*\bclass=)', '<textarea class="textarea"', html)
    html = re.sub(r'<input(?![^>]*\bclass=)(?![^>]*type="(?:checkbox|hidden|radio)")', '<input class="input input-sm"', html)
    html = re.sub(r'<input(?![^>]*\bclass=)(?=[^>]*type="checkbox")', '<input class="checkbox checkbox-sm"', html)
    return html


# Reloads the page every N seconds while something runs, but not while the user is busy with it:
# a section they opened, a field they are typing in, or an open dialog postpones the reload.
SOFT_REFRESH = """<script>(function () {
  var touched = false;
  document.addEventListener('click', function (ev) { if (ev.target.closest && ev.target.closest('summary')) touched = true; }, true);
  function busy() {
    var a = document.activeElement;
    return (touched && document.querySelector('details[open]')) || document.querySelector('dialog[open]') ||
      (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  }
  setTimeout(function tick() { if (busy()) { setTimeout(tick, 3000); } else { location.reload(); } }, %d * 1000);
})();</script>"""


def page(title, body, flash="", refresh=0):
    cur = getattr(REQ, "path", "/")
    tabs = [("/", "Jobs"), ("/docs", "Documents"), ("/outreach", "Recruiters"), ("/sources", "Sources"),
            ("/profile", "CV profile"), ("/settings", "Settings")]
    nav = "".join('<a role="tab" class="tab%s" href="%s">%s</a>' % (
        " tab-active" if (cur == h or (h == "/" and cur.startswith("/cv/"))) else "", h, l) for h, l in tabs)
    running = '<span class="badge badge-soft badge-warning">⏳ refresh running…</span>' if STATE["running"] else ""
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">%s
<script>document.documentElement.dataset.theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";</script>
<title>%s · jobwatch</title>
<link rel="icon" href="/favicon.ico" sizes="any"><link rel="icon" type="image/svg+xml" href="/favicon.svg"><link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#5b5bd6">
<link href="https://cdn.jsdelivr.net/npm/daisyui@5.0.0" rel="stylesheet" type="text/css"><style>%s</style></head><body><header><div class="bar"><span class="brand">💼 jobwatch</span><nav class="tabs tabs-box tabs-sm">%s</nav>
<div class="right">%s<form method="post" action="/test"><button class="btn btn-sm" name="what" value="ntfy" title="Test push notification to your phone">🔔 Test push</button></form>
<form method="post" action="/run" class="join"><button class="btn btn-primary btn-sm join-item" title="Fetch all sources now">↻ Refresh all</button><a class="btn btn-primary btn-outline btn-sm join-item" href="/sources#refresh" title="Choose which sources to fetch">Choose…</a></form></div></div></header>
<main>%s%s</main></body></html>"""
            % ((SOFT_REFRESH % refresh) if refresh else "", e(title), CSS, nav, running,
               '<div class="alert alert-info flash">%s</div>' % e(flash) if flash else "", daisify(body)))


def parse_dt(s):
    """Date in mixed formats -> timezone-aware datetime (UTC if none given), or None."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(s)
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def days_ago(d):
    """How many calendar days ago (local time)."""
    return (datetime.now().astimezone().date() - d.astimezone().date()).days


def rel_day(n):
    if n <= 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n < 14:
        return "%d days ago" % n
    if n < 60:
        return "%d weeks ago" % (n // 7)
    return "%d months ago" % (n // 30)


BUCKETS = [(0, "Today"), (1, "Yesterday"), (6, "This week (2–6 days ago)"), (13, "Last week (7–13 days ago)"),
           (30, "2–4 weeks ago"), (10 ** 6, "Older than 30 days – probably stale")]


def bucket_of(n):
    for lim, name in BUCKETS:
        if n <= lim:
            return name
    return BUCKETS[-1][1]


def progress_html():
    """Live status bar: per-source progress of a refresh and the background description fetch."""
    prog = STATE.get("progress")
    enr = STATE.get("enrich")
    if not prog and not enr:
        return ""
    parts = []
    if prog:
        pct = int(100 * prog["done"] / prog["total"]) if prog["total"] else 100
        icons = {"fetching": "⏳", "done": "✅", "error": "❌"}
        rows = "".join('<span class="chip">%s %s: %s</span>' % (
            icons.get(str(s).split(" ")[0], "•"), e(n), e(s)) for n, s in prog["sources"].items())
        parts.append('<div class="card"><b>Refreshing sources… %d of %d done</b>'
                     '<progress class="progress progress-primary w-full mt-2" value="%d" max="100"></progress>'
                     '<div class="chips" style="margin-top:8px">%s</div></div>' % (prog["done"], prog["total"], pct, rows))
    if enr and enr.get("total"):
        parts.append('<div class="card muted">LinkedIn descriptions: %d of %d fetched in the background '
                     '(work mode and score update as they arrive).</div>' % (enr.get("done", 0), enr["total"]))
    return "".join(parts)


def jobs_page(q):
    with CFG_LOCK:
        cfg = load_config()
    # first load of an empty instance: start the first refresh now instead of showing nothing
    with db() as con:
        empty = con.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    if empty and not STATE["running"] and not STATE.get("progress"):
        threading.Thread(target=run_all, kwargs={"force": True}, daemon=True).start()
        time.sleep(0.3)
    status = q.get("status", "active")
    srcs = sorted({s for s in q.get("source", "").split(",") if s in SOURCES_INFO})  # several sources may be picked
    src = ",".join(srcs)
    try:
        mn = int(q.get("min", cfg["min_score"]))
    except ValueError:
        mn = cfg["min_score"]
    text = q.get("q", "").strip()
    wf = q.get("work", cfg.get("default_work_filter", "remote_unknown"))
    sort = q.get("sort", "posted")
    age = q.get("age", "all")
    sql, args = "SELECT * FROM jobs WHERE score>=? AND dup_of IS NULL", [mn]
    wf_sql = {"remote": ["remote"], "remote_unknown": ["remote", "unknown"], "remote_hybrid": ["remote", "hybrid"],
              "hybrid": ["hybrid"], "onsite": ["onsite"]}.get(wf)
    if wf_sql:
        sql += " AND COALESCE(work_mode,'unknown') IN (%s)" % ",".join("?" * len(wf_sql))
        args += wf_sql
    if status == "active":
        sql += " AND status IN ('new','seen','saved')"
    elif status != "all":
        sql += " AND status=?"
        args.append(status)
    if srcs:
        sql += " AND source IN (%s)" % ",".join("?" * len(srcs))
        args += srcs
    if text:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
        args += ["%" + text + "%"] * 3
    with db() as con:
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        counts = {r["status"]: r["c"] for r in con.execute(
            "SELECT status, COUNT(*) c FROM jobs WHERE score>=? AND dup_of IS NULL GROUP BY status", (mn,))}
        last_ok = {r["source"]: r["last_ok"] for r in con.execute("SELECT source, last_ok FROM source_state")}
        wcount = {r["m"]: r["c"] for r in con.execute(
            "SELECT COALESCE(work_mode,'unknown') m, COUNT(*) c FROM jobs WHERE score>=? AND dup_of IS NULL AND status IN ('new','seen','saved') GROUP BY m", (mn,))}
        scount = {r["source"]: r["c"] for r in con.execute(
            "SELECT source, COUNT(*) c FROM jobs WHERE score>=? AND dup_of IS NULL AND status IN ('new','seen','saved') GROUP BY source", (mn,))}

    # dates: posted (if the source does not give it, the collection day), collected
    for r in rows:
        seen = parse_dt(r["first_seen"]) or datetime.now(timezone.utc)
        pd = parse_dt(r["posted"])
        r["_posted_known"] = pd is not None
        r["_posted"] = pd or seen
        r["_seen"] = seen
        r["_pdays"] = days_ago(r["_posted"])
        r["_sdays"] = days_ago(seen)
        # for a source that returns the full list (studios): if it is missing from the last successful fetch -> closed
        lo = last_ok.get(r["source"])
        r["_closed"] = r["source"] == "studios" and bool(lo) and (r["last_seen"] or "") < lo
    key_days = "_sdays" if sort == "seen" else "_pdays"
    if age != "all":
        try:
            lim = int(age)
            rows = [r for r in rows if r[key_days] <= lim]
        except ValueError:
            pass
    if sort == "seen":
        rows.sort(key=lambda r: (r["_seen"], r["score"]), reverse=True)
    elif sort == "score":
        rows.sort(key=lambda r: (r["score"], r["_posted"]), reverse=True)
    else:
        rows.sort(key=lambda r: (r["_posted"], r["score"]), reverse=True)
    total = len(rows)
    rows = rows[:300]

    opts = lambda cur, items: "".join('<option value="%s"%s>%s</option>' % (
        e(v), " selected" if v == cur else "", e(l)) for v, l in items)
    base_q = {"status": status, "source": src, "work": wf, "sort": sort, "age": age, "min": str(mn), "q": text}

    def link(**chg):
        d = dict(base_q, **chg)
        return "/?" + urllib.parse.urlencode({k: v for k, v in d.items() if v not in ("", None)})

    def chip(label, cnt, **chg):
        on = all(base_q.get(k) == v for k, v in chg.items())
        return '<a class="fchip%s" href="%s">%s%s</a>' % (" on" if on else "", e(link(**chg)), e(label),
                                                       ("<small>%d</small>" % cnt) if cnt is not None else "")

    # summary tiles (active postings, together with the work-mode filter)
    with db() as con:
        act_rows = [dict(r) for r in con.execute(
            "SELECT posted, first_seen, status FROM jobs WHERE score>=? AND dup_of IS NULL AND status IN ('new','seen','saved')"
            + ((" AND COALESCE(work_mode,'unknown') IN (%s)" % ",".join("?" * len(wf_sql))) if wf_sql else ""),
            [mn] + (wf_sql or []))]
    fresh = sum(1 for r in act_rows if parse_dt(r["posted"]) and days_ago(parse_dt(r["posted"])) <= 2)
    today = sum(1 for r in act_rows if parse_dt(r["first_seen"]) and days_ago(parse_dt(r["first_seen"])) == 0)
    stats = """<div class="stats">
<a class="stat" href="%s"><b>%d</b><span>🆕 fresh (≤2 days old)</span></a>
<a class="stat" href="%s"><b>%d</b><span>📥 collected today</span></a>
<a class="stat" href="%s"><b>%d</b><span>● new, not viewed yet</span></a>
<a class="stat" href="%s"><b>%d</b><span>⭐ saved</span></a>
<a class="stat" href="%s"><b>%d</b><span>✅ applied</span></a></div>""" % (
        e(link(age="3", sort="posted", status="active")), fresh, e(link(age="1", sort="seen", status="active")), today,
        e(link(status="new")), counts.get("new", 0), e(link(status="saved")), counts.get("saved", 0),
        e(link(status="applied")), counts.get("applied", 0))

    st_chips = "".join([chip("Active", None, status="active"), chip("New", counts.get("new", 0), status="new"),
                        chip("Saved", counts.get("saved", 0), status="saved"),
                        chip("Applied", counts.get("applied", 0), status="applied"),
                        chip("Hidden", counts.get("hidden", 0), status="hidden"), chip("All", None, status="all")])
    wm_chips = "".join([chip("🏠 Remote only", wcount.get("remote", 0), work="remote"),
                        chip("Remote + ❔", wcount.get("remote", 0) + wcount.get("unknown", 0), work="remote_unknown"),
                        chip("Remote + hybrid", None, work="remote_hybrid"),
                        chip("🏢🏠 Hybrid", wcount.get("hybrid", 0), work="hybrid"),
                        chip("🏢 On-site", wcount.get("onsite", 0), work="onsite"), chip("All", None, work="all")])
    sort_chips = "".join([chip("📅 Posted", None, sort="posted"), chip("📥 Collected", None, sort="seen"),
                          chip("🎯 Score", None, sort="score")])
    age_chips = "".join([chip("Any time", None, age="all"), chip("24 h", None, age="1"), chip("3 days", None, age="3"),
                         chip("7 days", None, age="7"), chip("30 days", None, age="30")])
    # sources: toggle chips, several can be on at once; "All" clears the choice
    src_chips = '<a class="fchip%s" href="%s">All</a>' % ("" if srcs else " on", e(link(source="")))
    for k, v in SOURCES_INFO.items():
        if not scount.get(k) and k not in srcs:
            continue  # no postings from it: no chip
        picked = sorted(set(srcs) ^ {k})
        src_chips += '<a class="fchip%s" href="%s" aria-pressed="%s">%s<small>%d</small></a>' % (
            " on" if k in srcs else "", e(link(source=",".join(picked))), "true" if k in srcs else "false",
            e(v[0]), scount.get(k, 0))
    hidden_inputs = "".join('<input type="hidden" name="%s" value="%s">' % (k, e(v))
                            for k, v in base_q.items() if k in ("status", "work", "sort", "age", "source") and v)
    more_open = " open" if (text or mn != cfg["min_score"]) else ""
    filt = """<section class="ui-section ui-filters" aria-label="Filters"><div class="ui-body">
<div class="row-chips"><span class="lbl">Status</span>%s</div>
<div class="row-chips"><span class="lbl">Work mode</span>%s</div>
<div class="row-chips"><span class="lbl">Source</span>%s<span class="muted" style="font-size:12.5px">click several to combine</span></div>
<div class="row-chips"><span class="lbl">Sort</span>%s</div>
<div class="row-chips"><span class="lbl">Age</span>%s</div>
<details class="ui-more"%s><summary>More filters: score, search</summary>
<form class="filters" method="get">%s
<label class="ui-inline" style="margin:0;font-weight:400">Min. score <input class="input input-sm" type="number" name="min" value="%d" style="width:80px"></label>
<input class="input input-sm" type="search" name="q" value="%s" placeholder="Search: title, company, location" aria-label="Search" style="width:260px">
<button class="btn btn-sm btn-primary">Filter</button><a class="btn btn-sm btn-ghost" href="/">Reset filters</a></form></details></div></section>""" % (
        st_chips, wm_chips, src_chips, sort_chips, age_chips, more_open, hidden_inputs, mn, e(text))
    head = """<div class="listhead"><span><b>%d</b> posting%s · age refers to <b>%s</b> · notifications from %d pts</span>
<form method="post" action="/mark-all-seen"><button class="btn btn-sm btn-ghost">Mark all new as seen</button></form></div>""" % (
        total, " (first 300 shown)" if total > 300 else "", "collected" if sort == "seen" else "posted",
        cfg["notify_min_score"])

    with db() as con:
        has_cv = {r["job_id"] for r in con.execute("SELECT job_id FROM cvs WHERE status='done'")}
    logos = logo_files()
    cards, cur_bucket = [], None
    for r in rows:
        if sort != "score":
            bk = bucket_of(r[key_days])
            if bk != cur_bucket:
                n_in = sum(1 for x in rows if bucket_of(x[key_days]) == bk)
                cards.append('<div class="bucket">%s <span>· %d</span></div>' % (e(bk), n_in))
                cur_bucket = bk
        sc = r["score"]
        cls = "s-hi" if sc >= cfg["notify_min_score"] else ("s-mid" if sc >= cfg["min_score"] else "")
        qid = e(urllib.parse.quote(r["id"], safe=""))
        chips = [mode_picker(r)]
        if r["location"] and r["location"].strip().lower() not in ("remote", "(remote)", "anywhere"):
            chips.append('<span class="chip">📍 %s</span>' % e(r["location"][:60]))
        if r["_closed"]:
            chips.append('<span class="chip closed">❌ probably closed</span>')
        if r["_posted_known"]:
            cls_age = "age-new" if r["_pdays"] <= 2 else ("age-old" if r["_pdays"] > 30 else "")
            chips.append('<span class="chip %s" title="Posted: %s">📅 %s</span>' % (
                cls_age, e(r["_posted"].astimezone().strftime("%Y-%m-%d")),
                e(("posted " + rel_day(r["_pdays"])) if r["_pdays"] > 2 else "🆕 " + rel_day(r["_pdays"]))))
        else:
            chips.append('<span class="chip" title="The source does not give the posting date">📅 posted date unknown</span>')
        chips.append('<span class="chip" title="Collected: %s">📥 %s</span>' % (e(fmt_dt(r["first_seen"])), e(rel_day(r["_sdays"]))))
        if r["status"] != "new":
            chips.append('<span class="chip st">%s</span>' % e(STATUS_LABEL.get(r["status"], r["status"])))
        if r.get("also_sources"):
            chips.append('<span class="chip" title="The same posting came from these sources too">🔁 also on %s</span>' % e(
                ", ".join(SOURCES_INFO.get(x, (x,))[0] for x in r["also_sources"].split(",") if x)))
        icon_btns = "".join('<button class="btn btn-sm btn-ghost btn-square" name="status" value="%s" title="%s" aria-label="%s">%s</button>'
                            % (s_, t, t, ic) for s_, ic, t in (
            ("saved", "⭐", "Save"), ("applied", "✅", "Applied"), ("seen", "👁", "Mark seen"),
            ("hidden", "🚫", "Hide"), ("new", "↺", "Mark new again")) if s_ != r["status"])
        cv_btn = ('<a class="btn btn-sm btn-outline" href="/cv/%s">Documents ✓</a>' if r["id"] in has_cv else
                  '<a class="btn btn-sm btn-primary" href="/cv/%s">CV + Resume</a>') % qid
        cards.append("""<article class="job st-%s%s"><div class="score %s" title="Score against your CV">%d<small>pts</small></div>
<div><div class="job-head">%s<div><div class="jt"><a href="/open/%s" target="_blank" rel="noopener noreferrer">%s</a></div>
<div class="co"><b>%s</b> <span class="src">· %s</span></div></div></div><div class="chips">%s</div>
<details class="why"><summary>Why this score?</summary>%s</details></div>
<form class="acts" method="post" action="/job/%s">%s<div class="icons">%s</div></form></article>""" % (
            e(r["status"]), " stale" if (r["_closed"] or r["_pdays"] > 30) else "", cls, sc,
            logo_html(r["company"], logos), qid, e(r["title"]), e(r["company"] or "?"),
            e(SOURCES_INFO.get(r["source"], (r["source"],))[0]),
            "".join(chips), e(r["matched"] or "-"), qid, cv_btn, icon_btns))
    if not cards:
        cards.append('<section class="ui-section"><div class="ui-body ui-empty">No postings match the filters. '
                     'Loosen them, or press “Refresh” to fetch new ones.</div></section>')
    busy = bool(STATE.get("progress") or STATE.get("enrich") or STATE["running"])
    top = ('<div class="ui-head"><h1>Jobs</h1><p>Postings from your sources, scored against your CV. '
           'Click a title to open the posting; “CV + Resume” writes documents tailored to it.</p>'
           '<form method="post" action="/add-job" class="join ui-addjob"><input class="input input-sm join-item" type="url" name="url" required '
           'placeholder="Found a posting elsewhere? Paste its link to add it" aria-label="Link of a posting to add">'
           '<button class="btn btn-sm btn-primary btn-outline join-item">Add posting</button></form></div>')
    return page("Jobs", '<div class="ui">%s%s%s%s%s%s</div>' % (top, progress_html(), stats, filt, head, "".join(cards)),
                q.get("msg", ""), refresh=3 if busy else 0)


def run_selection(f):
    """From the Sources form: (source names or None for all, own-source ids). "one" = a single row's button."""
    one = f.get("one", "")
    if one.startswith("cs:"):
        return set(), {one[3:]}
    if one:
        return {one}, set()
    if f.get("all") or "selected" not in f:
        return None, set()
    names = {k for k in FETCHED_SOURCES if ("run_" + k) in f}
    cids = {k[len("run_cs_"):] for k in f if k.startswith("run_cs_")}
    if cids:
        names.discard("custom")  # only the ticked own sources, not all of them
    return (names or (set() if cids else None)), cids


def sources_page(flash=""):
    with db() as con:
        st = {r["source"]: r for r in con.execute("SELECT * FROM source_state")}
        cnt = {r["source"]: r["c"] for r in con.execute("SELECT source, COUNT(*) c FROM jobs GROUP BY source")}
    with CFG_LOCK:
        cfg = load_config()
    busy = bool(STATE["running"])
    rows = []
    for k in FETCHED_SOURCES:
        name, url = SOURCES_INFO[k]
        s = st.get(k)
        on = cfg["sources"].get(k)
        if not on:
            status = '<span class="badge badge-soft badge-neutral">Off</span>'
        elif s and s["error"]:
            status = '<span class="badge badge-soft badge-error">Error</span><div class="ui-err">%s</div>' % e(s["error"][:200])
        else:
            status = ('<span class="badge badge-soft badge-success">OK</span>' if s else
                      '<span class="badge badge-soft badge-neutral">Not run yet</span>')
        tick = ('<input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="run_%s" aria-label="Select %s"%s>'
                % (k, e(name), "" if on else " disabled title=\"Turned off in Settings\""))
        one = ('<button class="btn btn-xs btn-ghost" name="one" value="%s"%s>Refresh</button>'
               % (k, "" if on and not busy else " disabled"))
        rows.append('<tr%s><td>%s</td><td>%s</td><td class="when">%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td><td>%s</td><td>%s</td></tr>' % (
            "" if on else ' class="off"', tick,
            ('<a href="%s" target="_blank" rel="noopener">%s</a>' % (e(url), e(name))) if url else e(name),
            e(fmt_dt(s["last_run"])) if s else "—", s["found"] if s else "—", s["new"] if s else "—", cnt.get(k, 0), status, one))
        if k == "custom":  # each own source can be refreshed on its own
            for src in cfg.get("custom_sources") or []:
                son = on and src.get("enabled", True)
                rows.append('<tr class="sub%s"><td><input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="run_cs_%s" '
                            'aria-label="Select %s"%s></td><td colspan="6">↳ %s <span class="muted">· %s</span></td>'
                            '<td><button class="btn btn-xs btn-ghost" name="one" value="cs:%s"%s>Refresh</button></td></tr>' % (
                                "" if son else " off", e(src["id"]), e(src.get("name") or src["url"]), "" if son else " disabled",
                                e(src.get("name") or src["url"]), e(CUSTOM_TYPES.get(src.get("type"), src.get("type"))),
                                e(src["id"]), "" if son and not busy else " disabled"))
    uns = "".join('<tr><td>%s</td><td class="muted">%s</td></tr>' % (
        ('<a href="%s" target="_blank" rel="noopener">%s ↗</a>' % (e(u), e(n))) if u else e(n), e(why)) for n, u, why in UNSUPPORTED)
    body = """<div class="ui"><div class="ui-head"><h1>Sources</h1><p>Where postings come from, and how the last refresh went.
Sources are turned on and off in <a href="/settings#search">Settings → Job search</a>.</p></div>%s
<section class="ui-section" id="refresh"><header><div><h2>Watched sources</h2><p class="ui-desc">Refreshed automatically every %d minutes
(Remotive at most every 6 hours). To fetch now, tick the sources you want, or use a row's Refresh button.</p></div></header>
<form method="post" action="/run"><div style="overflow-x:auto"><table class="table ui-table"><thead><tr><th style="width:36px">
<input type="checkbox" class="checkbox checkbox-sm" aria-label="Select all" onclick="document.querySelectorAll('#refresh tbody input[type=checkbox]:not(:disabled)').forEach(function (c) { c.checked = this.checked; }, this)"></th>
<th>Source</th><th>Last run</th><th class="num">Fetched</th><th class="num">New</th><th class="num">In the list</th><th>Status</th><th></th></tr></thead>
<tbody>%s</tbody></table></div>
<div class="ui-foot"><span class="muted" style="font-size:13px">%s</span><div class="ui-btns">
<button class="btn btn-sm btn-outline" name="all" value="1"%s>Refresh all</button>
<button class="btn btn-sm btn-primary" name="selected" value="1"%s>Refresh selected</button></div></div></form></section>
<section class="ui-section"><header><div><h2>Sites that cannot be watched automatically</h2><p class="ui-desc">Check these by hand now and then.</p></div></header>
<div style="overflow-x:auto"><table class="table ui-table"><thead><tr><th>Site</th><th>Why</th></tr></thead><tbody>%s</tbody></table></div></section></div>""" % (
        progress_html(), cfg["interval_minutes"], "".join(rows),
        "A refresh is running…" if busy else "Only the sources that are turned on can be selected.",
        " disabled" if busy else "", " disabled" if busy else "", uns)
    return page("Sources", body, flash, refresh=3 if busy else 0)


def parse_chips(text):
    """One chip per line, in order; duplicates (case-insensitive) dropped."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        c = re.sub(r"\s+", " ", line).strip().strip(",")
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def kv_text(d):
    return "\n".join("%s = %s" % (k, v) for k, v in d.items())


def parse_kv(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.rsplit("=", 1)
            try:
                out[k.strip().lower()] = int(v.strip())
            except ValueError:
                pass
    return out


def custom_source_fields(i, src):
    """One editable own-source block of the Settings form (i = index, "new" for the empty one)."""
    src = src or {}
    opts = "".join('<option value="%s"%s>%s</option>' % (k, " selected" if src.get("type", "vmp") == k else "", e(v))
                   for k, v in CUSTOM_TYPES.items())
    f = lambda name, label, val, extra="", wide=False: (
        '<div%s><label for="cs_%s_%s">%s</label><input class="input" id="cs_%s_%s" name="cs_%s_%s" value="%s"%s></div>'
        % (' class="wide"' if wide else "", i, name, label, i, name, i, name, e(val), extra))
    if i == "new":
        head = '<div class="ui-subhead"><b>Add a new source</b><span class="muted">Fill in the URL to add it; it is saved with the page.</span></div>'
    else:
        head = ('<div class="ui-subhead"><b>%s</b><span class="ui-checks">'
                '<label><input type="checkbox" class="toggle toggle-sm toggle-primary" name="cs_%s_enabled"%s> Enabled</label>'
                '<label><input type="checkbox" class="checkbox checkbox-sm checkbox-error" name="cs_%s_delete"> Delete on save</label></span></div>'
                % (e(src.get("name") or src.get("url", "")), i, " checked" if src.get("enabled", True) else "", i))
    return ('<div class="ui-sub">%s<input type="hidden" name="cs_%s_id" value="%s"><div class="ui-grid">'
            '<div><label for="cs_%s_type">Type</label><select class="select" id="cs_%s_type" name="cs_%s_type">%s</select></div>%s%s%s'
            '<div class="wide"><span class="help">Login (only if the site needs it):</span></div>%s%s'
            '<div><label for="cs_%s_password">Password %s</label><input class="input" type="password" id="cs_%s_password" name="cs_%s_password" '
            'autocomplete="new-password" placeholder="Leave empty to keep"></div></div></div>') % (
        head, i, e(src.get("id", "")), i, i, i, opts,
        f("name", "Name", src.get("name", ""), ' placeholder="e.g. munka.hu"'),
        f("url", "URL: search result page, web page or feed", src.get("url", ""),
          ' placeholder="https://vmp.munka.hu/allas/talalatok?kulcsszo=…"', wide=True),
        f("pattern", "Link pattern (only for “any web page”)", src.get("pattern", ""), ' placeholder="/job/"'),
        f("login_url", "Login page (empty = the default for VMP)", src.get("login_url", "")),
        f("username", "Username", src.get("username", ""), ' autocomplete="off"'),
        i, '<span class="badge badge-soft badge-success">set</span>' if src.get("password") else "", i, i)


def custom_sources_card(cfg):
    blocks = "".join(custom_source_fields(i, s) for i, s in enumerate(cfg.get("custom_sources") or []))
    return ('<p class="ui-lead"><b>munka.hu VMP</b>: search on <a href="https://vmp.munka.hu/allas/egyszeru_kereses" target="_blank" '
            'rel="noopener">vmp.munka.hu</a> and paste the address of the result page; with your VMP login the employer is shown too. '
            '<b>Any web page</b>: every link whose address contains the pattern becomes a posting. Passwords stay on this machine.</p>'
            '%s%s<input type="hidden" name="cs_count" value="%d">') % (
        blocks, custom_source_fields("new", None), len(cfg.get("custom_sources") or []))


def parse_custom_sources(f, old):
    """Rebuilds cfg["custom_sources"] from the Settings form. An empty password keeps the stored one."""
    by_id = {s.get("id"): s for s in old or []}
    out = []
    try:
        count = int(f.get("cs_count", "0") or 0)
    except ValueError:
        count = 0
    for i in list(range(count)) + ["new"]:
        g = lambda k: (f.get("cs_%s_%s" % (i, k)) or "").strip()
        if i != "new" and "cs_%s_delete" % i in f:
            continue
        if not g("url"):
            continue
        prev = by_id.get(g("id")) or {}
        src = {"id": prev.get("id") or secrets.token_hex(4),
               "enabled": True if i == "new" else ("cs_%s_enabled" % i) in f,
               "type": g("type") if g("type") in CUSTOM_TYPES else "page",
               "name": g("name") or urllib.parse.urlparse(g("url")).netloc,
               "url": g("url"), "pattern": g("pattern"), "login_url": g("login_url"),
               "username": g("username"),
               "password": f.get("cs_%s_password" % i) or prev.get("password", "")}
        if not src["username"]:
            src["password"] = ""
        out.append(src)
    return out


def test_custom_sources(cfg):
    """Runs every own source once (no saving). Returns a short report line per source."""
    lines = []
    for src in cfg.get("custom_sources") or []:
        name = src.get("name") or src.get("type")
        try:
            items = run_custom_source(src, is_new=lambda ext_id: False)
            sample = "; ".join("%s (%s)" % (it["title"], it["company"] or it["location"] or "-") for it in items[:3])
            lines.append("%s: %d posting(s)%s%s" % (name, len(items), " – logged in" if src.get("username") else "",
                                                     (" – e.g. " + sample) if sample else ""))
        except Exception as ex:
            lines.append("%s: ERROR %s" % (name, ex))
    return " | ".join(lines) or "No own sources yet."


def ui_field(name, label, value="", kind="text", help_="", extra="", wide=False):
    """A labelled input for the ui-grid."""
    return '<div%s><label for="%s">%s</label><input class="input" id="%s" name="%s" type="%s" value="%s"%s>%s</div>' % (
        ' class="wide"' if wide else "", name, label, name, name, kind, e(value), extra,
        ('<span class="help">%s</span>' % help_) if help_ else "")


def ui_toggle(name, label, on, help_=""):
    return ('<div class="wide"><label class="ui-toggle"><input type="checkbox" class="toggle toggle-primary" name="%s"%s> %s</label>%s</div>'
            % (name, " checked" if on else "", label, ('<span class="help">%s</span>' % help_) if help_ else ""))


def ui_sec(anchor, title, desc, inner, foot=""):
    return ('<section class="ui-section" id="%s" aria-labelledby="%s-h"><header><div><h2 id="%s-h">%s</h2>%s</div></header>'
            '<div class="ui-body">%s</div>%s</section>') % (
        anchor, anchor, anchor, title, ('<p class="ui-desc">%s</p>' % desc) if desc else "", inner,
        ('<div class="ui-foot">%s</div>' % foot) if foot else "")


SETTINGS_NAV = (("presets", "Presets"), ("notify", "Notifications"), ("search", "Job search"), ("own", "Own sources"), ("mail", "E-mail alerts"), ("scoring", "Scoring"),
                ("claude", "Claude API"), ("general", "General"), ("danger", "Maintenance"))


def settings_page(flash=""):
    with CFG_LOCK:
        cfg = load_config()
    n, m = cfg["ntfy"], cfg["email"]
    chk = lambda b: " checked" if b else ""
    sub = "%s/%s" % (n["server"].rstrip("/"), n["topic"])
    src_boxes = "".join('<label><input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="src_%s"%s> %s</label>' % (
        k, chk(cfg["sources"].get(k)), e(v[0])) for k, v in SOURCES_INFO.items() if k in FETCHED_SOURCES)
    modes = "".join('<label><input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="nwm_%s"%s> %s</label>' % (
        k, chk(k in cfg.get("notify_work_modes", ["remote", "unknown"])), e(v)) for k, v in WORK_MODES.items())
    filt = "".join('<option value="%s"%s>%s</option>' % (k, " selected" if k == cfg.get("default_work_filter", "remote_unknown") else "", l)
                   for k, l in (("remote", "Remote only"), ("remote_unknown", "Remote + unknown"),
                                ("remote_hybrid", "Remote + hybrid"), ("all", "All")))
    filt_boxes = "".join('<label><input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="sf_%s"%s> %s</label>' % (
        k, chk((cfg.get("search_filter") or {}).get(k)), e(SOURCES_INFO[k][0])) for k in SEARCH_FILTERABLE)
    set_badge = lambda on: '<span class="badge badge-soft badge-success">set</span>' if on else ""

    def ta(name, label, val, rows, help_="", wide=True):
        return '<div%s><label for="%s">%s</label><textarea class="textarea ui-mono" id="%s" name="%s" rows="%d">%s</textarea>%s</div>' % (
            ' class="wide"' if wide else "", name, label, name, name, rows, e(val),
            ('<span class="help">%s</span>' % help_) if help_ else "")

    def test(what, label):
        return '<button class="btn btn-sm btn-outline" formaction="/test" name="what" value="%s">%s</button>' % (what, label)

    notify = ui_sec("notify", "Notifications", "Where new, relevant postings are announced.",
        '<h3 class="ui-h3">Push notifications (ntfy)</h3><div class="ui-grid">'
        + ui_toggle("ntfy_enabled", "Send push notifications", n.get("enabled"))
        + '<div class="wide"><ol class="ui-steps"><li>Install the <b>ntfy</b> app on your phone (App Store / Play Store).</li>'
          '<li>In the app: “+” → Subscribe to topic → <code>%s</code> (server: %s).</li>'
          '<li>Press “Save and send a test push” below.</li></ol>'
          '<span class="help">The topic name works as a password: anyone who has it can read the notifications. '
          'Subscribe link: <a href="%s" target="_blank" rel="noopener">%s</a></span></div>' % (e(n["topic"]), e(n["server"]), e(sub), e(sub))
        + ui_field("ntfy_server", "Server", n["server"]) + ui_field("ntfy_topic", "Topic", n["topic"])
        + '</div><h3 class="ui-h3">E-mail</h3><div class="ui-grid">'
        + ui_toggle("email_enabled", "Send e-mail notifications", m.get("enabled"),
                    "Gmail needs an <b>app password</b> (Google account → Security → 2-Step Verification → App passwords), "
                    "not your normal password.")
        + ui_field("smtp_host", "SMTP server", m["smtp_host"]) + ui_field("smtp_port", "Port", m["smtp_port"], "number")
        + ui_field("smtp_user", "User (e-mail address)", m["username"], "email")
        + '<div><label for="smtp_pass">Password %s</label><input class="input" id="smtp_pass" type="password" name="smtp_pass" '
          'placeholder="Leave empty to keep" autocomplete="new-password"></div>' % set_badge(m.get("password"))
        + ui_field("email_to", "Recipient", m["to"], "email", "Empty: the sender's own address.")
        + '</div><h3 class="ui-h3">When to notify</h3><div class="ui-grid">'
        + ui_field("notify_min_score", "Minimum score", cfg["notify_min_score"], "number",
                   "A posting with at least this many points is announced.")
        + '<div><label>Work modes</label><div class="ui-checks">%s</div>'
          '<span class="help">No notification for the unticked ones.</span></div></div>' % modes,
        test("ntfy", "Save and send a test push") + test("email", "Save and send a test e-mail"))

    search = ui_sec("search", "Job search", "Which sites are searched, and how often.",
        '<div class="ui-grid"><div class="wide"><label>Sources</label><div class="ui-checks">%s</div></div>' % src_boxes
        + ui_field("interval", "Refresh every (minutes)", cfg["interval_minutes"], "number", "At least 15.", ' min="15"')
        + '<div><label>Work mode (remote / hybrid / on-site)</label><label class="ui-toggle" style="font-weight:400"><input type="checkbox" '
          'class="toggle toggle-sm toggle-primary" name="mode_claude"%s> Claude reads the postings the rules cannot settle</label>'
          '<span class="help">First the job board\'s own data and the posting text (in many languages) decide; Claude only reads '
          'what is still unknown, about ¼ cent per posting. You can always set it by hand on the card.</span></div>' % chk(cfg.get("mode_claude", True))
        + ta("search_terms", "What to search for", "\n".join(cfg.get("search_terms") or []), 7,
             "One per line: keywords, optionally <code>| location</code> (e.g. <code>unity | Hungary</code>). Used by every "
             "source: LinkedIn, Remotive and Flexa search with them, and the sites ticked below keep only the postings "
             "that contain every word of a line. LinkedIn: postings from the last 7 days.")
        + ta("search_locations", "Default locations (LinkedIn)", "\n".join(cfg.get("search_locations") or []), 3,
             "For the lines without a location, one per line: e.g. <code>European Union</code>, <code>Hungary</code>, "
             "<code>Worldwide</code>. Each such line is searched in every one, so keep the list short.", wide=False)
        + '<div><label>Keep only matching postings from</label><div class="ui-checks">%s</div><span class="help">These '
          'sites list all their jobs. Untick a hand-picked list (e.g. game studios) whose job titles vary; the '
          'scoring still ranks those.</span></div>' % filt_boxes
        + ta("studios", "Company job boards", "\n".join(cfg.get("studios") or []), 6,
             "One per line: <code>ashby|lever|greenhouse|bamboohr|smartrecruiters | id | name</code>.")
        + ta("rss_feeds", "RSS feeds", "\n".join(cfg.get("rss_feeds") or []), 3, "One URL per line.") + '</div>')

    own = ui_sec("own", "Own sources", "Any other job site, with a login if it needs one.",
                 custom_sources_card(cfg), test("custom", "Save and test own sources"))

    ms = cfg.get("mail_source") or {}
    mail = ui_sec("mail", "E-mail alerts", "For sites that only send their jobs by e-mail or need a login you cannot "
                  "automate (Jack &amp; Jill, LinkedIn job alerts, Indeed, Glassdoor…): jobwatch reads those e-mails from your "
                  "mailbox. It only reads: the mailbox is opened read-only, nothing is marked as read, moved or deleted. "
                  "Turn the source on under Job search → Sources.",
        '<div class="ui-grid">'
        + ta("mail_senders", "Senders to read", "\n".join(ms.get("senders") or []), 4,
             "One per line: an address or just the domain, e.g. <code>jackandjill.ai</code> or "
             "<code>jobalerts-noreply@linkedin.com</code>.")
        + ui_field("mail_days", "Look back (days)", ms.get("days", 7), "number", "Only e-mails from the last days.", ' min="1" max="60"')
        + '<div><label>Reading the e-mails</label><label class="ui-toggle" style="font-weight:400"><input type="checkbox" '
          'class="toggle toggle-sm toggle-primary" name="mail_use_claude"%s> Claude reads them</label><span class="help">More accurate '
          '(title, company, location) in any format; about 1 cent per e-mail, every e-mail once. Off: only the links that look like '
          'postings, free.</span></div>' % chk(ms.get("use_claude", True))
        + '<div class="wide"><span class="help">Mailbox: by default the e-mail account under Notifications (for Gmail the same app '
          'password works; IMAP is on by default). Fill these in only for another mailbox.</span></div>'
        + ui_field("mail_imap_host", "IMAP server", ms.get("imap_host", "imap.gmail.com"))
        + ui_field("mail_imap_port", "Port", ms.get("imap_port", 993), "number")
        + ui_field("mail_folder", "Folder", ms.get("folder", "INBOX"), help_="For Gmail labels: the label name.")
        + ui_field("mail_user", "User (empty = the e-mail account)", ms.get("username", ""), "email")
        + '<div><label for="mail_pass">Password %s</label><input class="input" id="mail_pass" type="password" name="mail_pass" '
          'placeholder="Empty = the e-mail account\'s" autocomplete="new-password"></div></div>' % set_badge(ms.get("password")),
        test("mail", "Save and test e-mail alerts"))
    own += mail

    mine, tpls, active = sorted(cfg.get("presets") or {}), template_presets(), cfg.get("active_preset") or ""
    sel = lambda v: " selected" if v == active else ""
    opts = (('<option value=""%s>None (custom settings)</option>' % ("" if active in mine or active[4:] in tpls else " selected"))
            + ('<optgroup label="My presets">%s</optgroup>' % "".join('<option value="%s"%s>%s</option>' % (
                e(n), sel(n), e(n)) for n in mine) if mine else "")
            + '<optgroup label="Templates (read-only)">%s</optgroup>' % "".join(
                '<option value="tpl:%s"%s>%s</option>' % (e(n), sel("tpl:" + n), e(n)) for n in tpls))
    pdata = dict({n: (cfg.get("presets") or {})[n] for n in mine}, **{"tpl:" + n: t for n, t in tpls.items()})
    pbtn = lambda act, label, cls="": ('<button class="btn btn-sm %s" formaction="/preset" name="preset_do" value="%s"%s>%s</button>'
                                       % (cls or "btn-outline", act, " formnovalidate" if act != "save" else "", label))
    presets = ui_sec("presets", "Presets", "A preset is what you are looking for: the search terms, locations, filters, the "
                     "scoring chips and the minimum score, saved under a name, e.g. to try game designer or other roles. "
                     "Picking one only fills in the settings below; nothing changes until you save. Saving the settings "
                     "also updates the active preset.",
        '<p class="ui-lead" id="preset_state" aria-live="polite">In use: <b>%s</b></p><div class="ui-grid"><div>'
        '<label for="preset_pick">Preset</label><select class="select" id="preset_pick" name="preset_pick">%s</select>'
        '<input type="hidden" name="active_preset_set" id="active_preset_set" value="%s">'
        '<div class="ui-inline">%s</div><span class="help">Picking one fills in Job search and Scoring below. Review it, '
        'then Save changes; leaving without saving asks first.</span></div>'
        '<div><label for="preset_name">Name for changed settings</label><input class="input" id="preset_name" '
        'name="preset_name" maxlength="60" placeholder="%s"><div class="ui-inline">%s</div>'
        '<span class="help">If you change the settings of a preset and save, they are kept as a new preset under this '
        'name (empty: “%s”); the picked preset stays as it was. Type an existing name to update that one.</span></div></div>'
        '<script type="application/json" id="preset_data">%s</script>' % (
            e(preset_label(active)) if active else "custom settings (no preset)", opts, e(active),
            pbtn("delete", "Delete this preset", "btn-ghost text-error") if mine else "", e(NEW_PRESET),
            pbtn("rename", "Rename the preset in use") if active in mine else "", e(NEW_PRESET),
            json.dumps(pdata, ensure_ascii=False).replace("</", "<\\/")))
    sc = cfg.get("scoring") or {}
    chips = lambda kind, label, help_: (
        '<div class="wide"><label for="sc_%s">%s</label><textarea class="textarea ui-mono chips-src" id="sc_%s" name="sc_%s" '
        'rows="4" data-kind="%s">%s</textarea><span class="help">%s</span></div>' % (
            kind, label, kind, kind, kind, e("\n".join(sc.get(kind) or [])), help_))
    scoring = ui_sec("scoring", "Scoring", "Tell jobwatch what interests you, most important first: the order is the score. "
                     "Drag a chip or use its arrows to move it; Enter adds one. <code>unity / unity3d</code> counts as one "
                     "(words that mean the same). Saving re-scores every posting.",
        '<div class="ui-grid">'
        + chips("interests", "What interests me (job title)", "By position: +6, +5, +4, +4, +3, +3, +2, +2, +2, then +1 each.")
        + chips("desc", "Bonus if the description mentions it", "+3, +2, then +1 each; at most +%d together. Not counted "
                "again when the title already has it." % DESC_MAX)
        + chips("less", "Less relevant (job title)", "&minus;3 each: still listed when the rest fits well.")
        + chips("never", "Not for me (job title)", "&minus;8 each: hides the posting.")
        + chips("loc_good", "Preferred locations", "+3, +2, then +1 each; only for postings whose title fits (4+ points).")
        + chips("loc_bad", "Locations to avoid", "&minus;5 each.")
        + '<div class="wide"><label for="score_try">Try a job title</label><input class="input" id="score_try" type="text" '
          'placeholder="e.g. Senior Unity Developer" autocomplete="off"><span class="help" id="score_try_out" aria-live="polite">'
          'The title part of the score, with the chips above (also before saving).</span></div>'
        + ui_field("min_score", "Minimum score in the list", cfg["min_score"], "number", "Postings below this are hidden by default.")
        + '<div><label for="default_work_filter">Default list filter</label><select class="select" id="default_work_filter" '
          'name="default_work_filter">%s</select></div></div>' % filt)

    claude = ui_sec("claude", "Claude API", "Used for tailored CVs, cover letters and recruiter messages. "
                    "A CV + cover letter costs about 5–15 cents, a recruiter message 1–3 cents. The key stays on this machine.",
        '<div class="ui-grid"><div class="wide"><label for="anthropic_api_key">Anthropic API key %s</label>'
        '<input class="input" id="anthropic_api_key" type="password" name="anthropic_api_key" placeholder="Leave empty to keep" '
        'autocomplete="new-password"><span class="help">Create one at <a href="https://console.anthropic.com/settings/keys" '
        'target="_blank" rel="noopener">console.anthropic.com → API Keys</a>.</span></div></div>' % set_badge(cfg.get("anthropic_api_key")))

    general = ui_sec("general", "General", "",
        '<div class="ui-grid">' + ui_field("public_url", "Web address of this app", cfg.get("public_url", ""), "url",
                                           "Used for the link in notifications.", wide=True) + '</div>')

    confirm = ("return confirm('Delete all found jobs and the source status? "
               "Settings, profile, CVs and documents are kept.')")
    danger = ui_sec("danger", "Maintenance", "",
        '<form method="post" action="/reset-jobs" class="ui-danger" onsubmit="%s"><div><b>Reset the job cache</b>'
        '<span class="help">Deletes the found postings; settings, profile, CVs and documents are kept. '
        'The next refresh starts from scratch.</span></div>'
        '<button class="btn btn-sm btn-error btn-outline" name="reset" value="1">Reset job cache</button></form>' % confirm)

    nav = "".join('<a href="#%s">%s</a>' % (a, l) for a, l in SETTINGS_NAV)
    body = ('<div class="ui"><div class="ui-head"><h1>Settings</h1><p>Notifications, job sources, scoring and API keys. '
            'Changes take effect after you save.</p></div>'
            '<div class="ui-layout"><nav class="ui-side" aria-label="Settings sections">%s</nav><div>'
            '<form method="post" action="/settings" id="settings">%s%s%s%s%s%s%s'
            '<input type="hidden" name="next" id="settings_next" value="">'
            '<div class="ui-savebar"><span class="muted" id="savebar_note" aria-live="polite">Saving re-scores every posting.</span>'
            '<button class="btn btn-primary" id="settings_save">Save changes</button></div></form>%s</div></div></div>'
            '<dialog id="leave_dlg" class="modal" aria-labelledby="leave_h"><div class="modal-box"><h3 id="leave_h" '
            'class="text-lg font-bold">Save your changes?</h3><p id="leave_what" class="py-2"></p><p id="leave_search" '
            'class="text-sm opacity-80" hidden>The search changed: saving clears the postings found so far and searches again '
            'with the new settings (%s).</p><div class="modal-action"><button type="button" class="btn btn-ghost" '
            'id="leave_stay">Stay here</button><button type="button" class="btn btn-outline" id="leave_discard">Don\'t save'
            '</button><button type="button" class="btn btn-primary" id="leave_save">Save</button></div></div>'
            '<form method="dialog" class="modal-backdrop"><button>close</button></form></dialog>') % (
        nav, presets, notify, search, own, scoring, claude, general, danger, e(KEPT_ON_NEW_SEARCH))
    return page("Settings", body + CHIPS_JS % json.dumps(
        {k: {"t": list(t), "rest": r} for k, (t, r) in SCORING_KINDS.items()}) + LEAVE_JS, flash)


def save_settings(f):
    g = lambda k, d="": f.get(k, d)
    with CFG_LOCK:
        cfg = load_config()
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
        save_config(cfg)
        changed = search_signature(cfg) != before
    rescore_all()
    if changed:
        search_again()
    return SaveResult(changed, note)


def saved_message(res):
    return (("Saved. The search changed, so the postings found so far were cleared (%s) and a new search started: "
             "the list fills up again in a few minutes." % KEPT_ON_NEW_SEARCH) if res else "Saved, postings re-scored.") + (
        " " + res.preset_note if getattr(res, "preset_note", "") else "")


# ---------------------------------------------------------------- CV
MAX_PDF_BYTES = 10 * 1024 * 1024


def pdf_from_multipart(content_type, body):
    """Extracts the "pdf" field from a multipart/form-data body. Empty bytes if missing."""
    from email.parser import BytesParser
    from email.policy import default
    if not content_type or "multipart/form-data" not in content_type:
        return b""
    msg = BytesParser(policy=default).parsebytes(b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body)
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == "pdf":
            return part.get_payload(decode=True) or b""
    return b""


def pdf_text(data):
    """Reads the text of a PDF (pypdf). A scanned PDF saved as an image has no text."""
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if not text:
        raise ValueError("No text can be read from the PDF (probably a scanned image). Paste the text instead.")
    return text


def profile_from_cv_text(text):
    """Builds the profile JSON from the CV text with the Claude API. On error raises ValueError with a user message."""
    import cvgen
    with CFG_LOCK:
        cfg = load_config()
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    try:
        prof = cvgen.profile_from_text(key, text)
    except cvgen.CVError as ex:
        raise ValueError(str(ex))
    return json.dumps(prof, ensure_ascii=False, indent=2)


def load_profile():
    import cvgen
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return copy.deepcopy(cvgen.DEFAULT_PROFILE)


import unicodedata


def owner_prefix(prof=None):
    """File-name prefix from the profile name, converted to ASCII: "Zoé Ünal" -> "Zoe_Unal"."""
    name = (prof or load_profile()).get("name") or "Candidate"
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", ascii_name)
    return "_".join(words) or "Candidate"


def doc_names(prof=None):
    p = owner_prefix(prof)
    return {"cv": p + "_CV.pdf", "resume": p + "_Resume.pdf", "letter": p + "_Cover_Letter.pdf"}
DOC_LABEL = {"cv": "CV (detailed)", "resume": "Resume (1 page)", "letter": "Cover letter"}


def safe_name(x, n=70):
    x = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", x or "").strip().strip(".")
    return re.sub(r"\s+", " ", x)[:n].strip() or "unknown"


def job_doc_dir(job):
    short = re.sub(r"[^A-Za-z0-9]+", "", job["id"].split(":", 1)[-1])[-10:]
    return os.path.join(DOCS_ROOT, safe_name(job["company"] or "Unknown company"), "%s [%s]" % (safe_name(job["title"], 80), short))


def cv_paths(jid):
    with db() as con:
        r = con.execute("SELECT doc_dir FROM cvs WHERE job_id=?", (jid,)).fetchone()
    d = r["doc_dir"] if r and r["doc_dir"] else None
    return {k: (os.path.join(d, v) if d else None) for k, v in doc_names().items()}


def cv_filename(job, kind):
    base = re.sub(r"[^A-Za-z0-9]+", "_", (job["company"] or "ceg")).strip("_")[:40]
    return "%s_%s_%s.pdf" % (owner_prefix(), {"cv": "CV", "resume": "Resume", "letter": "Cover_Letter"}[kind], base)


def write_job_info(d, job, data, cvrow=None):
    info = {"job_id": job["id"], "title": job["title"], "company": job["company"], "location": job["location"],
            "url": job["url"], "work_mode": job.get("work_mode"), "score": job["score"], "posted": job["posted"],
            "generated": now_iso(), "model": data.get("_model"), "photo": data.get("_photo", True),
            "language": data.get("language"), "match_notes": data.get("match_notes", []),
            "sent_to": cvrow["sent_to"] if cvrow else None, "sent_at": cvrow["sent_at"] if cvrow else None}
    with open(os.path.join(d, "info.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2)


def render_cv_files(jid, data, archive=False):
    """CV + Resume + cover letter into the company folder. archive=True: the previous version goes to korabbi/<time>/."""
    import cvgen
    with db() as con:
        job = dict(con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        cvrow = con.execute("SELECT * FROM cvs WHERE job_id=?", (jid,)).fetchone()
    d = job_doc_dir(job)
    os.makedirs(d, exist_ok=True)
    names = doc_names(load_profile())
    if archive and any(os.path.exists(os.path.join(d, v)) for v in names.values()):
        old = os.path.join(d, "korabbi", datetime.now().strftime("%Y-%m-%d_%H%M"))
        os.makedirs(old, exist_ok=True)
        for fn in list(names.values()) + ["Cover_Letter.txt", "info.json"]:
            if os.path.exists(os.path.join(d, fn)):
                shutil.move(os.path.join(d, fn), os.path.join(old, fn))
    prof = load_profile()
    photo = PHOTO_PATH if data.get("_photo", True) else None
    cvgen.render_cv_pdf(os.path.join(d, names["cv"]), prof, data, FONT_DIR, photo)
    cvgen.render_resume_pdf(os.path.join(d, names["resume"]), prof, data, FONT_DIR, photo)
    cvgen.render_letter_pdf(os.path.join(d, names["letter"]), prof, data, FONT_DIR)
    with open(os.path.join(d, "Cover_Letter.txt"), "w", encoding="utf-8") as fh:
        fh.write(data.get("cover_letter", ""))
    if cvrow and cvrow["description"]:
        with open(os.path.join(d, "hirdetes.txt"), "w", encoding="utf-8") as fh:
            fh.write("%s\n%s · %s\n%s\n\n%s" % (job["title"], job["company"], job["location"] or "", job["url"], cvrow["description"]))
    write_job_info(d, job, data, cvrow)
    with db() as con:
        con.execute("UPDATE cvs SET doc_dir=? WHERE job_id=?", (d, jid))
    return d


def cv_generate(jid, description):
    import cvgen
    with CFG_LOCK:
        cfg = load_config()
    key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    with db() as con:
        job = dict(con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
    try:
        if not key:
            raise cvgen.CVError("No Anthropic API key (Settings → Claude API).")
        data = cvgen.tailor(key, load_profile(), job, description)
        data["_photo"] = cvgen.photo_default(job)
        render_cv_files(jid, data, archive=True)
        with db() as con:
            con.execute("UPDATE cvs SET status='done', error=NULL, data=?, created=? WHERE job_id=?",
                        (json.dumps(data, ensure_ascii=False), now_iso(), jid))
    except Exception as ex:
        log.exception("CV generation error")
        with db() as con:
            con.execute("UPDATE cvs SET status='error', error=? WHERE job_id=?", (str(ex), jid))


def backfill_docs():
    """Moves old generations (from before the document folder existed) into the per-company folder, with the Resume. No API call."""
    with db() as con:
        rows = con.execute("SELECT job_id, data FROM cvs WHERE status='done' AND (doc_dir IS NULL OR doc_dir='')").fetchall()
    for r in rows:
        try:
            d = render_cv_files(r["job_id"], json.loads(r["data"]))
            log.info("Documents moved: %s", d)
        except Exception:
            log.exception("Document move error: %s", r["job_id"])


def smb_path(d):
    if SMB_BASE and SMB_PREFIX and d and d.startswith(SMB_PREFIX):
        return SMB_BASE + d[len(SMB_PREFIX):]
    return d


# ---------------------------------------------------------------- recruiter outreach
def parse_recruiters(text):
    """One recruiter per line: "Name | title @ company | LinkedIn profile URL or e-mail" (only the name is required)."""
    out = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if not parts[0]:
            continue
        contact = next((p for p in parts[1:] if p.startswith("http") or re.match(r"^[^@\s]+@[^@\s]+\.\w+$", p)), "")
        title = next((p for p in parts[1:] if p and p != contact), "")
        out.append({"name": parts[0][:120], "title": title[:200], "contact": contact[:300]})
    return out


def outreach_generate(oid):
    """Background job: writes the message for one outreach row with the Claude API."""
    import cvgen
    try:
        with CFG_LOCK:
            cfg = load_config()
        key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise cvgen.CVError("No Anthropic API key (Settings → Claude API).")
        with db() as con:
            r = dict(con.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone())
            job = con.execute("SELECT * FROM jobs WHERE id=?", (r["job_id"],)).fetchone() if r["job_id"] else None
        if r.get("role_ref") and (not r.get("role_text") or ROLE_UNREAD in r["role_text"]):  # a rewrite reuses it
            r["role_text"] = resolve_role(r["role_ref"])
            with db() as con:
                con.execute("UPDATE outreach SET role_text=? WHERE id=?", (r["role_text"], oid))
        data = cvgen.outreach_message(key, load_profile(), r, dict(job) if job else None,
                                      r["channel"], r["language"], r["note"] or "")
        with db() as con:
            con.execute("UPDATE outreach SET status='draft', error=NULL, subject=?, message=?, model=? WHERE id=?",
                        (data["subject"], data["message"], data["_model"], oid))
    except Exception as ex:
        log.exception("Outreach message %s", oid)
        with db() as con:
            con.execute("UPDATE outreach SET status='error', error=? WHERE id=?", (str(ex)[:500], oid))


def outreach_signature(prof):
    c = prof.get("contact") or {}
    return " · ".join(x for x in (c.get("phone"), c.get("email"), c.get("linkedin"), c.get("portfolio")) if x)


OUTREACH_CSS = ""  # the styles are in CSS (shared ui-* blocks + rc-* for this page)

CHIPS_JS = """<script>(function () {
var P = %s;
function pts(k, i) { var p = P[k]; return i < p.t.length ? p.t[i] : p.rest; }
function fmt(n) { return (n >= 0 ? "+" : "\u2212") + Math.abs(n); }
function alts(c) { return c.toLowerCase().split(/\\s+\\/\\s+/).map(function (s) { return s.trim(); }).filter(Boolean); }
function esc(s) { return s.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&"); }
function hit(as, txt) {
  for (var i = 0; i < as.length; i++)
    if (new RegExp("(?<![a-z0-9])" + esc(as[i]) + "(?![a-z0-9#+])").test(txt)) return as[i];
  return null;
}
document.querySelectorAll("textarea.chips-src").forEach(function (ta) {
  var k = ta.dataset.kind, ordered = P[k].t.length > 0, drag = null;
  var items = ta.value.split("\\n").map(function (s) { return s.trim(); }).filter(Boolean);
  var box = document.createElement("div"), inp = document.createElement("input");
  box.className = "chips"; box.setAttribute("role", "list");
  inp.type = "text"; inp.id = ta.id + "_in"; inp.placeholder = "Add… (Enter)"; inp.autocomplete = "off";
  var lab = document.querySelector('label[for="' + ta.id + '"]');
  if (lab) { lab.htmlFor = inp.id; inp.setAttribute("aria-label", "Add to: " + lab.textContent); }
  ta.hidden = true; ta.after(box);
  function sync(focus) {
    ta.value = items.join("\\n"); render();
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
        if (i > 0) c.append(button("Move " + t + " up", "\u2039", function () {
          items.splice(i - 1, 0, items.splice(i, 1)[0]); sync(".chip:nth-child(" + i + ") button"); }));
        if (i < items.length - 1) c.append(button("Move " + t + " down", "\u203a", function () {
          items.splice(i + 1, 0, items.splice(i, 1)[0]); sync(".chip:nth-child(" + (i + 2) + ") button:last-of-type"); }));
      }
      c.append(button("Remove " + t, "\u00d7", function () { items.splice(i, 1); sync("input"); }));
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
function lines(k) { var t = document.getElementById("sc_" + k); return t ? t.value.split("\\n").map(function (s) { return s.trim(); }).filter(Boolean) : []; }
function num(id, d) { var el = document.getElementById(id); return el && el.value !== "" ? +el.value : d; }
function calc() {
  var t = tr.value.toLowerCase().trim();
  if (!t) { out.textContent = hint; return; }
  var s = 0, parts = [];
  ["interests", "less", "never"].forEach(function (k) {
    lines(k).forEach(function (c, i) { var a = hit(alts(c), t); if (a) { var p = pts(k, i); s += p; parts.push(a + " " + fmt(p)); } });
  });
  var min = num("min_score", 4), note = num("notify_min_score", 7);
  out.textContent = (parts.length ? parts.join(", ") : "nothing matches") + "  =  " + s + " points (title only) \u00b7 " +
    (s >= note ? "listed and notified" : s >= min ? "listed" : "hidden; the description can add up to +" + P.desc.t[0] + "\u2026+4");
}
tr.addEventListener("input", calc); document.addEventListener("chips", calc);
document.querySelectorAll("#min_score,#notify_min_score").forEach(function (el) { el.addEventListener("input", calc); });
})();</script>"""

# Settings: notices unsaved changes; leaving the page or switching a preset asks first.
LEAVE_JS = """<script>(function () {
var form = document.getElementById("settings"), dlg = document.getElementById("leave_dlg");
if (!form || !dlg) return;
var SEARCH = ["search", "own"], SKIP = /^(preset_pick|preset_name|preset_do|what|next|save_first)$/;
function snap() {
  var out = {};
  form.querySelectorAll("section.ui-section").forEach(function (sec) {
    var v = [];
    sec.querySelectorAll("input,select,textarea").forEach(function (el) {
      if (!el.name || SKIP.test(el.name) || el.type === "button" || el.type === "submit") return;
      v.push(el.name + "=" + (el.type === "checkbox" || el.type === "radio" ? el.checked : el.value));
    });
    out[sec.id] = v.join("\\u0001");
  });
  return out;
}
var base = snap(), leaving = false, pending = null;
function dirty() {
  var now = snap(), out = [];
  Object.keys(now).forEach(function (k) { if (now[k] !== base[k]) out.push(k); });
  return out;
}
function title(id) { var h = document.getElementById(id + "-h"); return h ? h.textContent : id; }
var note = document.getElementById("savebar_note"), note0 = note.textContent;
var PF = /^(search_terms|search_locations|min_score|sf_.*|sc_.*)$/;
function psnap() {
  var v = [];
  form.querySelectorAll("input,textarea").forEach(function (el) {
    if (el.name && PF.test(el.name)) v.push(el.name + "=" + (el.type === "checkbox" ? el.checked : el.value)); });
  return v.join("\\u0001");
}
var pbase = psnap();
function newPreset() {
  if (psnap() === pbase) return "";
  var n = (document.getElementById("preset_name").value || "").trim() || document.getElementById("preset_name").placeholder;
  return " Saving keeps them as the preset \\u201c" + n + "\\u201d.";
}
function update() {
  var d = dirty();
  if (!d.length) { note.textContent = note0; return; }
  note.textContent = "Unsaved changes: " + d.map(title).join(", ") +
    (d.some(function (x) { return SEARCH.indexOf(x) >= 0; }) ? ". The search changed: saving clears the found postings and searches again." : ".") +
    newPreset();
}
form.addEventListener("input", update); form.addEventListener("change", update); document.addEventListener("chips", update);
form.addEventListener("submit", function () { leaving = true; });
window.addEventListener("beforeunload", function (ev) { if (!leaving && dirty().length) { ev.preventDefault(); ev.returnValue = ""; } });
function ask(onSave, onDiscard, saveLabel, discardLabel) {
  var d = dirty();
  document.getElementById("leave_what").textContent = "You changed: " + d.map(title).join(", ") + "." + newPreset();
  document.getElementById("leave_search").hidden = !d.some(function (x) { return SEARCH.indexOf(x) >= 0; });
  document.getElementById("leave_save").textContent = saveLabel;
  document.getElementById("leave_discard").textContent = discardLabel;
  pending = {save: onSave, discard: onDiscard};
  dlg.showModal();
}
document.getElementById("leave_stay").onclick = function () { pending = null; dlg.close(); };
document.getElementById("leave_save").onclick = function () { var p = pending; dlg.close(); if (p) { leaving = true; p.save(); } };
document.getElementById("leave_discard").onclick = function () { var p = pending; dlg.close(); if (p) { leaving = true; p.discard(); } };
// links and other forms that leave the page
document.addEventListener("click", function (ev) {
  var a = ev.target.closest("a[href]");
  if (!a || ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.shiftKey || a.target === "_blank") return;
  var href = a.getAttribute("href");
  if (!href || href.charAt(0) === "#" || !dirty().length) return;
  ev.preventDefault();
  ask(function () { document.getElementById("settings_next").value = a.pathname + a.search;
                    form.requestSubmit(document.getElementById("settings_save")); },
      function () { location.href = a.href; }, "Save and continue", "Don't save");
}, true);
document.querySelectorAll("form").forEach(function (f) {
  if (f === form || f.method === "dialog") return;
  f.addEventListener("submit", function (ev) {
    if (leaving || !dirty().length) return;
    ev.preventDefault();
    var sub = ev.submitter;
    ask(function () { document.getElementById("settings_next").value = "/settings";
                      form.requestSubmit(document.getElementById("settings_save")); },
        function () { leaving = true; f.requestSubmit(sub || undefined); }, "Save (stay here)", "Don't save, continue");
  });
});
// picking a preset or template fills in the form; it is applied only when you save
var pick = document.getElementById("preset_pick"), data = JSON.parse(document.getElementById("preset_data").textContent || "{}");
pick.addEventListener("change", function () {
  var p = data[pick.value];
  if (!p) return;
  function set(id, v) { var el = document.getElementById(id); if (el && v !== undefined) el.value = v; }
  if (p.search_terms) set("search_terms", p.search_terms.join("\\n"));
  if (p.search_locations) set("search_locations", p.search_locations.join("\\n"));
  if (p.min_score !== undefined) set("min_score", p.min_score);
  if (p.search_filter) form.querySelectorAll("input[name^=sf_]").forEach(function (c) { c.checked = !!p.search_filter[c.name.slice(3)]; });
  if (p.scoring) Object.keys(p.scoring).forEach(function (k) {
    var ta = document.getElementById("sc_" + k); if (ta && ta.setChips) ta.setChips(p.scoring[k]); });
  var tpl = pick.value.indexOf("tpl:") === 0;
  document.getElementById("active_preset_set").value = pick.value;
  pbase = psnap();   // this is the picked preset; changes from here on make a new one
  document.getElementById("preset_state").innerHTML = "";
  document.getElementById("preset_state").append("Loaded \\u201c" + pick.options[pick.selectedIndex].text + "\\u201d" +
    (tpl ? " (template)" : "") + " into the settings below. Nothing is applied until you click Save changes.");
  update();
});
})();</script>"""

OUTREACH_JS = """<script>
function ocount(id, lim) { var t = document.getElementById('om' + id), c = document.getElementById('on' + id);
  if (!c) return; c.textContent = t.value.length + ' / ' + lim + ' characters';
  c.className = 'rc-count' + (t.value.length > lim ? ' over' : ''); }
function ocopy(id, subj) {  // subj: copy the subject line instead of the message
  var t = document.getElementById((subj ? 'os' : 'om') + id), b = document.getElementById((subj ? 'ocs' : 'oc') + id), ok = false;
  var label = b.textContent; t.focus(); t.select(); try { ok = document.execCommand('copy'); } catch (err) {}
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(t.value); ok = true; }
  b.textContent = ok ? '✓ Copied' : 'Press Ctrl+C'; setTimeout(function () { b.textContent = label; }, 1800); }
function oview(id) { document.getElementById('dlg' + id).showModal(); }
</script>"""

OUTREACH_FORM_JS = """<script>
var RL = [];
function rval(id) { return document.getElementById(id).value.trim(); }
function rcell(tr, text, cls) { var td = document.createElement('td'); td.textContent = text; if (cls) td.className = cls; tr.appendChild(td); }
function rdraw() {
  var body = document.getElementById('rlist'), empty = document.getElementById('rempty'), table = document.getElementById('rtable');
  body.innerHTML = '';
  RL.forEach(function (r, i) {
    var tr = document.createElement('tr');
    rcell(tr, r.name + (r.about ? '  ℹ️' : ''));
    rcell(tr, r.title || '—', 'hide-sm');
    rcell(tr, r.role ? (r.role.length > 48 ? r.role.slice(0, 48) + '…' : r.role) : '—', 'hide-sm');
    rcell(tr, r.kind === 'agency' ? 'Agency' : r.kind === 'inhouse' ? 'In-house' : 'Auto');
    var td = document.createElement('td'), b = document.createElement('button');
    b.type = 'button'; b.className = 'btn btn-ghost btn-xs'; b.textContent = 'Remove'; b.setAttribute('aria-label', 'Remove ' + r.name);
    b.onclick = function () { RL.splice(i, 1); rdraw(); }; td.style.textAlign = 'right'; td.appendChild(b); tr.appendChild(td);
    body.appendChild(tr);
  });
  table.style.display = RL.length ? '' : 'none'; empty.style.display = RL.length ? 'none' : '';
  document.getElementById('rjson').value = JSON.stringify(RL);
  var go = document.getElementById('rgo'); go.disabled = !RL.length;
  go.textContent = RL.length ? 'Write ' + RL.length + ' message' + (RL.length === 1 ? '' : 's') : 'Write messages';
}
var RLOOK = null;
function rlookup() {  // LinkedIn link -> name + title from the profile's public link preview
  var url = rval('r_contact'), hint = document.getElementById('rhint');
  if (!/linkedin\\.com\\/in\\//i.test(url)) return Promise.resolve();
  hint.textContent = 'Reading the profile…';
  RLOOK = fetch('/outreach/lookup?url=' + encodeURIComponent(url)).then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok) { hint.textContent = d.error || ''; return; }
    if (rval('r_contact') === url) document.getElementById('r_contact').value = d.url;
    if (!rval('r_name')) document.getElementById('r_name').value = d.name || '';
    if (!rval('r_title')) document.getElementById('r_title').value = d.title || '';
    document.getElementById('r_about').value = d.about || '';
    hint.textContent = d.from === 'profile' ? '✓ Filled in from the profile. Check it, then add the person.'
      : 'The profile is not public: the name was guessed from the link. Check it and add the title by hand.';
  }).catch(function () { hint.textContent = 'Could not read the link. Fill in the name by hand.'; })
    .then(function () { RLOOK = null; });
  return RLOOK;
}
function radd() {
  if (!rval('r_name') && /linkedin\\.com\\/in\\//i.test(rval('r_contact'))) {  // only the link was given
    (RLOOK || rlookup()).then(function () { if (rval('r_name')) radd(); });
    return false;
  }
  if (!rval('r_name')) { document.getElementById('rhint').textContent = 'Give a LinkedIn link or a name first.';
    document.getElementById('r_contact').focus(); return false; }
  RL.push({name: rval('r_name'), title: rval('r_title'), contact: rval('r_contact'), kind: document.getElementById('r_kind').value,
           role: rval('r_role'), about: rval('r_about')});
  ['r_name', 'r_title', 'r_contact', 'r_kind', 'r_role', 'r_about'].forEach(function (id) { document.getElementById(id).value = ''; });
  document.getElementById('rhint').textContent = '';
  rdraw(); document.getElementById('r_contact').focus(); return true;
}
function renter(ev) { if (ev.key === 'Enter') { ev.preventDefault(); radd(); } }
function rsubmit() {  // a filled-in but not yet added recruiter counts too
  if (rval('r_name')) radd();
  if (!RL.length) { document.getElementById('rhint').textContent = 'Add at least one recruiter.'; return false; }
  return true;
}
function rchannel() {  // the introducer field matters only for introduction requests
  var intro = document.getElementById('channel').value === 'linkedin_intro';
  document.getElementById('viawrap').style.display = intro ? '' : 'none';
  if (intro) document.getElementById('rmore').open = true;
}
rdraw(); rchannel();
</script>"""


def outreach_initials(name):
    parts = [p for p in re.split(r"\s+", name or "") if p]
    return e("".join(p[0] for p in parts[:2]).upper() or "?")


def outreach_page(q, flash=""):
    import cvgen
    with CFG_LOCK:
        cfg = load_config()
    with db() as con:
        jobs = con.execute("SELECT id, title, company FROM jobs WHERE status != 'hidden' AND score >= ? "
                           "ORDER BY first_seen DESC LIMIT 100", (cfg["min_score"],)).fetchall()
        rows = con.execute("SELECT o.*, j.title AS job_title, j.company AS job_company FROM outreach o "
                           "LEFT JOIN jobs j ON j.id = o.job_id ORDER BY o.id DESC LIMIT 200").fetchall()
        picked = q.get("job", "")
        if picked and not any(j["id"] == picked for j in jobs):
            extra = con.execute("SELECT id, title, company FROM jobs WHERE id=?", (picked,)).fetchone()
            jobs = ([extra] if extra else []) + list(jobs)
    job_opts = '<option value="">None: a general message</option>' + "".join(
        '<option value="%s"%s>%s – %s</option>' % (e(j["id"]), " selected" if j["id"] == picked else "",
                                                   e(j["title"]), e(j["company"] or "?")) for j in jobs)
    ch_opts = "".join('<option value="%s">%s</option>' % (k, e(v[0])) for k, v in cvgen.OUTREACH_CHANNELS.items())
    drafts = [r for r in rows if r["status"] != "sent"]
    sent = [r for r in rows if r["status"] == "sent"]

    head = """<div class="ui-head"><h1>Recruiters</h1>
<p>Claude writes a short, personal first message to each recruiter, based on your CV profile and the role they hire for.
LinkedIn does not allow sending from other programs: copy the message and open the profile with one click. E-mails can be sent from here.</p>
<div class="ui-stats"><span class="badge badge-soft badge-primary">%d draft%s</span><span class="badge badge-soft badge-success">%d sent</span></div></div>""" % (
        len(drafts), "" if len(drafts) == 1 else "s", len(sent))

    form = """<section class="ui-section" aria-labelledby="new-h"><header><h2 id="new-h">New messages</h2></header>
<form method="post" action="/outreach/generate" onsubmit="return rsubmit()"><div class="ui-body">
<div class="ui-step"><span class="ui-num">1</span><div><h3>Who do you write to?</h3>
<div class="ui-grid">
<div class="wide"><label for="r_contact">LinkedIn profile link or e-mail</label>
<input class="input" id="r_contact" placeholder="https://www.linkedin.com/in/…" onkeydown="renter(event)" onpaste="setTimeout(rlookup, 0)" onchange="rlookup()" autocomplete="off">
<span class="help">Paste a LinkedIn link and the name and title fill in by themselves.</span></div>
<div><label for="r_name">Name</label><input class="input" id="r_name" placeholder="Anna Kovács" onkeydown="renter(event)" autocomplete="off"></div>
<div><label for="r_title">Title and company</label><input class="input" id="r_title" placeholder="Talent Acquisition @ Supercell" onkeydown="renter(event)" autocomplete="off"></div>
<div class="wide"><label for="r_role">Role they hire for <span class="help" style="display:inline">(optional)</span></label>
<input class="input" id="r_role" placeholder="LinkedIn job link, another job page, or a few words" onkeydown="renter(event)" autocomplete="off">
<span class="help">Makes the message specific to that role. Tip: the “Meet the hiring team” box on a LinkedIn posting shows who hires for it.</span></div>
<div><label for="r_kind">Recruiter works</label><select class="select" id="r_kind"><option value="">Decide from the title</option>
<option value="inhouse">In-house: hires for their own company</option><option value="agency">Agency or freelance: hires for clients</option></select></div>
<div style="align-self:end"><button type="button" class="btn btn-primary btn-outline" onclick="radd()">Add to the list</button></div>
</div>
<input type="hidden" id="r_about"><div class="ui-hint" id="rhint" role="status" aria-live="polite"></div>
<div class="rc-people"><div class="empty" id="rempty">No recruiters yet. Add them one by one above (Enter works too).</div>
<table class="table table-sm" id="rtable"><thead><tr><th>Name</th><th class="hide-sm">Title</th><th class="hide-sm">Role</th><th>Type</th><th></th></tr></thead>
<tbody id="rlist"></tbody></table></div>
<input type="hidden" name="recruiters_json" id="rjson" value="[]"></div></div>
<div class="ui-step"><span class="ui-num">2</span><div><h3>Message</h3>
<div class="ui-grid3">
<div><label for="channel">Channel</label><select class="select" name="channel" id="channel" onchange="rchannel()">%s</select></div>
<div><label for="language">Language</label><select class="select" name="language" id="language"><option value="auto">Automatic</option>
<option value="en">English</option><option value="hu">Hungarian</option><option value="de">German</option></select></div>
<div><label for="job_id">Posting for everyone</label><select class="select" name="job_id" id="job_id">%s</select></div>
</div>
<details class="ui-more" id="rmore"><summary>More options</summary><div class="ui-grid">
<div class="wide" id="viawrap"><label for="via">Mutual connection who introduces you</label>
<input class="input" name="via" id="via" placeholder="Péter Nagy | we worked together at Gameloft 2015–2017" autocomplete="off">
<span class="help">On the recruiter's LinkedIn profile, “mutual connections” shows who knows both of you.</span></div>
<div class="wide"><label for="note">Note for Claude</label><input class="input" name="note" id="note" placeholder="e.g. LinkedIn suggested them for the Unity Developer role" autocomplete="off"></div>
</div></details></div></div>
</div>
<div class="ui-foot"><span class="muted" style="font-size:13px">About 1–3 cents per message. Your contact details are not sent to the API.</span>
<button class="btn btn-primary" id="rgo">Write messages</button></div></form></section>""" % (ch_opts, job_opts)

    draft_html = []
    for r in drafts:
        label, limit, _ = cvgen.OUTREACH_CHANNELS.get(r["channel"], ("?", 1000, ""))
        oid, role = r["id"], outreach_role_label(r)
        badge = {"running": '<span class="badge badge-soft badge-info"><span class="loading loading-spinner loading-xs"></span> Writing</span>',
                 "error": '<span class="badge badge-soft badge-error">Error</span>',
                 "draft": '<span class="badge badge-soft badge-primary">Draft</span>'}.get(r["status"], "")
        who = """<div class="rc-who"><span class="rc-avatar" aria-hidden="true">%s</span><div><div class="name">%s</div><div class="sub">%s</div></div>
<div class="badges"><span class="badge badge-ghost">%s</span>%s</div></div>""" % (
            outreach_initials(r["name"]), e(r["name"]), e(r["title"] or ""), e(label), badge)
        if r["via"]:
            who += '<div class="rc-role">Goes to <b>%s</b>, who introduces you.</div>' % e(r["via"])
        if role:
            who += '<div class="rc-role">Role: %s</div>' % e(role)
        if r["role_ref"] and ROLE_UNREAD in (r["role_text"] or ""):
            who += ('<div role="alert" class="alert alert-warning alert-soft rc-alert"><span>The posting could not be read (LinkedIn limits requests), '
                    'so this message is not tailored to it. Paste the posting text into “Role they hire for” and write it again.</span></div>')
        if r["status"] == "running":
            draft_html.append('<article class="rc-draft">%s<p class="rc-role">Claude is writing the message…</p></article>' % who)
            continue
        if r["status"] == "error":
            draft_html.append("""<article class="rc-draft">%s<div role="alert" class="alert alert-error alert-soft rc-alert"><span>%s</span></div>
<form method="post" action="/outreach/%d" class="rc-actions"><div><button class="btn btn-sm btn-primary" name="act" value="regen">Try again</button></div>
<div><button class="btn btn-sm btn-ghost text-error" name="act" value="delete">Delete</button></div></form></article>""" % (who, e(r["error"]), oid))
            continue
        is_mail = "@" in (r["contact"] or "") and not (r["contact"] or "").startswith("http")
        open_btn = ('<a class="btn btn-sm btn-outline" href="%s" target="_blank" rel="noopener noreferrer">Open profile ↗</a>' % e(safe_url(r["contact"]))
                    if (r["contact"] or "").startswith("http") else "")
        send_btn = ('<button class="btn btn-sm btn-outline" name="act" value="send" title="Send to %s with the e-mail settings">Send e-mail</button>'
                    % e(r["contact"]) if is_mail and r["channel"] == "email" else "")
        draft_html.append("""<article class="rc-draft">%s<form method="post" action="/outreach/%d"><div class="rc-fields">
<div><label for="os%d">Subject</label><div class="join" style="width:100%%"><input class="input join-item" id="os%d" name="subject" value="%s" style="flex:1">
<button type="button" class="btn join-item" id="ocs%d" onclick="ocopy(%d, 1)">Copy</button></div></div>
<div><label for="om%d">Message</label><textarea class="textarea" id="om%d" name="message" rows="%d" oninput="ocount(%d,%d)">%s</textarea>
<div class="rc-count" id="on%d"></div><script>ocount(%d,%d)</script></div></div>
<div class="rc-actions"><div><button type="button" class="btn btn-sm btn-primary" id="oc%d" onclick="ocopy(%d)">Copy message</button>%s%s
<button class="btn btn-sm btn-success btn-soft" name="act" value="sent">Mark as sent</button></div>
<div><button class="btn btn-sm btn-ghost" name="act" value="save">Save edits</button><button class="btn btn-sm btn-ghost" name="act" value="regen">Rewrite</button>
<button class="btn btn-sm btn-ghost text-error" name="act" value="delete">Delete</button></div></div></form></article>""" % (
            who, oid, oid, oid, e(r["subject"] or ""), oid, oid, oid, oid, 4 if limit <= 300 else 9, oid, limit,
            e(r["message"] or ""), oid, oid, limit, oid, oid, open_btn, send_btn))
    drafts_sec = ('<section class="ui-section" aria-labelledby="dr-h"><header><h2 id="dr-h">Drafts</h2>'
                  '<span class="muted" style="font-size:13px">Edit, copy, send, then mark as sent.</span></header>%s</section>'
                  % "".join(draft_html)) if drafts else ""

    sent_rows, dialogs = [], []
    for r in sent:
        label = cvgen.OUTREACH_CHANNELS.get(r["channel"], ("?",))[0]
        oid, role = r["id"], outreach_role_label(r)
        sent_rows.append("""<tr onclick="oview(%d)"><td><button type="button" class="link link-hover" style="font-weight:600;text-align:left"
onclick="event.stopPropagation(); oview(%d)">%s</button><div class="muted" style="font-size:12.5px">%s</div></td>
<td class="hide-sm">%s</td><td class="hide-sm">%s</td><td class="when">%s</td></tr>""" % (
            oid, oid, e(r["name"]), e(r["title"] or ""), e(role or "—"), e(label), e(fmt_dt(r["sent_at"]))))
        open_btn = ('<a class="btn btn-sm btn-outline" href="%s" target="_blank" rel="noopener noreferrer">Open profile ↗</a>' % e(safe_url(r["contact"]))
                    if (r["contact"] or "").startswith("http") else "")
        dialogs.append("""<dialog class="modal" id="dlg%d" aria-labelledby="dlgt%d"><div class="modal-box rc-modal ui">
<form method="dialog"><button class="btn btn-sm btn-circle btn-ghost" style="position:absolute;right:10px;top:10px" aria-label="Close">✕</button></form>
<h3 id="dlgt%d">%s</h3><div class="sub">%s%s · %s · sent %s</div>
<label for="os%d">Subject</label><div class="join" style="width:100%%;margin-bottom:12px"><input class="input join-item" id="os%d" value="%s" readonly style="flex:1">
<button type="button" class="btn join-item" id="ocs%d" onclick="ocopy(%d, 1)">Copy</button></div>
<label for="om%d">Message</label><textarea class="textarea" id="om%d" rows="10" readonly>%s</textarea>
<form method="post" action="/outreach/%d" class="modal-action"><div><button type="button" class="btn btn-sm btn-primary" id="oc%d" onclick="ocopy(%d)">Copy message</button>%s</div>
<div><button class="btn btn-sm btn-ghost" name="act" value="unsent">Move back to drafts</button>
<button class="btn btn-sm btn-ghost text-error" name="act" value="delete">Delete</button></div></form></div>
<form method="dialog" class="modal-backdrop"><button class="" aria-label="Close">close</button></form></dialog>""" % (
            oid, oid, oid, e(r["name"]), e(r["title"] or ""), (" · " + e(role)) if role else "", e(label), e(fmt_dt(r["sent_at"])),
            oid, oid, e(r["subject"] or ""), oid, oid, oid, oid, e(r["message"] or ""), oid, oid, oid, open_btn))
    sent_sec = ("""<section class="ui-section" aria-labelledby="se-h"><header><h2 id="se-h">Sent</h2>
<span class="muted" style="font-size:13px">Select a row to see the message.</span></header>
<div style="overflow-x:auto"><table class="table rc-sent"><thead><tr><th>Recruiter</th><th class="hide-sm">Role</th><th class="hide-sm">Channel</th><th>Sent</th></tr></thead>
<tbody>%s</tbody></table></div></section>%s""" % ("".join(sent_rows), "".join(dialogs))) if sent else ""

    busy = any(r["status"] == "running" for r in rows)
    body = '<div class="ui rc">%s%s%s%s%s</div>%s' % (head, form, drafts_sec, sent_sec, OUTREACH_JS, OUTREACH_FORM_JS)
    return page("Recruiters", body, flash, refresh=3 if busy else 0)


def outreach_role_label(r):
    """Short name of the role a message is about: the recruiter's own role, else the chosen posting."""
    if r["role_ref"]:
        if ROLE_UNREAD in (r["role_text"] or ""):
            return ""
        return re.sub(r"^Title: |\n.*", "", r["role_text"] or r["role_ref"], flags=re.S)[:80]
    if r["job_title"]:
        return "%s – %s" % (r["job_title"], r["job_company"] or "?")
    return ""


def outreach_action(oid, f):
    """POST /outreach/<id>: act = save | sent | send | regen | delete. Returns the flash message."""
    act = f.get("act", "save")
    with db() as con:
        r = con.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
        if not r:
            return "No such message."
        if act == "delete":
            con.execute("DELETE FROM outreach WHERE id=?", (oid,))
            return "Deleted."
        if "message" in f:
            con.execute("UPDATE outreach SET message=?, subject=? WHERE id=?",
                        (f.get("message", "").strip(), f.get("subject", r["subject"] or "").strip(), oid))
        if act == "sent":
            con.execute("UPDATE outreach SET status='sent', sent_at=? WHERE id=?", (now_iso(), oid))
            return "Marked as sent, moved to the list at the bottom."
        if act == "unsent":
            con.execute("UPDATE outreach SET status='draft', sent_at=NULL WHERE id=?", (oid,))
            return "Moved back to drafts."
        if act == "regen":
            con.execute("UPDATE outreach SET status='running', error=NULL WHERE id=?", (oid,))
    if act == "regen":
        threading.Thread(target=outreach_generate, args=(oid,), daemon=True).start()
        return "Rewriting…"
    if act == "send":
        with CFG_LOCK:
            cfg = load_config()
        with db() as con:
            r = con.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
        sig = outreach_signature(load_profile())
        try:
            send_email(cfg, r["subject"] or "Hello", (r["message"] or "") + ("\n\n" + sig if sig else ""), to=r["contact"])
        except Exception as ex:
            return "Sending error: %s" % ex
        with db() as con:
            con.execute("UPDATE outreach SET status='sent', sent_at=? WHERE id=?", (now_iso(), oid))
        return "Sent to %s." % r["contact"]
    return "Saved."


LINKEDIN_PROFILE_RE = re.compile(r"^https?://([a-z]{2,3}\.)?(www\.)?linkedin\.com/in/([^/?#]+)", re.I)


def name_from_slug(slug):
    """"anna-kovacs-1a2b3c4d" -> "Anna Kovacs" (the id-like parts with digits are dropped)."""
    parts = [p for p in urllib.parse.unquote(slug).split("-") if p and not re.search(r"\d", p)]
    return " ".join(p[:1].upper() + p[1:] for p in parts)


def linkedin_preview(url):
    """Name and headline of a LinkedIn profile from its public link preview (og:title), like a chat app shows it.
    One request, only when the user pastes the link. Falls back to the name in the address."""
    m = LINKEDIN_PROFILE_RE.match((url or "").strip())
    if not m:
        return {"ok": False, "error": "Not a LinkedIn profile link (linkedin.com/in/…)."}
    clean = "https://www.linkedin.com/in/%s/" % m.group(3)
    guess = {"ok": True, "url": clean, "name": name_from_slug(m.group(3)), "title": "", "from": "address"}
    try:
        pg = fetch(clean, timeout=15)
    except Exception as ex:
        log.info("LinkedIn preview %s: %s", clean, ex)
        return guess
    t = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', pg) or re.search(r"<title>([^<]*)</title>", pg)
    title = html.unescape(t.group(1)).strip() if t else ""
    if not title or re.search(r"sign ?up|sign ?in|log ?in|authwall|join linkedin", title, re.I):
        return guess  # login wall: the profile is not public
    title = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title)
    name, _, headline = title.partition(" - ")
    d = re.search(r'<meta[^>]+(?:property="og:description"|name="description")[^>]+content="([^"]*)"', pg)
    # "headline · about text · Experience: X · Education: Y · Location: Z · 500 connections … View … profile …"
    about = re.split(r"\s·\s*\d[\d,+]*\s+(?:connections|followers)|View [^·]{0,80}profile on LinkedIn",
                     html.unescape(d.group(1)) if d else "")[0].strip(" ·")
    # a profile without a headline gets "<location> | Professional Profile" in the title: that is not a job title
    headline = re.sub(r"\s*\|\s*Professional Profile\s*$", "", headline, flags=re.I).strip()
    loc = re.search(r"Location:\s*([^·]+)", about)
    if loc and headline.lower() == loc.group(1).strip().lower() or re.fullmatch(r"[^,]+(,[^,]+){1,2}", headline) and \
            not re.search(r"\b(at|@|recruit|talent|hr|hiring|manager|partner|consultant|lead|head)\b", headline, re.I):
        headline = ""
    exp = re.search(r"Experience:\s*([^·]+)", about)
    if not headline and exp:
        headline = "@ " + exp.group(1).strip()  # at least the current company
    return {"ok": True, "url": clean, "name": name.strip() or guess["name"], "title": headline,
            "about": about[:1500], "from": "profile"}


LINKEDIN_JOB_RE = re.compile(r"linkedin\.com/.*?(?:jobs/view/(?:[^/?#]*?-)?|currentJobId=)(\d{6,})", re.I)


def linkedin_job(job_id):
    """Title, company, location and text of a LinkedIn posting from its public page."""
    pg = fetch("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/%s" % job_id, timeout=20)
    grab = lambda rx: (lambda m: strip_tags(m.group(1)) if m else "")(re.search(rx, pg, re.S))
    return {"title": grab(r'top-card-layout__title[^>]*>(.*?)</h'),
            "company": grab(r'topcard__org-name-link[^>]*>(.*?)</a>'),
            "location": grab(r'topcard__flavor topcard__flavor--bullet[^>]*>(.*?)</span>'),
            "description": linkedin_detail(job_id) or ""}


ROLE_UNREAD = "(the page could not be read)"


def resolve_role(ref, waits=(20, 60)):
    """What the recruiter is hiring for, as text for Claude: a LinkedIn job link, another job page, or plain text.
    A LinkedIn posting already in the database is used from there; on 429 (rate limit) it waits and retries."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    m = LINKEDIN_JOB_RE.search(ref)
    if m:
        with db() as con:
            row = con.execute("SELECT title, company, location, description FROM jobs WHERE id=?",
                              ("linkedin:" + m.group(1),)).fetchone()
        if row and row["description"]:
            return "Title: %s\nCompany: %s\nLocation: %s\nURL: %s\n\n%s" % (
                row["title"], row["company"], row["location"] or "-", ref, row["description"][:6000])
    for wait in (0,) + tuple(waits):
        time.sleep(wait)
        try:
            return _read_role_page(ref, m)
        except urllib.error.HTTPError as ex:
            if ex.code != 429:
                log.warning("Role page %s: %s", ref, ex)
                break
            log.warning("Role page %s: 429, retrying", ref)
        except Exception as ex:
            log.warning("Role page %s: %s", ref, ex)
            break
    return "URL: %s %s" % (ref, ROLE_UNREAD)


def _read_role_page(ref, m):
    """One attempt; raises on network / HTTP errors. Plain text (no link) is returned as it is."""
    if m:
        j = linkedin_job(m.group(1))
        if not j["title"] and not j["description"]:
            raise RuntimeError("no posting on the page")
        return "Title: %s\nCompany: %s\nLocation: %s\nURL: %s\n\n%s" % (
            j["title"], j["company"], j["location"] or "-", ref, j["description"][:6000])
    if re.match(r"^https?://", ref):
        pg = fetch(ref, timeout=20)
        t = re.search(r"<title>(.*?)</title>", pg, re.S)
        return "Page title: %s\nURL: %s\n\n%s" % (strip_tags(t.group(1)) if t else "", ref, strip_tags(pg)[:6000])
    return ref[:3000]


def recruiters_from_form(f):
    """The list built on the page (recruiters_json); the old one-per-line text still works too."""
    out = []
    try:
        items = json.loads(f.get("recruiters_json") or "[]")
    except ValueError:
        items = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and str(it.get("name") or "").strip():
            out.append({"name": str(it["name"]).strip()[:120], "title": str(it.get("title") or "").strip()[:200],
                        "contact": str(it.get("contact") or "").strip()[:300],
                        "kind": it.get("kind") if it.get("kind") in ("inhouse", "agency") else "",
                        "profile": str(it.get("about") or "").strip()[:1500],
                        "role_ref": str(it.get("role") or "").strip()[:3000]})
    return out + parse_recruiters(f.get("recruiters", ""))


def outreach_create(f):
    recs = recruiters_from_form(f)
    if not recs:
        return "Give at least one recruiter name."
    import cvgen
    channel = f.get("channel") if f.get("channel") in cvgen.OUTREACH_CHANNELS else "linkedin_message"
    if channel == "linkedin_intro" and not f.get("via", "").strip():
        return "For an introduction request, give the mutual connection's name."
    ids = []
    with db() as con:
        for rc in recs[:20]:
            via = f.get("via", "").strip()[:300] if channel == "linkedin_intro" else ""
            kind = rc.get("kind") or (f.get("kind") if f.get("kind") in ("inhouse", "agency") else "")
            cur = con.execute("INSERT INTO outreach(created,name,title,contact,channel,language,job_id,note,status,via,kind,"
                              "profile,role_ref) VALUES(?,?,?,?,?,?,?,?,'running',?,?,?,?)",
                              (now_iso(), rc["name"], rc["title"], rc["contact"], channel,
                               f.get("language", "auto"), f.get("job_id") or None, f.get("note", "").strip()[:500], via, kind,
                               rc.get("profile", ""), rc.get("role_ref", "")))
            ids.append(cur.lastrowid)
    for oid in ids:
        threading.Thread(target=outreach_generate, args=(oid,), daemon=True).start()
    return "Writing %d message(s)…" % len(ids)


def cv_page(jid, flash=""):
    import cvgen
    with db() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return page("Not found", "<p>No such posting.</p>")
        job = dict(r)
        cv = con.execute("SELECT * FROM cvs WHERE job_id=?", (jid,)).fetchone()
    qid = e(urllib.parse.quote(jid, safe=""))
    head = """<div class="ui-head"><a href="/" style="font-size:13px">← Jobs</a><div class="job-head" style="margin-top:6px">%s<div><h1>%s</h1>
<p style="margin-top:2px">%s · %s · %s · <b>%d points</b> · <a href="%s" target="_blank" rel="noopener">posting ↗</a></p></div></div>
<div class="ui-btns" style="margin-top:12px"><a class="btn btn-sm btn-outline" href="/outreach?job=%s">Message a recruiter about this posting</a></div></div>""" % (
        logo_html(job["company"], logo_files(), 52), e(job["title"]), e(job["company"] or "?"), e(job["location"] or "-"),
        e(WORK_MODES.get(job.get("work_mode") or "unknown", "")), job["score"], e(safe_url(job["url"])), qid)
    if cv and cv["status"] == "running":
        return page("Generating CV", '<div class="ui">%s<section class="ui-section"><div class="ui-body ui-empty">'
                    '<span class="loading loading-spinner loading-md"></span><p>Claude is writing the CV, Resume and cover letter for this posting '
                    '(about 1–2 minutes). The page refreshes by itself.</p></div></section></div>' % head, flash, refresh=5)
    desc = cv["description"] if cv and cv["description"] else cvgen.fetch_description(fetch, strip_tags, job)
    done = bool(cv and cv["status"] == "done")
    gen_form = """<section class="ui-section"><header><div><h2>%s</h2><p class="ui-desc">Claude works from this posting text: fix or extend it if
something is missing. Only facts from your CV profile go in; your contact details are not sent to the API.</p></div></header>
<form method="post" action="/cv/%s/generate"><div class="ui-body">%s<label for="description">Posting text</label>
<textarea class="textarea" id="description" name="description" rows="12">%s</textarea></div>
<div class="ui-foot"><span class="muted" style="font-size:13px">About 5–15 cents.</span><button class="btn %s">%s</button></div></form></section>""" % (
        "Write it again" if done else "Create documents for this posting", qid,
        ('<div role="alert" class="alert alert-error alert-soft" style="margin-bottom:12px"><span>%s</span></div>' % e(cv["error"]))
        if cv and cv["status"] == "error" else "",
        e(desc), "btn-outline" if done else "btn-primary",
        "Regenerate the documents" if done else "Write CV + Resume + cover letter")
    if not done:
        return page("CV", '<div class="ui">%s%s</div>' % (head, gen_form), flash)
    data = json.loads(cv["data"])
    notes = "".join("<li>%s</li>" % e(n) for n in data.get("match_notes", []))
    emails = cvgen.find_emails(desc)
    sent = ('<div role="status" class="alert alert-success alert-soft" style="margin-bottom:12px"><span>Sent %s → %s</span></div>'
            % (e(fmt_dt(cv["sent_at"])), e(cv["sent_to"]))) if cv["sent_at"] else ""
    paths = cv_paths(jid)
    folder = smb_path(os.path.dirname(paths["cv"])) if paths["cv"] else ""
    photo = data.get("_photo", True)
    docs = """<section class="ui-section"><header><div><h2>Documents</h2><p class="ui-desc">Written by %s, %s.</p></div></header>
<div class="ui-body"><div class="ui-btns"><a class="btn btn-sm btn-primary" href="/cv/%s/cv.pdf" target="_blank">CV (detailed)</a>
<a class="btn btn-sm btn-primary" href="/cv/%s/resume.pdf" target="_blank">Resume (1 page)</a>
<a class="btn btn-sm btn-outline" href="/cv/%s/letter.pdf" target="_blank">Cover letter</a></div>
<form method="post" action="/cv/%s/photo" class="ui-inline"><span>Photo on the CV: <b>%s</b></span>
<button class="btn btn-xs btn-ghost" name="photo" value="%s">%s</button>
<span class="help">By default no photo for US, UK, Canadian and Australian postings.</span></form>
<h3 class="ui-h3">%s</h3><p>%s</p>
<h3 class="ui-h3">Notes for you</h3><ul class="ui-list">%s</ul>
<p class="help">Folder (on a Mac: Finder → Go → Connect to Server): <code>%s</code></p></div></section>""" % (
        e(data.get("_model", "") or "Claude"), e(fmt_dt(cv["created"])), qid, qid, qid, qid,
        "yes" if photo else "no", "off" if photo else "on", "Remove the photo" if photo else "Add a photo",
        e(data["headline"]), e(data["summary"]), notes, e(folder))
    send = """<section class="ui-section"><header><div><h2>Send by e-mail</h2><p class="ui-desc">%s Nothing is sent until you press
the button and confirm. It goes from the e-mail account in Settings.</p></div></header>
<form method="post" action="/cv/%s/send" onsubmit="return confirm('Send the application to ' + this.to.value + '?')"><div class="ui-body">%s<div class="ui-grid">
<div><label for="to">Recipient</label><input class="input" id="to" name="to" value="%s" required type="email"></div>
<div><label for="subject">Subject</label><input class="input" id="subject" name="subject" value="%s"></div>
<div class="wide"><label for="body">Message</label><textarea class="textarea" id="body" name="body" rows="12">%s</textarea></div>
<div class="wide"><label>Attachments</label><div class="ui-checks">
<label><input type="radio" class="radio radio-sm radio-primary" name="attach" value="cv" checked> CV</label>
<label><input type="radio" class="radio radio-sm radio-primary" name="attach" value="resume"> Resume</label>
<label><input type="radio" class="radio radio-sm radio-primary" name="attach" value="both"> Both</label>
<label><input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="attach_letter"> Cover letter as a PDF too</label></div></div>
</div></div><div class="ui-foot"><span></span><button class="btn btn-primary">Send the application</button></div></form></section>""" % (
        ("E-mail addresses in the posting: " + e(", ".join(emails)) + ".") if emails else
        "The posting has no e-mail address; most companies want the PDF uploaded on their application form.",
        qid, sent, e(emails[0] if emails else ""), e(data.get("email_subject", "")), e(data["cover_letter"]))
    edit = """<section class="ui-section"><header><div><h2>Edit by hand</h2><p class="ui-desc">The CV, the Resume (<code>resume</code>) and the
cover letter in JSON. Saving rebuilds the PDFs, without an API call.</p></div></header>
<form method="post" action="/cv/%s/save"><div class="ui-body"><textarea class="textarea ui-mono" name="data" rows="18" aria-label="Document content (JSON)">%s</textarea></div>
<div class="ui-foot"><span></span><button class="btn btn-outline">Save and rebuild the PDFs</button></div></form></section>""" % (
        qid, e(json.dumps({k: v for k, v in data.items() if not k.startswith("_")}, ensure_ascii=False, indent=2)))
    return page("CV", '<div class="ui">%s%s%s%s%s</div>' % (head, docs, send, gen_form, edit), flash)


def docs_page():
    if not os.path.isdir(DOCS_ROOT):
        return page("Documents", '<div class="ui"><div class="ui-head"><h1>Documents</h1></div><section class="ui-section">'
                                 '<div class="ui-body ui-empty">No documents yet. On a posting in the Jobs list, press “CV + Resume” '
                                 'to create the first one.</div></section></div>')
    with db() as con:
        sent = {r["job_id"]: r for r in con.execute("SELECT job_id, sent_to, sent_at FROM cvs")}
    blocks = []
    for co in sorted(os.listdir(DOCS_ROOT), key=str.lower):
        cdir = os.path.join(DOCS_ROOT, co)
        if not os.path.isdir(cdir):
            continue
        jobs_html = []
        for jd in sorted(os.listdir(cdir)):
            d = os.path.join(cdir, jd)
            if not os.path.isdir(d):
                continue
            info = {}
            try:
                with open(os.path.join(d, "info.json"), encoding="utf-8") as fh:
                    info = json.load(fh)
            except Exception:
                pass
            rel = os.path.relpath(d, DOCS_ROOT)
            files = "".join('<a class="btn btn-sm btn-outline" href="/docs/file?p=%s" target="_blank">%s</a>' % (
                e(urllib.parse.quote(os.path.join(rel, fn))), e(DOC_LABEL[k])) for k, fn in doc_names().items()
                if os.path.exists(os.path.join(d, fn)))
            if os.path.exists(os.path.join(d, "hirdetes.txt")):
                files += '<a class="btn btn-sm btn-ghost" href="/docs/file?p=%s" target="_blank">Posting text</a>' % e(
                    urllib.parse.quote(os.path.join(rel, "hirdetes.txt")))
            older = len(os.listdir(os.path.join(d, "korabbi"))) if os.path.isdir(os.path.join(d, "korabbi")) else 0
            s_ = sent.get(info.get("job_id"))
            status = ('<span class="badge badge-soft badge-success">Sent %s → %s</span>' % (e(fmt_dt(s_["sent_at"])), e(s_["sent_to"]))
                      if s_ and s_["sent_at"] else "")
            jobs_html.append("""<div class="ui-row"><div class="ui-row-main"><div class="ui-row-title">%s %s</div>
<div class="muted" style="font-size:13px">Generated %s%s · %s · <a href="%s" target="_blank" rel="noopener">posting ↗</a></div>
<div class="ui-btns" style="margin-top:8px">%s</div></div><div>%s</div></div>""" % (
                e(info.get("title") or jd), status,
                e(fmt_dt(info.get("generated"))), (" · %d earlier version(s)" % older) if older else "",
                e(WORK_MODES.get(info.get("work_mode") or "unknown", "")), e(safe_url(info.get("url", ""))), files,
                ('<a class="btn btn-sm btn-primary" href="/cv/%s">Open</a>' % e(urllib.parse.quote(info["job_id"], safe="")))
                if info.get("job_id") else ""))
        if jobs_html:
            blocks.append('<section class="ui-section"><header><h2>%s</h2><span class="muted" style="font-size:13px">%d posting%s</span></header>%s</section>'
                          % (e(co), len(jobs_html), "" if len(jobs_html) == 1 else "s", "".join(jobs_html)))
    head = """<div class="ui-head"><h1>Documents</h1><p>Every tailored CV, Resume and cover letter, grouped by company. When you regenerate,
the old version is kept in the <code>korabbi/</code> subfolder. On a Mac: Finder → Go → Connect to Server → <code>%s</code></p></div>""" % e(smb_path(DOCS_ROOT))
    empty = ('<section class="ui-section"><div class="ui-body ui-empty">No documents yet. On a posting in the Jobs list, '
             'press “CV + Resume” to create the first one.</div></section>')
    return page("Documents", '<div class="ui">%s%s</div>' % (head, "".join(blocks) or empty))


def profile_page(flash="", prefill=None):
    current = prefill if prefill is not None else json.dumps(load_profile(), ensure_ascii=False, indent=2)
    body = """<div class="ui"><div class="ui-head"><h1>CV profile</h1><p>The facts Claude works from: only what is here can go into a CV,
cover letter or recruiter message. The <code>contact</code> part is never sent to the API; it only goes into the PDF header.</p>
<div class="ui-btns" style="margin-top:12px"><a class="btn btn-sm btn-primary" href="/base-cv.pdf?photo=1" target="_blank">Base CV with photo (PDF)</a>
<a class="btn btn-sm btn-outline" href="/base-cv.pdf?photo=0" target="_blank">Base CV without photo (for the US)</a></div></div>
<section class="ui-section"><header><div><h2>Create it from your existing CV</h2><p class="ui-desc">Claude reads the CV (contact details included)
and fills in the editor below. Nothing is saved until you check it and press Save. Needs the API key (Settings → Claude API).</p></div></header>
<div class="ui-body"><div class="ui-grid">
<form class="wide" method="post" action="/profile/import-pdf" enctype="multipart/form-data"><label for="pdf">From a PDF</label>
<div class="join" style="width:100%%"><input class="file-input join-item" style="flex:1" type="file" id="pdf" name="pdf" accept="application/pdf">
<button class="btn btn-primary join-item">Read the PDF</button></div></form>
<form class="wide" method="post" action="/profile/import"><label for="cv_text">…or from text</label>
<textarea class="textarea" id="cv_text" name="cv_text" rows="5" placeholder="Paste the text of your CV here"></textarea>
<div style="margin-top:8px"><button class="btn btn-sm btn-primary btn-outline">Read the text</button></div></form>
</div></div></section>
<section class="ui-section"><header><div><h2>Editor</h2><p class="ui-desc">JSON: name, headline, contact, summary, skills, experience, languages, objective.</p></div></header>
<form method="post" action="/profile"><div class="ui-body"><textarea class="textarea ui-mono" name="profile" rows="28" aria-label="CV profile (JSON)">%s</textarea></div>
<div class="ui-foot"><span class="muted" style="font-size:13px">Saved only when you press Save.</span><button class="btn btn-primary">Save</button></div></form></section></div>""" % (
        e(current))
    return page("CV profile", body, flash)


AUTH_PASSWORD = os.environ.get("JOBWATCH_PASSWORD", "")
AUTH_PUBLIC_PATHS = ("/health",)  # the platform's health check must get through without a password
# the icons too: browsers fetch them without the password, and they hold nothing private
STATIC_DIR = os.path.join(BASE, "static")
ICON_FILES = {"/favicon.ico": ("favicon.ico", "image/x-icon"), "/favicon.svg": ("favicon.svg", "image/svg+xml"),
              "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"), "/icon-192.png": ("icon-192.png", "image/png")}
MANIFEST = json.dumps({"name": "jobwatch", "short_name": "jobwatch", "start_url": "/", "display": "standalone",
                       "background_color": "#1d232a", "theme_color": "#5b5bd6",
                       "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                                 {"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml"}]}).encode()
AUTH_PUBLIC_PATHS = AUTH_PUBLIC_PATHS + tuple(ICON_FILES) + ("/manifest.webmanifest",)


def auth_ok(header, password):
    """True if no password is set, or the Basic auth header carries it (the user name is ignored)."""
    if not password:
        return True
    import base64
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except Exception:
        return False
    given = decoded.partition(":")[2]
    return secrets.compare_digest(given.encode("utf-8"), password.encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def require_login(self):
        """Sends 401 and returns False unless the request is allowed."""
        path = urllib.parse.urlparse(self.path).path
        if path in AUTH_PUBLIC_PATHS or auth_ok(self.headers.get("Authorization"), AUTH_PASSWORD):
            return True
        body = "Login required.".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="jobwatch", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def send_html(self, s, code=200):
        b = s.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def form(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[-1] for k, v in urllib.parse.parse_qs(data, keep_blank_values=True).items()}

    def back(self):
        ref = self.headers.get("Referer") or "/"
        p = urllib.parse.urlparse(ref)
        return (p.path or "/") + ("?" + p.query if p.query else "")

    def do_GET(self):
        if not self.require_login():
            return
        u = urllib.parse.urlparse(self.path)
        REQ.path = u.path
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path in ICON_FILES or u.path == "/manifest.webmanifest":
            if u.path == "/manifest.webmanifest":
                data, ctype = MANIFEST, "application/manifest+json"
            else:
                fname, ctype = ICON_FILES[u.path]
                with open(os.path.join(STATIC_DIR, fname), "rb") as fh:
                    data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=604800")
            self.end_headers()
            return self.wfile.write(data)
        try:
            if u.path == "/":
                return self.send_html(jobs_page(q))
            if u.path == "/sources":
                return self.send_html(sources_page(q.get("msg", "")))
            if u.path == "/outreach":
                return self.send_html(outreach_page(q, q.get("msg", "")))
            if u.path == "/outreach/lookup":
                body = json.dumps(linkedin_preview(q.get("url", "")), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if u.path == "/settings":
                return self.send_html(settings_page(q.get("msg", "")))
            m = re.match(r"^/logo/([a-z0-9]{1,200})$", u.path)
            if m:
                fname = logo_files().get(m.group(1))
                path = os.path.join(LOGO_DIR, fname) if fname else ""
                if not path or not os.path.isfile(path):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                ctype = {v: k for k, v in LOGO_TYPES.items()}.get(os.path.splitext(fname)[1], "application/octet-stream")
                data = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=604800")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                return self.wfile.write(data)
            if u.path.startswith("/open/"):
                jid = urllib.parse.unquote(u.path[6:])
                with db() as con:
                    r = con.execute("SELECT url,status FROM jobs WHERE id=?", (jid,)).fetchone()
                    if r and r["status"] == "new":
                        con.execute("UPDATE jobs SET status='seen' WHERE id=?", (jid,))
                return self.redirect(safe_url(r["url"]) if r else "/")
            if u.path == "/profile":
                return self.send_html(profile_page(q.get("msg", "")))
            if u.path == "/base-cv.pdf":
                import cvgen
                os.makedirs(CV_DIR, exist_ok=True)
                photo = q.get("photo", "1") == "1"
                out = os.path.join(CV_DIR, "base-%s.pdf" % ("photo" if photo else "nophoto"))
                prof = load_profile()
                cvgen.render_cv_pdf(out, prof, prof, FONT_DIR, PHOTO_PATH if photo else None)
                b = open(out, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'inline; filename="%s_CV%s.pdf"' % (owner_prefix(), "" if photo else "_no_photo"))
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if u.path == "/docs":
                return self.send_html(docs_page())
            if u.path == "/docs/file":
                full = os.path.realpath(os.path.join(DOCS_ROOT, urllib.parse.unquote(q.get("p", ""))))
                if not full.startswith(os.path.realpath(DOCS_ROOT) + os.sep) or not os.path.isfile(full):
                    return self.send_html(page("Not found", "<p>No such file.</p>"), 404)
                ctype = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
                         ".json": "application/json; charset=utf-8"}.get(os.path.splitext(full)[1], "application/octet-stream")
                b = open(full, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            m = re.match(r"^/cv/([^/]+?)(?:/(cv|resume|letter)\.pdf)?$", u.path)
            if m:
                jid = urllib.parse.unquote(m.group(1))
                if not m.group(2):
                    return self.send_html(cv_page(jid, q.get("msg", "")))
                with db() as con:
                    job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                path = cv_paths(jid)[m.group(2)]
                if not job or not path or not os.path.exists(path):
                    return self.send_html(page("Not found", "<p>No PDF yet.</p>"), 404)
                b = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'inline; filename="%s"' % cv_filename(job, m.group(2)))
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if u.path == "/api/jobs":
                with db() as con:
                    rows = [dict(r) for r in con.execute(
                        "SELECT id,source,title,company,location,url,posted,score,matched,first_seen,status "
                        "FROM jobs ORDER BY first_seen DESC LIMIT 500")]
                b = json.dumps(rows, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                return self.wfile.write(b)
            if u.path == "/health":
                return self.send_html("ok")
            self.send_html(page("Not found", "<p>404</p>"), 404)
        except Exception as ex:
            log.exception("GET hiba")
            self.send_html(page("Hiba", "<p class=err>%s</p>" % e(ex)), 500)

    def profile_import_result(self, text_source, flash_ok):
        try:
            prefill = profile_from_cv_text(text_source())
            return self.send_html(profile_page(flash_ok, prefill=prefill))
        except ValueError as ex:
            return self.send_html(profile_page("Failed: %s" % ex))

    def profile_import_text(self, text):
        return self.profile_import_result(lambda: text, "Check the preview, then save it.")

    def profile_import_pdf(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_PDF_BYTES + 4096:
            return self.send_html(profile_page("The file is too large (max. 10 MB)."))
        body = self.rfile.read(length)
        data = pdf_from_multipart(self.headers.get("Content-Type", ""), body)
        if not data:
            return self.send_html(profile_page("No PDF file received."))
        if len(data) > MAX_PDF_BYTES:
            return self.send_html(profile_page("The file is too large (max. 10 MB)."))
        def source():
            try:
                return pdf_text(data)
            except Exception as ex:
                raise ValueError("PDF reading error: %s" % ex)
        return self.profile_import_result(source, "Check the preview, then save it.")

    def do_POST(self):
        if not self.require_login():
            return
        u = urllib.parse.urlparse(self.path)
        REQ.path = u.path
        try:
            if u.path == "/profile/import-pdf":
                return self.profile_import_pdf()
            f = self.form()
            if u.path == "/profile/import":
                return self.profile_import_text(f.get("cv_text", ""))
            if u.path == "/reset-jobs":
                reset_job_cache()
                return self.redirect("/settings?msg=" + urllib.parse.quote("Job cache cleared. Press Refresh to fetch again."))
            if u.path == "/run":
                only, cids = run_selection(f)
                if only is None and not cids and not f.get("all") and ("one" in f or "selected" in f):
                    return self.redirect("/sources?msg=" + urllib.parse.quote("Tick at least one source to refresh."))
                if STATE["running"]:
                    return self.redirect(self.back() + ("&" if "?" in self.back() else "?") + "msg=" +
                                         urllib.parse.quote("A refresh is already running; try again when it has finished."))
                threading.Thread(target=run_all, kwargs={"force": True, "only": only, "custom_ids": cids}, daemon=True).start()
                time.sleep(0.3)
                return self.redirect(self.back())
            if u.path.startswith("/job/"):
                jid = urllib.parse.unquote(u.path[5:])
                st = f.get("status")
                if st in STATUS_LABEL:
                    with db() as con:
                        con.execute("UPDATE jobs SET status=? WHERE id=?", (st, jid))
                wm = f.get("work_mode")
                if wm in ("remote", "hybrid", "onsite"):   # set by hand: never overwritten
                    with db() as con:
                        con.execute("UPDATE jobs SET work_mode=?, mode_src='manual', mode_note='set by you' WHERE id=?", (wm, jid))
                elif wm == "auto":                           # back to automatic: decided again in the background
                    with db() as con:
                        con.execute("UPDATE jobs SET work_mode='unknown', mode_src=NULL, mode_note=NULL, mode_checked=NULL WHERE id=?", (jid,))
                    with CFG_LOCK:
                        cfg_ = load_config()
                    threading.Thread(target=enrich_work_modes, args=(cfg_,), daemon=True).start()
                return self.redirect(self.back())
            if u.path == "/profile":
                try:
                    prof = json.loads(f.get("profile", ""))
                    assert isinstance(prof, dict) and prof.get("name") and prof.get("experience")
                except Exception:
                    return self.redirect("/profile?msg=" + urllib.parse.quote("Invalid JSON, nothing saved."))
                os.makedirs(DATA, exist_ok=True)
                with open(PROFILE_PATH, "w", encoding="utf-8") as fh:
                    json.dump(prof, fh, ensure_ascii=False, indent=2)
                return self.redirect("/profile?msg=" + urllib.parse.quote("Saved."))
            m = re.match(r"^/cv/([^/]+)/(generate|save|send|photo)$", u.path)
            if m:
                jid, act = urllib.parse.unquote(m.group(1)), m.group(2)
                back = "/cv/" + urllib.parse.quote(jid, safe="")
                with db() as con:
                    job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                    if not job:
                        return self.redirect("/")
                    job = dict(job)
                    if act == "generate":
                        desc = f.get("description", "")[:20000]
                        con.execute("INSERT INTO cvs(job_id,status,description) VALUES(?, 'running', ?) "
                                    "ON CONFLICT(job_id) DO UPDATE SET status='running', error=NULL, description=excluded.description",
                                    (jid, desc))
                if act == "generate":
                    threading.Thread(target=cv_generate, args=(jid, desc), daemon=True).start()
                    return self.redirect(back)
                if act == "photo":
                    with db() as con:
                        data = json.loads(con.execute("SELECT data FROM cvs WHERE job_id=?", (jid,)).fetchone()["data"])
                        data["_photo"] = f.get("photo") == "on"
                        render_cv_files(jid, data)
                        con.execute("UPDATE cvs SET data=? WHERE job_id=?", (json.dumps(data, ensure_ascii=False), jid))
                    return self.redirect(back)
                if act == "save":
                    try:
                        data = json.loads(f.get("data", ""))
                        for k in ("headline", "summary", "experience", "skills", "cover_letter"):
                            assert k in data
                        with db() as con:
                            old = json.loads(con.execute("SELECT data FROM cvs WHERE job_id=?", (jid,)).fetchone()["data"])
                        data["_model"] = old.get("_model", "").replace(" (edited by hand)", "") + " (edited by hand)"
                        data["_photo"] = old.get("_photo", True)
                        render_cv_files(jid, data)
                        with db() as con:
                            con.execute("UPDATE cvs SET data=? WHERE job_id=?", (json.dumps(data, ensure_ascii=False), jid))
                        msg = "Saved, PDF rebuilt."
                    except Exception as ex:
                        msg = "Not saved: %s" % ex
                    return self.redirect(back + "?msg=" + urllib.parse.quote(msg))
                if act == "send":
                    to = f.get("to", "").strip()
                    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):
                        return self.redirect(back + "?msg=" + urllib.parse.quote("Invalid recipient."))
                    paths = cv_paths(jid)
                    kinds = {"cv": ["cv"], "resume": ["resume"], "both": ["cv", "resume"]}.get(f.get("attach"), ["cv"])
                    if "attach_letter" in f:
                        kinds.append("letter")
                    att = [(cv_filename(job, k), open(paths[k], "rb").read()) for k in kinds]
                    with CFG_LOCK:
                        cfg = load_config()
                    try:
                        send_email(cfg, f.get("subject", "").strip() or "Application: " + job["title"],
                                   f.get("body", ""), to=to, attachments=att)
                        with db() as con:
                            con.execute("UPDATE cvs SET sent_to=?, sent_at=? WHERE job_id=?", (to, now_iso(), jid))
                            con.execute("UPDATE jobs SET status='applied' WHERE id=?", (jid,))
                            cvrow = con.execute("SELECT * FROM cvs WHERE job_id=?", (jid,)).fetchone()
                        try:
                            write_job_info(os.path.dirname(paths["cv"]), job, json.loads(cvrow["data"]), cvrow)
                        except Exception:
                            log.exception("info.json update")
                        msg = "Sent: %s" % to
                    except Exception as ex:
                        msg = "Sending error: %s" % ex
                    return self.redirect(back + "?msg=" + urllib.parse.quote(msg))
            if u.path == "/outreach/generate":
                return self.redirect("/outreach?msg=" + urllib.parse.quote(outreach_create(f)))
            m = re.match(r"^/outreach/(\d+)$", u.path)
            if m:
                return self.redirect("/outreach?msg=" + urllib.parse.quote(outreach_action(int(m.group(1)), f)))
            if u.path == "/add-job":
                jid, msg = add_job_by_link(f.get("url", ""))
                if jid:
                    return self.redirect("/cv/%s?msg=%s" % (urllib.parse.quote(jid, safe=""), urllib.parse.quote(msg)))
                return self.redirect("/?msg=" + urllib.parse.quote(msg))
            if u.path == "/mark-all-seen":
                with db() as con:
                    con.execute("UPDATE jobs SET status='seen' WHERE status='new'")
                return self.redirect(self.back())
            if u.path == "/settings":
                msg = urllib.parse.quote(saved_message(save_settings(f)))
                nxt = f.get("next", "")
                if nxt.startswith("/") and not nxt.startswith("//"):  # saved from the "unsaved changes" dialog
                    return self.redirect(nxt + ("&" if "?" in nxt else "?") + "msg=" + msg)
                return self.redirect("/settings?msg=" + msg)
            if u.path == "/preset":
                return self.redirect("/settings?msg=" + urllib.parse.quote(preset_action(f) or "Nothing to do.") + "#presets")
            if u.path == "/test":
                if "ntfy_server" in f:  # sent from the settings form: save what is on the screen first
                    save_settings(f)
                with CFG_LOCK:
                    cfg = load_config()
                if f.get("what") == "custom":
                    return self.redirect("/settings?msg=" + urllib.parse.quote(test_custom_sources(cfg)[:1500]))
                if f.get("what") == "mail":
                    return self.redirect("/settings?msg=" + urllib.parse.quote(test_mail_source(cfg)[:1500]) + "#mail")
                try:
                    if f.get("what") == "email":
                        send_email(cfg, "jobwatch test", "E-mail notifications work.",
                                   "<p>E-mail notifications work. ✅</p>")
                    else:
                        send_ntfy(cfg, "jobwatch test", "Push notifications work ✅",
                                  click=cfg.get("public_url"))
                    msg = "Test sent."
                except Exception as ex:
                    msg = "Test error: %s" % ex
                return self.redirect("/settings?msg=" + urllib.parse.quote(msg))
            self.send_html(page("Not found", "<p>404</p>"), 404)
        except Exception as ex:
            log.exception("POST hiba")
            self.send_html(page("Hiba", "<p class=err>%s</p>" % e(ex)), 500)


def main():
    init_db()
    migrate_db()
    threading.Thread(target=backfill_docs, daemon=True).start()
    with CFG_LOCK:
        cfg = load_config()
    log.info("jobwatch starting: port %d, ntfy topic: %s", PORT, cfg["ntfy"]["topic"])
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
