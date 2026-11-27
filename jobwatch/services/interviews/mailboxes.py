"""Interviews found in the mailboxes (invitations, recruiters' e-mails)."""
import hashlib
import json
import re
import secrets
import threading
import time
from datetime import datetime

from jobwatch import cvgen
from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.env import log
from jobwatch.llm import llm_for
from jobwatch.repositories.interviews import InterviewRepository
from jobwatch.services.interviews.core import fmt_iv_when, interview_cfg, notify_interview, researcher
from jobwatch.services.interviews.ics import meeting_link, parse_ics
from jobwatch.services.state import IV_LOCK
from jobwatch.sources.mail import mail_body, mail_login
from jobwatch.util.timeutil import local_tz_name, now_iso, to_local

IV_MAIL_KEYWORDS = ("interview", "interj", "invitation", "invite", "meeting", "call with", "intro call", "video call",
                    "phone call", "calendly", "scheduled", "confirmed", "next step", "megh", "einladung", "gespr",
                    "entretien", "rozmow", "colloquio", "entrevista", "screening")


# not an interview, whatever the subject says: no Claude call for these
IV_NOT_RX = re.compile(r"accepted your invitation|invitation to connect|wants to connect|invited you to (?:join|follow)|"
                       r"\bsale\b|% off|discount|kedvezm|webinar|newsletter|last call", re.I)


# a fixed time is mentioned (a clock time, a weekday or "tomorrow"), in a few languages
IV_TIME_RX = re.compile(r"\b\d{1,2}[:.]\d{2}\b|\b\d{1,2}\s?(?:am|pm)\b|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b|"
                        r"tomorrow|holnap|h[ée]tf[őo]|kedd|szerd[aá]|cs[üu]t[öo]rt[öo]k|p[ée]ntek|morgen|demain|jutro", re.I)


IV_RX = re.compile(r"interview|interj|screening|technical (?:call|round)|hiring|recruit|gespr[äa]ch|entretien|rozmow"
                   r"|colloquio|entrevista|bewerbung|candidate", re.I)


# IMAP servers of the common mail providers, by the address's domain
IMAP_HOSTS = {"icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com", "mac.com": "imap.mail.me.com",
              "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
              "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com",
              "live.com": "outlook.office365.com", "msn.com": "outlook.office365.com",
              "yahoo.com": "imap.mail.yahoo.com", "freemail.hu": "imap.freemail.hu", "gmx.net": "imap.gmx.net",
              "gmx.com": "imap.gmx.com"}


APP_PASSWORD_HELP = {
    "imap.mail.me.com": ("iCloud needs an app-specific password: appleid.apple.com → Sign-In and Security → "
                         "App-Specific Passwords.", "https://appleid.apple.com/account/manage"),
    "imap.gmail.com": ("Gmail needs an app password (with 2-step verification on): Google Account → Security → "
                       "App passwords.", "https://myaccount.google.com/apppasswords"),
    "outlook.office365.com": ("Outlook.com may need an app password: Microsoft account → Security → Advanced "
                              "security options.", "https://account.microsoft.com/security"),
    "imap.mail.yahoo.com": ("Yahoo needs an app password: Account security → Generate app password.",
                            "https://login.yahoo.com/account/security"),
}


def imap_host_for(address):
    dom = (address or "").rsplit("@", 1)[-1].strip().lower()
    return IMAP_HOSTS.get(dom, "imap." + dom if dom else "")


def interview_mailboxes(cfg):
    """The mailboxes searched for interviews: the main one (Settings → Notifications / E-mail alerts) and the ones
    added on the Interviews page. [{id, label, host, port, user, password, folder}]"""
    out = []
    try:
        host, port, user, pw = mail_login(cfg)
        out.append({"id": "main", "label": user, "host": host, "port": port, "user": user, "password": pw,
                    "folder": (cfg.get("mail_source") or {}).get("folder") or "INBOX"})
    except Exception:
        pass
    for mb in interview_cfg(cfg).get("mailboxes") or []:
        if mb.get("user") and mb.get("password"):
            out.append(dict({"host": imap_host_for(mb["user"]), "port": 993, "folder": "INBOX", "label": mb["user"]}, **mb))
    return out


def imap_open(mb):
    """An IMAP connection to a mailbox, logged in, with its folder opened read-only."""
    import imaplib
    M = imaplib.IMAP4_SSL(mb["host"], int(mb.get("port") or 993), timeout=30)
    try:
        M.login(mb["user"], mb["password"])
        typ, _ = M.select('"%s"' % (mb.get("folder") or "INBOX").replace('"', ""), readonly=True)
        if typ != "OK":
            raise RuntimeError("cannot open the folder %s" % (mb.get("folder") or "INBOX"))
    except Exception:
        try:
            M.logout()
        except Exception:
            pass
        raise
    return M


def imap_error(ex):
    t = str(ex)
    if isinstance(ex, bytes) or t.startswith("b'"):
        t = t[2:-1]
    if "AUTHENTICATIONFAILED" in t.upper() or "invalid credentials" in t.lower() or "authentication failed" in t.lower():
        return "the login was refused (for most providers use an app password, not the normal one)"
    return t[:200] or type(ex).__name__


def read_mailbox_interviews(mb, since, done, limit):
    """The new interview-like e-mails of one mailbox: [(msgid, sender, subject, date, msg)]. Read-only (PEEK)."""
    import email as email_lib
    import email.policy
    out = []
    M = imap_open(mb)
    try:
        crit = ["OR"] * (len(IV_MAIL_KEYWORDS) - 1)
        for k in IV_MAIL_KEYWORDS:
            crit += ["SUBJECT", '"%s"' % k]
        typ, data = M.search(None, "SINCE", since, *crit)
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        for num in sorted(ids, key=int, reverse=True):
            typ, data = M.fetch(num, "(BODY.PEEK[])")
            raw = next((x[1] for x in data or [] if isinstance(x, tuple) and len(x) > 1), None) if typ == "OK" else None
            if not raw:
                continue
            msg = email_lib.message_from_bytes(raw, policy=email.policy.default)
            msgid = (msg.get("Message-ID") or "").strip() or hashlib.sha1(raw[:2000]).hexdigest()
            if msgid in done:
                continue
            done.add(msgid)   # the same e-mail in two mailboxes is read once
            out.append((msgid, str(msg.get("From", "")), str(msg.get("Subject", "")), str(msg.get("Date", "")), msg))
            if len(out) >= limit:
                break
    finally:
        try:
            M.logout()
        except Exception:
            pass
    return out


def read_interview_mails(cfg, limit=25):
    """The new interview-like e-mails of every mailbox, and the errors: ([(msgid, sender, subject, date, msg)],
    ["mailbox: error"]). A mailbox that cannot be read does not stop the others."""
    since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - 86400 * int(interview_cfg(cfg)["mail_days"] or 14)))
    with database.connect() as con:
        done = {r["msgid"] for r in con.execute("SELECT msgid FROM interview_mail_seen")}
    mails, errs = [], []
    for mb in interview_mailboxes(cfg):
        try:
            mails += read_mailbox_interviews(mb, since, done, limit)
            set_mailbox_state(mb["id"], "")
        except Exception as ex:
            errs.append("%s: %s" % (mb["label"], imap_error(ex)))
            set_mailbox_state(mb["id"], imap_error(ex))
    return mails, errs


def set_mailbox_state(mid, error):
    with database.connect() as con:
        con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES(?, ?)", ("mbox:" + str(mid), json.dumps(
            {"at": now_iso(), "error": error})))


def mailbox_state(mid):
    with database.connect() as con:
        r = con.execute("SELECT v FROM meta WHERE k=?", ("mbox:" + str(mid),)).fetchone()
    try:
        return json.loads(r["v"]) if r else {}
    except ValueError:
        return {}


class MailboxBook:
    """The extra mailboxes searched for interviews (Interviews page → Mailboxes): kept in the settings, a new one is
    added only after a test login works."""

    def add(self, user, password, host="", folder=""):
        """Adds (or replaces) a mailbox; returns the message to show."""
        user = (user or "").strip()
        password = re.sub(r"\s+", "", password or "")   # app passwords are shown in groups with spaces
        if not user or "@" not in user or not password:
            return "Give the e-mail address and its (app) password."
        mb = {"id": secrets.token_hex(4), "label": user, "user": user, "password": password,
              "host": (host or "").strip() or imap_host_for(user), "port": 993, "folder": (folder or "").strip() or "INBOX"}
        try:
            imap_open(mb).logout()
        except Exception as ex:
            return "Could not log in to %s: %s. Nothing was saved." % (user, imap_error(ex))
        with config_store.lock:
            cfg = config_store.load()
            iv = cfg.setdefault("interviews", {})
            iv["mailboxes"] = [m for m in iv.get("mailboxes") or [] if m.get("user", "").lower() != user.lower()] + [mb]
            config_store.save(cfg)
        set_mailbox_state(mb["id"], "")
        threading.Thread(target=scan_interview_mails, daemon=True).start()
        return "%s added; its e-mails of the last days are being checked now." % user

    def remove(self, mid):
        with config_store.lock:
            cfg = config_store.load()
            iv = cfg.setdefault("interviews", {})
            boxes = iv.get("mailboxes") or []
            mb = next((m for m in boxes if m.get("id") == mid), None)
            if not mb:
                return "No such mailbox."
            iv["mailboxes"] = [m for m in boxes if m.get("id") != mid]
            config_store.save(cfg)
        return "%s removed." % mb["user"]

    def test(self, mid):
        with config_store.lock:
            cfg = config_store.load()
        mb = next((m for m in interview_mailboxes(cfg) if m["id"] == mid), None)
        if not mb:
            return "No such mailbox."
        try:
            imap_open(mb).logout()
            set_mailbox_state(mid, "")
            return "%s: the login works." % mb["label"]
        except Exception as ex:
            set_mailbox_state(mid, imap_error(ex))
            return "%s: %s" % (mb["label"], imap_error(ex))


mailbox_book = MailboxBook()


def mail_calendar(msg):
    """The iCalendar texts attached to an e-mail."""
    out = []
    for part in msg.walk():
        ct, fn = part.get_content_type(), (part.get_filename() or "").lower()
        if ct in ("text/calendar", "application/ics") or fn.endswith(".ics"):
            try:
                c = part.get_content()
            except Exception:
                c = part.get_payload(decode=True) or b""
            out.append(c.decode("utf-8", "replace") if isinstance(c, bytes) else c)
    return out


def interview_from_mail(cfg, sender, subject, sent, msg, companies):
    """What an e-mail says about an interview: {company, role, start, minutes, place, people, notes, uid, cancelled}
    or None. A calendar invitation gives the exact time; Claude (if set) decides whether it is an interview and reads
    the e-mails that have no invitation. Without Claude only invitations that look like an interview count."""
    text, _links = mail_body(msg)
    events = [ev for c in mail_calendar(msg) for ev in parse_ics(c)]
    ev = next((x for x in events if x["start"] or x["cancelled"]), None)
    if not ev and (IV_NOT_RX.search(subject) or not IV_TIME_RX.search(subject + " " + text[:6000])):
        return None   # no invitation and no fixed time, or clearly not an interview: not worth a Claude call
    llm = llm_for(cfg)
    found = None
    if llm:
        cal = ""
        if ev:
            cal = "Summary: %s\nStart: %s\nEnd: %s\nLocation: %s\nOrganizer: %s\nAttendees: %s\nStatus: %s\n\n%s" % (
                ev["summary"], ev["start"] and to_local(ev["start"]).isoformat(timespec="minutes"),
                ev["end"] and to_local(ev["end"]).isoformat(timespec="minutes"), ev["location"], ev["organizer"],
                ", ".join(ev["attendees"][:10]), "cancelled" if ev["cancelled"] else "confirmed", ev["description"][:2500])
        d = cvgen.interview_from_email(llm, sender, subject, sent, text, cal, local_tz_name(),
                                       datetime.now().astimezone().isoformat(timespec="minutes"), companies)
        if not d.get("is_interview"):
            return None
        start = None
        if ev and ev["start"]:
            start = to_local(ev["start"])
        elif d.get("start"):
            try:
                start = to_local(datetime.fromisoformat(d["start"]))
            except ValueError:
                start = None
        minutes = d.get("minutes") or (int((ev["end"] - ev["start"]).total_seconds() // 60) if ev and ev["start"] and ev["end"] else 60)
        found = {"company": d.get("company") or "", "role": d.get("role") or "", "start": start, "minutes": minutes or 60,
                 "place": d.get("place") or (ev and (meeting_link(ev["location"], ev["description"]) or ev["location"])) or "",
                 "people": d.get("people") or "", "notes": d.get("summary") or "", "uid": (ev or {}).get("uid", ""),
                 "cancelled": bool(d.get("cancelled") or (ev and ev["cancelled"]))}
    elif ev and IV_RX.search(" ".join((subject, ev["summary"], ev["description"][:1500]))):
        mins = int((ev["end"] - ev["start"]).total_seconds() // 60) if ev["start"] and ev["end"] else 60
        org = re.sub(r".*@", "", ev["organizer"]) if "@" in ev["organizer"] else ev["organizer"]
        found = {"company": org.split(".")[0].title() if "@" in ev["organizer"] else org, "role": "", "minutes": mins or 60,
                 "start": to_local(ev["start"]) if ev["start"] else None, "notes": ev["summary"],
                 "place": meeting_link(ev["location"], ev["description"]) or ev["location"],
                 "people": ", ".join(a for a in ev["attendees"][:6] if "@" not in a), "uid": ev["uid"],
                 "cancelled": ev["cancelled"]}
    return found


def scan_interview_mails():
    """Finds interviews in the mailbox (new e-mails only), saves them, and tells you on ntfy. Returns a message."""
    if not IV_LOCK.acquire(blocking=False):
        return "A mailbox scan is already running."
    try:
        with config_store.lock:
            cfg = config_store.load()
        if not interview_cfg(cfg)["from_mail"]:
            return "Finding interviews in the e-mails is turned off."
        mails, mail_errs = read_interview_mails(cfg)
        if mail_errs and not mails and len(mail_errs) == len(interview_mailboxes(cfg)):
            return "Cannot read the mailbox: %s" % "; ".join(mail_errs)
        with database.connect() as con:
            companies = [r["company"] for r in con.execute(
                "SELECT DISTINCT company FROM jobs WHERE company<>'' AND (status IN ('applied','saved') OR id IN "
                "(SELECT job_id FROM cvs)) LIMIT 80")]
        added, changed, errs = [], 0, list(mail_errs)
        for msgid, sender, subject, sent, msg in mails:
            try:
                found = interview_from_mail(cfg, sender, subject, sent, msg, companies)
            except Exception as ex:
                errs.append("%s: %s" % (subject[:50], ex))
                continue   # tried again next time
            with database.connect() as con:
                if found and (found["start"] or found["cancelled"]):
                    same = None
                    if found["uid"]:
                        same = con.execute("SELECT * FROM interviews WHERE uid=?", (found["uid"],)).fetchone()
                    if not same and found["start"]:
                        day = found["start"].date().isoformat()
                        same = con.execute("SELECT * FROM interviews WHERE lower(company)=lower(?) AND substr(starts,1,10)=?",
                                           (found["company"], day)).fetchone()
                    if not same and found["cancelled"] and found["company"]:
                        same = con.execute("SELECT * FROM interviews WHERE lower(company)=lower(?) AND status='planned' "
                                           "ORDER BY starts LIMIT 1", (found["company"],)).fetchone()
                    if found["cancelled"]:
                        if same and same["status"] == "planned":
                            InterviewRepository(con).update(same["id"], status="cancelled")
                            changed += 1
                            notify_interview(cfg, "Interview cancelled: %s" % (same["company"] or "?"),
                                             "%s – the e-mail “%s” cancels it." % (fmt_iv_when(same), subject[:80]), same["id"])
                    elif same:
                        new = found["start"].isoformat(timespec="minutes")
                        if new != same["starts"]:
                            InterviewRepository(con).update(same["id"], starts=new, minutes=found["minutes"],
                                             place=found["place"] or same["place"], status="planned")
                            changed += 1
                            notify_interview(cfg, "Interview moved: %s" % (same["company"] or "?"),
                                             "New time: %s" % fmt_iv_when({"starts": new}), same["id"])
                    else:
                        iid = InterviewRepository(con).add(found["company"], found["role"], found["start"], found["minutes"],
                                            found["place"], found["people"], found["notes"], source="mail",
                                            uid=found["uid"], msgid=msgid)
                        added.append(iid)
                con.execute("INSERT OR REPLACE INTO interview_mail_seen(msgid, processed_at, found) VALUES(?,?,?)",
                            (msgid, now_iso(), 1 if found else 0))
        for iid in added:
            with database.connect() as con:
                r = con.execute("SELECT * FROM interviews WHERE id=?", (iid,)).fetchone()
            notify_interview(cfg, "Interview found: %s" % (r["company"] or "?"),
                             "%s%s\nFrom your e-mail; check it in jobwatch." % (
                                 fmt_iv_when(r), (" – " + r["role"]) if r["role"] else ""), iid)
            if interview_cfg(cfg)["research"]:
                researcher.start(iid)
        if errs:
            log.warning("Interview e-mail errors: %s", "; ".join(errs))
        msg = "%d e-mail(s) checked: %d new interview(s)%s." % (len(mails), len(added),
                                                               (", %d changed" % changed) if changed else "")
        return msg + (" Errors: %s" % "; ".join(errs)[:300] if errs else "")
    finally:
        IV_LOCK.release()
