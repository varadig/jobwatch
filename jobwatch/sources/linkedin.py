"""LinkedIn: the guest search pages and the posting details."""
import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from jobwatch.sources.base import JobSource
from jobwatch.domain.workmode import classify_mode_ex
from jobwatch.env import log

from jobwatch.search import linkedin_plan
from jobwatch.util.text import strip_tags
from jobwatch.net import http


class LinkedInSource(JobSource):
    """LinkedIn's public (guest) job search, one query per search term and location."""

    key = "linkedin"

    def fetch(self, cfg):
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
                        page = http.get(url)
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


def linkedin_detail(jid):
    """Full posting text from the public jobPosting endpoint (None if it failed)."""
    page = http.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/%s" % jid)
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
