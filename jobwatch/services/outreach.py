"""Recruiter messages: who to write to, about which role, and writing the message."""
import html
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from jobwatch import cvgen
from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.env import log
from jobwatch.llm import llm_for
from jobwatch.net import http
from jobwatch.services.documents import load_profile
from jobwatch.services.notify import notifier
from jobwatch.sources.linkedin import linkedin_detail
from jobwatch.util.text import strip_tags
from jobwatch.util.timeutil import now_iso


def parse_recruiters(text):
    """One recruiter per line: "Name | title @ company | LinkedIn profile URL or e-mail" (only the name is required)."""
    out = []
    for line in (text or "").splitlines():
        parts = [p.strip() for p in line.split("|")]
        if not parts[0]:
            continue
        contact = next((p for p in parts[1:] if p.startswith("http") or re.match(r"^[^@\s]+@[^@\s]+\.\w+$", p)), "")
        title = next((p for p in parts[1:] if p and p != contact), "")
        out.append({"name": parts[0][:120], "title": title[:200], "contact": contact[:300]})
    return out


def outreach_generate(oid):
    """Background job: writes the message for one outreach row with the Claude API."""
    try:
        with config_store.lock:
            cfg = config_store.load()
        llm = llm_for(cfg)
        if not llm:
            raise cvgen.CVError("No AI key is set (Settings → AI).")
        with database.connect() as con:
            r = dict(con.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone())
            job = con.execute("SELECT * FROM jobs WHERE id=?", (r["job_id"],)).fetchone() if r["job_id"] else None
        if r.get("role_ref") and (not r.get("role_text") or ROLE_UNREAD in r["role_text"]):  # a rewrite reuses it
            r["role_text"] = resolve_role(r["role_ref"])
            with database.connect() as con:
                con.execute("UPDATE outreach SET role_text=? WHERE id=?", (r["role_text"], oid))
        data = cvgen.outreach_message(llm, load_profile(), r, dict(job) if job else None,
                                      r["channel"], r["language"], r["note"] or "")
        with database.connect() as con:
            con.execute("UPDATE outreach SET status='draft', error=NULL, subject=?, message=?, model=? WHERE id=?",
                        (data["subject"], data["message"], data["_model"], oid))
    except Exception as ex:
        log.exception("Outreach message %s", oid)
        with database.connect() as con:
            con.execute("UPDATE outreach SET status='error', error=? WHERE id=?", (str(ex)[:500], oid))


def outreach_signature(prof):
    c = prof.get("contact") or {}
    return " · ".join(x for x in (c.get("phone"), c.get("email"), c.get("linkedin"), c.get("portfolio")) if x)


def outreach_role_label(r):
    """Short name of the role a message is about: the recruiter's own role, else the chosen posting."""
    if r["role_ref"]:
        if ROLE_UNREAD in (r["role_text"] or ""):
            return ""
        return re.sub(r"^Title: |\n.*", "", r["role_text"] or r["role_ref"], flags=re.S)[:80]
    if r["job_title"]:
        return "%s – %s" % (r["job_title"], r["job_company"] or "?")
    return ""


def outreach_action(oid, f):
    """POST /outreach/<id>: act = save | sent | send | regen | delete. Returns the flash message."""
    act = f.get("act", "save")
    with database.connect() as con:
        r = con.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
        if not r:
            return "No such message."
        if act == "delete":
            con.execute("DELETE FROM outreach WHERE id=?", (oid,))
            return "Deleted."
        if "message" in f:
            con.execute("UPDATE outreach SET message=?, subject=? WHERE id=?",
                        (f.get("message", "").strip(), f.get("subject", r["subject"] or "").strip(), oid))
        if act == "sent":
            con.execute("UPDATE outreach SET status='sent', sent_at=? WHERE id=?", (now_iso(), oid))
            return "Marked as sent, moved to the list at the bottom."
        if act == "unsent":
            con.execute("UPDATE outreach SET status='draft', sent_at=NULL WHERE id=?", (oid,))
            return "Moved back to drafts."
        if act == "regen":
            con.execute("UPDATE outreach SET status='running', error=NULL WHERE id=?", (oid,))
    if act == "regen":
        threading.Thread(target=outreach_generate, args=(oid,), daemon=True).start()
        return "Rewriting…"
    if act == "send":
        with config_store.lock:
            cfg = config_store.load()
        with database.connect() as con:
            r = con.execute("SELECT * FROM outreach WHERE id=?", (oid,)).fetchone()
        sig = outreach_signature(load_profile())
        try:
            notifier.email(cfg, r["subject"] or "Hello", (r["message"] or "") + ("\n\n" + sig if sig else ""), to=r["contact"])
        except Exception as ex:
            return "Sending error: %s" % ex
        with database.connect() as con:
            con.execute("UPDATE outreach SET status='sent', sent_at=? WHERE id=?", (now_iso(), oid))
        return "Sent to %s." % r["contact"]
    return "Saved."


LINKEDIN_PROFILE_RE = re.compile(r"^https?://([a-z]{2,3}\.)?(www\.)?linkedin\.com/in/([^/?#]+)", re.I)


def name_from_slug(slug):
    """"anna-kovacs-1a2b3c4d" -> "Anna Kovacs" (the id-like parts with digits are dropped)."""
    parts = [p for p in urllib.parse.unquote(slug).split("-") if p and not re.search(r"\d", p)]
    return " ".join(p[:1].upper() + p[1:] for p in parts)


def linkedin_preview(url):
    """Name and headline of a LinkedIn profile from its public link preview (og:title), like a chat app shows it.
    One request, only when the user pastes the link. Falls back to the name in the address."""
    m = LINKEDIN_PROFILE_RE.match((url or "").strip())
    if not m:
        return {"ok": False, "error": "Not a LinkedIn profile link (linkedin.com/in/…)."}
    clean = "https://www.linkedin.com/in/%s/" % m.group(3)
    guess = {"ok": True, "url": clean, "name": name_from_slug(m.group(3)), "title": "", "from": "address"}
    try:
        pg = http.get(clean, timeout=15)
    except Exception as ex:
        log.info("LinkedIn preview %s: %s", clean, ex)
        return guess
    t = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', pg) or re.search(r"<title>([^<]*)</title>", pg)
    title = html.unescape(t.group(1)).strip() if t else ""
    if not title or re.search(r"sign ?up|sign ?in|log ?in|authwall|join linkedin", title, re.I):
        return guess  # login wall: the profile is not public
    title = re.sub(r"\s*\|\s*LinkedIn\s*$", "", title)
    name, _, headline = title.partition(" - ")
    d = re.search(r'<meta[^>]+(?:property="og:description"|name="description")[^>]+content="([^"]*)"', pg)
    # "headline · about text · Experience: X · Education: Y · Location: Z · 500 connections … View … profile …"
    about = re.split(r"\s·\s*\d[\d,+]*\s+(?:connections|followers)|View [^·]{0,80}profile on LinkedIn",
                     html.unescape(d.group(1)) if d else "")[0].strip(" ·")
    # a profile without a headline gets "<location> | Professional Profile" in the title: that is not a job title
    headline = re.sub(r"\s*\|\s*Professional Profile\s*$", "", headline, flags=re.I).strip()
    loc = re.search(r"Location:\s*([^·]+)", about)
    if loc and headline.lower() == loc.group(1).strip().lower() or re.fullmatch(r"[^,]+(,[^,]+){1,2}", headline) and \
            not re.search(r"\b(at|@|recruit|talent|hr|hiring|manager|partner|consultant|lead|head)\b", headline, re.I):
        headline = ""
    exp = re.search(r"Experience:\s*([^·]+)", about)
    if not headline and exp:
        headline = "@ " + exp.group(1).strip()  # at least the current company
    return {"ok": True, "url": clean, "name": name.strip() or guess["name"], "title": headline,
            "about": about[:1500], "from": "profile"}


LINKEDIN_JOB_RE = re.compile(r"linkedin\.com/.*?(?:jobs/view/(?:[^/?#]*?-)?|currentJobId=)(\d{6,})", re.I)


def linkedin_job(job_id):
    """Title, company, location and text of a LinkedIn posting from its public page."""
    pg = http.get("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/%s" % job_id, timeout=20)
    grab = lambda rx: (lambda m: strip_tags(m.group(1)) if m else "")(re.search(rx, pg, re.S))
    return {"title": grab(r'top-card-layout__title[^>]*>(.*?)</h'),
            "company": grab(r'topcard__org-name-link[^>]*>(.*?)</a>'),
            "location": grab(r'topcard__flavor topcard__flavor--bullet[^>]*>(.*?)</span>'),
            "description": linkedin_detail(job_id) or ""}


ROLE_UNREAD = "(the page could not be read)"


def resolve_role(ref, waits=(20, 60)):
    """What the recruiter is hiring for, as text for Claude: a LinkedIn job link, another job page, or plain text.
    A LinkedIn posting already in the database is used from there; on 429 (rate limit) it waits and retries."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    m = LINKEDIN_JOB_RE.search(ref)
    if m:
        with database.connect() as con:
            row = con.execute("SELECT title, company, location, description FROM jobs WHERE id=?",
                              ("linkedin:" + m.group(1),)).fetchone()
        if row and row["description"]:
            return "Title: %s\nCompany: %s\nLocation: %s\nURL: %s\n\n%s" % (
                row["title"], row["company"], row["location"] or "-", ref, row["description"][:6000])
    for wait in (0,) + tuple(waits):
        time.sleep(wait)
        try:
            return _read_role_page(ref, m)
        except urllib.error.HTTPError as ex:
            if ex.code != 429:
                log.warning("Role page %s: %s", ref, ex)
                break
            log.warning("Role page %s: 429, retrying", ref)
        except Exception as ex:
            log.warning("Role page %s: %s", ref, ex)
            break
    return "URL: %s %s" % (ref, ROLE_UNREAD)


def _read_role_page(ref, m):
    """One attempt; raises on network / HTTP errors. Plain text (no link) is returned as it is."""
    if m:
        j = linkedin_job(m.group(1))
        if not j["title"] and not j["description"]:
            raise RuntimeError("no posting on the page")
        return "Title: %s\nCompany: %s\nLocation: %s\nURL: %s\n\n%s" % (
            j["title"], j["company"], j["location"] or "-", ref, j["description"][:6000])
    if re.match(r"^https?://", ref):
        pg = http.get(ref, timeout=20)
        t = re.search(r"<title>(.*?)</title>", pg, re.S)
        return "Page title: %s\nURL: %s\n\n%s" % (strip_tags(t.group(1)) if t else "", ref, strip_tags(pg)[:6000])
    return ref[:3000]


def recruiters_from_form(f):
    """The list built on the page (recruiters_json); the old one-per-line text still works too."""
    out = []
    try:
        items = json.loads(f.get("recruiters_json") or "[]")
    except ValueError:
        items = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and str(it.get("name") or "").strip():
            out.append({"name": str(it["name"]).strip()[:120], "title": str(it.get("title") or "").strip()[:200],
                        "contact": str(it.get("contact") or "").strip()[:300],
                        "kind": it.get("kind") if it.get("kind") in ("inhouse", "agency") else "",
                        "profile": str(it.get("about") or "").strip()[:1500],
                        "role_ref": str(it.get("role") or "").strip()[:3000]})
    return out + parse_recruiters(f.get("recruiters", ""))


def outreach_create(f):
    recs = recruiters_from_form(f)
    if not recs:
        return "Give at least one recruiter name."
    channel = f.get("channel") if f.get("channel") in cvgen.OUTREACH_CHANNELS else "linkedin_message"
    if channel == "linkedin_intro" and not f.get("via", "").strip():
        return "For an introduction request, give the mutual connection's name."
    ids = []
    with database.connect() as con:
        for rc in recs[:20]:
            via = f.get("via", "").strip()[:300] if channel == "linkedin_intro" else ""
            kind = rc.get("kind") or (f.get("kind") if f.get("kind") in ("inhouse", "agency") else "")
            cur = con.execute("INSERT INTO outreach(created,name,title,contact,channel,language,job_id,note,status,via,kind,"
                              "profile,role_ref) VALUES(?,?,?,?,?,?,?,?,'running',?,?,?,?)",
                              (now_iso(), rc["name"], rc["title"], rc["contact"], channel,
                               f.get("language", "auto"), f.get("job_id") or None, f.get("note", "").strip()[:500], via, kind,
                               rc.get("profile", ""), rc.get("role_ref", "")))
            ids.append(cur.lastrowid)
    for oid in ids:
        threading.Thread(target=outreach_generate, args=(oid,), daemon=True).start()
    return "Writing %d message(s)…" % len(ids)
