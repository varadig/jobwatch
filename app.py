#!/usr/bin/env python3
"""Allasfigyelo (jobwatch) - figyeli az allashirdeteseket, pontozza a CV-hez,
ntfy push / e-mail ertesitest kuld, es bongeszheto webes feluletet ad.
Csak Python standard library. Futtatas: python3 app.py  (port: JOBWATCH_PORT, alap 8090)
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
# Generalt dokumentumok (CV/level/kisereolevel) mappaja. Ha a fajlrendszer egy halozati megosztason van,
# a JOBWATCH_SMB_PREFIX (helyi ut eleje) es JOBWATCH_SMB_BASE (smb:// cim) megadasaval a Finder-link is megy.
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
    "studios": ("Cégek job boardjai", ""),
    "rss": ("Saját RSS feedek", ""),
}
UNSUPPORTED = [
    ("HiringCafe", "https://hiringcafe.com/", "Cloudflare tiltja az automatikus lekérést (403)"),
    ("JobLeads", "https://www.jobleads.com/", "Cloudflare tiltja az automatikus lekérést (403)"),
    ("Cord", "https://cord.com/", "JavaScript-alkalmazás, böngésző nélkül nem olvasható"),
    ("Jack & Jill", "https://app.jackandjill.ai/", "Bejelentkezés kell"),
    ("Cégek saját karrieroldala", "", "Nincs nyilvános API: kézzel nézd, vagy add hozzá a cég board-ját a Studios listához"),
]

DEFAULT_CONFIG = {
    # Ez a generikus alap. A konkret munkatipus (kulcsszavak, keresesek, forrasok) a
    # templates/ mappabol jon: tools/new_profile.py <nev> --template <sablon>.
    "interval_minutes": 60,
    "min_score": 4,          # ennyi pont alatt nem latszik alapbol a listaban
    "notify_min_score": 7,   # ennyi ponttol jar ertesites
    "notify_work_modes": ["remote", "unknown"],   # hybrid/irodai allasrol nem jon ertesites
    "default_work_filter": "remote_unknown",
    "public_url": "http://localhost:%d/" % PORT,
    "anthropic_api_key": "",
    "ntfy": {"enabled": True, "server": "https://ntfy.sh", "topic": ""},
    "email": {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
              "username": "", "password": "", "to": ""},
    "sources": {"linkedin": True, "meetfrank": True, "4dayweek": True, "flexa": True,
                "remotive": True, "studios": True, "rss": True},
    "linkedin_searches": [],   # pl. "project manager | Hungary | any"
    "remotive_queries": [],    # pl. ["project manager"]
    "rss_feeds": [],
    "studios": [],             # "rendszer | azonosito | nev", pl. "ashby | acme | Acme"
    "title_keywords": {},      # kulcsszo: pont, a cim tartalmazza-e
    "body_keywords": {},       # kulcsszo: pont, a leiras tartalmazza-e (max 4 pont)
    "location_keywords": {"remote": 1, "europe": 1},
    "negative_keywords": {"junior": -6, "intern": -8, "internship": -8, "trainee": -6, "student": -5},
}

CFG_LOCK = threading.Lock()
RUN_LOCK = threading.Lock()
STATE = {"running": False, "last_run": None, "next_run": None}
REQ = threading.local()  # aktualis keres utvonala (aktiv menupont)


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
        CREATE TABLE IF NOT EXISTS source_state(
          source TEXT PRIMARY KEY, last_run TEXT, last_ok TEXT, found INTEGER, new INTEGER,
          error TEXT, seeded INTEGER DEFAULT 0);
        """)


def migrate_db():
    with db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
        v = con.execute("SELECT v FROM meta WHERE k='mode_rules'").fetchone()
        if not v or v["v"] != "2":
            # uj szabalyok: a LinkedIn szuro nem megbizhato -> ujraosztalyozas szovegbol
            if "work_mode" in [r["name"] for r in con.execute("PRAGMA table_info(jobs)")]:
                for r in con.execute("SELECT id, source, title, location, description FROM jobs WHERE source IN ('linkedin','4dayweek','flexa','rss')").fetchall():
                    con.execute("UPDATE jobs SET work_mode=? WHERE id=?",
                                (classify_mode(r["title"], r["location"], r["description"]), r["id"]))
            con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('mode_rules','2')")
        ccols = [r["name"] for r in con.execute("PRAGMA table_info(cvs)")]
        if ccols and "doc_dir" not in ccols:
            con.execute("ALTER TABLE cvs ADD COLUMN doc_dir TEXT")
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
    score += min(body_pts, 4)  # a leiras max 4 pontot adhat
    for term, w in cfg["negative_keywords"].items():
        if term_re(term).search(title):
            score += int(w)
            hits.append("%s(%d)" % (term, int(w)))
    loc = (j.get("location") or "").lower()
    for term, w in (cfg.get("location_keywords") or {}).items():
        if term_re(term).search(loc):
            if int(w) > 0 and title_pts < 4:
                continue  # hely bonusz csak relevans poziciora
            score += int(w)
            hits.append("📍%s(%+d)" % (term, int(w)))
    if re.search(r",\s*[A-Z]{2}$", j.get("location") or ""):  # US allam (pl. "Louisville, CO")
        score -= 4
        hits.append("📍USA(-4)")
    score += j.get("bonus", 0)
    return score, hits


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


WORK_MODES = {"remote": "🏠 Remote", "hybrid": "🏢🏠 Hybrid", "onsite": "🏢 Irodai", "unknown": "❔ Ismeretlen"}
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
    """Munkavegzes modja. Cim/helyszin eros jel; a leirasban csak egyertelmu kifejezesek szamitanak."""
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
        wts = [""]  # a guest API a munkavegzes-szurot (f_WT) figyelmen kivul hagyja; a modot a szovegbol allapitjuk meg
        for wt, start in [(w, st) for w in wts for st in (0, 25)]:
            if start and wt in done_short:
                continue
            q = {"keywords": kw, "location": loc, "f_TPR": "r604800", "start": str(start)}
            if wt:
                q["f_WT"] = wt  # csak keresesi szukites; a LinkedIn guest szuroje nem megbizhato
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
                    log.warning("LinkedIn 429, részeredménnyel folytatom (%d)", len(out))
                    return out
                raise RuntimeError("LinkedIn 429 Too Many Requests (később újrapróbálja)")
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
            errs.append("nincs __NEXT_DATA__ (megváltozott az oldal?)")
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
                errs.append("%s: ismeretlen rendszer '%s'" % (name, ats))
        except Exception as ex:
            errs.append("%s: %s" % (name, ex))
        time.sleep(1)
    if errs:
        log.warning("Stúdió hibák: %s", "; ".join(errs))
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


def linkedin_detail(jid):
    """Teljes hirdetesszoveg a nyilvanos jobPosting vegpontrol (None, ha nem sikerult)."""
    page = fetch("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/%s" % jid)
    m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', page, re.S)
    if not m:
        return None
    return strip_tags(m.group(1).replace("<br>", "\n").replace("</li>", "\n").replace("</p>", "\n"))[:12000]


def enrich_linkedin(con, items, limit=20):
    """Uj / meg leiras nelkuli LinkedIn hirdetesekhez letolti a szoveget, es abbol dont a munkavegzes modjarol."""
    n = 0
    for it in items:
        row = con.execute("SELECT description FROM jobs WHERE id=?", ("linkedin:" + it["ext_id"],)).fetchone()
        if row and row["description"]:
            continue
        if n >= limit:
            break
        try:
            d = linkedin_detail(it["ext_id"])
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                log.warning("LinkedIn részletek: 429, a többit a következő körben")
                break
            continue
        except Exception:
            continue
        n += 1
        if d:
            it["description"] = d
            it["work_mode"] = classify_mode(it["title"], it["location"], d)
        time.sleep(6)
    return n


SOURCE_FUNCS = {"linkedin": src_linkedin, "meetfrank": src_meetfrank, "4dayweek": src_4dayweek,
                "flexa": src_flexa, "remotive": src_remotive, "studios": src_studios,
                "rss": src_rss}
MIN_INTERVAL = {"remotive": 360}  # perc; a Remotive API max ~4 hivast ker naponta


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
        raise RuntimeError("Az e-mail (SMTP) nincs beállítva a Beállításokban.")
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
                    send_ntfy(cfg, "Új állás (%d pont): %s" % (j["score"], j["title"]),
                              "%s · %s\n%s" % (j["company"], j["location"] or "-", j["url"]),
                              click=j["url"], priority=4 if j["score"] >= cfg["notify_min_score"] + 3 else 3)
            else:
                lines = ["• %s – %s (%d)" % (j["title"], j["company"], j["score"]) for j in jobs[:15]]
                send_ntfy(cfg, "%d új releváns állás" % len(jobs), "\n".join(lines),
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
            send_email(cfg, "Állásfigyelő: %d új releváns állás" % len(jobs), text,
                       '<p>Új, a CV-dhez illő hirdetések:</p><ul>%s</ul><p><a href="%s">Állásfigyelő megnyitása</a></p>'
                       % (rows, html.escape(cfg.get("public_url", ""))))
        except Exception as ex:
            errors.append("e-mail: %s" % ex)
    for e in errors:
        log.error("Értesítési hiba: %s", e)
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


def run_all(force=False):
    if not RUN_LOCK.acquire(blocking=False):
        return
    STATE["running"] = True
    try:
        with CFG_LOCK:
            cfg = load_config()
        to_notify = []
        for name, fn in SOURCE_FUNCS.items():
            if not cfg["sources"].get(name):
                continue
            with db() as con:
                if not source_due(con, cfg, name, force):
                    continue
                st = con.execute("SELECT seeded FROM source_state WHERE source=?", (name,)).fetchone()
                seeded = bool(st and st["seeded"])
            ts = now_iso()
            try:
                items = fn(cfg)
                err = None
            except Exception as ex:
                log.exception("Forrás hiba: %s", name)
                items, err = [], "%s: %s" % (type(ex).__name__, ex)
            new = 0
            if name == "linkedin" and items:
                with db() as con:
                    enrich_linkedin(con, items)
            with db() as con:
                for it in items:
                    if not it.get("title") or not it.get("url", "").startswith("http"):
                        continue
                    jid = "%s:%s" % (name, it["ext_id"])
                    sc, hits = score_job(cfg, it)
                    dup = norm(it["title"]) + "|" + norm(it["company"])
                    wm = it.get("work_mode") or classify_mode(it["title"], it["location"], it.get("description"))
                    ex_row = con.execute("SELECT id FROM jobs WHERE id=?", (jid,)).fetchone()
                    if ex_row:
                        if it.get("description") and name == "linkedin":
                            sc2, hits2 = score_job(cfg, it)
                            con.execute("UPDATE jobs SET last_seen=?, work_mode=?, description=?, score=?, matched=? WHERE id=?",
                                        (ts, wm, it["description"], sc2, ", ".join(hits2), jid))
                        elif it.get("work_mode"):  # forrasbol jovo pontos ertek felulirja a becslest
                            con.execute("UPDATE jobs SET last_seen=?, work_mode=? WHERE id=?", (ts, wm, jid))
                        else:
                            con.execute("UPDATE jobs SET last_seen=? WHERE id=?", (ts, jid))
                        continue
                    if con.execute("SELECT 1 FROM jobs WHERE dupkey=?", (dup,)).fetchone():
                        continue  # mar lattuk mas forrasbol
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
            log.info("%s: %d hirdetés, %d új%s", name, len(items), new, " HIBA: " + err if err else "")
        errs = notify_jobs(cfg, to_notify)
        if to_notify and not errs:
            with db() as con:
                con.executemany("UPDATE jobs SET notified=1 WHERE id=?", [(j["id"],) for j in to_notify])
        elif to_notify:
            # ha legalabb egy csatorna mukodott, ne kuldjuk ujra
            ok_channels = (cfg["ntfy"].get("enabled") and not any(e.startswith("ntfy") for e in errs)) or \
                          (cfg["email"].get("enabled") and not any(e.startswith("e-mail") for e in errs))
            if ok_channels:
                with db() as con:
                    con.executemany("UPDATE jobs SET notified=1 WHERE id=?", [(j["id"],) for j in to_notify])
    finally:
        STATE["running"] = False
        STATE["last_run"] = now_iso()
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
            log.exception("Ütemező hiba")
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


STATUS_LABEL = {"new": "új", "seen": "látott", "saved": "mentett", "applied": "jelentkeztem", "hidden": "elrejtett"}


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


# Egyedi osztaly -> daisyUI komponens. A markup a regi osztalyneveket hasznalja; ez az atalakitas
# adja a daisyUI-stilusokat. Ami nincs a listaban, az marad a sajat (CSS) stilusa.
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
    tabs = [("/", "Állások"), ("/docs", "Dokumentumok"), ("/sources", "Források"), ("/profile", "CV-profil"), ("/settings", "Beállítások")]
    nav = "".join('<a role="tab" class="tab%s" href="%s">%s</a>' % (
        " tab-active" if (cur == h or (h == "/" and cur.startswith("/cv/"))) else "", h, l) for h, l in tabs)
    running = '<span class="badge badge-soft badge-warning">⏳ frissítés fut…</span>' if STATE["running"] else ""
    return ("""<!doctype html><html lang="hu"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">%s
<script>document.documentElement.dataset.theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";</script>
<title>%s · Állásfigyelő</title>
<link href="https://cdn.jsdelivr.net/npm/daisyui@5.0.0" rel="stylesheet" type="text/css"><style>%s</style></head><body><header><div class="bar"><span class="brand">💼 Állásfigyelő</span><nav class="tabs tabs-box tabs-sm">%s</nav>
<div class="right">%s<form method="post" action="/test"><button class="btn btn-sm" name="what" value="ntfy" title="Teszt push értesítés a telefonra">🔔 Teszt push</button></form>
<form method="post" action="/run"><button class="btn btn-primary btn-sm" title="Minden forrás lekérése most">↻ Frissítés</button></form></div></div></header>
<main>%s%s</main></body></html>"""
            % (('<meta http-equiv="refresh" content="%d">' % refresh) if refresh else "", e(title), CSS, nav, running,
               '<div class="alert alert-info flash">%s</div>' % e(flash) if flash else "", daisify(body)))


def parse_dt(s):
    """Vegyes formatumu datum -> idozonas datetime (UTC, ha nincs megadva), vagy None."""
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
    """Hany naptari nappal ezelott (helyi ido szerint)."""
    return (datetime.now().astimezone().date() - d.astimezone().date()).days


def rel_day(n):
    if n <= 0:
        return "ma"
    if n == 1:
        return "tegnap"
    if n < 14:
        return "%d napja" % n
    if n < 60:
        return "%d hete" % (n // 7)
    return "%d hónapja" % (n // 30)


BUCKETS = [(0, "Ma"), (1, "Tegnap"), (6, "Ezen a héten (2–6 napja)"), (13, "Múlt héten (7–13 napja)"),
           (30, "2–4 hete"), (10 ** 6, "Régebbi, mint 30 nap – valószínűleg elavult")]


def bucket_of(n):
    for lim, name in BUCKETS:
        if n <= lim:
            return name
    return BUCKETS[-1][1]


def jobs_page(q):
    with CFG_LOCK:
        cfg = load_config()
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

    # datumok: feladas (ha a forras nem adja meg, a gyujtes napja), gyujtes
    for r in rows:
        seen = parse_dt(r["first_seen"]) or datetime.now(timezone.utc)
        pd = parse_dt(r["posted"])
        r["_posted_known"] = pd is not None
        r["_posted"] = pd or seen
        r["_seen"] = seen
        r["_pdays"] = days_ago(r["_posted"])
        r["_sdays"] = days_ago(seen)
        # teljes allaslistat ado forrasnal (studiok) ha az utolso sikeres lekeresben mar nem szerepelt -> lezarva
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

    # osszesito csempek (aktiv allasok, a munkavegzes-szurovel egyutt)
    with db() as con:
        act_rows = [dict(r) for r in con.execute(
            "SELECT posted, first_seen, status FROM jobs WHERE score>=? AND status IN ('new','seen','saved')"
            + ((" AND COALESCE(work_mode,'unknown') IN (%s)" % ",".join("?" * len(wf_sql))) if wf_sql else ""),
            [mn] + (wf_sql or []))]
    fresh = sum(1 for r in act_rows if parse_dt(r["posted"]) and days_ago(parse_dt(r["posted"])) <= 2)
    today = sum(1 for r in act_rows if parse_dt(r["first_seen"]) and days_ago(parse_dt(r["first_seen"])) == 0)
    stats = """<div class="stats">
<a class="stat" href="%s"><b>%d</b><span>🆕 friss (≤2 napos)</span></a>
<a class="stat" href="%s"><b>%d</b><span>📥 ma begyűjtve</span></a>
<a class="stat" href="%s"><b>%d</b><span>● új, még nem nézted</span></a>
<a class="stat" href="%s"><b>%d</b><span>⭐ mentett</span></a>
<a class="stat" href="%s"><b>%d</b><span>✅ jelentkeztem</span></a></div>""" % (
        e(link(age="3", sort="posted", status="active")), fresh, e(link(age="1", sort="seen", status="active")), today,
        e(link(status="new")), counts.get("new", 0), e(link(status="saved")), counts.get("saved", 0),
        e(link(status="applied")), counts.get("applied", 0))

    st_chips = "".join([chip("Aktív", None, status="active"), chip("Új", counts.get("new", 0), status="new"),
                        chip("Mentett", counts.get("saved", 0), status="saved"),
                        chip("Jelentkeztem", counts.get("applied", 0), status="applied"),
                        chip("Elrejtett", counts.get("hidden", 0), status="hidden"), chip("Mind", None, status="all")])
    wm_chips = "".join([chip("🏠 Csak remote", wcount.get("remote", 0), work="remote"),
                        chip("Remote + ❔", wcount.get("remote", 0) + wcount.get("unknown", 0), work="remote_unknown"),
                        chip("Remote + hybrid", None, work="remote_hybrid"),
                        chip("🏢🏠 Hybrid", wcount.get("hybrid", 0), work="hybrid"),
                        chip("🏢 Irodai", wcount.get("onsite", 0), work="onsite"), chip("Mind", None, work="all")])
    sort_chips = "".join([chip("📅 Feladás", None, sort="posted"), chip("📥 Begyűjtés", None, sort="seen"),
                          chip("🎯 Pontszám", None, sort="score")])
    age_chips = "".join([chip("Bármikor", None, age="all"), chip("24 óra", None, age="1"), chip("3 nap", None, age="3"),
                         chip("7 nap", None, age="7"), chip("30 nap", None, age="30")])
    src_items = [("", "Minden forrás")] + [(k, v[0]) for k, v in SOURCES_INFO.items()]
    hidden_inputs = "".join('<input type="hidden" name="%s" value="%s">' % (k, e(v))
                            for k, v in base_q.items() if k in ("status", "work", "sort", "age"))
    more_open = " open" if (src or text or mn != cfg["min_score"]) else ""
    filt = """<div class="panel">
<div class="row-chips"><span class="lbl">Állapot</span>%s</div>
<div class="row-chips"><span class="lbl">Munkavégzés</span>%s</div>
<div class="row-chips"><span class="lbl">Rendezés</span>%s</div>
<div class="row-chips"><span class="lbl">Életkor</span>%s</div>
<details class="more"%s><summary>További szűrők (forrás, pontszám, keresés)</summary>
<form class="filters" method="get">%s<select name="source">%s</select>
<label style="margin:0;font-weight:400">Min. pont <input type="number" name="min" value="%d" style="width:70px"></label>
<input type="search" name="q" value="%s" placeholder="Keresés: cím, cég, hely"><button>Szűrés</button>
<a class="btn" href="/">Alaphelyzet</a></form></details></div>""" % (
        st_chips, wm_chips, sort_chips, age_chips, more_open, hidden_inputs, opts(src, src_items), mn, e(text))
    head = """<div class="listhead"><span><b>%d</b> hirdetés%s · az életkor a <b>%s</b> dátumára vonatkozik · értesítés %d ponttól</span>
<form method="post" action="/mark-all-seen"><button class="sm">Minden új → látott</button></form></div>""" % (
        total, " (az első 300 látszik)" if total > 300 else "", "begyűjtés" if sort == "seen" else "feladás",
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
            chips.append('<span class="chip closed">❌ valószínűleg lezárva</span>')
        if r["_posted_known"]:
            cls_age = "age-new" if r["_pdays"] <= 2 else ("age-old" if r["_pdays"] > 30 else "")
            chips.append('<span class="chip %s" title="Feladva: %s">📅 %s</span>' % (
                cls_age, e(r["_posted"].astimezone().strftime("%Y-%m-%d")),
                e(("feladva " + rel_day(r["_pdays"])) if r["_pdays"] > 2 else "🆕 " + rel_day(r["_pdays"]))))
        else:
            chips.append('<span class="chip" title="A forrás nem adja meg a feladás dátumát">📅 feladás ismeretlen</span>')
        chips.append('<span class="chip" title="Begyűjtve: %s">📥 %s</span>' % (e(fmt_dt(r["first_seen"])), e(rel_day(r["_sdays"]))))
        if r["status"] != "new":
            chips.append('<span class="chip st">%s</span>' % e(STATUS_LABEL.get(r["status"], r["status"])))
        icon_btns = "".join('<button name="status" value="%s" title="%s">%s</button>' % (s_, t, ic) for s_, ic, t in (
            ("saved", "⭐", "Mentés"), ("applied", "✅", "Jelentkeztem"), ("seen", "👁", "Látottnak jelöl"),
            ("hidden", "🚫", "Elrejtés"), ("new", "↺", "Vissza újnak")) if s_ != r["status"])
        cv_btn = ('<a class="btn" href="/cv/%s">📄 Dokumentumok ✓</a>' if r["id"] in has_cv else
                  '<a class="btn primary" href="/cv/%s">📄 CV + Resume</a>') % qid
        cards.append("""<article class="job st-%s%s"><div class="score %s" title="Pontszám a CV-dhez">%d<small>pont</small></div>
<div><div class="jt"><a href="/open/%s" target="_blank" rel="noopener noreferrer">%s</a></div>
<div class="co"><b>%s</b> <span class="src">· %s</span></div><div class="chips">%s</div>
<details class="why"><summary>Miért ennyi pont?</summary>%s</details></div>
<form class="acts" method="post" action="/job/%s">%s<div class="icons">%s</div></form></article>""" % (
            e(r["status"]), " stale" if (r["_closed"] or r["_pdays"] > 30) else "", cls, sc,
            qid, e(r["title"]), e(r["company"] or "?"), e(SOURCES_INFO.get(r["source"], (r["source"],))[0]),
            "".join(chips), e(r["matched"] or "-"), qid, cv_btn, icon_btns))
    if not cards:
        cards.append('<div class="card muted">Nincs a szűrésnek megfelelő hirdetés. Próbáld lazítani a szűrőket, vagy nyomd meg a „Frissítés” gombot.</div>')
    return page("Állások", stats + filt + head + "".join(cards))


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
            '<span class="ok">OK</span>' if s else '<span class="muted">még nem futott</span>')
        rows.append("<tr><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            ('<a href="%s" target="_blank" rel="noopener">%s</a>' % (e(url), e(name))) if url else e(name),
            "" if on else ' <span class="badge">kikapcsolva</span>',
            e(fmt_dt(s["last_run"])) if s else "-", s["found"] if s else "-", s["new"] if s else "-",
            cnt.get(k, 0), status))
    uns = "".join('<tr><td>%s</td><td class="muted">%s</td></tr>' % (
        ('<a href="%s" target="_blank" rel="noopener">%s</a>' % (e(u), e(n))) if u else e(n), e(why)) for n, u, why in UNSUPPORTED)
    body = """<div class="card"><h2>Figyelt források</h2><table><tr><th>Forrás</th><th>Utolsó futás</th><th>Lekért</th><th>Új</th><th>Összes</th><th>Állapot</th></tr>%s</table>
<p class="muted">Frissítés %d percenként (a Remotive legfeljebb 6 óránként).</p></div>
<div class="card"><h2>Automatikusan nem figyelhető oldalak</h2><p class="muted">Ezeket érdemes kézzel ránézni időnként.</p><table>%s</table></div>""" % (
        "".join(rows), cfg["interval_minutes"], uns)
    return page("Források", body)


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


def settings_page(flash=""):
    with CFG_LOCK:
        cfg = load_config()
    n, m = cfg["ntfy"], cfg["email"]
    chk = lambda b: " checked" if b else ""
    src_boxes = "".join('<label style="font-weight:400;display:inline-block;margin-right:14px"><input type="checkbox" name="src_%s"%s> %s</label>' % (
        k, chk(cfg["sources"].get(k)), e(v[0])) for k, v in SOURCES_INFO.items())
    sub = "%s/%s" % (n["server"].rstrip("/"), n["topic"])
    body = """<div class="card"><h2>🔔 Teszt értesítés</h2><form method="post" action="/test" style="display:flex;gap:8px;flex-wrap:wrap">
<button class="primary" name="what" value="ntfy">🔔 Teszt push küldése a telefonra</button><button name="what" value="email">✉️ Teszt e-mail</button></form></div>
<form method="post" action="/settings">
<div class="card"><h2>📱 Push értesítés (ntfy)</h2>
<p class="muted">1. Telepítsd az <b>ntfy</b> appot (Android: Play Store/F-Droid, iPhone: App Store). 2. Az appban „+” → Subscribe to topic → írd be: <b>%s</b> (szerver: %s). 3. Nyomd meg lent a „Teszt push” gombot.<br>A téma neve egyben a jelszó: akinek megvan, az is látja az értesítéseket.</p>
<label><input type="checkbox" name="ntfy_enabled"%s> Push értesítés bekapcsolva</label>
<div class="row"><div><label>Szerver</label><input name="ntfy_server" value="%s" style="width:100%%"></div>
<div><label>Téma (topic)</label><input name="ntfy_topic" value="%s" style="width:100%%"></div></div>
<p class="muted">Feliratkozási link: <a href="%s" target="_blank" rel="noopener">%s</a></p></div>
<div class="card"><h2>✉️ E-mail értesítés</h2>
<p class="muted">Gmailhez <b>alkalmazásjelszó</b> kell (Google-fiók → Biztonság → Kétlépcsős azonosítás → Alkalmazásjelszavak), nem a rendes jelszavad.</p>
<label><input type="checkbox" name="email_enabled"%s> E-mail értesítés bekapcsolva</label>
<div class="row"><div><label>SMTP szerver</label><input name="smtp_host" value="%s" style="width:100%%"></div>
<div><label>Port</label><input name="smtp_port" value="%s" style="width:100%%"></div>
<div><label>Felhasználó (e-mail cím)</label><input name="smtp_user" value="%s" style="width:100%%"></div>
<div><label>Jelszó %s</label><input type="password" name="smtp_pass" placeholder="(üresen hagyva marad)" style="width:100%%"></div>
<div><label>Címzett (üres = feladó)</label><input name="email_to" value="%s" style="width:100%%"></div></div></div>
<div class="card"><h2>🤖 Claude API (hirdetéshez igazított CV)</h2>
<p class="muted">Kulcs: <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">console.anthropic.com → API Keys</a>. Egy CV + kísérőlevél kb. 5–15 cent. A kulcs csak a Pi-n tárolódik.</p>
<label>Anthropic API-kulcs %s</label><input type="password" name="anthropic_api_key" placeholder="(üresen hagyva marad)" style="width:100%%"></div>
<div class="card"><h2>⚙️ Általános</h2><div class="row">
<div><label>Frissítés (perc)</label><input type="number" name="interval" value="%d" min="15"></div>
<div><label>Listában min. pont</label><input type="number" name="min_score" value="%d"></div>
<div><label>Értesítés min. pont</label><input type="number" name="notify_min_score" value="%d"></div>
<div><label>Értesítés ezekre</label>%s</div>
<div><label>Lista alapszűrő</label><select name="default_work_filter">%s</select></div>
<div><label>Webes cím (értesítés linkje)</label><input name="public_url" value="%s" style="width:100%%"></div></div>
<label>Források</label>%s</div>
<div class="card"><h2>🔎 LinkedIn keresések</h2><p class="muted">Soronként: <code>kulcsszavak | helyszín</code> (a munkavégzés módját a hirdetés szövegéből állapítja meg, mert a LinkedIn nyilvános szűrője nem működik) (az elmúlt 7 nap hirdetései).</p>
<textarea name="linkedin_searches">%s</textarea>
<label>Remote-first stúdiók (soronként: <code>ashby|lever|greenhouse|bamboohr|smartrecruiters | azonosító | név</code>)</label>
<textarea name="studios" style="min-height:140px">%s</textarea>
<label>Saját RSS feedek (soronként egy URL)</label><textarea name="rss_feeds" style="min-height:60px">%s</textarea></div>
<div class="card"><h2>🎯 Pontozás (a CV-d alapján)</h2><p class="muted">Soronként <code>kifejezés = pont</code>. A címben talált kifejezések számítanak a legtöbbet; a negatívak csak a címre vonatkoznak. Mentéskor minden hirdetés újrapontozódik.</p>
<div class="row"><div><label>Cím</label><textarea name="title_keywords" style="min-height:260px">%s</textarea></div>
<div><label>Leírás</label><textarea name="body_keywords" style="min-height:260px">%s</textarea></div>
<div><label>Negatív (cím)</label><textarea name="negative_keywords" style="min-height:260px">%s</textarea></div>
<div><label>Helyszín</label><textarea name="location_keywords" style="min-height:260px">%s</textarea></div></div></div>
<button class="primary">Mentés</button></form>
<div class="card" style="margin-top:14px"><h2>Teszt</h2><form method="post" action="/test" style="display:flex;gap:8px">
<button name="what" value="ntfy">Teszt push</button><button name="what" value="email">Teszt e-mail</button></form></div>""" % (
        e(n["topic"]), e(n["server"]), chk(n.get("enabled")), e(n["server"]), e(n["topic"]), e(sub), e(sub),
        chk(m.get("enabled")), e(m["smtp_host"]), e(m["smtp_port"]), e(m["username"]),
        "(beállítva ✓)" if m.get("password") else "", e(m["to"]),
        "(beállítva ✓)" if cfg.get("anthropic_api_key") else "",
        cfg["interval_minutes"], cfg["min_score"], cfg["notify_min_score"],
        "".join('<label style="font-weight:400;display:inline-block;margin-right:10px"><input type="checkbox" name="nwm_%s"%s> %s</label>' % (
            k, chk(k in cfg.get("notify_work_modes", ["remote", "unknown"])), e(v)) for k, v in WORK_MODES.items()),
        "".join('<option value="%s"%s>%s</option>' % (k, " selected" if k == cfg.get("default_work_filter", "remote_unknown") else "", l)
                for k, l in (("remote", "Csak remote"), ("remote_unknown", "Remote + ismeretlen"), ("remote_hybrid", "Remote + hybrid"), ("all", "Minden"))), e(cfg.get("public_url", "")), src_boxes,
        e("\n".join(cfg["linkedin_searches"])), e("\n".join(cfg.get("studios") or [])),
        e("\n".join(cfg.get("rss_feeds") or [])),
        e(kv_text(cfg["title_keywords"])), e(kv_text(cfg["body_keywords"])), e(kv_text(cfg["negative_keywords"])),
        e(kv_text(cfg.get("location_keywords") or {})))
    return page("Beállítások", body, flash)


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
        for k in ("title_keywords", "body_keywords", "negative_keywords", "location_keywords"):
            cfg[k] = parse_kv(g(k))
        save_config(cfg)
    rescore_all()


# ---------------------------------------------------------------- CV
MAX_PDF_BYTES = 10 * 1024 * 1024


def pdf_from_multipart(content_type, body):
    """Kiszedi a "pdf" mezo fajlt a multipart/form-data torzsebol. Ures bytes, ha nincs."""
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
    """PDF szoveganak kiolvasasa (pypdf). Szkennelt, kepkent mentett PDF-bol nincs szoveg."""
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if not text:
        raise ValueError("A PDF-ből nem olvasható ki szöveg (valószínűleg szkennelt kép). Másold be a szöveget.")
    return text


def profile_from_cv_text(text):
    """Claude API-val profil-JSON a CV szovegebol. Hibanal ValueError a felhasznaloi uzenettel."""
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
    """Fajlnev-eleje a profil nevebol, ASCII-ra alakitva: "Zoé Ünal" -> "Zoe_Unal"."""
    name = (prof or load_profile()).get("name") or "Candidate"
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", ascii_name)
    return "_".join(words) or "Candidate"


def doc_names(prof=None):
    p = owner_prefix(prof)
    return {"cv": p + "_CV.pdf", "resume": p + "_Resume.pdf", "letter": p + "_Cover_Letter.pdf"}
DOC_LABEL = {"cv": "CV (részletes)", "resume": "Resume (1 oldal)", "letter": "Kísérőlevél"}


def safe_name(x, n=70):
    x = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", x or "").strip().strip(".")
    return re.sub(r"\s+", " ", x)[:n].strip() or "ismeretlen"


def job_doc_dir(job):
    short = re.sub(r"[^A-Za-z0-9]+", "", job["id"].split(":", 1)[-1])[-10:]
    return os.path.join(DOCS_ROOT, safe_name(job["company"] or "Ismeretlen cég"), "%s [%s]" % (safe_name(job["title"], 80), short))


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
    """CV + Resume + kiserolevel a ceg mappajaba. archive=True: az elozo valtozat a korabbi/<ido>/ ala kerul."""
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
            raise cvgen.CVError("Nincs Anthropic API-kulcs (Beállítások → Claude API).")
        data = cvgen.tailor(key, load_profile(), job, description)
        data["_photo"] = cvgen.photo_default(job)
        render_cv_files(jid, data, archive=True)
        with db() as con:
            con.execute("UPDATE cvs SET status='done', error=NULL, data=?, created=? WHERE job_id=?",
                        (json.dumps(data, ensure_ascii=False), now_iso(), jid))
    except Exception as ex:
        log.exception("CV generálás hiba")
        with db() as con:
            con.execute("UPDATE cvs SET status='error', error=? WHERE job_id=?", (str(ex), jid))


def backfill_docs():
    """Regi (dokumentumtar elotti) generalasok atrakasa a cegenkenti mappaba, Resume-mal egyutt. Nincs API-hivas."""
    with db() as con:
        rows = con.execute("SELECT job_id, data FROM cvs WHERE status='done' AND (doc_dir IS NULL OR doc_dir='')").fetchall()
    for r in rows:
        try:
            d = render_cv_files(r["job_id"], json.loads(r["data"]))
            log.info("Dokumentumok áthelyezve: %s", d)
        except Exception:
            log.exception("Dokumentum-áthelyezés hiba: %s", r["job_id"])


def smb_path(d):
    if SMB_BASE and SMB_PREFIX and d and d.startswith(SMB_PREFIX):
        return SMB_BASE + d[len(SMB_PREFIX):]
    return d


def cv_page(jid, flash=""):
    import cvgen
    with db() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return page("Nincs ilyen", "<p>Nincs ilyen hirdetés.</p>")
        job = dict(r)
        cv = con.execute("SELECT * FROM cvs WHERE job_id=?", (jid,)).fetchone()
    qid = e(urllib.parse.quote(jid, safe=""))
    head = '<p><a href="/">← Vissza az állásokhoz</a></p><div class="card"><h2>📄 <a href="%s" target="_blank" rel="noopener">%s</a></h2><div class="meta">%s · %s · %s · %d pont</div></div>' % (
        e(safe_url(job["url"])), e(job["title"]), e(job["company"]), e(job["location"] or "-"),
        e(WORK_MODES.get(job.get("work_mode") or "unknown", "")), job["score"])
    if cv and cv["status"] == "running":
        return page("CV készül", head + '<div class="card"><p>⏳ A Claude most készíti a hirdetéshez igazított CV-t, Resume-ot és kísérőlevelet (kb. 1–2 perc). Az oldal magától frissül.</p></div>', flash, refresh=5)
    desc = cv["description"] if cv and cv["description"] else cvgen.fetch_description(fetch, strip_tags, job)
    gen_form = """<div class="card"><h2>%s</h2>%s<form method="post" action="/cv/%s/generate">
<label>Hirdetés szövege (ebből dolgozik a Claude; javíthatod, kiegészítheted)</label><textarea name="description" style="min-height:220px">%s</textarea>
<p class="muted">Csak a CV-profilod tényei kerülnek bele, a kapcsolati adataid nem mennek el az API-nak.</p>
<button class="primary">%s</button></form></div>""" % (
        "Újragenerálás" if cv and cv["status"] == "done" else "Hirdetéshez igazított CV készítése",
        ('<p class="err">Hiba: %s</p>' % e(cv["error"])) if cv and cv["status"] == "error" else "",
        qid, e(desc), "🔄 Újragenerálás" if cv and cv["status"] == "done" else "✨ CV + Resume + kísérőlevél generálása (Claude)")
    if not cv or cv["status"] != "done":
        return page("CV", head + gen_form, flash)
    data = json.loads(cv["data"])
    notes = "".join("<li>%s</li>" % e(n) for n in data.get("match_notes", []))
    emails = cvgen.find_emails(desc)
    sent = ('<p class="ok">✅ Elküldve: %s → %s</p>' % (e(fmt_dt(cv["sent_at"])), e(cv["sent_to"]))) if cv["sent_at"] else ""
    paths = cv_paths(jid)
    folder = smb_path(os.path.dirname(paths["cv"])) if paths["cv"] else ""
    body = head + """<div class="card"><h2>Dokumentumok</h2><p><a class="btn primary" href="/cv/%s/cv.pdf" target="_blank">⬇️ CV (részletes)</a>
<a class="btn primary" href="/cv/%s/resume.pdf" target="_blank">⬇️ Resume (1 oldal)</a>
<a class="btn" href="/cv/%s/letter.pdf" target="_blank">⬇️ Kísérőlevél</a></p>
<p class="muted">Mappa (Macről Finder → Ugrás → Kapcsolódás szerverhez): <code>%s</code></p>
<form method="post" action="/cv/%s/photo" style="margin:6px 0">Fotó a CV-n: <b>%s</b> &nbsp;<button name="photo" value="%s">%s</button>
<span class="muted"> (alapból: amerikai/brit/kanadai/ausztrál helyre fotó nélkül)</span></form>
<p><b>%s</b></p><p>%s</p><label>Megjegyzések neked</label><ul>%s</ul><p class="muted">Modell: %s · %s</p></div>
<div class="card"><h2>✉️ Küldés e-mailben a cégnek</h2>%s
<p class="muted">Csak akkor küld, ha megnyomod a gombot (és megerősíted). A Gmailedből megy.%s</p>
<form method="post" action="/cv/%s/send" onsubmit="return confirm('Biztosan elküldöd a jelentkezést erre a címre: ' + this.to.value + ' ?')">
<label>Címzett (toborzó e-mail címe)</label><input name="to" value="%s" style="width:100%%" required type="email">
<label>Tárgy</label><input name="subject" value="%s" style="width:100%%">
<label>Levél szövege</label><textarea name="body" style="min-height:260px;font-family:inherit;font-size:14px">%s</textarea>
<label>Csatolmány</label>
<label style="font-weight:400;display:inline-block;margin-right:12px"><input type="radio" name="attach" value="cv" checked> CV</label>
<label style="font-weight:400;display:inline-block;margin-right:12px"><input type="radio" name="attach" value="resume"> Resume</label>
<label style="font-weight:400;display:inline-block;margin-right:12px"><input type="radio" name="attach" value="both"> Mindkettő</label>
<label style="font-weight:400"><input type="checkbox" name="attach_letter"> + kísérőlevél PDF-ként is</label>
<button class="primary">📨 Jelentkezés elküldése</button></form></div>
<div class="card"><h2>Kézi szerkesztés</h2><p class="muted">A CV, a Resume (<code>resume</code> rész) és a kísérőlevél tartalma JSON-ban. Mentéskor a PDF-ek újrakészülnek (nincs API-hívás).</p>
<form method="post" action="/cv/%s/save"><textarea name="data" style="min-height:300px">%s</textarea><button>💾 Mentés és PDF frissítése</button></form></div>""" % (
        qid, qid, qid, e(folder), qid, "van" if data.get("_photo", True) else "nincs",
        "off" if data.get("_photo", True) else "on", "Fotó levétele" if data.get("_photo", True) else "Fotó hozzáadása",
        e(data["headline"]), e(data["summary"]), notes, e(data.get("_model", "")), e(fmt_dt(cv["created"])),
        sent, (" Talált címek a hirdetésben: " + e(", ".join(emails))) if emails else " A hirdetésben nem találtam e-mail címet; a legtöbb helyen az űrlapra kell feltölteni a PDF-et.",
        qid, e(emails[0] if emails else ""), e(data.get("email_subject", "")), e(data["cover_letter"]),
        qid, e(json.dumps({k: v for k, v in data.items() if not k.startswith("_")}, ensure_ascii=False, indent=2)))
    return page("CV", body + gen_form, flash)


def docs_page():
    if not os.path.isdir(DOCS_ROOT):
        return page("Dokumentumok", '<div class="card muted">Még nincs generált dokumentum. Egy hirdetésnél nyomd meg a „📄 CV + Resume” gombot.</div>')
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
                files += '<a class="btn sm" href="/docs/file?p=%s" target="_blank">Hirdetés szövege</a>' % e(urllib.parse.quote(os.path.join(rel, "hirdetes.txt")))
            older = len(os.listdir(os.path.join(d, "korabbi"))) if os.path.isdir(os.path.join(d, "korabbi")) else 0
            s_ = sent.get(info.get("job_id"))
            status = ('<span class="chip wm-remote">✅ elküldve %s → %s</span>' % (e(fmt_dt(s_["sent_at"])), e(s_["sent_to"]))) if s_ and s_["sent_at"] else ""
            jobs_html.append("""<div class="docs-job"><div><b>%s</b> %s %s</div><div class="meta">Generálva: %s%s · <a href="%s" target="_blank" rel="noopener">hirdetés</a> · %s</div>
<div class="files">%s</div></div>""" % (
                e(info.get("title") or jd), status,
                ('<a class="btn sm" href="/cv/%s">megnyitás</a>' % e(urllib.parse.quote(info["job_id"], safe=""))) if info.get("job_id") else "",
                e(fmt_dt(info.get("generated"))), (" · %d korábbi változat" % older) if older else "",
                e(safe_url(info.get("url", ""))), e(WORK_MODES.get(info.get("work_mode") or "unknown", "")), files))
        if jobs_html:
            blocks.append('<div class="card docs-co"><h3>🏢 %s</h3>%s</div>' % (e(co), "".join(jobs_html)))
    intro = """<div class="card"><h2>Dokumentumok cégenként</h2><p class="muted">Minden generálás itt marad: CV, Resume, kísérőlevél, a hirdetés szövege és egy info.json.
Újragenerálásnál a régi változat a <code>korabbi/</code> almappába kerül. A Macről is eléred: Finder → Ugrás → Kapcsolódás szerverhez → <code>%s</code></p></div>""" % e(smb_path(DOCS_ROOT))
    return page("Dokumentumok", intro + ("".join(blocks) or '<div class="card muted">Még nincs generált dokumentum.</div>'))


def profile_page(flash="", prefill=None):
    current = prefill if prefill is not None else json.dumps(load_profile(), ensure_ascii=False, indent=2)
    body = """<div class="card"><h2>CV-profil (tényanyag)</h2><p class="muted">Ebből dolgozik a Claude. Csak ami itt szerepel, az kerülhet a CV-be.
A <code>contact</code> rész nem megy el az API-nak, csak a PDF fejlécébe kerül. Telefonszámot itt adhatsz meg.</p>
<p><a href="/base-cv.pdf?photo=1" target="_blank"><button class="primary">⬇️ Alap CV fotóval (PDF)</button></a>
<a href="/base-cv.pdf?photo=0" target="_blank"><button>⬇️ Alap CV fotó nélkül (USA)</button></a></p></div>
<div class="card"><h2>Profil készítése a meglévő CV-ből</h2>
<p class="muted">A CV teljes szövege (beleértve az elérhetőségeidet is) elmegy az Anthropic API-nak, a Claude feldolgozza.
Az eredmény csak előnézet: az alábbi szerkesztőben ellenőrizd és javítsd, mentés csak a Mentés gombbal történik.
Kell hozzá az API-kulcs (Beállítások → Claude API).</p>
<form method="post" action="/profile/import-pdf" enctype="multipart/form-data" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
<input type="file" name="pdf" accept="application/pdf"><button class="primary">PDF feldolgozása</button></form>
<form method="post" action="/profile/import" style="margin-top:10px">
<textarea name="cv_text" style="min-height:140px" placeholder="…vagy másold be a CV szövegét ide"></textarea><button class="primary">Szöveg feldolgozása</button></form></div>
<div class="card"><h2>Szerkesztő</h2>
<form method="post" action="/profile"><textarea name="profile" style="min-height:600px">%s</textarea><button class="primary">Mentés</button></form></div>""" % (
        e(current))
    return page("CV-profil", body, flash)


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
        body = "Bejelentkezés szükséges.".encode("utf-8")
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
                    return self.send_html(page("Nincs", "<p>Nincs ilyen fájl.</p>"), 404)
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
                    return self.send_html(page("Nincs", "<p>Még nincs PDF.</p>"), 404)
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
            self.send_html(page("Nincs ilyen", "<p>404</p>"), 404)
        except Exception as ex:
            log.exception("GET hiba")
            self.send_html(page("Hiba", "<p class=err>%s</p>" % e(ex)), 500)

    def profile_import_result(self, text_source, flash_ok):
        try:
            prefill = profile_from_cv_text(text_source())
            return self.send_html(profile_page(flash_ok, prefill=prefill))
        except ValueError as ex:
            return self.send_html(profile_page("Nem sikerült: %s" % ex))

    def profile_import_text(self, text):
        return self.profile_import_result(lambda: text, "Ellenőrizd az előnézetet, majd mentsd.")

    def profile_import_pdf(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_PDF_BYTES + 4096:
            return self.send_html(profile_page("A fájl túl nagy (max. 10 MB)."))
        body = self.rfile.read(length)
        data = pdf_from_multipart(self.headers.get("Content-Type", ""), body)
        if not data:
            return self.send_html(profile_page("Nem érkezett PDF-fájl."))
        if len(data) > MAX_PDF_BYTES:
            return self.send_html(profile_page("A fájl túl nagy (max. 10 MB)."))
        def source():
            try:
                return pdf_text(data)
            except Exception as ex:
                raise ValueError("PDF olvasási hiba: %s" % ex)
        return self.profile_import_result(source, "Ellenőrizd az előnézetet, majd mentsd.")

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
                    return self.redirect("/profile?msg=" + urllib.parse.quote("Hibás JSON, nem mentettem."))
                os.makedirs(DATA, exist_ok=True)
                with open(PROFILE_PATH, "w", encoding="utf-8") as fh:
                    json.dump(prof, fh, ensure_ascii=False, indent=2)
                return self.redirect("/profile?msg=" + urllib.parse.quote("Mentve."))
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
                        data["_model"] = old.get("_model", "").replace(" (kézzel szerkesztve)", "") + " (kézzel szerkesztve)"
                        data["_photo"] = old.get("_photo", True)
                        render_cv_files(jid, data)
                        with db() as con:
                            con.execute("UPDATE cvs SET data=? WHERE job_id=?", (json.dumps(data, ensure_ascii=False), jid))
                        msg = "Mentve, PDF frissítve."
                    except Exception as ex:
                        msg = "Nem mentettem: %s" % ex
                    return self.redirect(back + "?msg=" + urllib.parse.quote(msg))
                if act == "send":
                    to = f.get("to", "").strip()
                    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):
                        return self.redirect(back + "?msg=" + urllib.parse.quote("Érvénytelen címzett."))
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
                            log.exception("info.json frissítés")
                        msg = "Elküldve: %s" % to
                    except Exception as ex:
                        msg = "Küldési hiba: %s" % ex
                    return self.redirect(back + "?msg=" + urllib.parse.quote(msg))
            if u.path == "/mark-all-seen":
                with db() as con:
                    con.execute("UPDATE jobs SET status='seen' WHERE status='new'")
                return self.redirect(self.back())
            if u.path == "/settings":
                save_settings(f)
                return self.redirect("/settings?msg=" + urllib.parse.quote("Mentve, a hirdetések újrapontozva."))
            if u.path == "/test":
                with CFG_LOCK:
                    cfg = load_config()
                try:
                    if f.get("what") == "email":
                        send_email(cfg, "Állásfigyelő teszt", "Működik az e-mail értesítés.",
                                   "<p>Működik az e-mail értesítés. ✅</p>")
                    else:
                        send_ntfy(cfg, "Állásfigyelő teszt", "Működik a push értesítés ✅",
                                  click=cfg.get("public_url"))
                    msg = "Teszt elküldve."
                except Exception as ex:
                    msg = "Teszt hiba: %s" % ex
                return self.redirect("/settings?msg=" + urllib.parse.quote(msg))
            self.send_html(page("Nincs ilyen", "<p>404</p>"), 404)
        except Exception as ex:
            log.exception("POST hiba")
            self.send_html(page("Hiba", "<p class=err>%s</p>" % e(ex)), 500)


def main():
    init_db()
    migrate_db()
    threading.Thread(target=backfill_docs, daemon=True).start()
    with CFG_LOCK:
        cfg = load_config()
    log.info("Állásfigyelő indul: port %d, ntfy topic: %s", PORT, cfg["ntfy"]["topic"])
    threading.Thread(target=scheduler, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
