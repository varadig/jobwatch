"""Interviews: settings, storing, research and reminders."""
import json
import os
import secrets
import threading
from datetime import datetime
from datetime import timedelta

from jobwatch import cvgen

from jobwatch.env import log
from jobwatch.repositories.jobs import set_job_status
from jobwatch.services.documents import load_profile

from jobwatch.util.timeutil import now_iso, to_local
from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.services.notify import notifier


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


def find_job_for(con, company):
    """The posting of a company (the one you worked with first), for linking an interview to it."""
    if not company:
        return None
    r = con.execute("SELECT id FROM jobs WHERE lower(company)=lower(?) ORDER BY CASE status WHEN 'applied' THEN 0 "
                    "WHEN 'saved' THEN 1 ELSE 2 END, first_seen DESC LIMIT 1", (company.strip(),)).fetchone()
    return r["id"] if r else None


def add_interview(con, company, role, start, minutes=60, place="", people="", notes="", job_id=None, source="manual",
                  uid="", msgid=""):
    """Saves an interview and returns its id. A posting it belongs to is marked applied."""
    job_id = job_id or find_job_for(con, company)
    cur = con.execute("INSERT INTO interviews(job_id, company, role, starts, minutes, place, people, notes, source, uid, msgid, "
                      "status, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,'planned',?,?)",
                      (job_id, (company or "")[:120], (role or "")[:200], start.isoformat(timespec="minutes"),
                       int(minutes or 60), (place or "")[:500], (people or "")[:500], (notes or "")[:4000], source,
                       uid or "", msgid or "", now_iso(), now_iso()))
    if job_id:
        st = con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        if st and st["status"] in ("new", "seen", "saved"):
            set_job_status(con, job_id, "applied")
    return cur.lastrowid


def update_interview(con, iid, **kw):
    """Changes an interview. A new time starts the reminders again."""
    r = con.execute("SELECT * FROM interviews WHERE id=?", (iid,)).fetchone()
    if not r:
        return False
    if "starts" in kw and kw["starts"] != r["starts"]:
        kw.update(reminded_day=None, reminded_soon=None)
    kw["updated"] = now_iso()
    con.execute("UPDATE interviews SET %s WHERE id=?" % ", ".join("%s=?" % k for k in kw), list(kw.values()) + [iid])
    return True


def start_research(iid, force=False):
    """Researches the company of an interview in the background (Claude with web search), if a key is set."""
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
    threading.Thread(target=research_interview, args=(iid, key), daemon=True).start()
    return True


def research_interview(iid, key):
    try:
        with database.connect() as con:
            r = con.execute("SELECT * FROM interviews WHERE id=?", (iid,)).fetchone()
            job = con.execute("SELECT * FROM jobs WHERE id=?", (r["job_id"],)).fetchone() if r and r["job_id"] else None
        if not r:
            return
        try:
            prof = load_profile()
            candidate = "%s. %s" % (prof.get("headline", ""), prof.get("summary", ""))
        except Exception:
            candidate = ""
        posting = ""
        if job:
            posting = "Title: %s\nLocation: %s\nURL: %s\n\n%s" % (job["title"], job["location"] or "-", job["url"],
                                                                 job["description"] or "")
        text, sources = cvgen.company_research(key, r["company"], r["role"], posting, r["people"], r["place"], candidate)
        with database.connect() as con:
            con.execute("UPDATE interviews SET research=?, research_sources=?, research_status='done', research_at=?, "
                        "research_error=NULL WHERE id=?", (text, json.dumps(sources), now_iso(), iid))
        log.info("Interview %d: company research done (%d sources)", iid, len(sources))
    except Exception as ex:
        log.exception("Interview research failed")
        with database.connect() as con:
            con.execute("UPDATE interviews SET research_status='error', research_error=? WHERE id=?", (str(ex)[:500], iid))


def interview_reminders(now=None):
    """Sends the due reminders: on the day (from the morning time on) and shortly before. Returns what was sent."""
    with config_store.lock:
        cfg = config_store.load()
    ivc = interview_cfg(cfg)
    now = now or datetime.now().astimezone()
    before = timedelta(minutes=int(ivc["before_min"] or 0))
    try:
        hh, mm = (int(x) for x in str(ivc["morning"] or "08:00").split(":")[:2])
    except ValueError:
        hh, mm = 8, 0
    base = (cfg.get("public_url") or "").rstrip("/")
    sent = []
    with database.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM interviews WHERE status='planned' AND reminded_soon IS NULL")]
    for r in rows:
        st = iv_start(r)
        if not st or now >= st:
            continue
        what = "%s%s" % (r["company"] or "?", (" – " + r["role"]) if r["role"] else "")
        link = r["place"] if (r["place"] or "").startswith("http") else ("%s/interviews/%d" % (base, r["id"]) if base else None)
        kind = None
        if before and now >= st - before:
            kind = "soon"
            mins = max(1, int((st - now).total_seconds() // 60))
            title = "Interview in %d min: %s" % (mins, r["company"] or "?")
            msg = "%s at %s%s%s" % (what, st.strftime("%H:%M"), ("\n" + r["place"]) if r["place"] else "",
                                    ("\nWith: " + r["people"]) if r["people"] else "")
            prio = 5
        elif not r["reminded_day"] and st.date() == now.date() and now >= st.replace(hour=hh, minute=mm):
            kind = "day"
            title = "Interview today at %s: %s" % (st.strftime("%H:%M"), r["company"] or "?")
            msg = "%s, %d min%s%s" % (what, r["minutes"] or 60, ("\n" + r["place"]) if r["place"] else "",
                                      ("\nWith: " + r["people"]) if r["people"] else "")
            prio = 4
        if not kind:
            continue
        if cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic"):
            try:
                notifier.push(cfg, title, msg, click=link, priority=prio)
            except Exception:
                log.exception("Interview reminder failed")
                continue   # tried again on the next tick
        with database.connect() as con:
            con.execute("UPDATE interviews SET reminded_day=COALESCE(reminded_day, ?)%s WHERE id=?" % (
                ", reminded_soon=?" if kind == "soon" else ""), ([now_iso(), now_iso()] if kind == "soon" else [now_iso()]) + [r["id"]])
        sent.append((r["id"], kind))
    return sent


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
