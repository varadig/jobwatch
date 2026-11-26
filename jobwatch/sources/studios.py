"""Company job boards (Ashby, Greenhouse, Lever, BambooHR, SmartRecruiters)."""
import html
import json
import time
from datetime import datetime
from datetime import timezone
from jobwatch.sources.base import Filtering, JobSource
from jobwatch.domain.workmode import norm_mode
from jobwatch.env import log

from jobwatch.util.text import strip_tags
from jobwatch.net import http


class StudiosSource(JobSource):
    """The job boards of the companies listed in Settings (Ashby, Greenhouse, Lever, BambooHR, SmartRecruiters)."""

    key = "studios"
    filtering = Filtering.OPTIONAL

    def fetch(self, cfg):
        out, errs = [], []
        for line in cfg.get("studios") or []:
            parts = [x.strip() for x in line.split("|")]
            if len(parts) < 2:
                continue
            ats, slug = parts[0].lower(), parts[1]
            name = parts[2] if len(parts) > 2 else slug
            try:
                if ats == "ashby":
                    d = json.loads(http.get("https://api.ashbyhq.com/posting-api/job-board/%s" % slug))
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
                    for j in json.loads(http.get("https://api.lever.co/v0/postings/%s?mode=json" % slug)):
                        c = j.get("categories") or {}
                        ts = j.get("createdAt")
                        out.append({"ext_id": j["id"], "title": j.get("text", ""), "company": name,
                                    "location": " ".join(x for x in (c.get("location"), j.get("workplaceType")) if x),
                                    "work_mode": norm_mode(j.get("workplaceType")),
                                    "url": j.get("hostedUrl", ""),
                                    "posted": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else "",
                                    "description": (j.get("descriptionPlain") or "")[:8000]})
                elif ats == "greenhouse":
                    d = json.loads(http.get("https://boards-api.greenhouse.io/v1/boards/%s/jobs?content=true" % slug))
                    for j in d.get("jobs", []):
                        out.append({"ext_id": str(j["id"]), "title": j.get("title", ""), "company": name,
                                    "location": (j.get("location") or {}).get("name", ""), "url": j.get("absolute_url", ""),
                                    "posted": j.get("updated_at", ""),
                                    "description": strip_tags(html.unescape(j.get("content", "")))[:8000]})
                elif ats == "bamboohr":
                    d = json.loads(http.get("https://%s.bamboohr.com/careers/list" % slug, headers={"Accept": "application/json"}))
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
                    d = json.loads(http.get("https://api.smartrecruiters.com/v1/companies/%s/postings" % slug))
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


