"""The building blocks of the pages: icons, the layout, form fields, sections."""
import hashlib
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from jobwatch.config import config_store
from jobwatch.domain.workmode import mode_why, unknown_reason
from jobwatch.env import STATIC_DIR
from jobwatch.models import FETCHED_SOURCES, SOURCES_INFO, WORK_MODES
from jobwatch.services.state import STATE
from jobwatch.util.text import e, norm

REQ = threading.local()  # current request path (active menu item)


WORK_ICONS = {"remote": "home", "hybrid": "home_work", "onsite": "apartment", "unknown": "help"}


# Material Symbols Outlined (Google, Apache License 2.0). static/material-symbols.woff2 holds only these icons, each at
# its own code point: after adding one here, rebuild it with tools/make_symbols.py.
ICONS = {
    "location_on": 0xe0c8, "home": 0xe88a, "home_work": 0xea09, "apartment": 0xea40, "help": 0xe887,
    "edit": 0xe150, "auto_awesome": 0xe65f, "autorenew": 0xe028, "history": 0xe28e, "hourglass_top": 0xea5b,
    "work": 0xe8f9, "notifications": 0xe7f4, "refresh": 0xe5d5, "delete": 0xe872, "check_circle": 0xe86c,
    "error": 0xe000, "event_busy": 0xe615, "fiber_new": 0xe05e, "move_to_inbox": 0xe168, "star": 0xe838,
    "visibility": 0xe417, "visibility_off": 0xe8f5, "calendar_today": 0xe935, "target": 0xe719, "repeat": 0xe040,
    "open_in_new": 0xe895, "close": 0xe14c, "arrow_back": 0xe5c4, "subdirectory_arrow_right": 0xe5da,
    "check": 0xe5ca, "circle": 0xef4a, "expand_more": 0xe5cf, "tune": 0xe429,
    "event": 0xe24f, "calendar_month": 0xebcc, "videocam": 0xe04b, "travel_explore": 0xe2db, "download": 0xe171,
    "mail": 0xe0be, "person": 0xe7fd, "add": 0xe145, "task_alt": 0xe2e6, "undo": 0xe166,
}


def font_version():
    try:
        with open(os.path.join(STATIC_DIR, "material-symbols.woff2"), "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()[:10]
    except OSError:
        return "0"


def ic(name, fill=False):
    """An icon (decorative: the text next to it, or the button's label, says what it means)."""
    return '<span class="ms%s" aria-hidden="true">&#x%x;</span>' % (" fill" if fill else "", ICONS[name])


def wm_html(k):
    """A work mode with its icon."""
    return ic(WORK_ICONS.get(k, "help")) + " " + e(WORK_MODES.get(k, k))


def logo_html(company, files, size=40):
    """The company's logo, or its initials on a colour picked from the name when there is no logo."""
    key = norm(company)
    if key in files:
        return '<img class="logo" src="/logo/%s" alt="" width="%d" height="%d" loading="lazy">' % (e(key), size, size)
    words = [w for w in re.split(r"[\s\-_.,&/]+", company or "") if w and w[0].isalnum()]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    hue = int(hashlib.md5(key.encode()).hexdigest()[:4], 16) % 360 if key else 0
    return ('<span class="logo ph" style="width:%dpx;height:%dpx;background:hsl(%d 45%% 42%%)" aria-hidden="true">%s</span>'
            % (size, size, hue, e(initials)))


STATUS_BUTTONS = (("saved", "star", "Save"), ("applied", "check_circle", "Applied"), ("seen", "visibility", "Mark seen"),
                  ("hidden", "visibility_off", "Hide"), ("new", "history", "Mark new again"))


def mode_picker(r):
    """The work-mode chip on a card: shows where the value comes from, and opens a menu to set it by hand."""
    wm = r.get("work_mode") or "unknown"
    src = r.get("mode_src") or ""
    mark = {"manual": " " + ic("edit"), "claude": " " + ic("auto_awesome")}.get(src, "")
    qid = e(urllib.parse.quote(r["id"], safe=""))
    opts = "".join('<button class="btn btn-xs btn-ghost%s" name="work_mode" value="%s">%s</button>' % (
        " btn-active" if k == wm and src == "manual" else "", k, wm_html(k)) for k in ("remote", "hybrid", "onsite"))
    why_short = (' <span class="wm-why">· %s</span>' % e(unknown_reason(r)[0])) if wm == "unknown" and src != "manual" else ""
    return ('<details class="wm-pick"><summary class="chip wm-%s" title="%s">%s%s%s ▾</summary>'
            '<form class="wm-menu" method="post" action="/job/%s"><div class="muted">%s</div>%s'
            '<button class="btn btn-xs btn-ghost" name="work_mode" value="auto" title="Let jobwatch decide again">%s Automatic</button>'
            '</form></details>') % (e(wm), e(mode_why(r)), wm_html(wm), mark, why_short, qid, e(mode_why(r)), opts,
                                    ic("autorenew"))


def mode_quick(r):
    """On a card whose work mode is still unknown: one-click buttons to set it by hand (remembered)."""
    if (r.get("work_mode") or "unknown") != "unknown" or r.get("mode_src") == "manual":
        return ""
    return ('<form class="wm-quick" method="post" action="/job/%s"><span>Set:</span>%s</form>' % (
        e(urllib.parse.quote(r["id"], safe="")), "".join(
            '<button class="btn btn-xs btn-ghost" name="work_mode" value="%s" title="Set by hand: %s">%s</button>' % (
                k, e(WORK_MODES[k]), wm_html(k)) for k in ("remote", "hybrid", "onsite"))))


def md_inline(s):
    s = e(s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener noreferrer">\1</a>', s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    return s


def md_html(text):
    """A small, safe renderer for the research briefing: headings, bullet lists, paragraphs, bold and links."""
    out, in_list = [], False
    for line in (text or "").splitlines():
        t = line.strip()
        bullet = re.match(r"(?:[-*•]|\d+[.)])\s+(.*)", t)
        if bullet:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append("<li>%s</li>" % md_inline(bullet.group(1)))
            continue
        if in_list:
            out.append("</ul>")
            in_list = False
        if not t:
            continue
        if t.startswith("#"):
            out.append("<h3>%s</h3>" % md_inline(t.lstrip("#").strip()))
        else:
            out.append("<p>%s</p>" % md_inline(t))
    if in_list:
        out.append("</ul>")
    return "".join(out)







# Custom class -> daisyUI component. The markup uses the old class names; this mapping
# gives the daisyUI styles. Classes not in the list keep their own (CSS) style.
DAISY_CLASS = {
    "card": "card bg-base-100 shadow-sm",
    "card muted": "card bg-base-100 shadow-sm muted",
    "card docs-co": "card bg-base-100 shadow-sm docs-co",
    "btn": "btn btn-sm",
    "btn sm": "btn btn-sm",
    "btn primary": "btn btn-primary btn-sm",
    "btn primary sm": "btn btn-primary btn-sm",
    "primary": "btn btn-primary btn-sm",
    "primary sm": "btn btn-primary btn-sm",
    "sm": "btn btn-sm",
    "badge": "badge badge-soft badge-primary",
    "stat": "stat",
    "stats": "stats shadow-sm",
    "flash": "alert alert-info",
}


def daisify(html):
    html = re.sub(r'class="([^"]*)"', lambda m: 'class="%s"' % DAISY_CLASS.get(m.group(1), m.group(1)), html)
    html = re.sub(r'<button(?![^>]*\bclass=)', '<button class="btn btn-sm"', html)
    html = re.sub(r'<select(?![^>]*\bclass=)', '<select class="select select-sm"', html)
    html = re.sub(r'<textarea(?![^>]*\bclass=)', '<textarea class="textarea"', html)
    html = re.sub(r'<input(?![^>]*\bclass=)(?![^>]*type="(?:checkbox|hidden|radio)")', '<input class="input input-sm"', html)
    html = re.sub(r'<input(?![^>]*\bclass=)(?=[^>]*type="checkbox")', '<input class="checkbox checkbox-sm"', html)
    return html


# Reloads the page every N seconds while something runs, but not while the user is busy with it:
# a section they opened, a field they are typing in, or an open dialog postpones the reload.
SOFT_REFRESH = """<script>(function () {
  var touched = false;
  document.addEventListener('click', function (ev) { if (ev.target.closest && ev.target.closest('summary')) touched = true; }, true);
  function busy() {
    var a = document.activeElement;
    return (touched && document.querySelector('details[open]')) || document.querySelector('dialog[open]') ||
      (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName));
  }
  setTimeout(function tick() { if (busy()) { setTimeout(tick, 3000); } else { location.reload(); } }, %d * 1000);
})();</script>"""


def refresh_menu():
    """Header: "Refresh all" fetches every source; hovering it (or tabbing into it) shows the switched-on sources, to
    refresh only the ticked ones. One form: "Refresh all" sends no "selected", so the ticks do not matter for it."""
    try:
        with config_store.lock:
            on = config_store.load().get("sources") or {}
    except Exception:
        on = {}
    boxes = "".join('<label><input type="checkbox" class="checkbox checkbox-xs checkbox-primary" name="run_%s"> %s</label>' % (
        k, e(SOURCES_INFO[k][0])) for k in FETCHED_SOURCES if on.get(k))
    menu = ('<div class="rf-menu" role="group" aria-label="Refresh only some sources"><div class="rf-title">Refresh only these</div>'
            '<div class="rf-list">%s</div><button class="btn btn-xs btn-primary" name="selected" value="1">Refresh selected</button>'
            '<a class="rf-more" href="/sources#refresh">Own sources and status…</a></div>' % boxes) if boxes else ""
    return ('<form method="post" action="/run" class="rf-wrap"><button class="btn btn-primary btn-sm" title="Fetch all sources now">'
            '%s Refresh all</button>%s</form>') % (ic("refresh"), menu)


_ASSET_VERSIONS = {}


def asset_url(path):
    """The address of a static file with its checksum (?v=…), so browsers keep it but a changed one loads at once."""
    if path not in _ASSET_VERSIONS:
        try:
            with open(os.path.join(STATIC_DIR, path), "rb") as fh:
                digest = hashlib.md5(fh.read()).hexdigest()[:10]
            if path.endswith(".css"):   # the stylesheet names the icon font: its version follows the font's too
                digest += font_version()[:4]
        except OSError:
            digest = "0"
        _ASSET_VERSIONS[path] = digest
    return "/static/%s?v=%s" % (path, _ASSET_VERSIONS[path])


def script(name):
    """A page script from static/js."""
    return '<script src="%s"></script>' % asset_url("js/%s.js" % name)


def page(title, body, flash="", refresh=0):
    cur = getattr(REQ, "path", "/")
    tabs = [("/", "Jobs"), ("/docs", "Documents"), ("/outreach", "Recruiters"), ("/interviews", "Interviews"),
            ("/sources", "Sources"),
            ("/profile", "CV profile"), ("/settings", "Settings")]
    nav = "".join('<a role="tab" class="tab%s" href="%s">%s</a>' % (
        " tab-active" if (cur == h or (h == "/" and cur.startswith("/cv/"))) else "", h, l) for h, l in tabs)
    running = '<span class="badge badge-soft badge-warning">' + ic("hourglass_top") + ' refresh running…</span>' if STATE["running"] else ""
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">%s
<script>document.documentElement.dataset.theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";</script>
%s
<title>%s · jobwatch</title>
<link rel="icon" href="/favicon.ico" sizes="any"><link rel="icon" type="image/svg+xml" href="/favicon.svg"><link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#5b5bd6">
<link href="https://cdn.jsdelivr.net/npm/daisyui@5.0.0" rel="stylesheet" type="text/css"><link rel="stylesheet" href="%s"></head><body><header><div class="bar"><span class="brand">%s jobwatch</span><nav class="tabs tabs-box tabs-sm">%s</nav>
<div class="right">%s%s</div></div></header>
<main>%s%s</main>%s</body></html>"""
            % ((SOFT_REFRESH % refresh) if refresh else "", script("app"), e(title), asset_url("css/app.css"), ic("work"), nav, running, refresh_menu(),
               '<div class="alert alert-info flash">%s</div>' % e(flash) if flash else "", daisify(body), CLEAR_DIALOG))


CLEAR_DIALOG = """<dialog id="clear_dlg" class="modal" aria-labelledby="clear_h"><form method="post" action="/reset-jobs" class="modal-box">
<h3 id="clear_h" class="text-lg font-bold">Clear the job cache?</h3>
<p class="py-2">All the postings found so far are deleted, with the source status and the list of alert e-mails already read.</p>
<p class="text-sm opacity-80">Kept: your settings, CV profile, documents and recruiter messages; the postings you made a CV for stay in
the list, marked applied.</p>
<label class="label mt-3" style="gap:8px;cursor:pointer"><input type="checkbox" class="checkbox checkbox-sm" name="refetch" value="1" checked>
Search again right away</label>
<div class="modal-action"><button type="button" class="btn btn-ghost" onclick="this.closest('dialog').close()" autofocus>Cancel</button>
<button class="btn btn-error" name="reset" value="1">Clear cache</button></div></form>
<form method="dialog" class="modal-backdrop"><button aria-label="Close" tabindex="-1"></button></form></dialog>"""


def with_msg(path, msg):
    """path (a local URL) with ?msg= set to msg."""
    u = urllib.parse.urlparse(path if path.startswith("/") and not path.startswith("//") else "/")
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k != "msg"] + [("msg", msg)]
    return u.path + "?" + urllib.parse.urlencode(q)


def progress_html():
    """Live status bar: per-source progress of a refresh and the background description fetch."""
    prog = STATE.get("progress")
    enr = STATE.get("enrich")
    if not prog and not enr:
        return ""
    parts = []
    if prog:
        pct = int(100 * prog["done"] / prog["total"]) if prog["total"] else 100
        icons = {"fetching": ic("hourglass_top"), "done": ic("check_circle"), "error": ic("error")}
        rows = "".join('<span class="chip">%s %s: %s</span>' % (
            icons.get(str(s).split(" ")[0], "•"), e(n), e(s)) for n, s in prog["sources"].items())
        parts.append('<div class="card"><b>Refreshing sources… %d of %d done</b>'
                     '<progress class="progress progress-primary w-full mt-2" value="%d" max="100"></progress>'
                     '<div class="chips" style="margin-top:8px">%s</div></div>' % (prog["done"], prog["total"], pct, rows))
    if enr and enr.get("total"):
        parts.append('<div class="card muted">LinkedIn descriptions: %d of %d fetched in the background '
                     '(work mode and score update as they arrive).</div>' % (enr.get("done", 0), enr["total"]))
    return "".join(parts)


def ui_field(name, label, value="", kind="text", help_="", extra="", wide=False):
    """A labelled input for the ui-grid."""
    return '<div%s><label for="%s">%s</label><input class="input" id="%s" name="%s" type="%s" value="%s"%s>%s</div>' % (
        ' class="wide"' if wide else "", name, label, name, name, kind, e(value), extra,
        ('<span class="help">%s</span>' % help_) if help_ else "")


def ui_toggle(name, label, on, help_=""):
    return ('<div class="wide"><label class="ui-toggle"><input type="checkbox" class="toggle toggle-primary" name="%s"%s> %s</label>%s</div>'
            % (name, " checked" if on else "", label, ('<span class="help">%s</span>' % help_) if help_ else ""))


def ui_sec(anchor, title, desc, inner, foot=""):
    return ('<section class="ui-section" id="%s" aria-labelledby="%s-h"><header><div><h2 id="%s-h">%s</h2>%s</div></header>'
            '<div class="ui-body">%s</div>%s</section>') % (
        anchor, anchor, anchor, title, ('<p class="ui-desc">%s</p>' % desc) if desc else "", inner,
        ('<div class="ui-foot">%s</div>' % foot) if foot else "")
