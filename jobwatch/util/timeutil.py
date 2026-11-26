"""Time helpers: ISO strings, parsing and formatting dates, the local time zone."""
import os
import time
from datetime import datetime
from datetime import timezone
from email.utils import parsedate_to_datetime


def now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def epoch_iso(v):
    try:
        return datetime.fromtimestamp(int(float(v)), timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def rfc822_iso(v):
    try:
        return parsedate_to_datetime(v).isoformat()
    except Exception:
        return ""


def parsedate_iso(value):
    try:
        return parsedate_to_datetime(str(value)).isoformat()
    except Exception:
        return ""


def local_tz():
    return datetime.now().astimezone().tzinfo


def local_tz_name():
    return os.environ.get("TZ") or time.tzname[0] or "local time"


def to_local(dt):
    return dt.astimezone(local_tz()) if dt.tzinfo else dt.replace(tzinfo=local_tz())


def parse_local(date_s, time_s):
    """The form's date + time (the server's local time) as an aware datetime, or None."""
    try:
        return datetime.strptime("%s %s" % (date_s.strip(), (time_s or "09:00").strip()), "%Y-%m-%d %H:%M").astimezone()
    except ValueError:
        return None


def fmt_dt(s):
    if not s:
        return ""
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo:
            d = d.astimezone()
        return d.strftime("%Y-%m-%d %H:%M") if (d.hour or d.minute) else d.strftime("%Y-%m-%d")
    except Exception:
        return s[:16]


def parse_dt(s):
    """Date in mixed formats -> timezone-aware datetime (UTC if none given), or None."""
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            d = parsedate_to_datetime(s)
        except Exception:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def days_ago(d):
    """How many calendar days ago (local time)."""
    return (datetime.now().astimezone().date() - d.astimezone().date()).days


def rel_day(n):
    if n <= 0:
        return "today"
    if n == 1:
        return "yesterday"
    if n < 14:
        return "%d days ago" % n
    if n < 60:
        return "%d weeks ago" % (n // 7)
    return "%d months ago" % (n // 30)


BUCKETS = [(0, "Today"), (1, "Yesterday"), (6, "This week (2–6 days ago)"), (13, "Last week (7–13 days ago)"),
           (30, "2–4 weeks ago"), (10 ** 6, "Older than 30 days – probably stale")]


def bucket_of(n):
    for lim, name in BUCKETS:
        if n <= lim:
            return name
    return BUCKETS[-1][1]
