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
}
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
    "default_work_filter": "remote_unknown",
    "public_url": "http://localhost:%d/" % PORT,
    "anthropic_api_key": "",
    "ntfy": {"enabled": True, "server": "https://ntfy.sh", "topic": ""},
    "email": {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
              "username": "", "password": "", "to": ""},
    "sources": {"linkedin": True, "meetfrank": True, "4dayweek": True, "flexa": True,
                "remotive": True, "studios": True, "rss": True, "custom": True},
    "linkedin_searches": [],   # pl. "project manager | Hungary | any"
    "remotive_queries": [],    # pl. ["project manager"]
    "rss_feeds": [],
    "custom_sources": [],      # own sources from Settings, see CUSTOM_TYPES
    "studios": [],            # "rendszer | azonosito | nev", pl. "ashby | acme | Acme"
    "title_keywords": {},      # keyword: points if the title contains it
    "body_keywords": {},       # keyword: points if the description contains it (max 4 points)
    "location_keywords": {"remote": 1, "europe": 1},
    "negative_keywords": {"junior": -6, "intern": -8, "internship": -8, "trainee": -6, "student": -5},
}

CFG_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()
STATE = {"running": False, "last_run": None, "next_run": None, "progress": None, "enrich": None}
REQ = threading.local()  # current request path (active menu item)


# ---------------------------------------------------------------- config / db
def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict) and k not in (
                    "title_keywords", "body_keywords", "negative_keywords", "location_keywords"):
                cfg[k].update(v)
            else:
                cfg[k] = v
    if not cfg["ntfy"].get("topic"):
        cfg["ntfy"]["topic"] = "jobwatch-" + secrets.token_hex(6)
        save_config(cfg)
    return cfg


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
          status TEXT, error TEXT, sent_at TEXT, model TEXT, via TEXT, kind TEXT);
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
        if ocols and "kind" not in ocols:
            con.execute("ALTER TABLE outreach ADD COLUMN kind TEXT")
        cols = [r["name"] for r in con.execute("PRAGMA table_info(jobs)")]
        if "work_mode" not in cols:
            con.execute("ALTER TABLE jobs ADD COLUMN work_mode TEXT DEFAULT 'unknown'")
            for r in con.execute("SELECT id, source, title, location, description FROM jobs").fetchall():
                m = "remote" if r["source"] == "remotive" else classify_mode(r["title"], r["location"], r["description"])
                con.execute("UPDATE jobs SET work_mode=? WHERE id=?", (m, r["id"]))


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- scoring
_re_cache = {}


def term_re(term):
    t = term.lower().strip()
    if t not in _re_cache:
        _re_cache[t] = re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9#+])")
    return _re_cache[t]


def score_job(cfg, j):
    title = (j.get("title") or "").lower()
    body = " ".join([(j.get("description") or ""), (j.get("company") or "")]).lower()
    score, hits = 0, []
    for term, w in cfg["title_keywords"].items():
        if term_re(term).search(title):
            score += int(w)
            hits.append(term)
    title_pts = score
    body_pts = 0
    for term, w in cfg["body_keywords"].items():
        if term not in hits and term_re(term).search(body):
            body_pts += int(w)
            hits.append(term)
    score += min(body_pts, 4)  # the description can give at most 4 points
    for term, w in cfg["negative_keywords"].items():
        if term_re(term).search(title):
            score += int(w)
            hits.append("%s(%d)" % (term, int(w)))
    loc = (j.get("location") or "").lower()
    for term, w in (cfg.get("location_keywords") or {}).items():
        if term_re(term).search(loc):
            if int(w) > 0 and title_pts < 4:
                continue  # location bonus only for a relevant position
            score += int(w)
            hits.append("📍%s(%+d)" % (term, int(w)))
    if re.search(r",\s*[A-Z]{2}$", j.get("location") or ""):  # US allam (pl. "Louisville, CO")
        score -= 4
        hits.append("📍USA(-4)")
    score += j.get("bonus", 0)
    return score, hits


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


WORK_MODES = {"remote": "🏠 Remote", "hybrid": "🏢🏠 Hybrid", "onsite": "🏢 On-site", "unknown": "❔ Unknown"}
WT_LINKEDIN = {"2": "remote", "3": "hybrid", "1": "onsite"}


RE_HYB = re.compile(r"\bhybrid\b|hibrid|teilweise remote|\b\d\s*(?:-\s*\d\s*)?days?\s*(?:a|per)\s*week\s*(?:in|at|from)\s*(?:the|our)?\s*office|"
                    r"(?:in|at|from)\s*(?:the|our)\s*office\s*\d\s*(?:-\s*\d\s*)?days", re.I)
RE_ONS = re.compile(r"\bon-?site\b|\bin-office\b|office-based|fully in[- ]office|\birodai\b|vor ort|"
                    r"work(?:ing)? from (?:our|the) (?:office|studio)|relocat(?:e|ion) to", re.I)
RE_REM_STRONG = re.compile(r"fully remote|100\s*% remote|remote-first|remote first|\bfully distributed|work from anywhere|"
                           r"remote work is possible|remote (?:position|role|job|opportunity)|work(?:ing)? remotely|"
                           r"távmunka|teljesen remote", re.I)
RE_REM_TL = re.compile(r"\bremote\b|anywhere|home office|\bwfh\b", re.I)


def classify_mode(title="", location="", description=""):
    """Work mode. Title/location are strong signals; in the description only unambiguous phrases count."""
    tl = " ".join(x or "" for x in (title, location))
    if RE_HYB.search(tl):
        return "hybrid"
    if re.search(r"\bon-?site\b|in-office", tl, re.I):
        return "onsite"
    if RE_REM_TL.search(tl):
        return "remote"
    d = description or ""
    if RE_REM_STRONG.search(d) and not RE_HYB.search(d):
        return "remote"
    if RE_HYB.search(d):
        return "hybrid"
    if RE_ONS.search(d):
        return "onsite"
    if RE_REM_STRONG.search(d):
        return "remote"
    return "unknown"


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
    for line in cfg["linkedin_searches"]:
        done_short = set()
        parts = [p.strip() for p in line.split("|")]
        if not parts or not parts[0]:
            continue
        kw = parts[0]
        loc = parts[1] if len(parts) > 1 and parts[1] else "European Union"
        mode = parts[2].lower() if len(parts) > 2 else "remote"
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
    for q in cfg.get("remotive_queries", []):
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
    for q in cfg.get("remotive_queries", []):
        data = json.loads(fetch("https://remotive.com/api/remote-jobs?" + urllib.parse.urlencode({"search": q})))
        for j in data.get("jobs", []):
            out.append({"ext_id": str(j.get("id")), "title": j.get("title", ""),
                        "company": j.get("company_name", ""),
                        "location": j.get("candidate_required_location", ""), "url": j.get("url", ""),
                        "posted": j.get("publication_date", ""),
                        "description": strip_tags(j.get("description", ""))[:8000], "work_mode": "remote"})
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
        row = con.execute("SELECT description FROM jobs WHERE id=?", ("linkedin:" + it["ext_id"],)).fetchone()
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
            it["work_mode"] = classify_mode(it["title"], it["location"], d)
        time.sleep(6)
    return n


SOURCE_FUNCS = {"linkedin": src_linkedin, "meetfrank": src_meetfrank, "4dayweek": src_4dayweek,
                "flexa": src_flexa, "remotive": src_remotive, "studios": src_studios,
                "rss": src_rss, "custom": src_custom}
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


def enrich_pending_linkedin(cfg, limit=60):
    """Background job: fetches the description of LinkedIn postings that still have none,
    then updates their work mode and score in the database. Never blocks a refresh."""
    if not ENRICH_LOCK.acquire(blocking=False):
        return
    try:
        with db() as con:
            rows = con.execute("SELECT id, title, location FROM jobs WHERE source='linkedin' "
                               "AND (description IS NULL OR description='') ORDER BY first_seen DESC LIMIT ?",
                               (limit,)).fetchall()
            items = [{"ext_id": r["id"][len("linkedin:"):], "title": r["title"], "location": r["location"]}
                     for r in rows]
            if not items:
                return
            STATE["enrich"] = {"total": len(items), "done": 0}
            enrich_linkedin(con, items, limit=limit)
            STATE["enrich"]["done"] = sum(1 for it in items if it.get("description"))
        with db() as con:
            for it in items:
                if not it.get("description"):
                    continue
                jid = "linkedin:" + it["ext_id"]
                sc, hits = score_job(cfg, it)
                con.execute("UPDATE jobs SET description=?, work_mode=?, score=?, matched=? WHERE id=?",
                            (it["description"], it["work_mode"], sc, ", ".join(hits), jid))
        log.info("LinkedIn descriptions: %d updated", sum(1 for it in items if it.get("description")))
    except Exception:
        log.exception("LinkedIn background enrichment failed")
    finally:
        STATE["enrich"] = None
        ENRICH_LOCK.release()


def run_all(force=False):
    if not RUN_LOCK.acquire(blocking=False):
        return
    STATE["running"] = True
    try:
        with CFG_LOCK:
            cfg = load_config()
        to_notify = []
        # 1) decide which sources are due, 2) fetch them in parallel, 3) save them one by one in order
        due = []
        with db() as con:
            for name, fn in SOURCE_FUNCS.items():
                if cfg["sources"].get(name) and source_due(con, cfg, name, force):
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
            new = 0
            with db() as con:
                for it in items:
                    if not it.get("title") or not it.get("url", "").startswith("http"):
                        continue
                    jid = "%s:%s" % (name, it["ext_id"])
                    sc, hits = score_job(cfg, it)
                    # without an employer the title alone is not enough to call two postings the same
                    dup = norm(it["title"]) + "|" + (norm(it["company"]) or it["url"])
                    wm = it.get("work_mode") or classify_mode(it["title"], it["location"], it.get("description"))
                    ex_row = con.execute("SELECT id FROM jobs WHERE id=?", (jid,)).fetchone()
                    if ex_row:
                        if it.get("description") and name == "linkedin":
                            sc2, hits2 = score_job(cfg, it)
                            con.execute("UPDATE jobs SET last_seen=?, work_mode=?, description=?, score=?, matched=? WHERE id=?",
                                        (ts, wm, it["description"], sc2, ", ".join(hits2), jid))
                        elif it.get("work_mode"):  # an exact value from the source overrides the estimate
                            con.execute("UPDATE jobs SET last_seen=?, work_mode=? WHERE id=?", (ts, wm, jid))
                        else:
                            con.execute("UPDATE jobs SET last_seen=? WHERE id=?", (ts, jid))
                        continue
                    if con.execute("SELECT 1 FROM jobs WHERE dupkey=?", (dup,)).fetchone():
                        continue  # already seen from another source
                    notified = 0 if (seeded and sc >= cfg["notify_min_score"]
                                     and wm in cfg.get("notify_work_modes", ["remote", "unknown"])) else 1
                    con.execute(
                        "INSERT INTO jobs(id,source,title,company,location,url,posted,description,score,matched,"
                        "dupkey,first_seen,last_seen,status,notified,work_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'new',?,?)",
                        (jid, name, it["title"], it["company"], it["location"], it["url"], it["posted"],
                         it.get("description", ""), sc, ", ".join(hits), dup, ts, ts, notified, wm))
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
            prog = STATE["progress"]
            prog["done"] += 1
            prog["sources"][name] = ("error" if err else "done") + " (%d)" % len(items)
            log.info("%s: %d postings, %d new%s", name, len(items), new, " ERROR: " + err if err else "")
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
        if any(name == "linkedin" for name, _, _ in due):
            # descriptions for LinkedIn postings come later, in the background, so the list is not held up
            threading.Thread(target=enrich_pending_linkedin, args=(cfg,), daemon=True).start()
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
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,Segoe UI,Roboto,Inter,sans-serif}
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


def page(title, body, flash="", refresh=0):
    cur = getattr(REQ, "path", "/")
    tabs = [("/", "Jobs"), ("/docs", "Documents"), ("/outreach", "Recruiters"), ("/sources", "Sources"),
            ("/profile", "CV profile"), ("/settings", "Settings")]
    nav = "".join('<a role="tab" class="tab%s" href="%s">%s</a>' % (
        " tab-active" if (cur == h or (h == "/" and cur.startswith("/cv/"))) else "", h, l) for h, l in tabs)
    running = '<span class="badge badge-soft badge-warning">⏳ refresh running…</span>' if STATE["running"] else ""
    return ("""<!doctype html><html lang="hu"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">%s
<script>document.documentElement.dataset.theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";</script>
<title>%s · jobwatch</title>
<link href="https://cdn.jsdelivr.net/npm/daisyui@5.0.0" rel="stylesheet" type="text/css"><style>%s</style></head><body><header><div class="bar"><span class="brand">💼 jobwatch</span><nav class="tabs tabs-box tabs-sm">%s</nav>
<div class="right">%s<form method="post" action="/test"><button class="btn btn-sm" name="what" value="ntfy" title="Test push notification to your phone">🔔 Test push</button></form>
<form method="post" action="/run"><button class="btn btn-primary btn-sm" title="Fetch all sources now">↻ Refresh</button></form></div></div></header>
<main>%s%s</main></body></html>"""
            % (('<meta http-equiv="refresh" content="%d">' % refresh) if refresh else "", e(title), CSS, nav, running,
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
    src = q.get("source", "")
    try:
        mn = int(q.get("min", cfg["min_score"]))
    except ValueError:
        mn = cfg["min_score"]
    text = q.get("q", "").strip()
    wf = q.get("work", cfg.get("default_work_filter", "remote_unknown"))
    sort = q.get("sort", "posted")
    age = q.get("age", "all")
    sql, args = "SELECT * FROM jobs WHERE score>=?", [mn]
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
    if src:
        sql += " AND source=?"
        args.append(src)
    if text:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
        args += ["%" + text + "%"] * 3
    with db() as con:
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        counts = {r["status"]: r["c"] for r in con.execute(
            "SELECT status, COUNT(*) c FROM jobs WHERE score>=? GROUP BY status", (mn,))}
        last_ok = {r["source"]: r["last_ok"] for r in con.execute("SELECT source, last_ok FROM source_state")}
        wcount = {r["m"]: r["c"] for r in con.execute(
            "SELECT COALESCE(work_mode,'unknown') m, COUNT(*) c FROM jobs WHERE score>=? AND status IN ('new','seen','saved') GROUP BY m", (mn,))}

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
            "SELECT posted, first_seen, status FROM jobs WHERE score>=? AND status IN ('new','seen','saved')"
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
    src_items = [("", "All sources")] + [(k, v[0]) for k, v in SOURCES_INFO.items()]
    hidden_inputs = "".join('<input type="hidden" name="%s" value="%s">' % (k, e(v))
                            for k, v in base_q.items() if k in ("status", "work", "sort", "age"))
    more_open = " open" if (src or text or mn != cfg["min_score"]) else ""
    filt = """<div class="panel">
<div class="row-chips"><span class="lbl">Status</span>%s</div>
<div class="row-chips"><span class="lbl">Work mode</span>%s</div>
<div class="row-chips"><span class="lbl">Sort</span>%s</div>
<div class="row-chips"><span class="lbl">Age</span>%s</div>
<details class="more"%s><summary>More filters (source, score, search)</summary>
<form class="filters" method="get">%s<select name="source">%s</select>
<label style="margin:0;font-weight:400">Min. score <input type="number" name="min" value="%d" style="width:70px"></label>
<input type="search" name="q" value="%s" placeholder="Search: title, company, location"><button>Filter</button>
<a class="btn" href="/">Alaphelyzet</a></form></details></div>""" % (
        st_chips, wm_chips, sort_chips, age_chips, more_open, hidden_inputs, opts(src, src_items), mn, e(text))
    head = """<div class="listhead"><span><b>%d</b> posting%s · age refers to <b>%s</b> · notifications from %d pts</span>
<form method="post" action="/mark-all-seen"><button class="sm">Mark all new → seen</button></form></div>""" % (
        total, " (first 300 shown)" if total > 300 else "", "collected" if sort == "seen" else "posted",
        cfg["notify_min_score"])

    with db() as con:
        has_cv = {r["job_id"] for r in con.execute("SELECT job_id FROM cvs WHERE status='done'")}
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
        chips = ['<span class="chip wm-%s">%s</span>' % (e(r["work_mode"] or "unknown"),
                                                         e(WORK_MODES.get(r["work_mode"] or "unknown", "")))]
        if r["location"] and r["location"].strip().lower() not in ("remote", "(remote)", "anywhere"):
            chips.append('<span class="chip">📍 %s</span>' % e(r["location"][:60]))
        if r["_closed"]:
            chips.append('<span class="chip closed">❌ probably closed</span>')
        if r["_posted_known"]:
            cls_age = "age-new" if r["_pdays"] <= 2 else ("age-old" if r["_pdays"] > 30 else "")
            chips.append('<span class="chip %s" title="Feladva: %s">📅 %s</span>' % (
                cls_age, e(r["_posted"].astimezone().strftime("%Y-%m-%d")),
                e(("feladva " + rel_day(r["_pdays"])) if r["_pdays"] > 2 else "🆕 " + rel_day(r["_pdays"]))))
        else:
            chips.append('<span class="chip" title="The source does not give the posting date">📅 posted date unknown</span>')
        chips.append('<span class="chip" title="Collected: %s">📥 %s</span>' % (e(fmt_dt(r["first_seen"])), e(rel_day(r["_sdays"]))))
        if r["status"] != "new":
            chips.append('<span class="chip st">%s</span>' % e(STATUS_LABEL.get(r["status"], r["status"])))
        icon_btns = "".join('<button name="status" value="%s" title="%s">%s</button>' % (s_, t, ic) for s_, ic, t in (
            ("saved", "⭐", "Save"), ("applied", "✅", "Applied"), ("seen", "👁", "Mark seen"),
            ("hidden", "🚫", "Hide"), ("new", "↺", "Mark new again")) if s_ != r["status"])
        cv_btn = ('<a class="btn" href="/cv/%s">📄 Documents ✓</a>' if r["id"] in has_cv else
                  '<a class="btn primary" href="/cv/%s">📄 CV + Resume</a>') % qid
        cards.append("""<article class="job st-%s%s"><div class="score %s" title="Score against your CV">%d<small>pts</small></div>
<div><div class="jt"><a href="/open/%s" target="_blank" rel="noopener noreferrer">%s</a></div>
<div class="co"><b>%s</b> <span class="src">· %s</span></div><div class="chips">%s</div>
<details class="why"><summary>Why this score?</summary>%s</details></div>
<form class="acts" method="post" action="/job/%s">%s<div class="icons">%s</div></form></article>""" % (
            e(r["status"]), " stale" if (r["_closed"] or r["_pdays"] > 30) else "", cls, sc,
            qid, e(r["title"]), e(r["company"] or "?"), e(SOURCES_INFO.get(r["source"], (r["source"],))[0]),
            "".join(chips), e(r["matched"] or "-"), qid, cv_btn, icon_btns))
    if not cards:
        cards.append('<div class="card muted">No postings match the filters. Try loosening them, or press “Refresh”.</div>')
    busy = bool(STATE.get("progress") or STATE.get("enrich") or STATE["running"])
    return page("Jobs", progress_html() + stats + filt + head + "".join(cards), refresh=3 if busy else 0)


def sources_page():
    with db() as con:
        st = {r["source"]: r for r in con.execute("SELECT * FROM source_state")}
        cnt = {r["source"]: r["c"] for r in con.execute("SELECT source, COUNT(*) c FROM jobs GROUP BY source")}
    with CFG_LOCK:
        cfg = load_config()
    rows = []
    for k, (name, url) in SOURCES_INFO.items():
        s = st.get(k)
        on = cfg["sources"].get(k)
        status = ('<span class="err">%s</span>' % e(s["error"])) if s and s["error"] else (
            '<span class="ok">OK</span>' if s else '<span class="muted">not run yet</span>')
        rows.append("<tr><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            ('<a href="%s" target="_blank" rel="noopener">%s</a>' % (e(url), e(name))) if url else e(name),
            "" if on else ' <span class="badge">kikapcsolva</span>',
            e(fmt_dt(s["last_run"])) if s else "-", s["found"] if s else "-", s["new"] if s else "-",
            cnt.get(k, 0), status))
    uns = "".join('<tr><td>%s</td><td class="muted">%s</td></tr>' % (
        ('<a href="%s" target="_blank" rel="noopener">%s</a>' % (e(u), e(n))) if u else e(n), e(why)) for n, u, why in UNSUPPORTED)
    body = """<div class="card"><h2>Watched sources</h2><table><tr><th>Source</th><th>Last run</th><th>Fetched</th><th>New</th><th>Total</th><th>Status</th></tr>%s</table>
<p class="muted">Refresh every %d minutes (Remotive at most every 6 hours).</p></div>
<div class="card"><h2>Sites that cannot be watched automatically</h2><p class="muted">Check these manually now and then.</p><table>%s</table></div>""" % (
        "".join(rows), cfg["interval_minutes"], uns)
    return page("Sources", body)


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
    f = lambda name, label, val, extra="": (
        '<div><label>%s</label><input name="cs_%s_%s" value="%s" style="width:100%%"%s></div>' % (label, i, name, e(val), extra))
    head = ('<label style="font-weight:400"><input type="checkbox" name="cs_%s_enabled"%s> enabled</label> '
            '<label style="font-weight:400;margin-left:14px"><input type="checkbox" name="cs_%s_delete"> delete</label>'
            % (i, " checked" if src.get("enabled", True) else "", i)) if i != "new" else "<b>➕ Add a new source</b>"
    return ('<div style="border:1px solid #8884;border-radius:8px;padding:10px;margin:10px 0">%s'
            '<input type="hidden" name="cs_%s_id" value="%s"><div class="row">'
            '<div><label>Type</label><select name="cs_%s_type">%s</select></div>%s%s%s</div><div class="row">%s%s'
            '<div><label>Password %s</label><input type="password" name="cs_%s_password" autocomplete="new-password" '
            'placeholder="(leave empty to keep)" style="width:100%%"></div></div></div>') % (
        head, i, e(src.get("id", "")), i, opts,
        f("name", "Name", src.get("name", ""), ' placeholder="e.g. munka.hu"'),
        f("url", "URL (search result page / page / feed)", src.get("url", ""), ' placeholder="https://vmp.munka.hu/allas/talalatok?kulcsszo=..."'),
        f("pattern", "Link pattern (only for “any web page”)", src.get("pattern", ""), ' placeholder="/job/"'),
        f("login_url", "Login page (empty = default for VMP)", src.get("login_url", "")),
        f("username", "Username (only if login is needed)", src.get("username", ""), ' autocomplete="off"'),
        "(set ✓)" if src.get("password") else "", i)


def custom_sources_card(cfg):
    blocks = "".join(custom_source_fields(i, s) for i, s in enumerate(cfg.get("custom_sources") or []))
    return ('<div class="card"><h2>🧩 Own sources</h2><p class="muted">Add any job site. <b>munka.hu VMP</b>: search on '
            '<a href="https://vmp.munka.hu/allas/egyszeru_kereses" target="_blank" rel="noopener">vmp.munka.hu</a>, then paste '
            'the address of the result page. With your VMP username and password the employer is shown too. '
            '<b>Any web page</b>: every link on the page whose address contains the pattern becomes a posting. '
            'Passwords are stored only on this machine (in the settings file). “Test own sources” tries them without saving postings.</p>'
            '%s%s<input type="hidden" name="cs_count" value="%d"></div>') % (
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


def settings_page(flash=""):
    with CFG_LOCK:
        cfg = load_config()
    n, m = cfg["ntfy"], cfg["email"]
    chk = lambda b: " checked" if b else ""
    src_boxes = "".join('<label style="font-weight:400;display:inline-block;margin-right:14px"><input type="checkbox" name="src_%s"%s> %s</label>' % (
        k, chk(cfg["sources"].get(k)), e(v[0])) for k, v in SOURCES_INFO.items())
    sub = "%s/%s" % (n["server"].rstrip("/"), n["topic"])
    body = """<div class="card"><h2>🔔 Test notification</h2><form method="post" action="/test" style="display:flex;gap:8px;flex-wrap:wrap">
<button class="primary" name="what" value="ntfy">🔔 Send a test push to your phone</button><button name="what" value="email">✉️ Test e-mail</button></form></div>
<form method="post" action="/settings">
<div class="card"><h2>📱 Push notifications (ntfy)</h2>
<p class="muted">1. Install the <b>ntfy</b> app (Android: Play Store/F-Droid, iPhone: App Store). 2. In the app press “+” → Subscribe to topic → enter: <b>%s</b> (server: %s). 3. Press “Test push” below.<br>The topic name is also the password: anyone who has it can see the notifications.</p>
<label><input type="checkbox" name="ntfy_enabled"%s> Push notifications enabled</label>
<div class="row"><div><label>Szerver</label><input name="ntfy_server" value="%s" style="width:100%%"></div>
<div><label>Topic</label><input name="ntfy_topic" value="%s" style="width:100%%"></div></div>
<p class="muted">Subscribe link: <a href="%s" target="_blank" rel="noopener">%s</a></p></div>
<div class="card"><h2>✉️ E-mail notifications</h2>
<p class="muted">Gmail needs an <b>app password</b> (Google account → Security → 2-Step Verification → App passwords), not your normal password.</p>
<label><input type="checkbox" name="email_enabled"%s> E-mail notifications enabled</label>
<div class="row"><div><label>SMTP szerver</label><input name="smtp_host" value="%s" style="width:100%%"></div>
<div><label>Port</label><input name="smtp_port" value="%s" style="width:100%%"></div>
<div><label>User (e-mail address)</label><input name="smtp_user" value="%s" style="width:100%%"></div>
<div><label>Password %s</label><input type="password" name="smtp_pass" placeholder="(leave empty to keep)" style="width:100%%"></div>
<div><label>Recipient (empty = sender)</label><input name="email_to" value="%s" style="width:100%%"></div></div></div>
<div class="card"><h2>🤖 Claude API (CV tailored to a posting)</h2>
<p class="muted">Key: <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com → API Keys</a>. One CV + cover letter costs about 5–15 cents. The key is stored only on this machine.</p>
<label>Anthropic API key %s</label><input type="password" name="anthropic_api_key" placeholder="(leave empty to keep)" style="width:100%%"></div>
<div class="card"><h2>⚙️ General</h2><div class="row">
<div><label>Refresh (minutes)</label><input type="number" name="interval" value="%d" min="15"></div>
<div><label>Min. score in list</label><input type="number" name="min_score" value="%d"></div>
<div><label>Min. score for notifications</label><input type="number" name="notify_min_score" value="%d"></div>
<div><label>Notify for</label>%s</div>
<div><label>Default list filter</label><select name="default_work_filter">%s</select></div>
<div><label>Web address (link in notifications)</label><input name="public_url" value="%s" style="width:100%%"></div></div>
<label>Sources</label>%s</div>
<div class="card"><h2>🔎 LinkedIn searches</h2><p class="muted">One per line: <code>keywords | location</code> (the work mode is taken from the posting text, because LinkedIn's public filter does not work). Postings from the last 7 days.</p>
<textarea name="linkedin_searches">%s</textarea>
<label>Company job boards (one per line: <code>ashby|lever|greenhouse|bamboohr|smartrecruiters | id | name</code>)</label>
<textarea name="studios" style="min-height:140px">%s</textarea>
<label>Own RSS feeds (one URL per line)</label><textarea name="rss_feeds" style="min-height:60px">%s</textarea></div>
%s
<div class="card"><h2>🎯 Scoring (based on your CV)</h2><p class="muted">One per line: <code>term = points</code>. Terms found in the title count the most; negative terms apply only to the title. Every posting is re-scored when you save.</p>
<div class="row"><div><label>Title</label><textarea name="title_keywords" style="min-height:260px">%s</textarea></div>
<div><label>Description</label><textarea name="body_keywords" style="min-height:260px">%s</textarea></div>
<div><label>Negative (title)</label><textarea name="negative_keywords" style="min-height:260px">%s</textarea></div>
<div><label>Location</label><textarea name="location_keywords" style="min-height:260px">%s</textarea></div></div></div>
<button class="primary">Save</button></form>
<div class="card" style="margin-top:14px"><h2>Test</h2><form method="post" action="/test" style="display:flex;gap:8px">
<button name="what" value="ntfy">Test push</button><button name="what" value="email">Test e-mail</button><button name="what" value="custom">🧩 Test own sources</button></form><form method="post" action="/reset-jobs" style="margin-top:10px" onsubmit="return confirm('Delete all found jobs and source status? Settings, profile, CVs and documents are kept.')"><button name="reset" value="1">Reset job cache (delete found jobs)</button> <span class="muted">Settings, profile, CVs and documents are kept; the next refresh starts from scratch.</span></form></div>""" % (
        e(n["topic"]), e(n["server"]), chk(n.get("enabled")), e(n["server"]), e(n["topic"]), e(sub), e(sub),
        chk(m.get("enabled")), e(m["smtp_host"]), e(m["smtp_port"]), e(m["username"]),
        "(set ✓)" if m.get("password") else "", e(m["to"]),
        "(set ✓)" if cfg.get("anthropic_api_key") else "",
        cfg["interval_minutes"], cfg["min_score"], cfg["notify_min_score"],
        "".join('<label style="font-weight:400;display:inline-block;margin-right:10px"><input type="checkbox" name="nwm_%s"%s> %s</label>' % (
            k, chk(k in cfg.get("notify_work_modes", ["remote", "unknown"])), e(v)) for k, v in WORK_MODES.items()),
        "".join('<option value="%s"%s>%s</option>' % (k, " selected" if k == cfg.get("default_work_filter", "remote_unknown") else "", l)
                for k, l in (("remote", "Remote only"), ("remote_unknown", "Remote + unknown"), ("remote_hybrid", "Remote + hybrid"), ("all", "All"))), e(cfg.get("public_url", "")), src_boxes,
        e("\n".join(cfg["linkedin_searches"])), e("\n".join(cfg.get("studios") or [])),
        e("\n".join(cfg.get("rss_feeds") or [])), custom_sources_card(cfg),
        e(kv_text(cfg["title_keywords"])), e(kv_text(cfg["body_keywords"])), e(kv_text(cfg["negative_keywords"])),
        e(kv_text(cfg.get("location_keywords") or {})))
    return page("Settings", body, flash)


def save_settings(f):
    g = lambda k, d="": f.get(k, d)
    with CFG_LOCK:
        cfg = load_config()
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
        cfg["min_score"] = int(g("min_score", "4") or 4)
        cfg["notify_min_score"] = int(g("notify_min_score", "7") or 7)
        cfg["notify_work_modes"] = [k for k in WORK_MODES if ("nwm_" + k) in f]
        cfg["default_work_filter"] = g("default_work_filter", "remote_unknown")
        cfg["public_url"] = g("public_url").strip()
        cfg["sources"] = {k: ("src_" + k) in f for k in SOURCES_INFO}
        cfg["linkedin_searches"] = [l.strip() for l in g("linkedin_searches").splitlines() if l.strip()]
        cfg["studios"] = [l.strip() for l in g("studios").splitlines() if l.strip().count("|") >= 1]
        cfg["rss_feeds"] = [l.strip() for l in g("rss_feeds").splitlines() if l.strip().startswith("http")]
        cfg["custom_sources"] = parse_custom_sources(f, cfg.get("custom_sources"))
        for k in ("title_keywords", "body_keywords", "negative_keywords", "location_keywords"):
            cfg[k] = parse_kv(g(k))
        save_config(cfg)
    rescore_all()


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


OUTREACH_JS = """<script>
function ocount(id, lim) { var t = document.getElementById('om' + id), c = document.getElementById('on' + id);
  c.textContent = t.value.length + ' / ' + lim + ' characters'; c.style.color = t.value.length > lim ? '#d33' : ''; }
function ocopy(id) { var t = document.getElementById('om' + id), b = document.getElementById('oc' + id), ok = false;
  t.focus(); t.select(); try { ok = document.execCommand('copy'); } catch (err) {}
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(t.value); ok = true; }
  b.textContent = ok ? '✅ Copied' : 'Selected: press Ctrl+C'; }
</script>"""


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
    job_opts = '<option value="">— no specific posting (general message) —</option>' + "".join(
        '<option value="%s"%s>%s – %s</option>' % (e(j["id"]), " selected" if j["id"] == picked else "",
                                                   e(j["title"]), e(j["company"] or "?")) for j in jobs)
    ch_opts = "".join('<option value="%s">%s (max %d characters)</option>' % (k, e(v[0]), v[1])
                      for k, v in cvgen.OUTREACH_CHANNELS.items())
    form = """<div class="card"><h2>✉️ Message recruiters</h2>
<p class="muted">Claude writes a short, personal first message to each recruiter from your CV profile (your contact details are not sent to the API).
LinkedIn does not allow sending messages from other programs, so for LinkedIn you copy the text and open the recruiter's profile with one click; e-mails can be sent from here.</p>
<form method="post" action="/outreach/generate" onsubmit="return rsubmit()">
<h3>1. Recruiters</h3>
<div class="row" id="radd">
<div style="flex:2 1 320px"><label>LinkedIn profile link (name and title fill in by themselves) or e-mail</label><input id="r_contact" placeholder="https://www.linkedin.com/in/…" onkeydown="renter(event)" onpaste="setTimeout(rlookup, 0)" onchange="rlookup()" style="width:100%%"></div>
<div><label>Name *</label><input id="r_name" placeholder="Anna Kovács" onkeydown="renter(event)"></div>
<div><label>Title @ company</label><input id="r_title" placeholder="Talent Acquisition @ Supercell" onkeydown="renter(event)"></div>
<div><label>Recruiter works</label><select id="r_kind"><option value="">automatic (from the title)</option><option value="inhouse">in-house: hires for their own company</option><option value="agency">agency / freelance: hires for clients</option></select></div>
<div style="align-self:end"><button type="button" onclick="radd()">➕ Add</button></div></div>
<p class="muted" id="rhint"></p>
<ul id="rlist" style="list-style:none;padding:0;margin:8px 0"></ul>
<input type="hidden" name="recruiters_json" id="rjson" value="[]">
<h3>2. Message</h3>
<div class="row"><div><label>Posting (optional)</label><select name="job_id" style="max-width:100%%">%s</select></div>
<div><label>Channel</label><select name="channel">%s</select></div>
<div><label>Language</label><select name="language"><option value="auto">automatic</option><option value="en">English</option><option value="hu">Hungarian</option><option value="de">German</option></select></div></div>
<label>Mutual connection who could introduce you (only for the “ask a mutual connection” channel): <code>Name | how you know each other</code></label>
<input name="via" style="width:100%%" placeholder="Péter Nagy | we worked together at Gameloft 2015–2017">
<p class="muted">On LinkedIn, open the recruiter's profile and click “mutual connections” to see who knows both of you.</p>
<label>Note for Claude (optional, e.g. “LinkedIn suggested them for the Unity Developer role”)</label><input name="note" style="width:100%%">
<p><button class="primary" id="rgo">✨ Write messages (Claude)</button> <span class="muted">About 1–3 cents per message.</span></p></form></div>
<script>
var RL = [];
function rval(id) { return document.getElementById(id).value.trim(); }
function rdraw() {
  var ul = document.getElementById('rlist'); ul.innerHTML = '';
  RL.forEach(function (r, i) {
    var li = document.createElement('li'); li.style.cssText = 'display:flex;gap:8px;align-items:center;padding:4px 0;border-bottom:1px solid #8883';
    var t = document.createElement('span'); t.style.flex = '1';
    t.textContent = r.name + (r.title ? ' · ' + r.title : '') + (r.contact ? ' · ' + r.contact : '') +
      (r.kind ? (r.kind === 'agency' ? ' · agency' : ' · in-house') : '');
    var b = document.createElement('button'); b.type = 'button'; b.className = 'btn btn-sm'; b.textContent = '✕';
    b.title = 'Remove'; b.onclick = function () { RL.splice(i, 1); rdraw(); };
    li.appendChild(t); li.appendChild(b); ul.appendChild(li);
  });
  document.getElementById('rjson').value = JSON.stringify(RL);
  document.getElementById('rgo').textContent = '✨ Write ' + (RL.length || '') + ' message' + (RL.length === 1 ? '' : 's') + ' (Claude)';
  document.getElementById('rhint').textContent = RL.length ? '' : 'Fill in the fields and press “Add” (or Enter) for each recruiter.';
}
var RLOOK = null;
function rlookup() {  // LinkedIn link -> name + title from the profile's public link preview
  var url = rval('r_contact'), hint = document.getElementById('rhint');
  if (!/linkedin\\.com\\/in\\//i.test(url)) return Promise.resolve();
  hint.textContent = '⏳ Reading the profile link…';
  RLOOK = fetch('/outreach/lookup?url=' + encodeURIComponent(url)).then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok) { hint.textContent = d.error || ''; return; }
    if (rval('r_contact') === url) document.getElementById('r_contact').value = d.url;
    if (!rval('r_name')) document.getElementById('r_name').value = d.name || '';
    if (!rval('r_title')) document.getElementById('r_title').value = d.title || '';
    hint.textContent = d.from === 'profile' ? '✅ Filled in from the profile. Check it, then press Add.'
      : 'The profile is not public, so only the name was guessed from the link: check it and add the title by hand.';
  }).catch(function () { hint.textContent = 'Could not read the link: fill in the name by hand.'; })
    .then(function () { RLOOK = null; });
  return RLOOK;
}
function radd() {
  if (!rval('r_name') && /linkedin\\.com\\/in\\//i.test(rval('r_contact'))) {  // only the link was given
    (RLOOK || rlookup()).then(function () { if (rval('r_name')) radd(); });
    return false;
  }
  if (!rval('r_name')) { document.getElementById('r_name').focus(); return false; }
  RL.push({name: rval('r_name'), title: rval('r_title'), contact: rval('r_contact'), kind: document.getElementById('r_kind').value});
  ['r_name', 'r_title', 'r_contact', 'r_kind'].forEach(function (id) { document.getElementById(id).value = ''; });
  rdraw(); document.getElementById('r_name').focus(); return true;
}
function renter(ev) { if (ev.key === 'Enter') { ev.preventDefault(); radd(); } }
function rsubmit() {  // a filled-in but not yet added recruiter counts too
  if (rval('r_name')) radd();
  if (!RL.length) { document.getElementById('rhint').textContent = 'Add at least one recruiter.'; return false; }
  return true;
}
rdraw();
</script>""" % (job_opts, ch_opts)
    cards = []
    for r in rows:
        label, limit, _ = cvgen.OUTREACH_CHANNELS.get(r["channel"], ("?", 1000, ""))
        oid = r["id"]
        status = {"running": "⏳ writing…", "draft": "draft", "sent": "✅ sent %s" % fmt_dt(r["sent_at"]),
                  "error": "❌ error"}.get(r["status"], r["status"])
        head = '<div class="jt"><b>%s</b>%s%s</div><div class="chips"><span class="chip">%s</span>%s<span class="chip">%s</span></div>' % (
            e(r["name"]), (" · " + e(r["title"])) if r["title"] else "",
            (' <span class="muted">— via %s (send the message to them)</span>' % e(r["via"])) if r["via"] else "", e(label),
            ('<span class="chip">📌 %s – %s</span>' % (e(r["job_title"]), e(r["job_company"] or "?"))) if r["job_title"] else "",
            e(status))
        if r["status"] == "running":
            cards.append('<div class="card">%s<p class="muted">Claude is writing the message…</p></div>' % head)
            continue
        if r["status"] == "error":
            cards.append('<div class="card">%s<p class="err">%s</p><form method="post" action="/outreach/%d">'
                         '<button name="act" value="regen">🔄 Try again</button> <button name="act" value="delete">🗑 Delete</button></form></div>'
                         % (head, e(r["error"]), oid))
            continue
        is_mail = "@" in (r["contact"] or "") and not (r["contact"] or "").startswith("http")
        open_btn = ('<a class="btn" href="%s" target="_blank" rel="noopener noreferrer">🔗 Open profile</a>' % e(safe_url(r["contact"]))
                    if (r["contact"] or "").startswith("http") else "")
        send_btn = ('<button name="act" value="send" title="Send to %s with the e-mail settings">✉️ Send e-mail</button>' % e(r["contact"])
                    if is_mail and r["channel"] == "email" else "")
        subj = ('<label>Subject</label><input name="subject" value="%s" style="width:100%%">' % e(r["subject"] or "")
                if r["channel"] == "email" else "")
        cards.append("""<div class="card">%s<form method="post" action="/outreach/%d">%s
<textarea id="om%d" name="message" style="min-height:%dpx" oninput="ocount(%d,%d)">%s</textarea>
<div class="muted" id="on%d"></div><script>ocount(%d,%d)</script>
<p style="display:flex;gap:6px;flex-wrap:wrap"><button type="button" id="oc%d" onclick="ocopy(%d)">📋 Copy</button>%s%s
<button name="act" value="save">💾 Save edits</button><button name="act" value="sent">✅ Mark as sent</button>
<button name="act" value="regen">🔄 Rewrite</button><button name="act" value="delete">🗑 Delete</button></p></form></div>""" % (
            head, oid, subj, oid, 90 if limit <= 300 else 200, oid, limit, e(r["message"] or ""),
            oid, oid, limit, oid, oid, open_btn, send_btn))
    busy = any(r["status"] == "running" for r in rows)
    return page("Recruiters", OUTREACH_JS + form + "".join(cards), flash, refresh=3 if busy else 0)


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
            return "Marked as sent."
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
    return {"ok": True, "url": clean, "name": name.strip() or guess["name"], "title": headline.strip(), "from": "profile"}


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
                        "kind": it.get("kind") if it.get("kind") in ("inhouse", "agency") else ""})
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
            cur = con.execute("INSERT INTO outreach(created,name,title,contact,channel,language,job_id,note,status,via,kind) "
                              "VALUES(?,?,?,?,?,?,?,?,'running',?,?)",
                              (now_iso(), rc["name"], rc["title"], rc["contact"], channel,
                               f.get("language", "auto"), f.get("job_id") or None, f.get("note", "").strip()[:500], via, kind))
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
    head = '<p><a href="/">← Back to jobs</a></p><div class="card"><h2>📄 <a href="%s" target="_blank" rel="noopener">%s</a></h2><div class="meta">%s · %s · %s · %d pont</div><p><a class="btn" href="/outreach?job=%s">✉️ Message a recruiter about this posting</a></p></div>' % (
        e(safe_url(job["url"])), e(job["title"]), e(job["company"]), e(job["location"] or "-"),
        e(WORK_MODES.get(job.get("work_mode") or "unknown", "")), job["score"], qid)
    if cv and cv["status"] == "running":
        return page("Generating CV", head + '<div class="card"><p>⏳ Claude is now writing the CV, Resume and cover letter tailored to the posting (about 1–2 minutes). The page refreshes itself.</p></div>', flash, refresh=5)
    desc = cv["description"] if cv and cv["description"] else cvgen.fetch_description(fetch, strip_tags, job)
    gen_form = """<div class="card"><h2>%s</h2>%s<form method="post" action="/cv/%s/generate">
<label>Posting text (Claude works from this; you can fix or extend it)</label><textarea name="description" style="min-height:220px">%s</textarea>
<p class="muted">Only facts from your CV profile go in; your contact details are not sent to the API.</p>
<button class="primary">%s</button></form></div>""" % (
        "Regenerate" if cv and cv["status"] == "done" else "Create a CV tailored to this posting",
        ('<p class="err">Hiba: %s</p>' % e(cv["error"])) if cv and cv["status"] == "error" else "",
        qid, e(desc), "🔄 Regenerate" if cv and cv["status"] == "done" else "✨ Generate CV + Resume + cover letter (Claude)")
    if not cv or cv["status"] != "done":
        return page("CV", head + gen_form, flash)
    data = json.loads(cv["data"])
    notes = "".join("<li>%s</li>" % e(n) for n in data.get("match_notes", []))
    emails = cvgen.find_emails(desc)
    sent = ('<p class="ok">✅ Sent: %s → %s</p>' % (e(fmt_dt(cv["sent_at"])), e(cv["sent_to"]))) if cv["sent_at"] else ""
    paths = cv_paths(jid)
    folder = smb_path(os.path.dirname(paths["cv"])) if paths["cv"] else ""
    body = head + """<div class="card"><h2>Documents</h2><p><a class="btn primary" href="/cv/%s/cv.pdf" target="_blank">⬇️ CV (detailed)</a>
<a class="btn primary" href="/cv/%s/resume.pdf" target="_blank">⬇️ Resume (1 page)</a>
<a class="btn" href="/cv/%s/letter.pdf" target="_blank">⬇️ Cover letter</a></p>
<p class="muted">Folder (on a Mac: Finder → Go → Connect to Server): <code>%s</code></p>
<form method="post" action="/cv/%s/photo" style="margin:6px 0">Photo on the CV: <b>%s</b> &nbsp;<button name="photo" value="%s">%s</button>
<span class="muted"> (by default: no photo for US/UK/Canada/Australia locations)</span></form>
<p><b>%s</b></p><p>%s</p><label>Notes for you</label><ul>%s</ul><p class="muted">Model: %s · %s</p></div>
<div class="card"><h2>✉️ Send by e-mail to the company</h2>%s
<p class="muted">It sends only when you press the button (and confirm). It is sent from your Gmail.%s</p>
<form method="post" action="/cv/%s/send" onsubmit="return confirm('Really send the application to this address: ' + this.to.value + ' ?')">
<label>Recipient (recruiter's e-mail address)</label><input name="to" value="%s" style="width:100%%" required type="email">
<label>Subject</label><input name="subject" value="%s" style="width:100%%">
<label>Message</label><textarea name="body" style="min-height:260px;font-family:inherit;font-size:14px">%s</textarea>
<label>Attachment</label>
<label style="font-weight:400;display:inline-block;margin-right:12px"><input type="radio" name="attach" value="cv" checked> CV</label>
<label style="font-weight:400;display:inline-block;margin-right:12px"><input type="radio" name="attach" value="resume"> Resume</label>
<label style="font-weight:400;display:inline-block;margin-right:12px"><input type="radio" name="attach" value="both"> Both</label>
<label style="font-weight:400"><input type="checkbox" name="attach_letter"> + cover letter as PDF too</label>
<button class="primary">📨 Send application</button></form></div>
<div class="card"><h2>Manual editing</h2><p class="muted">The CV, the Resume (<code>resume</code> section) and the cover letter content are in JSON. Saving rebuilds the PDFs (no API call).</p>
<form method="post" action="/cv/%s/save"><textarea name="data" style="min-height:300px">%s</textarea><button>💾 Save and rebuild PDFs</button></form></div>""" % (
        qid, qid, qid, e(folder), qid, "yes" if data.get("_photo", True) else "no",
        "off" if data.get("_photo", True) else "on", "Remove photo" if data.get("_photo", True) else "Add photo",
        e(data["headline"]), e(data["summary"]), notes, e(data.get("_model", "")), e(fmt_dt(cv["created"])),
        sent, (" E-mail addresses found in the posting: " + e(", ".join(emails))) if emails else " No e-mail address found in the posting; in most places you have to upload the PDF on the application form.",
        qid, e(emails[0] if emails else ""), e(data.get("email_subject", "")), e(data["cover_letter"]),
        qid, e(json.dumps({k: v for k, v in data.items() if not k.startswith("_")}, ensure_ascii=False, indent=2)))
    return page("CV", body + gen_form, flash)


def docs_page():
    if not os.path.isdir(DOCS_ROOT):
        return page("Documents", '<div class="card muted">No documents generated yet. On a posting, press the “📄 CV + Resume” button.</div>')
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
            files = "".join('<a class="btn sm" href="/docs/file?p=%s" target="_blank">%s</a>' % (
                e(urllib.parse.quote(os.path.join(rel, fn))), e(DOC_LABEL[k])) for k, fn in doc_names().items()
                if os.path.exists(os.path.join(d, fn)))
            if os.path.exists(os.path.join(d, "hirdetes.txt")):
                files += '<a class="btn sm" href="/docs/file?p=%s" target="_blank">Posting text</a>' % e(urllib.parse.quote(os.path.join(rel, "hirdetes.txt")))
            older = len(os.listdir(os.path.join(d, "korabbi"))) if os.path.isdir(os.path.join(d, "korabbi")) else 0
            s_ = sent.get(info.get("job_id"))
            status = ('<span class="chip wm-remote">✅ sent %s → %s</span>' % (e(fmt_dt(s_["sent_at"])), e(s_["sent_to"]))) if s_ and s_["sent_at"] else ""
            jobs_html.append("""<div class="docs-job"><div><b>%s</b> %s %s</div><div class="meta">Generated: %s%s · <a href="%s" target="_blank" rel="noopener">posting</a> · %s</div>
<div class="files">%s</div></div>""" % (
                e(info.get("title") or jd), status,
                ('<a class="btn sm" href="/cv/%s">open</a>' % e(urllib.parse.quote(info["job_id"], safe=""))) if info.get("job_id") else "",
                e(fmt_dt(info.get("generated"))), (" · %d earlier version(s)" % older) if older else "",
                e(safe_url(info.get("url", ""))), e(WORK_MODES.get(info.get("work_mode") or "unknown", "")), files))
        if jobs_html:
            blocks.append('<div class="card docs-co"><h3>🏢 %s</h3>%s</div>' % (e(co), "".join(jobs_html)))
    intro = """<div class="card"><h2>Documents by company</h2><p class="muted">Every generation is kept here: CV, Resume, cover letter, the posting text and an info.json.
When you regenerate, the old version goes to the <code>korabbi/</code> subfolder. On a Mac: Finder → Go → Connect to Server → <code>%s</code></p></div>""" % e(smb_path(DOCS_ROOT))
    return page("Documents", intro + ("".join(blocks) or '<div class="card muted">No documents generated yet.</div>'))


def profile_page(flash="", prefill=None):
    current = prefill if prefill is not None else json.dumps(load_profile(), ensure_ascii=False, indent=2)
    body = """<div class="card"><h2>CV profile (source facts)</h2><p class="muted">Claude works from this. Only what is listed here can go into the CV.
The <code>contact</code> section is not sent to the API; it only goes into the PDF header. You can enter your phone number here.</p>
<p><a href="/base-cv.pdf?photo=1" target="_blank"><button class="primary">⬇️ Base CV with photo (PDF)</button></a>
<a href="/base-cv.pdf?photo=0" target="_blank"><button>⬇️ Base CV without photo (US)</button></a></p></div>
<div class="card"><h2>Create the profile from your existing CV</h2>
<p class="muted">The full text of your CV (including your contact details) is sent to the Anthropic API, and Claude processes it.
The result is only a preview: check and fix it in the editor below; it is saved only with the Save button.
It needs the API key (Settings → Claude API).</p>
<form method="post" action="/profile/import-pdf" enctype="multipart/form-data" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
<input type="file" name="pdf" accept="application/pdf"><button class="primary">Process PDF</button></form>
<form method="post" action="/profile/import" style="margin-top:10px">
<textarea name="cv_text" style="min-height:140px" placeholder="…or paste the CV text here"></textarea><button class="primary">Process text</button></form></div>
<div class="card"><h2>Editor</h2>
<form method="post" action="/profile"><textarea name="profile" style="min-height:600px">%s</textarea><button class="primary">Save</button></form></div>""" % (
        e(current))
    return page("CV profile", body, flash)


AUTH_PASSWORD = os.environ.get("JOBWATCH_PASSWORD", "")
AUTH_PUBLIC_PATHS = ("/health",)  # the platform's health check must get through without a password


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
        try:
            if u.path == "/":
                return self.send_html(jobs_page(q))
            if u.path == "/sources":
                return self.send_html(sources_page())
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
                with db() as con:
                    con.execute("DELETE FROM jobs")
                    con.execute("DELETE FROM source_state")
                return self.redirect("/settings?msg=" + urllib.parse.quote("Job cache cleared. Press Refresh to fetch again."))
            if u.path == "/run":
                threading.Thread(target=run_all, kwargs={"force": True}, daemon=True).start()
                time.sleep(0.3)
                return self.redirect(self.back())
            if u.path.startswith("/job/"):
                jid = urllib.parse.unquote(u.path[5:])
                st = f.get("status")
                if st in STATUS_LABEL:
                    with db() as con:
                        con.execute("UPDATE jobs SET status=? WHERE id=?", (st, jid))
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
            if u.path == "/mark-all-seen":
                with db() as con:
                    con.execute("UPDATE jobs SET status='seen' WHERE status='new'")
                return self.redirect(self.back())
            if u.path == "/settings":
                save_settings(f)
                return self.redirect("/settings?msg=" + urllib.parse.quote("Saved, postings re-scored."))
            if u.path == "/test":
                with CFG_LOCK:
                    cfg = load_config()
                if f.get("what") == "custom":
                    return self.redirect("/settings?msg=" + urllib.parse.quote(test_custom_sources(cfg)[:1500]))
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
