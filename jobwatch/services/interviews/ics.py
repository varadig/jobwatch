"""iCalendar: reading invitations and writing interviews as calendar events."""
import re
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from datetime import timedelta
from datetime import timezone

from jobwatch.services.interviews.core import interview_cfg, iv_start


def ics_unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def ics_unescape(v):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m.group(1) in "nN" else m.group(1), v or "")


def ics_time(value, params):
    """An aware datetime from DTSTART/DTEND, or None (an all-day date)."""
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        return None
    m = re.fullmatch(r"(\d{8}T\d{4,6})(Z?)", v)
    if not m:
        return None
    raw = m.group(1) + ("00" if len(m.group(1)) == 13 else "")
    dt = datetime.strptime(raw, "%Y%m%dT%H%M%S")
    if m.group(2):
        return dt.replace(tzinfo=timezone.utc)
    tz = None
    if params.get("TZID"):
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(params["TZID"].strip('"'))
        except Exception:
            tz = None   # an unknown (e.g. Windows) zone name: taken as local time
    return dt.replace(tzinfo=tz) if tz else dt.astimezone()


def parse_ics(text):
    """The events of an iCalendar text: [{uid, start, end, summary, location, description, organizer, attendees, url,
    method, cancelled}]."""
    events, cur, method = [], None, ""
    for line in ics_unfold(text):
        if ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *ps = head.split(";")
        name = name.upper()
        params = dict(p.split("=", 1) for p in ps if "=" in p)
        params = {k.upper(): v for k, v in params.items()}
        if name == "METHOD":
            method = value.strip().upper()
        elif name == "BEGIN" and value.strip().upper() == "VEVENT":
            cur = {"attendees": [], "summary": "", "location": "", "description": "", "organizer": "", "url": "", "uid": "",
                   "start": None, "end": None, "status": ""}
        elif name == "END" and value.strip().upper() == "VEVENT" and cur is not None:
            cur["method"] = method
            cur["cancelled"] = method == "CANCEL" or cur["status"].upper() == "CANCELLED"
            events.append(cur)
            cur = None
        elif cur is not None:
            if name in ("DTSTART", "DTEND"):
                cur["start" if name == "DTSTART" else "end"] = ics_time(value, params)
            elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "URL", "STATUS"):
                cur[name.lower()] = ics_unescape(value).strip()
            elif name == "ORGANIZER":
                cur["organizer"] = (params.get("CN") or value.replace("mailto:", "")).strip('"')
            elif name == "ATTENDEE":
                cur["attendees"].append((params.get("CN") or value.replace("mailto:", "")).strip('"'))
    return events


def meeting_link(*texts):
    for t in texts:
        m = re.search(r"https?://[^\s<>\"')]*(?:zoom\.us|teams\.microsoft|teams\.live|meet\.google|webex|whereby|"
                      r"around\.co|gather\.town|discord\.gg|skype)[^\s<>\"')]*", t or "", re.I)
        if m:
            return m.group(0).rstrip(".,;")
    return ""


def ics_escape(v):
    return (v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\r", "").replace("\n", "\\n")


def ics_fold(line):
    out = []
    while len(line) > 74:
        out.append(line[:74])
        line = " " + line[74:]
    return "\r\n".join(out + [line])


def ics_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def interviews_ics(rows, cfg):
    """An iCalendar text of the interviews (planned ones; cancelled ones as cancelled)."""
    base = (cfg.get("public_url") or "").rstrip("/")
    before = interview_cfg(cfg)["before_min"]
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//jobwatch//interviews//EN", "CALSCALE:GREGORIAN",
             "X-WR-CALNAME:jobwatch interviews"]
    for r in rows:
        st = iv_start(r)
        if not st:
            continue
        end = st + timedelta(minutes=r["minutes"] or 60)
        desc = "\n".join(x for x in (r["role"] and "Role: " + r["role"], r["people"] and "With: " + r["people"],
                                     r["notes"], base and "%s/interviews/%d" % (base, r["id"])) if x)
        lines += ["BEGIN:VEVENT", "UID:jobwatch-interview-%d@jobwatch" % r["id"], "DTSTAMP:" + ics_utc(datetime.now(timezone.utc)),
                  "DTSTART:" + ics_utc(st), "DTEND:" + ics_utc(end),
                  "SUMMARY:" + ics_escape("Interview: %s%s" % (r["company"] or "?", (" – " + r["role"]) if r["role"] else "")),
                  "STATUS:" + ("CANCELLED" if r["status"] == "cancelled" else "CONFIRMED")]
        if r["place"]:
            lines.append("LOCATION:" + ics_escape(r["place"]))
        if desc:
            lines.append("DESCRIPTION:" + ics_escape(desc))
        if base:
            lines.append("URL:%s/interviews/%d" % (base, r["id"]))
        if before and r["status"] == "planned":
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:" + ics_escape("Interview: %s" % (r["company"] or "")),
                      "TRIGGER:-PT%dM" % int(before), "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(x) for x in lines) + "\r\n"


def ics_filename(r):
    """The download name of an interview's calendar file, in ASCII: "Interview_Kodesage_2026-12-01_10-40.ics"."""
    company = unicodedata.normalize("NFKD", r.get("company") or "").encode("ascii", "ignore").decode("ascii")
    parts = ["Interview"] + re.findall(r"[A-Za-z0-9]+", company)[:6]
    st = iv_start(r)
    if st:
        parts.append(st.strftime("%Y-%m-%d_%H-%M"))
    return "_".join(parts) + ".ics"


def google_calendar_link(r):
    st = iv_start(r)
    if not st:
        return ""
    end = st + timedelta(minutes=r["minutes"] or 60)
    return "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode({
        "action": "TEMPLATE", "text": "Interview: %s%s" % (r["company"] or "?", (" – " + r["role"]) if r["role"] else ""),
        "dates": "%s/%s" % (ics_utc(st), ics_utc(end)), "location": r["place"] or "",
        "details": "\n".join(x for x in (r["people"] and "With: " + r["people"], r["notes"]) if x)})
