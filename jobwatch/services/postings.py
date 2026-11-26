"""A single posting: adding one by its link, and its full text for the detail view."""
import hashlib
import html
import json
import re
import threading

from jobwatch.domain.identity import dup_key, url_key
from jobwatch.domain.scoring import score_job
from jobwatch.domain.workmode import classify_mode_ex
from jobwatch.env import log
from jobwatch.models import SOURCES_INFO

from jobwatch.repositories.jobs import JobRepository
from jobwatch.services.enrich import enrich_work_modes
from jobwatch.services.logos import fetch_logos, remember_logo
from jobwatch.services.outreach import LINKEDIN_JOB_RE, linkedin_job
from jobwatch.util.text import strip_tags
from jobwatch.util.timeutil import now_iso
from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.net import http


def job_from_page(url):
    """Title, company, location, description (and logo) of any job page: from its JobPosting structured data
    (schema.org, what Google for Jobs reads) when it has one, else from the page title and text."""
    m = LINKEDIN_JOB_RE.search(url)
    if m:
        j = linkedin_job(m.group(1))
        return dict(j, url="https://www.linkedin.com/jobs/view/%s/" % m.group(1), ext_id="linkedin-" + m.group(1), logo="", posted="")
    pg = http.get(url, timeout=25)
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
    with database.connect() as con:
        row = con.execute("SELECT id, dup_of FROM jobs WHERE urlkey=? LIMIT 1", (url_key(url),)).fetchone()
        if row:
            return row["dup_of"] or row["id"], "This posting is already in the list."
    try:
        it = job_from_page(url)
    except Exception as ex:
        return None, "Could not read the page: %s" % ex
    if not it["title"]:
        return None, "The page has no readable posting (it may need a login). Copy its text into the CV page instead."
    with config_store.lock:
        cfg = config_store.load()
    sc, hits = score_job(cfg, it)
    if it.get("work_mode") == "remote":   # the page's structured data says TELECOMMUTE
        wm, msrc, mnote = "remote", "source", "from the posting page (structured data)"
    else:
        wm, mnote = classify_mode_ex(it["title"], it["location"], it["description"])
        msrc = "text" if wm != "unknown" else None
    jid, ts = "manual:" + it["ext_id"], now_iso()
    with database.connect() as con:
        remember_logo(con, it["company"], it.get("logo"))
        same = JobRepository(con).find_duplicate(it["title"], it["company"], it["url"])
        if same:
            JobRepository(con).merge_into(same, "manual", it)
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


class _CleanHTML(__import__("html.parser").parser.HTMLParser):
    """Keeps the text and the simple structure of a posting (paragraphs, lists, headings, bold); drops the rest."""
    KEEP = {"p", "br", "ul", "ol", "li", "strong", "b", "em", "i", "h3", "h4", "blockquote"}
    MAP = {"h1": "h3", "h2": "h3", "h5": "h4", "h6": "h4"}
    BLOCK = {"div", "section", "article", "tr", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript", "iframe", "svg", "button", "form"):
            self.skip += 1
            return
        t = self.MAP.get(tag, tag)
        if not self.skip and t in self.KEEP:
            self.out.append("<%s>" % t)
        elif not self.skip and tag in self.BLOCK:
            self.out.append("<br>")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript", "iframe", "svg", "button", "form"):
            self.skip = max(0, self.skip - 1)
            return
        t = self.MAP.get(tag, tag)
        if not self.skip and t in self.KEEP and t != "br":
            self.out.append("</%s>" % t)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))


def clean_posting_html(raw):
    """Safe HTML of a posting: plain text becomes paragraphs; HTML keeps only simple tags (no links, no scripts)."""
    raw = (raw or "").strip()
    if "&lt;" in raw[:500] and "<" not in raw[:200]:
        raw = html.unescape(raw)
    if not re.search(r"<(p|br|ul|li|div|strong|b|h\d)\b", raw, re.I):
        paras = [x.strip() for x in re.split(r"\n\s*\n", raw) if x.strip()]
        return "".join("<p>%s</p>" % html.escape(x).replace("\n", "<br>") for x in paras)
    c = _CleanHTML()
    c.feed(raw)
    c.close()
    out = re.sub(r"(\s*<br>\s*){3,}", "<br><br>", "".join(c.out))
    out = re.sub(r"<(p|li|h3|h4)>\s*</\1>", "", out)
    return re.sub(r"^(\s*<br>)+|(<br>\s*)+$", "", out.strip()).strip()


def fetch_posting_html(r):
    """The posting as it is on its own page (LinkedIn's public posting, or the page's JobPosting data). None if not found."""
    uk = r["urlkey"] or ""
    if uk.startswith("li:"):
        pg = http.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/%s" % uk[3:], timeout=20)
        m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', pg, re.S)
        return m.group(1) if m else None
    if not (r["url"] or "").startswith("http"):
        return None
    pg = http.get(r["url"], timeout=20)
    for blk in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', pg, re.S | re.I):
        try:
            data = json.loads(blk.strip())
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for n in nodes:
            if isinstance(n, dict) and "JobPosting" in str(n.get("@type")) and n.get("description"):
                return n["description"]
    return None


def posting_preview(jid):
    """The cleaned posting HTML for the detail page: stored once fetched; fills in a missing description too."""
    with database.connect() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    if not r:
        return None
    if r["desc_html"]:
        return r["desc_html"]
    try:
        raw = fetch_posting_html(r)
    except Exception as ex:
        log.info("Posting preview %s: %s", jid, ex)
        raw = None
    if not raw:
        return None
    clean = clean_posting_html(raw)
    if len(strip_tags(clean)) < 40:
        return None
    with database.connect() as con:
        con.execute("UPDATE jobs SET desc_html=? WHERE id=?", (clean, jid))
        if not (r["description"] or "").strip():
            text = strip_tags(clean.replace("<br>", "\n").replace("</p>", "\n").replace("</li>", "\n"))[:12000]
            with config_store.lock:
                cfg = config_store.load()
            sc, hits = score_job(cfg, dict(dict(r), description=text))
            con.execute("UPDATE jobs SET description=?, score=?, matched=? WHERE id=?", (text, sc, ", ".join(hits), jid))
    return clean
