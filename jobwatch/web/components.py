"""The building blocks of the pages: icons, the layout, form fields, sections."""
from jobwatch.env import STATIC_DIR
import hashlib
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request

from jobwatch.domain.workmode import mode_why, unknown_reason
from jobwatch.models import FETCHED_SOURCES, SOURCES_INFO, WORK_MODES
from jobwatch.services.state import STATE
from jobwatch.util.text import e, norm
from jobwatch.config import config_store


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


def _font_version():
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


CSS = """
@font-face{font-family:"Material Symbols";font-style:normal;font-weight:400;font-display:block;src:url(/material-symbols.woff2?v=FONTVER) format("woff2")}
.ms{font-family:"Material Symbols";font-weight:400;font-style:normal;font-size:1.2em;line-height:1;display:inline-block;
  vertical-align:-.22em;letter-spacing:normal;text-transform:none;white-space:nowrap;direction:ltr;user-select:none;
  -webkit-font-smoothing:antialiased;font-variation-settings:"FILL" 0}
.ms.fill{font-variation-settings:"FILL" 1}
.btn .ms{font-size:1.25em}
:root{--bg:var(--color-base-200);--card:var(--color-base-100);--fg:var(--color-base-content);
--muted:color-mix(in oklab,var(--color-base-content) 65%,transparent);--line:var(--color-base-300);
--soft:color-mix(in oklab,var(--color-base-content) 7%,var(--color-base-100));
--acc:var(--color-primary);--acc-soft:color-mix(in oklab,var(--color-primary) 14%,transparent);
--good:var(--color-success);--good-soft:color-mix(in oklab,var(--color-success) 16%,transparent);
--warn:var(--color-warning);--warn-soft:color-mix(in oklab,var(--color-warning) 18%,transparent);
--bad:var(--color-error);--bad-soft:color-mix(in oklab,var(--color-error) 16%,transparent);--radius:var(--radius-box,12px)}
*{box-sizing:border-box}html{background:var(--bg);min-height:100%}body{margin:0;min-height:100vh;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,Segoe UI,Roboto,Inter,sans-serif}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
a.btn-primary,a.btn-primary:hover{color:var(--color-primary-content)}
a.btn-primary:hover{text-decoration:none}
header{position:sticky;top:0;z-index:5;background:var(--card);border-bottom:1px solid var(--line)}
.bar{max-width:1040px;margin:0 auto;padding:8px 16px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.brand{font-weight:700;font-size:16px;margin-right:6px;white-space:nowrap}
nav.tabs{display:flex;gap:2px;flex-wrap:wrap}nav.tabs a{padding:6px 11px;border-radius:8px;color:var(--muted);font-weight:500}
nav.tabs a:hover{background:var(--soft);text-decoration:none}nav.tabs a.on{background:var(--acc-soft);color:var(--acc)}
.pp{position:relative}.pp-native{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
.pp-btn{width:100%;text-align:left;cursor:pointer}
.pp-list{position:absolute;left:0;right:0;top:calc(100% + 4px);z-index:30;margin:0;padding:4px;list-style:none;background:var(--card);
  border:1px solid var(--line);border-radius:var(--radius);box-shadow:0 10px 28px rgba(0,0,0,.25);max-height:340px;overflow-y:auto;
  display:none}
.pp-list.open{display:block}.pp-list:focus{outline:none}
.pp-group{padding:8px 10px 4px;font-size:11.5px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.03em}
.pp-list [role=option]{display:flex;align-items:center;gap:8px;padding:6px 6px 6px 10px;border-radius:8px;cursor:pointer;font-size:14px}
.pp-list [role=option].act,.pp-list [role=option]:hover{background:var(--soft)}
.pp-list [role=option][aria-selected=true] .pp-t{font-weight:650;color:var(--acc)}
.pp-list [role=option].busy{opacity:.5;pointer-events:none}
.pp-t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pp-x{flex:none;display:inline-flex;align-items:center;justify-content:center;min-width:26px;height:26px;padding:0 6px;border:0;
  border-radius:6px;background:none;color:var(--muted);cursor:pointer;font-size:12.5px}
.pp-x:hover,.pp-x:focus-visible{background:color-mix(in oklab,var(--color-error) 16%,transparent);color:var(--color-error)}
.pp-x.sure{background:var(--color-error);color:var(--color-error-content);font-weight:600}
.iv{align-items:center}.iv.past{opacity:.7}
.iv[data-href]{cursor:pointer;transition:background .15s}.iv[data-href]:hover{background:var(--soft)}
.iv-m{margin:0}.iv-m .ui-head{padding-right:34px}.iv-flash{margin-bottom:12px}
.iv-when{flex:none;width:96px;display:flex;flex-direction:column;line-height:1.25}
.iv-when b{font-size:15px}.iv-when span{font-size:13.5px}.iv-when small{color:var(--acc);font-weight:600;font-size:12.5px}
.iv-sub{font-size:13.5px;margin-top:2px;overflow-wrap:anywhere}
.ui-body.flush{padding:0}
details.iv-add,details.iv-past{padding:0}
details.iv-add>summary,details.iv-past>summary{cursor:pointer;padding:14px 18px;font-weight:650}
details.iv-add>section{border:0;margin:0;background:none}
details.iv-past>.ui-row:first-of-type{border-top:1px solid var(--line)}
.iv-research h3{font-size:15px;font-weight:650;margin:18px 0 6px}.iv-research h3:first-child{margin-top:0}
.iv-research ul{margin:0 0 8px 20px;padding:0;list-style:disc}.iv-research li{margin:3px 0}
.iv-src{margin:6px 0 0 18px;list-style:disc;font-size:13px}
.iv-feed{margin-top:16px}
.mb-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:8px 0;border-bottom:1px solid var(--line)}
.mb-main{flex:1;min-width:200px;overflow-wrap:anywhere}.mb-st{flex:none}
details.mb-add{margin-top:12px}details.mb-add>summary{cursor:pointer;font-weight:600;color:var(--acc)}
.mb-help{margin:0 0 10px 18px;list-style:disc;font-size:13px;color:var(--muted)}
@media (max-width:640px){.iv{flex-wrap:wrap}.iv-when{width:auto;flex-direction:row;gap:8px;align-items:baseline}}
.rf-wrap{position:relative}
.rf-menu{position:absolute;right:0;top:100%;margin-top:6px;z-index:20;min-width:230px;width:max-content;max-width:calc(100vw - 32px);padding:10px 12px;background:var(--card);
  border:1px solid var(--line);border-radius:var(--radius);box-shadow:0 10px 28px rgba(0,0,0,.25);
  opacity:0;visibility:hidden;transform:translateY(-6px);transition:opacity .16s ease,transform .18s ease,visibility 0s linear .18s}
.rf-menu::before{content:"";position:absolute;left:0;right:0;top:-8px;height:8px}
.rf-wrap:hover .rf-menu,.rf-wrap:focus-within .rf-menu{opacity:1;visibility:visible;transform:none;transition-delay:0s}
.rf-title{font-size:12px;font-weight:600;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.03em}
.rf-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:5px 18px;margin-bottom:10px;max-height:70vh;overflow-y:auto}
@media (max-width:520px){.rf-list{grid-template-columns:1fr}}
.rf-list label{display:flex;align-items:center;gap:8px;font-size:13.5px;cursor:pointer;white-space:nowrap}
.rf-more{display:block;margin-top:8px;font-size:12.5px;color:var(--acc)}
@media (prefers-reduced-motion: reduce){.rf-menu{transition:none}}
.bar .right{margin-left:auto;display:flex;gap:6px;align-items:center}
main{max-width:1040px;margin:0 auto;padding:16px}
textarea{width:100%;min-height:120px;font-family:ui-monospace,Menlo,monospace;font-size:13px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:10px 14px;color:var(--fg)}
.stat:hover{text-decoration:none;border-color:var(--acc)}.stat b{display:block;font-size:22px;line-height:1.2}.stat span{color:var(--muted);font-size:13px}
.panel{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:12px 14px;margin-bottom:14px}
.row-chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:2px 0 8px}.row-chips .lbl{color:var(--muted);font-size:13px;width:92px}
a.fchip{padding:4px 11px;border-radius:99px;border:1px solid var(--line);color:var(--fg);font-size:13.5px;background:var(--card)}
a.fchip:hover{text-decoration:none;border-color:var(--acc)}a.fchip.on{background:var(--acc);border-color:var(--acc);color:#fff}
a.fchip small{opacity:.75;margin-left:3px}
details.more summary{cursor:pointer;color:var(--muted);font-size:13.5px;margin-top:4px}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
.listhead{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;margin:4px 0 6px;color:var(--muted);font-size:13.5px}
.bucket{position:sticky;top:52px;z-index:2;background:var(--bg);font-size:13px;font-weight:700;letter-spacing:.04em;text-transform:uppercase;
color:var(--muted);margin:16px 0 8px;padding:6px 0;border-bottom:1px solid var(--line)}.bucket span{font-weight:400}
.job{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);border-radius:var(--radius);padding:12px 14px;margin-bottom:10px;
display:grid;grid-template-columns:46px 1fr auto;gap:4px 14px;align-items:start}
.job.st-new{border-left-color:var(--acc)}.job.st-saved{border-left-color:var(--warn)}.job.st-applied{border-left-color:var(--good)}
.job.st-hidden,.job.stale{opacity:.6}
.score{width:46px;height:46px;border-radius:12px;display:flex;flex-direction:column;align-items:center;justify-content:center;font-weight:700;font-size:17px;background:var(--soft);color:var(--muted);line-height:1}
.score small{font-size:9.5px;font-weight:500;margin-top:2px}.s-hi{background:var(--good);color:#fff}.s-mid{background:var(--warn-soft);color:var(--warn)}
.jt{font-weight:650;font-size:16px;line-height:1.3}.jt a{color:var(--fg)}.jt a:hover{color:var(--acc)}
.co{margin-top:1px;font-size:14px}.co .src{color:var(--muted);font-size:12.5px}
.chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:7px}
.chip{display:inline-flex;align-items:center;gap:3px;font-size:12.5px;padding:2px 8px;border-radius:99px;background:var(--soft);color:var(--muted);white-space:nowrap}
.chip.wm-remote{background:var(--good-soft);color:var(--good)}.chip.wm-hybrid{background:var(--warn-soft);color:var(--warn)}.chip.wm-onsite{background:var(--bad-soft);color:var(--bad)}
.chip.age-new{background:var(--acc-soft);color:var(--acc);font-weight:600}.chip.age-old{background:var(--soft);color:var(--muted)}.chip.closed{background:var(--bad-soft);color:var(--bad)}
.chip.st{background:transparent;border:1px solid var(--line)}
details.why{margin-top:6px;font-size:12.5px;color:var(--muted)}details.why summary{cursor:pointer;width:max-content}
.acts{display:flex;flex-direction:column;gap:6px;align-items:stretch;min-width:150px}
.acts .icons{display:flex;gap:4px;justify-content:flex-end}.acts .icons button{padding:4px 8px;font-size:15px}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:14px 16px;margin-bottom:14px}
.card h2{font-size:16px;margin:0 0 10px}label{display:block;margin:8px 0 3px;font-weight:600;font-size:13px}
.row{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
table{width:100%;border-collapse:collapse}td,th{padding:7px 9px;border-bottom:1px solid var(--line);text-align:left;font-size:14px;vertical-align:top}
.meta,.muted{color:var(--muted)}.meta{font-size:13.5px}
.err{color:var(--bad)}.ok{color:var(--good)}.flash{padding:9px 12px;border-radius:8px;background:var(--acc-soft);border:1px solid var(--acc);margin-bottom:12px}
.badge{display:inline-block;font-size:11.5px;padding:1px 8px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.docs-co{margin-bottom:16px}.docs-co h3{margin:0 0 6px;font-size:15px}.docs-job{padding:8px 0;border-top:1px solid var(--line)}
.files{display:flex;flex-wrap:wrap;gap:6px;margin-top:5px}
@media (max-width:700px){.job{grid-template-columns:42px 1fr}.acts{grid-column:1/-1;flex-direction:row;flex-wrap:wrap;min-width:0}
.acts .icons{justify-content:flex-start}.score{width:42px;height:42px}.bucket{top:88px}.row-chips .lbl{width:100%}}
/* shared page building blocks (ui-*) and the Recruiters page (rc-*) */
.ui-table td,.ui-table th{vertical-align:middle;font-size:14px}.ui-table .num{text-align:right}.ui-table td.when{white-space:nowrap;color:var(--muted)}
.ui-table tr.off td{opacity:.55}.ui-err{font-size:12px;color:var(--bad);margin-top:3px;max-width:42ch}
.ui-row{display:flex;justify-content:space-between;align-items:flex-start;gap:14px;padding:14px 18px;border-top:1px solid var(--line)}
.ui-section>header+.ui-row{border-top:0}.ui-row-main{min-width:0;flex:1}.ui-row-title{font-weight:650;font-size:15px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.ui-btns{display:flex;gap:6px;flex-wrap:wrap}.ui-empty{color:var(--muted);text-align:center;padding:28px 18px}
.ui-inline{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin:12px 0 0;font-size:14px}.ui-inline .help{margin:0}
.ui-list{margin:0;padding-left:20px}.ui-list li{margin:3px 0}
.ui-addjob{margin-top:12px;width:100%}.ui-addjob input{flex:1}
details.wm-pick{display:inline-block;position:relative}.wm-why{opacity:.8;font-weight:400}details.wm-pick>summary{list-style:none;cursor:pointer}
details.wm-pick>summary::-webkit-details-marker{display:none}
.wm-menu{position:absolute;z-index:6;top:calc(100% + 4px);left:0;min-width:230px;max-width:320px;display:flex;flex-direction:column;
  align-items:stretch;gap:2px;padding:8px;background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.18)}
.wm-menu .muted{font-size:12px;line-height:1.35;padding:2px 4px 6px;white-space:normal}.wm-menu .btn{justify-content:flex-start}
.job-head{display:flex;gap:12px;align-items:center}.job-head>div{min-width:0}
.logo{flex:0 0 auto;border-radius:9px;object-fit:contain;background:#fff;border:1px solid var(--line)}
.logo.ph{display:inline-flex;align-items:center;justify-content:center;color:#fff;font-weight:700;font-size:14px;
  letter-spacing:.02em;border:0;user-select:none}
.ui-section>header>div{min-width:0}.ui-desc{margin:3px 0 0;color:var(--muted);font-size:13.5px;max-width:75ch}
.ui-h3{font-size:14px;font-weight:650;margin:18px 0 10px;padding-top:14px;border-top:1px solid var(--line)}
.ui-body>.ui-h3:first-child{margin-top:0;padding-top:0;border-top:0}
.ui-checks{display:flex;flex-wrap:wrap;gap:8px 18px}.ui .ui-checks label{display:inline-flex;align-items:center;gap:7px;font-weight:400;margin:0}
.ui .ui-toggle{display:inline-flex;align-items:center;gap:10px;font-weight:600;font-size:14px;margin:0}
.ui-steps{margin:0 0 6px;padding-left:20px;font-size:13.5px}.ui-steps li{margin:2px 0}
.ui-mono{font-family:ui-monospace,Menlo,Consolas,monospace!important;font-size:13px!important}
.ui-lead{margin:0 0 12px;font-size:13.5px;color:var(--muted)}
.ui-sub{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:12px;background:var(--soft)}
.ui-subhead{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px}
.ui-subhead .muted{font-size:12.5px}
.ui-layout{display:grid;grid-template-columns:180px minmax(0,1fr);gap:22px;align-items:start}
.ui-side{position:sticky;top:70px;display:flex;flex-direction:column;gap:2px}
.ui-side a{padding:7px 12px;border-radius:8px;color:var(--muted);font-weight:500;font-size:14px}
.ui-side a:hover{background:var(--soft);color:var(--fg);text-decoration:none}
.ui-savebar{position:sticky;bottom:0;z-index:4;display:flex;justify-content:space-between;align-items:center;gap:10px;
  padding:12px 18px;margin:0 0 18px;background:var(--card);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:0 -6px 18px rgba(0,0,0,.12)}.ui-savebar .muted{font-size:13px}
.ui-danger{display:flex;justify-content:space-between;align-items:center;gap:14px;flex-wrap:wrap}
.ui-danger .help{display:block}
.chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding:7px;min-height:44px;border:1px solid var(--line);
  border-radius:10px;background:var(--card)}.chips:focus-within{border-color:var(--acc)}
.chip{display:inline-flex;align-items:center;gap:2px;padding:2px 3px 2px 9px;border-radius:99px;background:var(--soft);
  border:1px solid var(--line);font-size:13px;cursor:grab;user-select:none;white-space:nowrap}
.chip .pts{font:600 12px ui-monospace,Menlo,Consolas,monospace;color:var(--acc);margin-right:5px}.chip.neg .pts{color:#d14343}
.chip button{border:0;background:none;padding:0 5px;border-radius:99px;cursor:pointer;color:var(--muted);font-size:15px;line-height:20px}
.chip button:hover,.chip button:focus-visible{background:var(--line);color:var(--fg)}.chip.dragging{opacity:.4}
.chip.over{outline:2px solid var(--acc)}
article.job[data-href]{cursor:pointer;transition:box-shadow .12s}
article.job[data-href]:hover{box-shadow:0 0 0 1px color-mix(in oklab,var(--acc) 50%,transparent),0 3px 12px rgba(0,0,0,.10)}
.jt a.ext{font-weight:400;color:var(--muted);margin-left:4px;font-size:14px}.jt a.ext:hover{color:var(--acc);text-decoration:none}
.wm-quick{display:inline-flex;align-items:center;gap:1px;font-size:12px;color:var(--muted)}.wm-quick span{margin:0 3px 0 2px}
.modal-backdrop button{display:block;width:100%;height:100%;opacity:0;cursor:default}
article.job.busy{opacity:.55;transition:opacity .15s}
article.job.leaving{opacity:0;transform:translateX(32px);height:0!important;min-height:0;margin-bottom:0;padding-top:0;padding-bottom:0;
  border-width:0;overflow:hidden;pointer-events:none;
  transition:opacity .22s ease,transform .26s ease,height .3s ease .1s,margin .3s ease .1s,padding .3s ease .1s,border-width .3s ease .1s}
article.job.updated{animation:jw-flash .9s ease}
@keyframes jw-flash{0%{box-shadow:0 0 0 2px var(--acc)}100%{box-shadow:0 0 0 0 transparent}}
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.jd-modal{width:min(940px,94vw);max-width:940px;max-height:90vh;padding:18px 20px;background:var(--bg)}
.jd-modal .jd{margin:0}.jd-modal .ui-section:last-child{margin-bottom:0}.jd-close{position:absolute;right:12px;top:12px;z-index:2}
.jd-modal .jd-head{padding-right:34px}
.jd-head{display:flex;gap:14px;align-items:center}.jd-t{min-width:0;flex:1}.jd-head h1{font-size:22px;font-weight:700;margin:0;line-height:1.25}
.jd-score{flex:0 0 auto}.jd .chips{margin-top:12px}
.jd-acts{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:14px}.jd-status{display:flex;flex-wrap:wrap;gap:4px}
.jd-text{line-height:1.65;max-width:80ch;font-size:15px}.jd-text p{margin:0 0 10px}.jd-text h3,.jd-text h4{font-weight:650;margin:18px 0 6px}
.jd-text h3{font-size:16.5px}.jd-text ul{list-style:disc;padding-left:22px;margin:0 0 10px}.jd-text ol{list-style:decimal;padding-left:22px;margin:0 0 10px}
.jd-text li{margin:2px 0}.jd-text blockquote{border-left:3px solid var(--line);padding-left:12px;color:var(--muted)}
.chips input{flex:1;min-width:150px;border:0;outline:0;background:transparent;font-size:13px;padding:4px;color:inherit}
section.ui-section{scroll-margin-top:70px}
@media (max-width:860px){.ui-layout{grid-template-columns:1fr}.ui-side{position:static;flex-direction:row;flex-wrap:wrap}}
.rc{max-width:900px;margin:0 auto}
.ui-head{margin:4px 0 18px}.ui-head h1{font-size:22px;font-weight:700;margin:0}
.ui-head p{margin:4px 0 0;color:var(--muted);max-width:70ch}
.ui-stats{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}
.ui-section{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);margin-bottom:18px}
.ui-section>header{position:static;background:none;display:flex;align-items:center;justify-content:space-between;gap:10px;
  padding:14px 18px;border-bottom:1px solid var(--line)}
.ui-section>header h2{font-size:16px;font-weight:650;margin:0}
.ui-body{padding:16px 18px}
.ui-step{display:flex;gap:12px;margin-bottom:20px}.ui-step:last-child{margin-bottom:0}
.ui-num{flex:0 0 26px;height:26px;border-radius:99px;background:var(--acc-soft);color:var(--acc);font-weight:700;
  display:flex;align-items:center;justify-content:center;font-size:13px;margin-top:1px}
.ui-step>div{flex:1;min-width:0}.ui-step h3{font-size:14.5px;font-weight:650;margin:2px 0 10px}
.ui-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px 14px}.ui-grid .wide{grid-column:1/-1}
.ui-grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px 14px}
.ui label{display:block;font-weight:600;font-size:13px;margin:0 0 4px}
.ui .help{display:block;font-weight:400;color:var(--muted);font-size:12.5px;margin-top:3px}
.ui input.input,.ui select.select{width:100%}
.ui textarea.textarea{width:100%;font-family:inherit;font-size:14.5px;line-height:1.55;min-height:120px}
.rc-people{margin-top:12px;border:1px solid var(--line);border-radius:10px;overflow:hidden}
.rc-people table td,.rc-people table th{font-size:13.5px;vertical-align:middle}
.rc-people .empty{padding:14px;color:var(--muted);font-size:13.5px;text-align:center}
.ui-hint{min-height:20px;font-size:13px;color:var(--muted);margin-top:6px}
.ui-foot{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;
  padding:14px 18px;border-top:1px solid var(--line);background:var(--soft);border-radius:0 0 var(--radius) var(--radius)}
.flt-toggle{display:flex;align-items:center;gap:8px;width:100%;padding:12px 18px;background:none;border:0;cursor:pointer;
  color:var(--fg);font-size:14px;text-align:left;border-radius:var(--radius)}
.flt-toggle:hover{background:var(--soft)}.flt-toggle:focus-visible{outline:2px solid var(--acc);outline-offset:-2px}
.flt-sum{flex:1;min-width:0;color:var(--muted);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.flt-sum .ms{font-size:1.1em}
.flt-chev{display:inline-flex;transition:transform .3s ease}.ui-filters.open .flt-chev{transform:rotate(180deg)}
.flt-panel{display:grid;grid-template-rows:0fr;transition:grid-template-rows .32s ease}
.flt-inner{overflow:hidden;min-height:0}
.flt-inner>.ui-body{opacity:0;transform:translateY(-6px);transition:opacity .22s ease,transform .3s ease;border-top:1px solid var(--line)}
.ui-filters.open .flt-panel{grid-template-rows:1fr}
.ui-filters.open .flt-inner>.ui-body{opacity:1;transform:none;transition-delay:.06s}
.ui-filters.no-anim .flt-panel,.ui-filters.no-anim .flt-inner>.ui-body,.ui-filters.no-anim .flt-chev{transition:none}
@media (prefers-reduced-motion: reduce){.flt-panel,.flt-inner>.ui-body,.flt-chev{transition:none}}
.ui-more summary{cursor:pointer;font-weight:600;font-size:13.5px;color:var(--acc);margin:4px 0}
.ui-more[open] summary{margin-bottom:12px}
.rc-draft{border-top:1px solid var(--line);padding:16px 18px}.rc-draft:first-child{border-top:0}
.rc-who{display:flex;gap:12px;align-items:flex-start}
.rc-avatar{flex:0 0 38px;height:38px;border-radius:99px;background:var(--acc-soft);color:var(--acc);font-weight:700;
  display:flex;align-items:center;justify-content:center;font-size:14px}
.rc-who .name{font-weight:650;font-size:15px;line-height:1.3}.rc-who .sub{color:var(--muted);font-size:13px}
.rc-who .badges{margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.rc-role{margin:8px 0 0 50px;font-size:13px;color:var(--muted)}
.rc-fields{margin:12px 0 0 50px}.rc-fields>div{margin-bottom:10px}
.rc-count{text-align:right;font-size:12px;color:var(--muted);margin-top:2px}.rc-count.over{color:var(--bad);font-weight:600}
.rc-actions{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;margin:4px 0 0 50px}
.rc-actions>div{display:flex;gap:6px;flex-wrap:wrap}
.rc-sent tbody tr{cursor:pointer}.rc-sent tbody tr:hover{background:var(--soft)}
.rc-sent td.when{white-space:nowrap;color:var(--muted)}
.rc-modal{max-width:680px;width:92vw}.rc-modal h3{font-size:18px;font-weight:700;margin:0}
.rc-modal .sub{color:var(--muted);font-size:13.5px;margin:2px 0 14px}
.rc-modal .modal-action{justify-content:space-between;flex-wrap:wrap}
.rc-modal .modal-action>div{display:flex;gap:6px;flex-wrap:wrap}
.rc-alert{margin:10px 0 0 50px}
@media (max-width:700px){.ui-grid,.ui-grid3{grid-template-columns:1fr}.rc-role,.rc-fields,.rc-actions,.rc-alert{margin-left:0}
  .rc-who .badges{display:none}.rc-sent .hide-sm{display:none}}
"""


CSS = CSS.replace("FONTVER", _font_version())


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


def page(title, body, flash="", refresh=0):
    cur = getattr(REQ, "path", "/")
    tabs = [("/", "Jobs"), ("/docs", "Documents"), ("/outreach", "Recruiters"), ("/interviews", "Interviews"),
            ("/sources", "Sources"),
            ("/profile", "CV profile"), ("/settings", "Settings")]
    nav = "".join('<a role="tab" class="tab%s" href="%s">%s</a>' % (
        " tab-active" if (cur == h or (h == "/" and cur.startswith("/cv/"))) else "", h, l) for h, l in tabs)
    running = '<span class="badge badge-soft badge-warning">' + ic("hourglass_top") + ' refresh running…</span>' if STATE["running"] else ""
    return ("""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">%s
<script>document.documentElement.dataset.theme = matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
(function () {  // mouse wheel: a notch carries further and glides to a stop (momentum with friction)
  if (window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  var v = 0, raf = 0, BOOST = 0.3, FRICTION = 0.88;   // one notch (100 px) travels about 100 * BOOST / (1 - FRICTION) = 250 px
  function ownScroller(el) {   // inside something that scrolls by itself: leave it to the browser
    for (; el && el.nodeType === 1 && el !== document.body && el !== document.documentElement; el = el.parentElement) {
      if (el.matches("textarea, select, dialog")) return true;
      var oy = getComputedStyle(el).overflowY;
      if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 1) return true;
    }
    return false;
  }
  function step() {
    var before = window.scrollY;
    window.scrollBy(0, v);
    v *= FRICTION;
    if (Math.abs(v) < 0.6 || window.scrollY === before) { v = 0; raf = 0; return; }
    raf = requestAnimationFrame(step);
  }
  addEventListener("wheel", function (ev) {
    if (ev.ctrlKey || ev.shiftKey || ev.defaultPrevented || Math.abs(ev.deltaX) > Math.abs(ev.deltaY)) return;
    var d = ev.deltaY * (ev.deltaMode === 1 ? 40 : ev.deltaMode === 2 ? innerHeight : 1);
    if (ev.deltaMode === 0 && Math.abs(d) < 50) return;   // a touchpad: small steps, it has its own momentum
    if (document.querySelector("dialog[open]") || ownScroller(ev.target)) return;
    ev.preventDefault();
    if (v && (v > 0) !== (d > 0)) v = 0;   // the other way: stop first
    v += d * BOOST;
    if (!raf) raf = requestAnimationFrame(step);
  }, {passive: false});
  ["mousedown", "keydown", "touchstart"].forEach(function (t) { addEventListener(t, function () { v = 0; }, {passive: true}); });
})();
(function () {  // a click outside an open dialog's box closes it (only if the press started outside too: not while selecting text)
  var downOut = false;
  function outside(ev) { var d = ev.target.closest && ev.target.closest("dialog[open]"); return d && !ev.target.closest(".modal-box") ? d : null; }
  document.addEventListener("pointerdown", function (ev) { downOut = !!outside(ev); }, true);
  document.addEventListener("click", function (ev) { var d = outside(ev); if (d && downOut) { ev.preventDefault(); d.close(); } downOut = false; }, true);
})();</script>
<title>%s · jobwatch</title>
<link rel="icon" href="/favicon.ico" sizes="any"><link rel="icon" type="image/svg+xml" href="/favicon.svg"><link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/manifest.webmanifest"><meta name="theme-color" content="#5b5bd6">
<link href="https://cdn.jsdelivr.net/npm/daisyui@5.0.0" rel="stylesheet" type="text/css"><style>%s</style></head><body><header><div class="bar"><span class="brand">%s jobwatch</span><nav class="tabs tabs-box tabs-sm">%s</nav>
<div class="right">%s%s</div></div></header>
<main>%s%s</main>%s</body></html>"""
            % ((SOFT_REFRESH % refresh) if refresh else "", e(title), CSS, ic("work"), nav, running, refresh_menu(),
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
