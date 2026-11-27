"""The Interviews page and an interview's view."""
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.llm import llm_for
from jobwatch.models import INTERVIEW_STATUS
from jobwatch.services.interviews.core import calendar_token, fmt_iv_when, interview_cfg, iv_start, rel_when
from jobwatch.services.interviews.ics import google_calendar_link
from jobwatch.services.interviews.mailboxes import APP_PASSWORD_HELP, interview_mailboxes, mailbox_state
from jobwatch.services.logos import logo_files
from jobwatch.util.text import e
from jobwatch.util.timeutil import fmt_dt
from jobwatch.web.components import ic, logo_html, md_html, page, script, ui_field, ui_sec


def mailboxes_html(cfg):
    rows = []
    for mb in interview_mailboxes(cfg):
        st = mailbox_state(mb["id"])
        status = ('<span class="badge badge-soft badge-error">%s</span>' % e(st["error"][:120]) if st.get("error") else
                  '<span class="badge badge-soft badge-success">checked %s</span>' % e(fmt_dt(st["at"])) if st.get("at") else
                  '<span class="badge badge-soft badge-neutral">not checked yet</span>')
        btns = ('<button class="btn btn-xs btn-ghost" name="act" value="test">Test</button>'
                + ('' if mb["id"] == "main" else '<button class="btn btn-xs btn-ghost text-error" name="act" value="remove" '
                   'aria-label="Remove %s">Remove</button>' % e(mb["label"])))
        rows.append('<form method="post" action="/interviews/mailbox" class="mb-row"><input type="hidden" name="mb_id" value="%s">'
                    '<div class="mb-main">%s <b>%s</b><span class="muted"> · %s%s</span></div><div class="mb-st">%s</div>'
                    '<div class="ui-btns">%s</div></form>' % (
                        e(mb["id"]), ic("mail"), e(mb["label"]), e(mb["host"]),
                        " · the main mailbox (Settings)" if mb["id"] == "main" else "", status, btns))
    helps = "".join('<li>%s <a href="%s" target="_blank" rel="noopener noreferrer">Open</a></li>' % (e(t), e(u))
                    for t, u in APP_PASSWORD_HELP.values())
    return ((''.join(rows) if rows else '<p class="muted">No mailbox yet.</p>')
            + ('<details class="mb-add"%s><summary>%s Add a mailbox</summary>' % (" open" if not rows else "", ic("add")))
            + '<form method="post" action="/interviews/mailbox" class="ui-grid" autocomplete="off" style="margin-top:10px">'
              '<input type="hidden" name="act" value="add">'
              + ui_field("mb_user", "E-mail address", "", "email", "The server is found from the address (iCloud, Gmail, "
                         "Outlook, Yahoo…).", ' required autocomplete="off"')
              + '<div><label for="mb_password">App password</label><input class="input" id="mb_password" type="password" '
                'name="mb_password" required autocomplete="new-password"><span class="help">Not your normal password.</span></div>'
              + ui_field("mb_host", "IMAP server (optional)", "", help_="Only if it is not found from the address.")
              + ui_field("mb_folder", "Folder (optional)", "", help_="Default: INBOX.")
              + ('<div class="wide"><ul class="mb-help">%s</ul><button class="btn btn-sm btn-primary">Test and add</button>'
                 '<span class="help">The password is kept in jobwatch\'s settings file on this server, like the other '
                 'passwords there. The mailbox is only read: nothing is changed, moved or marked as read.</span></div>'
                 '</form></details>' % helps))


def iv_form(r=None, job=None, action="/interviews"):
    """The add / edit form of an interview."""
    r = r or {}
    st = iv_start(r) if r.get("starts") else None
    company = r.get("company") or (job and job["company"]) or ""
    role = r.get("role") or (job and job["title"]) or ""
    return ('<form method="post" action="' + e(action) + '" class="ui-grid">'
            + (('<input type="hidden" name="job_id" value="%s">' % e(job["id"])) if job else "")
            + ui_field("iv_company", "Company", company, extra=" required maxlength=\"120\"")
            + ui_field("iv_role", "Role", role, extra=" maxlength=\"200\"")
            + ui_field("iv_date", "Date", st.strftime("%Y-%m-%d") if st else "", "date", extra=" required")
            + ui_field("iv_time", "Time", st.strftime("%H:%M") if st else "", "time", "Your local time.", " required")
            + ui_field("iv_minutes", "Length (minutes)", r.get("minutes") or 60, "number", extra=' min="5" max="600"')
            + ui_field("iv_place", "Where", r.get("place") or "", help_="Meeting link (Zoom, Teams, Meet…), address or phone.")
            + ui_field("iv_people", "With", r.get("people") or "", help_="Interviewers' names and titles, if you know them.",
                       wide=True)
            + '<div class="wide"><label for="iv_notes">Notes</label><textarea class="textarea" id="iv_notes" name="iv_notes" '
              'rows="3">%s</textarea></div>' % e(r.get("notes") or "")
            + '<div class="wide ui-inline"><button class="btn btn-primary btn-sm" name="act" value="save">%s</button></div></form>'
            % ("Save changes" if r.get("id") else "Add interview"))


def iv_row(r, logos, now):
    return _iv_row(r, logos, now).replace('<div class="ui-row iv', '<div data-href="/interviews/%d" class="ui-row iv' % r["id"], 1)


def _iv_row(r, logos, now):
    st = iv_start(r)
    end = st + timedelta(minutes=r["minutes"] or 60) if st else None
    badges = []
    if r["source"] == "mail":
        badges.append('<span class="badge badge-soft badge-info">%s from e-mail</span>' % ic("mail"))
    if r["status"] != "planned":
        badges.append('<span class="badge badge-soft badge-neutral">%s</span>' % e(INTERVIEW_STATUS.get(r["status"], r["status"])))
    rs = r["research_status"]
    if rs == "done":
        badges.append('<span class="badge badge-soft badge-success">%s research ready</span>' % ic("travel_explore"))
    elif rs == "running":
        badges.append('<span class="badge badge-soft badge-warning">%s researching…</span>' % ic("hourglass_top"))
    join = ('<a class="btn btn-sm btn-primary" href="%s" target="_blank" rel="noopener noreferrer">%s Join</a>' % (
        e(r["place"]), ic("videocam"))) if (r["place"] or "").startswith("http") and r["status"] == "planned" else ""
    gcal = google_calendar_link(r)
    return ('<div class="ui-row iv%s"><div class="iv-when"><b>%s</b><span>%s</span><small>%s</small></div>'
            '<div class="ui-row-main"><div class="ui-row-title">%s<a href="/interviews/%d">%s</a>%s</div>'
            '<div class="muted iv-sub">%s%s</div><div class="chips" style="margin-top:6px">%s</div></div>'
            '<div class="ui-btns">%s<a class="btn btn-sm btn-outline" href="/interviews/%d">Open</a>%s'
            '<a class="btn btn-sm btn-ghost" href="/interviews/%d.ics" title="Download for your calendar">%s .ics</a></div></div>') % (
        " past" if (st and st < now) or r["status"] != "planned" else "",
        e(st.strftime("%a %d %b")) if st else "?", e("%s–%s" % (st.strftime("%H:%M"), end.strftime("%H:%M"))) if st else "",
        e(rel_when(st, now)) if st and r["status"] == "planned" else "",
        logo_html(r["company"], logos, 28), r["id"], e(r["company"] or "?"),
        (' <span class="muted" style="font-weight:400">· %s</span>' % e(r["role"])) if r["role"] else "",
        ("%s %s" % (ic("location_on"), e(r["place"][:80]))) if r["place"] else "",
        (" · %s %s" % (ic("person"), e(r["people"][:120]))) if r["people"] else "",
        "".join(badges), join, r["id"],
        ('<a class="btn btn-sm btn-ghost" href="%s" target="_blank" rel="noopener noreferrer" title="Add to Google Calendar">'
         '%s Google</a>' % (e(gcal), ic("calendar_month"))) if gcal else "", r["id"], ic("download"))


def interviews_page(q, flash=""):
    with config_store.lock:
        cfg = config_store.load()
    ivc = interview_cfg(cfg)
    now = datetime.now().astimezone()
    with database.connect() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM interviews ORDER BY starts")]
        job = con.execute("SELECT * FROM jobs WHERE id=?", (q.get("job"),)).fetchone() if q.get("job") else None
    logos = logo_files()
    soon = [r for r in rows if r["status"] == "planned" and (iv_start(r) or now) >= now - timedelta(hours=2)]
    past = [r for r in reversed(rows) if r not in soon]
    llm = llm_for(cfg)
    key = llm.can_search_web   # the research needs web search: only Claude has it
    ai = bool(llm)
    ntfy_on = cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic")
    mailbox = bool(interview_mailboxes(cfg))
    feed = "%s/calendar/%s.ics" % ((cfg.get("public_url") or "").rstrip("/"), calendar_token())
    head = ('<div class="ui-head"><h1>Interviews</h1><p>Your interviews, with a reminder on your phone on the day (from %s) and '
            '%d minutes before.%s</p></div>') % (
        e(ivc["morning"]), int(ivc["before_min"] or 0),
        "" if ntfy_on else ' <b>Push notifications are off:</b> turn them on in <a href="/settings#notify">Settings → Notifications</a>.')
    add = ui_sec("add", "Add an interview", "From a posting: open it and click “Interview”. "
                 + ("Claude then researches the company and the interviewers on the web." if key and ivc["research"] else
                    "To have Claude research the company, open the interview and click “Research the company”." if key else ""),
                 iv_form(job=job))
    if not job and rows:
        add = add.replace('<section class="ui-section" id="add"', '<details class="ui-section iv-add" id="add"><summary>%s Add an interview</summary><section' % ic("add"), 1)
        add += "</details>"
    upcoming = ui_sec("upcoming", "Coming up", "", "".join(iv_row(r, logos, now) for r in soon) if soon else
                      '<div class="ui-empty">No interviews planned.%s</div>' % (
                          " When an invitation arrives by e-mail, it shows up here." if mailbox and ivc["from_mail"] else ""))
    upcoming = upcoming.replace('<div class="ui-body">', '<div class="ui-body flush">', 1)
    old = ""
    if past:
        old = ('<details class="ui-section iv-past"><summary>Past and cancelled · %d</summary>%s</details>' % (
            len(past), "".join(iv_row(r, logos, now) for r in past)))
    chk = lambda b: " checked" if b else ""
    settings = ui_sec("ivset", "Reminders and calendar", "",
        '<form method="post" action="/interviews/settings" class="ui-grid">'
        + ui_field("iv_morning", "Reminder on the day, at", ivc["morning"], "time", "Sent on the interview's day.")
        + ui_field("iv_before", "And before it (minutes)", ivc["before_min"], "number", "0: none.", ' min="0" max="1440"')
        + '<div><label>From the mailbox</label><label class="ui-toggle" style="font-weight:400"><input type="checkbox" '
          'class="toggle toggle-sm toggle-primary" name="iv_from_mail"%s> Find interviews in my e-mails</label><span class="help">%s'
          '</span></div>' % (chk(ivc["from_mail"]),
                             ("Every 30 minutes the e-mails of the last %d days in your mailboxes (below) whose subject looks like an interview are read "
                              "(calendar invitations directly%s). Nothing is marked as read." % (
                                  int(ivc["mail_days"] or 14), ", the others by the AI model" if ai else "; set an AI key (Settings → AI) to "
                                  "read the others too")) if mailbox else
                             'Needs a mailbox: add one below.')
        + '<div><label>Research</label><label class="ui-toggle" style="font-weight:400"><input type="checkbox" '
          'class="toggle toggle-sm toggle-primary" name="iv_research"%s> Research every new interview automatically</label>'
          '<span class="help">Claude searches the web: the company, recent news, the interviewers\' professional background, '
          'likely questions, using your Anthropic API key (roughly 10–50 cents each; DeepSeek cannot search the web). Off: it runs only when you click “Research the company” on an interview.%s</span></div>' % (
              chk(ivc["research"]), "" if key else ' Needs a <a href="/settings#ai">Anthropic API key</a>.')
        + '<div class="wide ui-inline"><button class="btn btn-sm btn-primary">Save</button>'
          '<button class="btn btn-sm btn-outline" formaction="/interviews/scan"%s>%s Check the e-mails now</button></div></form>'
          % ("" if mailbox and ivc["from_mail"] else " disabled", ic("mail"))
        + '<div class="iv-feed"><label for="iv_feed">Calendar feed</label><div class="join" style="width:100%%">'
          '<input class="input join-item ui-mono" id="iv_feed" value="%s" readonly style="flex:1">'
          '<button type="button" class="btn join-item" onclick="var i=document.getElementById(\'iv_feed\');i.select();'
          'navigator.clipboard&&navigator.clipboard.writeText(i.value);this.textContent=\'Copied\'">Copy</button></div>'
          '<span class="help">Subscribe to it in your calendar app (“add calendar from URL”) to see the interviews there. '
          'It works where the calendar can reach this address (Google Calendar needs it to be reachable from the internet; '
          'otherwise use “Google” or “.ics” on an interview). Keep it private: anyone with it can see your interviews.</span></div>'
          % e(feed))
    boxes = ui_sec("mailboxes", "Mailboxes", "Where interview invitations can arrive. Add every address you gave to "
                   "employers or job sites.", mailboxes_html(cfg))
    return page("Interviews", '<div class="ui">%s%s<div id="iv_lists">%s%s</div>%s%s</div>' % (
        head, add, upcoming, old, settings, boxes) + IV_MODAL, flash)


def interview_page(iid, flash="", modal=False):
    """An interview with its preparation. modal: only the content, for the dialog on the Interviews page."""
    with database.connect() as con:
        r = con.execute("SELECT * FROM interviews WHERE id=?", (iid,)).fetchone()
        job = con.execute("SELECT * FROM jobs WHERE id=?", (r["job_id"],)).fetchone() if r and r["job_id"] else None
    if not r and modal:
        return '<div class="ui-empty">This interview is not here any more.</div>'
    if not r:
        return page("Not found", '<div class="ui"><section class="ui-section"><div class="ui-body ui-empty">This interview '
                                 'is not here any more. <a href="/interviews">All interviews</a></div></section></div>', flash)
    r = dict(r)
    now = datetime.now().astimezone()
    with config_store.lock:
        cfg = config_store.load()
    key = llm_for(cfg).can_search_web   # the research needs web search: only Claude has it
    st = iv_start(r)
    act = lambda v, label, cls="btn-outline": '<button class="btn btn-sm %s" name="act" value="%s">%s</button>' % (cls, v, label)
    acts = "".join([
        act("done", ic("task_alt") + " Mark done") if r["status"] == "planned" else act("planned", ic("undo") + " Planned again"),
        act("cancelled", "Mark cancelled", "btn-ghost") if r["status"] == "planned" else "",
        act("research", ic("travel_explore") + (" Research again" if r["research"] else " Research the company"))
        if key and r["research_status"] != "running" else ""])
    rs = r["research_status"]
    if rs == "done" and r["research"]:
        try:
            srcs = json.loads(r["research_sources"] or "[]")
        except ValueError:
            srcs = []
        research = '<div class="iv-research">%s</div>%s<p class="muted" style="font-size:13px">Researched %s by Claude with web search: check anything important.</p>' % (
            md_html(r["research"]),
            ('<details class="why"><summary>Sources · %d</summary><ul class="iv-src">%s</ul></details>' % (len(srcs), "".join(
                '<li><a href="%s" target="_blank" rel="noopener noreferrer">%s</a></li>' % (e(u), e(t[:120])) for t, u in srcs[:40])))
            if srcs else "", e(fmt_dt(r["research_at"])))
    elif rs == "running":
        research = ('<p>%s Claude is researching the company and the interviewers… This takes a minute or two; the page '
                    'updates by itself.</p>' % ic("hourglass_top"))
    elif rs == "error":
        research = '<div class="ui-err">The research failed: %s</div>' % e(r["research_error"] or "")
    else:
        research = ('<p class="muted">Not researched yet.%s</p>' % (
            "" if key else ' Set a <a href="/settings#ai">Anthropic API key</a> to have the company researched.'))
    gcal = google_calendar_link(r)
    info = ('<div class="ui-head"><a href="/interviews" style="font-size:13px">%s Interviews</a><div class="job-head" style="margin-top:6px">%s'
            '<div><h1 id="iv-title">%s</h1><p style="margin-top:2px"><b>%s</b>%s · %d min · %s</p></div></div>'
            '<div class="chips" style="margin-top:10px">%s%s%s</div>'
            '<div class="ui-btns" style="margin-top:12px">%s%s<a class="btn btn-sm btn-ghost" href="/interviews/%d.ics">%s .ics</a>%s</div></div>') % (
        ic("arrow_back"), logo_html(r["company"], logo_files(), 52),
        e("%s%s" % (r["company"] or "?", (" – " + r["role"]) if r["role"] else "")),
        e(fmt_iv_when(r)), (" (%s)" % e(rel_when(st, now))) if st and r["status"] == "planned" else "", r["minutes"] or 60,
        e(INTERVIEW_STATUS.get(r["status"], r["status"])),
        ('<span class="chip">%s %s</span>' % (ic("location_on"), e(r["place"]))) if r["place"] else "",
        ('<span class="chip">%s %s</span>' % (ic("person"), e(r["people"]))) if r["people"] else "",
        '<span class="chip">%s %s</span>' % (ic("notifications"), e(
            "reminded" if r["reminded_soon"] else ("reminded on the day" if r["reminded_day"] else "reminders on")))
        if r["status"] == "planned" else "",
        ('<a class="btn btn-sm btn-primary" href="%s" target="_blank" rel="noopener noreferrer">%s Join</a>' % (
            e(r["place"]), ic("videocam"))) if (r["place"] or "").startswith("http") else "",
        ('<a class="btn btn-sm btn-outline" href="%s" target="_blank" rel="noopener noreferrer">%s Add to Google Calendar</a>' % (
            e(gcal), ic("calendar_month"))) if gcal else "", r["id"], ic("download"),
        ('<a class="btn btn-sm btn-ghost" href="/cv/%s">The posting and your CV</a>' % e(urllib.parse.quote(job["id"], safe="")))
        if job else "")
    body = (info
            + ui_sec("prep", "Preparation", "", research
                     + '<form method="post" action="/interviews/%d" class="ui-inline" style="margin-top:12px">%s</form>' % (r["id"], acts))
            + (ui_sec("notes", "Notes", "", '<p style="white-space:pre-wrap">%s</p>' % e(r["notes"])) if r["notes"] else "")
            + '<details class="ui-section iv-add"><summary>Edit</summary><div class="ui-body">%s'
              '<form method="post" action="/interviews/%d" style="margin-top:14px"><details><summary class="text-error" '
              'style="cursor:pointer">Delete this interview…</summary><button class="btn btn-sm btn-error" name="act" '
              'value="delete" style="margin-top:8px">Yes, delete it</button></details></form></div></details>' % (
                  iv_form(r, action="/interviews/%d" % r["id"]), r["id"]))
    if modal:   # no back link; while the research runs the dialog reloads itself (data-running)
        body = body.replace('<a href="/interviews" style="font-size:13px">%s Interviews</a>' % ic("arrow_back"), "", 1)
        return '<div class="ui iv-m"%s>%s</div>' % (' data-running="1"' if rs == "running" else "", body)
    return page("Interview: %s" % (r["company"] or "?"), '<div class="ui">%s</div>' % body, flash,
                refresh=10 if rs == "running" else 0)


# Interviews page: an interview opens in a dialog (Ctrl/Cmd or middle click: its own page). Its buttons and forms are
# sent in the background: the dialog shows the result, the lists behind it are refreshed, and a deleted one closes it.
# While Claude researches, the dialog reloads itself every few seconds.
IV_MODAL = """<dialog id="iv_dlg" class="modal" aria-labelledby="iv-title"><div class="modal-box jd-modal">
<form method="dialog"><button class="btn btn-sm btn-circle btn-ghost jd-close" aria-label="Close">""" + ic("close") + """</button></form>
<div id="iv_dlg_body"></div></div><form method="dialog" class="modal-backdrop"><button aria-label="Close" tabindex="-1"></button></form></dialog>
<div id="iv_live" class="sr-only" aria-live="polite"></div>""" + script("interview-dialog")
