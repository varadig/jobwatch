"""Job boards with a public feed or API."""
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from jobwatch.net import http
from jobwatch.search import search_keywords
from jobwatch.sources.base import Filtering, JobSource
from jobwatch.util.text import strip_tags
from jobwatch.util.timeutil import epoch_iso, rfc822_iso


class MeetFrankSource(JobSource):
    """MeetFrank's latest remote jobs (pages of its listing)."""

    key = "meetfrank"
    filtering = Filtering.OPTIONAL

    def fetch(self, cfg):
        out, errs = [], []
        for page_url in ("https://meetfrank.com/latest-remote-jobs-in-meetfrankcom",
                         "https://meetfrank.com/fully-remote-jobs-in-meetfrankcom"):
            try:
                s = http.get(page_url)
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


class FourDayWeekSource(JobSource):
    """4dayweek.io: jobs with a four-day week (RSS)."""

    key = "4dayweek"
    filtering = Filtering.OPTIONAL

    def fetch(self, cfg):
        return parse_rss(http.get("https://4dayweek.io/feed"))


class FlexaSource(JobSource):
    """Flexa: flexible and remote jobs, searched by each term."""

    key = "flexa"

    def fetch(self, cfg):
        out, seen = [], set()
        for q in search_keywords(cfg):
            s = http.get("https://flexa.careers/jobs?" + urllib.parse.urlencode({"search": q}))
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


class RemotiveSource(JobSource):
    """Remotive's API, searched by each term (it asks for at most ~4 calls a day)."""

    key = "remotive"
    min_interval = 360

    def fetch(self, cfg):
        out = []
        for q in search_keywords(cfg):
            data = json.loads(http.get("https://remotive.com/api/remote-jobs?" + urllib.parse.urlencode({"search": q})))
            for j in data.get("jobs", []):
                out.append({"ext_id": str(j.get("id")), "title": j.get("title", ""),
                            "company": j.get("company_name", ""),
                            "location": j.get("candidate_required_location", ""), "url": j.get("url", ""),
                            "posted": j.get("publication_date", ""),
                            "description": strip_tags(j.get("description", ""))[:8000], "work_mode": "remote",
                            "logo": j.get("company_logo") or ""})
        return out


class RemoteOkSource(JobSource):
    """Remote OK: one JSON list of the latest remote postings (link back to them, as its terms ask)."""

    key = "remoteok"
    filtering = Filtering.TITLE
    optional = True
    min_interval = 120

    def fetch(self, cfg):
        out = []
        for j in json.loads(http.get("https://remoteok.com/api")):
            if not isinstance(j, dict) or not j.get("id") or not j.get("position"):
                continue   # the first entry is the legal notice
            out.append({"ext_id": str(j["id"]), "title": j.get("position", ""), "company": j.get("company", ""),
                        "location": j.get("location") or "", "url": j.get("url") or j.get("apply_url", ""),
                        "posted": j.get("date", ""), "description": strip_tags(j.get("description", ""))[:8000],
                        "work_mode": "remote", "logo": j.get("company_logo") or j.get("logo") or ""})
        return out


class WeWorkRemotelySource(JobSource):
    """We Work Remotely: the RSS feed of all categories (title: "Company: Title")."""

    key = "weworkremotely"
    filtering = Filtering.TITLE
    optional = True

    def fetch(self, cfg):
        out = []
        root = ET.fromstring(http.get("https://weworkremotely.com/remote-jobs.rss"))
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            company, _, t = title.partition(": ")
            if not t:
                company, t = "", title
            link = (it.findtext("link") or "").strip()
            out.append({"ext_id": it.findtext("guid") or link, "title": t.strip(), "company": company.strip(),
                        "location": (it.findtext("region") or "").strip(), "url": link,
                        "posted": rfc822_iso(it.findtext("pubDate") or ""),
                        "description": strip_tags(it.findtext("description") or "")[:8000], "work_mode": "remote"})
        return out


class HimalayasSource(JobSource):
    """Himalayas: remote postings, searched by each term."""

    key = "himalayas"
    optional = True
    min_interval = 120

    def fetch(self, cfg):
        out, seen = [], set()
        for q in search_keywords(cfg):
            d = json.loads(http.get("https://himalayas.app/jobs/api/search?" + urllib.parse.urlencode({"q": q})))
            for j in d.get("jobs", []):
                gid = j.get("guid") or j.get("applicationLink") or ""
                if not gid or gid in seen:
                    continue
                seen.add(gid)
                locs = j.get("locationRestrictions") or []
                out.append({"ext_id": gid.rstrip("/").rsplit("/companies/", 1)[-1], "title": j.get("title", ""),
                            "company": j.get("companyName", ""),
                            "location": ", ".join(locs[:6]) + (" …" if len(locs) > 6 else "") if locs else "Worldwide",
                            "url": gid, "posted": epoch_iso(j.get("pubDate")),
                            "description": strip_tags(j.get("description", ""))[:8000], "work_mode": "remote",
                            "logo": j.get("companyLogo") or ""})
            time.sleep(1)
        return out


class JobicySource(JobSource):
    """Jobicy: remote postings searched by each term; its search also matches inside words, so they are checked again."""

    key = "jobicy"
    filtering = Filtering.ALWAYS
    optional = True
    min_interval = 120

    def fetch(self, cfg):
        out, seen = [], set()
        for q in search_keywords(cfg):
            d = json.loads(http.get("https://jobicy.com/api/v2/remote-jobs?" + urllib.parse.urlencode({"count": 50, "tag": q})))
            for j in d.get("jobs", []):
                jid = str(j.get("id") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                out.append({"ext_id": jid, "title": html.unescape(j.get("jobTitle", "")), "company": j.get("companyName", ""),
                            "location": j.get("jobGeo") or "", "url": j.get("url", ""), "posted": j.get("pubDate", ""),
                            "description": strip_tags(j.get("jobDescription") or j.get("jobExcerpt") or "")[:8000],
                            "work_mode": "remote", "logo": j.get("companyLogo") or ""})
            time.sleep(1)
        return out


class WorkingNomadsSource(JobSource):
    """Working Nomads: one JSON list of the latest remote postings."""

    key = "workingnomads"
    filtering = Filtering.TITLE
    optional = True
    min_interval = 120

    def fetch(self, cfg):
        out = []
        for j in json.loads(http.get("https://www.workingnomads.com/api/exposed_jobs/")):
            url = j.get("url") or ""
            m = re.search(r"/(\d+)/?$", url)
            out.append({"ext_id": m.group(1) if m else url, "title": j.get("title", ""), "company": j.get("company_name", ""),
                        "location": j.get("location") or "", "url": url, "posted": j.get("pub_date", ""),
                        "description": strip_tags(j.get("description", ""))[:8000], "work_mode": "remote"})
        return out


class ArbeitnowSource(JobSource):
    """Arbeitnow: European (mostly German) postings, newest first; the first three pages."""

    key = "arbeitnow"
    filtering = Filtering.TITLE
    optional = True
    min_interval = 120

    def fetch(self, cfg):
        out = []
        for page in (1, 2, 3):
            d = json.loads(http.get("https://www.arbeitnow.com/api/job-board-api?page=%d" % page))
            for j in d.get("data", []):
                out.append({"ext_id": j.get("slug") or j.get("url", ""), "title": j.get("title", ""),
                            "company": j.get("company_name", ""), "location": j.get("location") or "",
                            "url": j.get("url", ""), "posted": epoch_iso(j.get("created_at")),
                            "description": strip_tags(j.get("description", ""))[:8000],
                            "work_mode": "remote" if j.get("remote") is True else None})
            if not (d.get("links") or {}).get("next"):
                break
            time.sleep(2)
        return out


class RemoteGameJobsSource(JobSource):
    """Remote Game Jobs: remote game-industry postings (RSS)."""

    key = "remotegamejobs"
    filtering = Filtering.ALWAYS
    optional = True

    def fetch(self, cfg):
        out = []
        root = ET.fromstring(http.get("https://remotegamejobs.com/feed.rss"))
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            m = re.match(r"(.+?) is hiring (?:an? )?(.+?)(?: \(Remote Job\))?$", title)
            company, t = (m.group(1), m.group(2)) if m else ("", title)
            link = (it.findtext("link") or "").strip()
            out.append({"ext_id": it.findtext("guid") or link, "title": t, "company": company, "location": "Remote",
                        "url": link, "posted": rfc822_iso(it.findtext("pubDate") or ""),
                        "description": strip_tags(it.findtext("description") or "")[:8000], "work_mode": "remote"})
        return out


class WorkWithIndiesSource(JobSource):
    """Work With Indies: indie game studios' postings (RSS)."""

    key = "workwithindies"
    filtering = Filtering.ALWAYS
    optional = True

    def fetch(self, cfg):
        out = []
        root = ET.fromstring(http.get("https://workwithindies.com/careers/rss.xml"))
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            m = re.match(r"(.+?) is hiring (?:an? )?(.+?)(?: to work (?:from|in) (.+))?$", title)
            company, t, where = (m.group(1), m.group(2), m.group(3) or "") if m else ("", title, "")
            link = (it.findtext("link") or "").strip()
            out.append({"ext_id": it.findtext("guid") or link, "title": t, "company": company,
                        "location": where if where.lower() != "anywhere" else "Remote (anywhere)", "url": link,
                        "posted": rfc822_iso(it.findtext("pubDate") or ""),
                        "description": strip_tags(it.findtext("description") or "")[:8000],
                        "work_mode": "remote" if where.lower() in ("anywhere", "remote") else None})
        return out


class RssSource(JobSource):
    """Your own RSS feeds from Settings."""

    key = "rss"
    filtering = Filtering.OPTIONAL

    def fetch(self, cfg):
        out = []
        for u in cfg.get("rss_feeds") or []:
            u = u.strip()
            if u:
                out += parse_rss(http.get(u))
        return out


