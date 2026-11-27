"""The Log page: the activity log, filtered; and the history of one posting (shown in its detail view)."""
import json
import urllib.parse
from datetime import datetime

from jobwatch.db import database
from jobwatch.repositories.events import KINDS, EventRepository
from jobwatch.util.text import e
from jobwatch.web.components import ic, page

PAGE = 150
DAYS = (("1", "Last 24 hours"), ("7", "Last 7 days"), ("30", "Last 30 days"), ("90", "Everything kept (90 days)"))
LEVEL_BADGE = {"warn": "badge-warning", "error": "badge-error"}


def filters_of(q):
    """The query string's filters: kind, level, text, days, job."""
    days = q.get("days") if q.get("days") in {d for d, _ in DAYS} else "7"
    return {"kind": q.get("kind") if q.get("kind") in KINDS else "", "level": q.get("level") if q.get("level") in ("warn", "error", "info") else "",
            "text": (q.get("q") or "").strip()[:100], "days": int(days), "job_id": (q.get("job") or "").strip() or None}


def query_events(q, limit=PAGE, before=None):
    f = filters_of(q)
    with database.connect() as con:
        return EventRepository(con).query(kind=f["kind"] or None, level=f["level"] or None, text=f["text"] or None,
                                          job_id=f["job_id"], days=f["days"], before=before, limit=limit)


def stamp(ts):
    """A UTC time from the log as local date and time with seconds."""
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ts or ""


def detail_html(ev):
    """The event's details, as readable text."""
    if not ev.get("detail"):
        return ""
    try:
        data = json.loads(ev["detail"])
    except ValueError:
        return "<pre>%s</pre>" % e(ev["detail"])
    return "<pre>%s</pre>" % e(json.dumps(data, ensure_ascii=False, indent=1))


def event_row(ev):
    badge = LEVEL_BADGE.get(ev["level"], "badge-ghost")
    job = ('<a href="/job/%s" class="lg-job" title="Open the posting">%s</a> ' % (e(urllib.parse.quote(ev["job_id"], safe="")), ic("open_in_new"))) \
        if ev.get("job_id") and ":" in ev["job_id"] else ""
    det = detail_html(ev)
    return ('<tr class="lg-%s"><td class="when">%s</td><td><span class="badge badge-soft %s badge-sm">%s</span></td><td>%s%s%s</td></tr>' % (
        e(ev["level"]), e(stamp(ev["ts"])), badge, e(ev["kind"]), job,
        e(ev["summary"]), ('<details class="lg-det"><summary>details</summary>%s</details>' % det) if det else ""))


def log_page(q, flash=""):
    f = filters_of(q)
    before = int(q["before"]) if (q.get("before") or "").isdigit() else None
    rows = query_events(q, PAGE + 1, before)
    more = len(rows) > PAGE
    rows = rows[:PAGE]
    with database.connect() as con:
        counts = EventRepository(con).counts(f["days"])
    link = lambda **c: "/log?" + "&".join("%s=%s" % (k, v) for k, v in dict(
        {"kind": f["kind"], "level": f["level"], "q": f["text"], "days": f["days"], "job": f["job_id"] or ""}, **c).items() if v not in ("", None))
    kind_opts = '<option value="">All kinds</option>' + "".join(
        '<option value="%s"%s>%s%s</option>' % (k, " selected" if k == f["kind"] else "", k, (" (%d)" % counts[k]) if k in counts else "")
        for k in KINDS)
    level_opts = "".join('<option value="%s"%s>%s</option>' % (v, " selected" if v == f["level"] else "", t) for v, t in (
        ("", "Any level"), ("warn", "Warnings and errors"), ("info", "Information only")))
    days_opts = "".join('<option value="%s"%s>%s</option>' % (d, " selected" if int(d) == f["days"] else "", t) for d, t in DAYS)
    chips = "".join('<a class="fchip%s" href="%s">%s<small>%d</small></a>' % (" on" if k == f["kind"] else "", e(link(kind="" if k == f["kind"] else k)), e(k), n)
                    for k, n in counts.items())
    explain = "".join("<li><b>%s</b>: %s</li>" % (e(k), e(v)) for k, v in KINDS.items())
    body = ('<div class="ui"><div class="ui-head"><h1>Log</h1><p>What jobwatch did, and why: every status change, every refresh, every '
            'model call, every notification. Kept for 90 days. To find out why a posting you hid came back, search for its title '
            'and read its <b>status</b>, <b>job_new</b>, <b>restored</b> and <b>possible_repeat</b> lines.</p></div>'
            '<section class="ui-section"><div class="ui-body"><form method="get" action="/log" class="lg-filters">'
            '<select class="select select-sm" name="kind" aria-label="Kind">%s</select>'
            '<select class="select select-sm" name="level" aria-label="Level">%s</select>'
            '<select class="select select-sm" name="days" aria-label="Period">%s</select>'
            '<input class="input input-sm" type="search" name="q" value="%s" placeholder="Search: title, company, id" aria-label="Search" style="min-width:220px">'
            '%s<button class="btn btn-sm btn-primary">Filter</button><a class="btn btn-sm btn-ghost" href="/log">Reset</a>'
            '<a class="btn btn-sm btn-outline" href="%s" title="All matching events as JSON lines">%s Export</a></form>'
            '<div class="row-chips" style="margin-top:12px">%s</div></div></section>'
            '<section class="ui-section"><div style="overflow-x:auto"><table class="table ui-table lg"><thead><tr><th>When</th><th>Kind</th>'
            '<th>What happened</th></tr></thead><tbody>%s</tbody></table></div>%s</section>'
            '<details class="ui-section lg-kinds"><summary>What the kinds mean</summary><ul>%s</ul></details></div>') % (
        kind_opts, level_opts, days_opts, e(f["text"]),
        ('<input type="hidden" name="job" value="%s">' % e(f["job_id"])) if f["job_id"] else "",
        e(link().replace("/log?", "/log/export?") if link() != "/log?" else "/log/export"), ic("download"), chips,
        "".join(event_row(r) for r in rows) or '<tr><td colspan="3" class="ui-empty">No events match.</td></tr>',
        ('<div class="ui-foot"><a class="btn btn-sm btn-ghost" href="%s">Older events</a></div>' % e(link(before=rows[-1]["id"]))) if more else "",
        explain)
    return page("Log", body, flash)


def history_html(jid, ref, limit=40):
    """The history of a posting (and of its copies: same title and company), for its detail view."""
    with database.connect() as con:
        rows = EventRepository(con).query(job_id=jid, ref=ref, limit=limit)
    if not rows:
        return ""
    items = "".join('<li><span class="when">%s</span> <span class="badge badge-soft badge-xs %s">%s</span> %s</li>' % (
        e(stamp(r["ts"])), LEVEL_BADGE.get(r["level"], "badge-ghost"), e(r["kind"]), e(r["summary"])) for r in rows)
    return ('<details class="why"><summary>History · %d</summary><ul class="hist">%s</ul>'
            '<a class="muted" style="font-size:13px" href="/log?job=%s&days=90">Open in the log</a></details>') % (
        len(rows), items, e(urllib.parse.quote(jid, safe="")))

