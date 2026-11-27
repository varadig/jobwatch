"""The Jobs page, a posting's detail view and the actions on postings; refreshing and clearing the list; the Sources page."""
import threading
import time
import urllib.parse

from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.models import FETCHED_SOURCES, STATUS_LABEL
from jobwatch.repositories.jobs import JobRepository
from jobwatch.services.enrich import enrich_work_modes
from jobwatch.services.postings import add_job_by_link, posting_preview
from jobwatch.services.refresh import refresher, reset_job_cache
from jobwatch.services.state import STATE
from jobwatch.util.text import safe_url
from jobwatch.web.components import with_msg
from jobwatch.web.controllers.base import Controller
from jobwatch.web.http import Response
from jobwatch.web.views.jobs import job_detail_html, job_page, jobs_page
from jobwatch.web.views.sources import sources_page


def run_selection(f):
    """Which sources a refresh form asks for: (source keys, or None for all; own-source ids). "one" = a row's button."""
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


class JobsController(Controller):
    def register(self, router):
        router.get(r"/", self.index)
        router.get(r"/job/([^/]+)", self.show)
        router.get(r"/job/([^/]+)/preview", self.preview)
        router.get(r"/open/(.+)", self.open_original)
        router.get(r"/api/jobs", self.api_list)
        router.get(r"/sources", self.sources)
        router.post(r"/job/([^/]+)", self.update)
        router.post(r"/add-job", self.add)
        router.post(r"/mark-all-seen", self.mark_all_seen)
        router.post(r"/run", self.refresh)
        router.post(r"/reset-jobs", self.clear)

    def index(self, req):
        return Response.html(jobs_page(req.query))

    def sources(self, req):
        return Response.html(sources_page(req.msg))

    def show(self, req):
        """The posting's page, or (?partial=1) the content of the dialog on the Jobs page."""
        jid = urllib.parse.unquote(req.params[0])
        if req.query.get("partial"):
            return Response.html(job_detail_html(jid, modal=True)[1])
        back = req.back()
        return Response.html(job_page(jid, back if not back.startswith("/job/") else "/", req.msg))

    def preview(self, req):
        h = posting_preview(urllib.parse.unquote(req.params[0]))
        return Response.html(h or "", 200 if h else 404)

    def open_original(self, req):
        """Opens the original posting (and marks a new one seen)."""
        jid = urllib.parse.unquote(req.params[0])
        with database.connect() as con:
            r = con.execute("SELECT url, status FROM jobs WHERE id=?", (jid,)).fetchone()
            if r and r["status"] == "new":
                JobRepository(con).set_status(jid, "seen", by="opened the original posting")
        return Response.redirect(safe_url(r["url"]) if r else "/")

    def api_list(self, req):
        with database.connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT id,source,title,company,location,url,posted,score,matched,first_seen,status "
                "FROM jobs ORDER BY first_seen DESC LIMIT 500")]
        return Response.json(rows)

    def update(self, req):
        """A posting's status, or its work mode set by hand ("auto": decided again in the background)."""
        f = req.form
        jid = urllib.parse.unquote(req.params[0])
        status, mode = f.get("status"), f.get("work_mode")
        with database.connect() as con:
            jobs = JobRepository(con)
            if status in STATUS_LABEL:
                jobs.set_status(jid, status, by="you, on the %s" % ("dialog or card" if req.headers.get("X-Requested-With") else "page"))
            if mode in ("remote", "hybrid", "onsite"):   # set by hand: never overwritten, remembered
                con.execute("UPDATE jobs SET work_mode=?, mode_src='manual', mode_note='set by you' WHERE id=?", (mode, jid))
                jobs.set_manual_mode(jid, mode)
            elif mode == "auto":
                jobs.set_manual_mode(jid, None)
                con.execute("UPDATE jobs SET work_mode='unknown', mode_src=NULL, mode_note=NULL, mode_checked=NULL "
                            "WHERE id=?", (jid,))
        if mode == "auto":
            with config_store.lock:
                cfg = config_store.load()
            threading.Thread(target=enrich_work_modes, args=(cfg,), daemon=True).start()
        nxt = f.get("next", "")
        if status in ("hidden", "seen") and not nxt.startswith("/job/") and self.safe_next(nxt, ""):
            return Response.redirect(nxt)   # done with it on its page: back to the list
        return Response.redirect(req.back())

    def add(self, req):
        jid, msg = add_job_by_link(req.form.get("url", ""))
        if jid:
            return Response.redirect("/cv/" + urllib.parse.quote(jid, safe=""), msg)
        return Response.redirect("/", msg)

    def mark_all_seen(self, req):
        with database.connect() as con:
            jobs = JobRepository(con)
            for r in con.execute("SELECT id FROM jobs WHERE status='new'").fetchall():
                jobs.set_status(r["id"], "seen", by="mark all new as seen")
        return Response.redirect(req.back())

    def refresh(self, req):
        f = req.form
        only, cids = run_selection(f)
        if only is None and not cids and not f.get("all") and ("one" in f or "selected" in f):
            return Response.redirect("/sources", "Tick at least one source to refresh.")
        if STATE["running"]:
            return Response.redirect(req.back(), "A refresh is already running; try again when it has finished.")
        threading.Thread(target=refresher.run, kwargs={"force": True, "only": only, "custom_ids": cids}, daemon=True).start()
        time.sleep(0.3)   # so the page shows it running
        return Response.redirect(req.back())

    def clear(self, req):
        reset_job_cache()
        if req.form.get("refetch"):
            threading.Thread(target=refresher.run, kwargs={"force": True}, daemon=True).start()
            msg = "Job cache cleared and a new search started: the list fills up again in a few minutes."
        else:
            msg = "Job cache cleared. Press Refresh all to fetch again."
        return Response.redirect(with_msg(req.back(), msg + " The postings you made a CV for stay, marked applied."))
