"""Interviews: their settings, the company research and the reminders."""
import json
import os
import secrets
import threading
from datetime import datetime, timedelta

from jobwatch import cvgen
from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.env import log
from jobwatch.repositories.interviews import InterviewRepository
from jobwatch.services.documents import load_profile
from jobwatch.services.notify import notifier
from jobwatch.util.timeutil import now_iso, to_local

# An interview: added by hand (optionally from a posting) or found in the mailbox (a calendar invitation, a booking
# confirmation, a recruiter's e-mail). Reminders go to ntfy on the day (in the morning) and shortly before. Claude can
# research the company and the interviewers on the web. The interviews are also an iCalendar feed to subscribe to.
INTERVIEW_DEFAULTS = {"morning": "08:00", "before_min": 60, "from_mail": True, "mail_days": 14, "research": False, "cal_token": ""}


IV_SCAN_EVERY = 30 * 60           # seconds between e-mail scans


def interview_cfg(cfg):
    out = dict(INTERVIEW_DEFAULTS)
    out.update(cfg.get("interviews") or {})
    return out


def iv_start(r):
    try:
        return to_local(datetime.fromisoformat(r["starts"]))
    except (TypeError, ValueError):
        return None


def calendar_token(cfg_lock=True):
    """The secret part of the calendar feed's address (made on first use)."""
    with config_store.lock:
        cfg = config_store.load()
        iv = cfg.setdefault("interviews", {})
        if not iv.get("cal_token"):
            iv["cal_token"] = secrets.token_urlsafe(24)
            config_store.save(cfg)
        return iv["cal_token"]


class Researcher:
    """Researches the company of an interview with Claude and web search, in the background (it takes a minute or
    two); the result is stored with the interview. Needs a Claude API key."""

    def start(self, iid, force=False):
        """Starts the research; False if there is no key, no such interview, or it is already running."""
        with config_store.lock:
            cfg = config_store.load()
        key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            return False
        with database.connect() as con:
            r = con.execute("SELECT research_status FROM interviews WHERE id=?", (iid,)).fetchone()
            if not r or (r["research_status"] == "running" and not force):
                return False
            con.execute("UPDATE interviews SET research_status='running', research_error=NULL WHERE id=?", (iid,))
        threading.Thread(target=self.run, args=(iid, key), daemon=True).start()
        return True

    def run(self, iid, key):
        try:
            with database.connect() as con:
                r = InterviewRepository(con).get(iid)
                job = con.execute("SELECT * FROM jobs WHERE id=?", (r["job_id"],)).fetchone() if r and r["job_id"] else None
            if not r:
                return
            text, sources = cvgen.company_research(key, r["company"], r["role"], self._posting(job), r["people"],
                                                   r["place"], self._candidate())
            with database.connect() as con:
                InterviewRepository(con).update(iid, research=text, research_sources=json.dumps(sources),
                                                research_status="done", research_at=now_iso(), research_error=None)
            log.info("Interview %d: company research done (%d sources)", iid, len(sources))
        except Exception as ex:
            log.exception("Interview research failed")
            with database.connect() as con:
                InterviewRepository(con).update(iid, research_status="error", research_error=str(ex)[:500])

    @staticmethod
    def _posting(job):
        if not job:
            return ""
        return "Title: %s\nLocation: %s\nURL: %s\n\n%s" % (job["title"], job["location"] or "-", job["url"],
                                                          job["description"] or "")

    @staticmethod
    def _candidate():
        """A line about you, so the likely questions fit you."""
        try:
            prof = load_profile()
            return "%s. %s" % (prof.get("headline", ""), prof.get("summary", ""))
        except Exception:
            return ""


class Reminders:
    """The ntfy reminders of the planned interviews: on the day (from the morning time on) and shortly before."""

    def send_due(self, now=None):
        """Sends the reminders that are due now. Returns [(interview id, "day" | "soon")]."""
        with config_store.lock:
            cfg = config_store.load()
        ivc = interview_cfg(cfg)
        now = now or datetime.now().astimezone()
        before = timedelta(minutes=int(ivc["before_min"] or 0))
        try:
            hh, mm = (int(x) for x in str(ivc["morning"] or "08:00").split(":")[:2])
        except ValueError:
            hh, mm = 8, 0
        with database.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT * FROM interviews WHERE status='planned' AND reminded_soon IS NULL")]
        sent = []
        for r in rows:
            st = iv_start(r)
            if not st or now >= st:
                continue
            if before and now >= st - before:
                kind = "soon"
            elif not r["reminded_day"] and st.date() == now.date() and now >= st.replace(hour=hh, minute=mm):
                kind = "day"
            else:
                continue
            if not self._push(cfg, r, st, now, kind):
                continue   # tried again on the next tick
            with database.connect() as con:
                fields = {"reminded_day": r["reminded_day"] or now_iso()}
                if kind == "soon":
                    fields["reminded_soon"] = now_iso()
                InterviewRepository(con).update(r["id"], **fields)
            sent.append((r["id"], kind))
        return sent

    @staticmethod
    def _push(cfg, r, st, now, kind):
        """Sends one reminder; tapping it opens the meeting (or the interview). False if sending failed."""
        if not (cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic")):
            return True   # nowhere to send: counts as done
        base = (cfg.get("public_url") or "").rstrip("/")
        what = "%s%s" % (r["company"] or "?", (" – " + r["role"]) if r["role"] else "")
        extra = ("\n" + r["place"] if r["place"] else "") + ("\nWith: " + r["people"] if r["people"] else "")
        link = r["place"] if (r["place"] or "").startswith("http") else ("%s/interviews/%d" % (base, r["id"]) if base else None)
        if kind == "soon":
            title = "Interview in %d min: %s" % (max(1, int((st - now).total_seconds() // 60)), r["company"] or "?")
            msg, prio = "%s at %s%s" % (what, st.strftime("%H:%M"), extra), 5
        else:
            title = "Interview today at %s: %s" % (st.strftime("%H:%M"), r["company"] or "?")
            msg, prio = "%s, %d min%s" % (what, r["minutes"] or 60, extra), 4
        try:
            notifier.push(cfg, title, msg, click=link, priority=prio)
            return True
        except Exception:
            log.exception("Interview reminder failed")
            return False


researcher = Researcher()
reminders = Reminders()


def notify_interview(cfg, title, message, iid):
    if not (cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic")):
        return
    base = (cfg.get("public_url") or "").rstrip("/")
    try:
        notifier.push(cfg, title, message, click=("%s/interviews/%d" % (base, iid)) if base else None, priority=4)
    except Exception:
        log.exception("Interview notification failed")


def fmt_iv_when(r):
    st = iv_start(r)
    return st.strftime("%a %d %b %Y, %H:%M") if st else "time not set"


def rel_when(st, now=None):
    now = now or datetime.now().astimezone()
    days = (st.date() - now.date()).days
    if st < now:
        return "past"
    if days == 0:
        mins = int((st - now).total_seconds() // 60)
        return "in %d min" % mins if mins < 60 else "today"
    return "tomorrow" if days == 1 else "in %d days" % days
