"""Job alert e-mails: reading the mailbox and the postings in the e-mails."""
import hashlib
import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from jobwatch import cvgen
from jobwatch.db import database
from jobwatch.env import log
from jobwatch.llm import llm_for
from jobwatch.services.state import STATE
from jobwatch.sources.base import JobSource
from jobwatch.util.text import html_attr, strip_tags
from jobwatch.util.timeutil import now_iso, parsedate_iso

MAIL_SKIP_TEXT = re.compile(r"unsubscribe|leiratkoz|preferences|settings|privacy|help|view (it )?in (your )?browser|"
                            r"see all|show all|manage|terms|sign in|log ?in|download the app|feedback|^here$|^click", re.I)


MAIL_JOB_URL = re.compile(r"/(jobs?|careers?|positions?|vacanc\w*|openings?|roles?|apply|opportunit\w*|allas\w*)(/|\?|$|-)", re.I)


def canonical_job_url(url):
    """LinkedIn alert links carry tracking parameters; one posting should be one address."""
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})", url)
    return "https://www.linkedin.com/jobs/view/%s/" % m.group(1) if m else url


def mail_body(msg):
    """(plain text, [(link text, url)]) of an e-mail; the HTML part is preferred for the links."""
    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    links, text = [], ""
    if html_part is not None:
        h = html_part.get_content()
        for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", h, re.S | re.I):
            href = html_attr(m.group(1), "href") or ""
            if href.startswith("http"):
                links.append((strip_tags(m.group(2)) or (html_attr(m.group(1), "title") or ""), href))
        text = strip_tags(h.replace("</tr>", "\n").replace("<br", "\n<br").replace("</p>", "\n"))
    if text_part is not None and not text:
        text = text_part.get_content()
    return text, links


def jobs_from_links(links):
    """Without Claude: links that look like postings, their text as the title."""
    out, seen = [], set()
    for t, u in links:
        t = re.sub(r"\s+", " ", t).strip()
        u = canonical_job_url(html.unescape(u))
        if not (6 <= len(t) <= 160) or MAIL_SKIP_TEXT.search(t) or u in seen:
            continue
        if not (MAIL_JOB_URL.search(urllib.parse.urlparse(u).path) or "linkedin.com/jobs/view/" in u):
            continue
        seen.add(u)
        out.append({"title": t, "company": "", "location": "", "url": u})
    return out


def mail_login(cfg):
    """(IMAP host, port, user, password): the alert settings, else the e-mail (SMTP) login."""
    ms, em = cfg.get("mail_source") or {}, cfg["email"]
    user = ms.get("username") or em.get("username")
    pw = ms.get("password") or em.get("password")
    if not (user and pw):
        raise RuntimeError("no mailbox login: set the e-mail account in Settings → Notifications, or one for e-mail alerts")
    return ms.get("imap_host") or "imap.gmail.com", int(ms.get("imap_port") or 993), user, pw


def read_alert_mails(cfg, only_new=True, limit=40):
    """The alert e-mails of the last days from the listed senders, newest first: [(msgid, sender, subject, msg)].
    The mailbox is opened read-only and messages are fetched with PEEK, so nothing is marked as read."""
    import email as email_lib
    import email.policy
    import imaplib
    ms = cfg.get("mail_source") or {}
    senders = [s.strip() for s in ms.get("senders") or [] if s.strip()]
    if not senders:
        raise RuntimeError("no senders listed for e-mail alerts")
    host, port, user, pw = mail_login(cfg)
    since = time.strftime("%d-%b-%Y", time.gmtime(time.time() - 86400 * int(ms.get("days") or 7)))
    out = []
    with imaplib.IMAP4_SSL(host, port, timeout=30) as M:
        M.login(user, pw)
        typ, _ = M.select('"%s"' % (ms.get("folder") or "INBOX").replace('"', ""), readonly=True)
        if typ != "OK":
            raise RuntimeError("cannot open the mail folder %s" % ms.get("folder"))
        ids = set()
        for s in senders:
            typ, data = M.search(None, "SINCE", since, "FROM", '"%s"' % s.replace('"', ""))
            if typ == "OK" and data and data[0]:
                ids.update(data[0].split())
        with database.connect() as con:
            done = {r["msgid"] for r in con.execute("SELECT msgid FROM mail_seen")} if only_new else set()
        for num in sorted(ids, key=int, reverse=True)[:limit * 3]:
            typ, data = M.fetch(num, "(BODY.PEEK[])")
            if typ != "OK" or not data or not isinstance(data[0], tuple):
                continue
            msg = email_lib.message_from_bytes(data[0][1], policy=email.policy.default)
            msgid = (msg.get("Message-ID") or "").strip() or hashlib.sha1(data[0][1][:2000]).hexdigest()
            if msgid in done:
                continue
            out.append((msgid, str(msg.get("From", "")), str(msg.get("Subject", "")), msg))
            if len(out) >= limit:
                break
    return out


def jobs_from_mail(cfg, sender, subject, msg):
    """The postings in one alert e-mail: Claude reads it when that is turned on (and a key is set), else the links."""
    text, links = mail_body(msg)
    llm = llm_for(cfg)
    if (cfg.get("mail_source") or {}).get("use_claude", True) and llm:
        found = cvgen.jobs_from_email(llm, subject, sender, text, links)
        for j in found:
            j["url"] = canonical_job_url(html.unescape(j.get("url") or ""))
        return found
    return jobs_from_links(links)


MAIL_PER_RUN = 15   # newest first; the rest waits for the next run, so one run stays short


MAIL_WORKERS = 4    # e-mails read by Claude at the same time


class MailAlertSource(JobSource):
    """Job alert e-mails from the listed senders, read from the mailbox."""

    key = "mail"

    def fetch(self, cfg):
        from concurrent.futures import ThreadPoolExecutor
        mails = read_alert_mails(cfg, limit=MAIL_PER_RUN)
        prog = (STATE.get("progress") or {}).get("sources")
        done_n = [0]

        def one(m):
            msgid, sender, subject, msg = m
            try:
                return msgid, sender, msg, jobs_from_mail(cfg, sender, subject, msg), None
            except Exception as ex:
                return msgid, sender, msg, [], "%s: %s" % (subject[:60], ex)
            finally:
                done_n[0] += 1
                if prog is not None and "mail" in prog:
                    prog["mail"] = "fetching %d / %d e-mails" % (done_n[0], len(mails))

        out, errs, done = [], [], []
        with ThreadPoolExecutor(max_workers=MAIL_WORKERS) as pool:
            for msgid, sender, msg, jobs, err in pool.map(one, mails):
                if err:
                    errs.append(err)
                    continue  # not marked as done: tried again next time
                via = re.sub(r".*@", "", re.sub(r"[<>\s]", "", sender.rsplit(" ", 1)[-1]))
                for j in jobs:
                    url = j.get("url") or ""
                    if not url.startswith("http"):
                        continue
                    out.append({"ext_id": hashlib.sha1(url.encode()).hexdigest()[:16], "title": j["title"][:200],
                                "company": j.get("company", "")[:120], "location": j.get("location", "")[:120] or via,
                                "url": url, "posted": parsedate_iso(msg.get("Date")), "description": "",
                                "work_mode": j.get("work_mode") if j.get("work_mode") in ("remote", "hybrid", "onsite") else None})
                done.append((msgid, len(jobs)))
        cfg.setdefault("_mail_done", []).extend(done)
        if errs:
            log.warning("E-mail alert errors: %s", "; ".join(errs))
            if not out:
                raise RuntimeError("; ".join(errs))
        return out


def mark_mails_done(done):
    with database.connect() as con:
        con.executemany("INSERT OR REPLACE INTO mail_seen(msgid, processed_at, found) VALUES(?,?,?)",
                        [(m, now_iso(), n) for m, n in done])


def test_mail_source(cfg):
    """Settings → "test": reads the newest alert e-mail (even if already processed) and shows what it finds."""
    try:
        mails = read_alert_mails(cfg, only_new=False, limit=1)
    except Exception as ex:
        return "E-mail alerts: ERROR %s" % ex
    if not mails:
        return "E-mail alerts: mailbox OK, but no e-mail from the listed senders in the last %s days." % (
            (cfg.get("mail_source") or {}).get("days", 7))
    _, sender, subject, msg = mails[0]
    try:
        jobs = jobs_from_mail(cfg, sender, subject, msg)
    except Exception as ex:
        return "E-mail alerts: mailbox OK, newest e-mail “%s”, but reading it failed: %s" % (subject[:80], ex)
    sample = "; ".join("%s (%s)" % (j["title"], j.get("company") or "-") for j in jobs[:3])
    return "E-mail alerts: mailbox OK. Newest e-mail “%s”: %d posting(s)%s" % (subject[:80], len(jobs), (" – e.g. " + sample) if sample else "")
