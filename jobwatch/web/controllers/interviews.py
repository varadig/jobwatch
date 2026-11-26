"""The Interviews page: the interviews, their calendar files and feed, reminders, mailboxes and company research."""
import re
import secrets
from datetime import datetime, timedelta

from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.models import INTERVIEW_STATUS
from jobwatch.repositories.interviews import InterviewRepository
from jobwatch.services.interviews.core import interview_cfg, researcher
from jobwatch.services.interviews.ics import ics_filename, interviews_ics
from jobwatch.services.interviews.mailboxes import mailbox_book, scan_interview_mails
from jobwatch.util.timeutil import parse_local
from jobwatch.web.controllers.base import Controller
from jobwatch.web.http import Response
from jobwatch.web.views.interviews import interview_page, interviews_page

CALENDAR = "text/calendar; charset=utf-8"


def interview_fields(f):
    """The interview form's values: (start, or None if the date / time is missing; length in minutes)."""
    start = parse_local(f.get("iv_date", ""), f.get("iv_time", ""))
    try:
        minutes = max(5, min(600, int(f.get("iv_minutes") or 60)))
    except ValueError:
        minutes = 60
    return start, minutes


class InterviewsController(Controller):
    def register(self, router):
        router.get(r"/interviews", self.index)
        router.get(r"/interviews/(\d+)", self.show)
        router.get(r"/interviews/(\d+)\.ics", self.calendar_file)
        # calendar apps cannot log in: the secret address is the key
        router.get(r"/calendar/([A-Za-z0-9_-]{20,})\.ics", self.calendar_feed, public=True)
        router.post(r"/interviews", self.create)
        router.post(r"/interviews/settings", self.save_settings)
        router.post(r"/interviews/mailbox", self.mailbox)
        router.post(r"/interviews/scan", self.scan)
        router.post(r"/interviews/(\d+)", self.update)

    def index(self, req):
        return Response.html(interviews_page(req.query, req.msg))

    def show(self, req):
        """An interview's page, or (?partial=1) the content of the dialog on the Interviews page."""
        iid = int(req.params[0])
        if req.query.get("partial"):
            return Response.html(interview_page(iid, modal=True))
        return Response.html(interview_page(iid, req.msg))

    def calendar_file(self, req):
        with database.connect() as con:
            r = InterviewRepository(con).get(int(req.params[0]))
        with config_store.lock:
            cfg = config_store.load()
        rows = [r] if r else []
        return Response.file(interviews_ics(rows, cfg).encode("utf-8"), CALENDAR,
                             download_name=ics_filename(r) if r else "Interview.ics")

    def calendar_feed(self, req):
        """Every interview as a calendar to subscribe to (the cancelled ones of the last 60 days as cancelled)."""
        with config_store.lock:
            cfg = config_store.load()
        if not secrets.compare_digest(req.params[0], interview_cfg(cfg).get("cal_token") or "-"):
            return Response.html("Not found", 404)
        since = (datetime.now() - timedelta(days=60)).isoformat()
        with database.connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM interviews WHERE status<>'cancelled' OR starts > ? ORDER BY starts", (since,))]
        return Response.file(interviews_ics(rows, cfg).encode("utf-8"), CALENDAR)

    def create(self, req):
        f = req.form
        start, minutes = interview_fields(f)
        if not start or not f.get("iv_company", "").strip():
            return Response.redirect("/interviews", "Give the company, the date and the time.", "add")
        with database.connect() as con:
            job_id = f.get("job_id") or None
            if job_id and not con.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                job_id = None
            iid = InterviewRepository(con).add(f["iv_company"].strip(), f.get("iv_role", "").strip(), start, minutes,
                                               f.get("iv_place", "").strip(), f.get("iv_people", "").strip(),
                                               f.get("iv_notes", "").strip(), job_id=job_id)
        with config_store.lock:
            cfg = config_store.load()
        researching = interview_cfg(cfg)["research"] and researcher.start(iid)
        return Response.redirect("/interviews/%d" % iid, "Interview added; you get a reminder on the day and before it."
                                 + (" Claude is researching the company." if researching else ""))

    def update(self, req):
        """The buttons and the edit form of an interview."""
        f, iid = req.form, int(req.params[0])
        act = f.get("act", "")
        here = "/interviews/%d" % iid
        with database.connect() as con:
            interviews = InterviewRepository(con)
            if not interviews.get(iid):
                return Response.redirect("/interviews", "This interview is not here any more.")
            if act == "save":
                start, minutes = interview_fields(f)
                if not start or not f.get("iv_company", "").strip():
                    return Response.redirect(here, "Give the company, the date and the time.")
                interviews.update(iid, company=f["iv_company"].strip()[:120], role=f.get("iv_role", "").strip()[:200],
                                  starts=start.isoformat(timespec="minutes"), minutes=minutes,
                                  place=f.get("iv_place", "").strip()[:500], people=f.get("iv_people", "").strip()[:500],
                                  notes=f.get("iv_notes", "").strip()[:4000])
                return Response.redirect(here, "Saved.")
            if act in INTERVIEW_STATUS:
                interviews.update(iid, status=act)
                return Response.redirect(here, "Marked %s." % INTERVIEW_STATUS[act].lower())
            if act == "delete":
                interviews.delete(iid)
                return Response.redirect("/interviews", "Interview deleted.")
        if act == "research":
            return Response.redirect(here, "Claude is researching the company…" if researcher.start(iid, force=True)
                                     else "Set a Claude API key first (Settings → Claude API).")
        return Response.redirect(here, "Nothing to do.")

    def save_settings(self, req):
        f = req.form
        with config_store.lock:
            cfg = config_store.load()
            iv = cfg.setdefault("interviews", {})
            if re.fullmatch(r"\d{1,2}:\d{2}", f.get("iv_morning", "")):
                iv["morning"] = f["iv_morning"]
            try:
                iv["before_min"] = max(0, min(1440, int(f.get("iv_before") or 0)))
            except ValueError:
                pass
            iv["from_mail"] = "iv_from_mail" in f
            iv["research"] = "iv_research" in f
            config_store.save(cfg)
        return Response.redirect("/interviews", "Saved.")

    def mailbox(self, req):
        f = req.form
        act = f.get("act", "")
        if act == "add":
            msg = mailbox_book.add(f.get("mb_user", ""), f.get("mb_password", ""), f.get("mb_host", ""), f.get("mb_folder", ""))
        elif act == "remove":
            msg = mailbox_book.remove(f.get("mb_id", ""))
        elif act == "test":
            msg = mailbox_book.test(f.get("mb_id", ""))
        else:
            msg = "Nothing to do."
        return Response.redirect("/interviews", msg, "mailboxes")

    def scan(self, req):
        return Response.redirect("/interviews", scan_interview_mails())
