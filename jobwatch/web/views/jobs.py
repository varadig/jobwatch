"""The Jobs page and a posting's detail view."""
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from datetime import timezone

from jobwatch.models import SOURCES_INFO, STATUS_LABEL
from jobwatch.repositories.jobs import JobRepository
from jobwatch.services.logos import logo_files
from jobwatch.services.postings import clean_posting_html
from jobwatch.services.refresh import refresher
from jobwatch.services.state import STATE
from jobwatch.util.text import e
from jobwatch.util.timeutil import bucket_of, days_ago, fmt_dt, parse_dt, rel_day
from jobwatch.web.components import STATUS_BUTTONS, ic, logo_html, mode_picker, mode_quick, page, progress_html, wm_html
from jobwatch.config import config_store
from jobwatch.db import database


def jobs_page(q):
    with config_store.lock:
        cfg = config_store.load()
    # first load of an empty instance: start the first refresh now instead of showing nothing
    with database.connect() as con:
        empty = con.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    if empty and not STATE["running"] and not STATE.get("progress"):
        threading.Thread(target=refresher.run, kwargs={"force": True}, daemon=True).start()
        time.sleep(0.3)
    status = q.get("status", "active")
    srcs = sorted({s for s in q.get("source", "").split(",") if s in SOURCES_INFO})  # several sources may be picked
    src = ",".join(srcs)
    try:
        mn = int(q.get("min", cfg["min_score"]))
    except ValueError:
        mn = cfg["min_score"]
    text = q.get("q", "").strip()
    wf = q.get("work", cfg.get("default_work_filter", "remote_unknown"))
    sort = q.get("sort", "posted")
    age = q.get("age", "all")
    sql, args = "SELECT * FROM jobs WHERE score>=? AND dup_of IS NULL", [mn]
    wf_sql = {"remote": ["remote"], "remote_unknown": ["remote", "unknown"], "remote_hybrid": ["remote", "hybrid"],
              "hybrid": ["hybrid"], "onsite": ["onsite"]}.get(wf)
    if wf_sql:
        sql += " AND COALESCE(work_mode,'unknown') IN (%s)" % ",".join("?" * len(wf_sql))
        args += wf_sql
    if status == "active":
        sql += " AND status IN ('new','seen','saved')"
    elif status != "all":
        sql += " AND status=?"
        args.append(status)
    if srcs:
        sql += " AND source IN (%s)" % ",".join("?" * len(srcs))
        args += srcs
    if text:
        sql += " AND (title LIKE ? OR company LIKE ? OR location LIKE ?)"
        args += ["%" + text + "%"] * 3
    with database.connect() as con:
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
        counts = {r["status"]: r["c"] for r in con.execute(
            "SELECT status, COUNT(*) c FROM jobs WHERE score>=? AND dup_of IS NULL GROUP BY status", (mn,))}
        last_ok = {r["source"]: r["last_ok"] for r in con.execute("SELECT source, last_ok FROM source_state")}
        wcount = {r["m"]: r["c"] for r in con.execute(
            "SELECT COALESCE(work_mode,'unknown') m, COUNT(*) c FROM jobs WHERE score>=? AND dup_of IS NULL AND status IN ('new','seen','saved') GROUP BY m", (mn,))}
        scount = {r["source"]: r["c"] for r in con.execute(
            "SELECT source, COUNT(*) c FROM jobs WHERE score>=? AND dup_of IS NULL AND status IN ('new','seen','saved') GROUP BY source", (mn,))}

    # dates: posted (if the source does not give it, the collection day), collected
    for r in rows:
        seen = parse_dt(r["first_seen"]) or datetime.now(timezone.utc)
        pd = parse_dt(r["posted"])
        r["_posted_known"] = pd is not None
        r["_posted"] = pd or seen
        r["_seen"] = seen
        r["_pdays"] = days_ago(r["_posted"])
        r["_sdays"] = days_ago(seen)
        # for a source that returns the full list (studios): if it is missing from the last successful fetch -> closed
        lo = last_ok.get(r["source"])
        r["_closed"] = r["source"] == "studios" and bool(lo) and (r["last_seen"] or "") < lo
    key_days = "_sdays" if sort == "seen" else "_pdays"
    if age != "all":
        try:
            lim = int(age)
            rows = [r for r in rows if r[key_days] <= lim]
        except ValueError:
            pass
    if sort == "seen":
        rows.sort(key=lambda r: (r["_seen"], r["score"]), reverse=True)
    elif sort == "score":
        rows.sort(key=lambda r: (r["score"], r["_posted"]), reverse=True)
    else:
        rows.sort(key=lambda r: (r["_posted"], r["score"]), reverse=True)
    total = len(rows)
    rows = rows[:300]

    opts = lambda cur, items: "".join('<option value="%s"%s>%s</option>' % (
        e(v), " selected" if v == cur else "", e(l)) for v, l in items)
    base_q = {"status": status, "source": src, "work": wf, "sort": sort, "age": age, "min": str(mn), "q": text}

    def link(**chg):
        d = dict(base_q, **chg)
        return "/?" + urllib.parse.urlencode({k: v for k, v in d.items() if v not in ("", None)})

    picked_labels = []   # what the filters are set to: shown on the folded filter panel's button

    def chip(label, cnt, **chg):
        on = all(base_q.get(k) == v for k, v in chg.items())
        if on:
            picked_labels.append(label)
        return '<a class="fchip%s" href="%s">%s%s</a>' % (" on" if on else "", e(link(**chg)), label,
                                                       ("<small>%d</small>" % cnt) if cnt is not None else "")

    # summary tiles (active postings, together with the work-mode filter)
    with database.connect() as con:
        act_rows = [dict(r) for r in con.execute(
            "SELECT posted, first_seen, status FROM jobs WHERE score>=? AND dup_of IS NULL AND status IN ('new','seen','saved')"
            + ((" AND COALESCE(work_mode,'unknown') IN (%s)" % ",".join("?" * len(wf_sql))) if wf_sql else ""),
            [mn] + (wf_sql or []))]
    fresh = sum(1 for r in act_rows if parse_dt(r["posted"]) and days_ago(parse_dt(r["posted"])) <= 2)
    today = sum(1 for r in act_rows if parse_dt(r["first_seen"]) and days_ago(parse_dt(r["first_seen"])) == 0)
    stats = """<div class="stats">
<a class="stat" href="%s"><b>%d</b><span>{fiber_new} fresh (≤2 days old)</span></a>
<a class="stat" href="%s"><b>%d</b><span>{move_to_inbox} collected today</span></a>
<a class="stat" href="%s"><b>%d</b><span>{circle} new, not viewed yet</span></a>
<a class="stat" href="%s"><b>%d</b><span>{star} saved</span></a>
<a class="stat" href="%s"><b>%d</b><span>{check_circle} applied</span></a></div>""".format(
        fiber_new=ic("fiber_new"), move_to_inbox=ic("move_to_inbox"), circle=ic("circle", True), star=ic("star"),
        check_circle=ic("check_circle")) % (
        e(link(age="3", sort="posted", status="active")), fresh, e(link(age="1", sort="seen", status="active")), today,
        e(link(status="new")), counts.get("new", 0), e(link(status="saved")), counts.get("saved", 0),
        e(link(status="applied")), counts.get("applied", 0))

    st_chips = "".join([chip("Active", None, status="active"), chip("New", counts.get("new", 0), status="new"),
                        chip("Saved", counts.get("saved", 0), status="saved"),
                        chip("Applied", counts.get("applied", 0), status="applied"),
                        chip("Hidden", counts.get("hidden", 0), status="hidden"), chip("All", None, status="all")])
    wm_chips = "".join([chip(ic("home") + " Remote only", wcount.get("remote", 0), work="remote"),
                        chip("Remote + " + ic("help"), wcount.get("remote", 0) + wcount.get("unknown", 0), work="remote_unknown"),
                        chip("Remote + hybrid", None, work="remote_hybrid"),
                        chip(wm_html("hybrid"), wcount.get("hybrid", 0), work="hybrid"),
                        chip(wm_html("onsite"), wcount.get("onsite", 0), work="onsite"), chip("All", None, work="all")])
    sort_chips = "".join([chip(ic("calendar_today") + " Posted", None, sort="posted"),
                          chip(ic("move_to_inbox") + " Collected", None, sort="seen"),
                          chip(ic("target") + " Score", None, sort="score")])
    age_chips = "".join([chip("Any time", None, age="all"), chip("24 h", None, age="1"), chip("3 days", None, age="3"),
                         chip("7 days", None, age="7"), chip("30 days", None, age="30")])
    # sources: toggle chips, several can be on at once; "All" clears the choice
    src_chips = '<a class="fchip%s" href="%s">All</a>' % ("" if srcs else " on", e(link(source="")))
    for k, v in SOURCES_INFO.items():
        if not scount.get(k) and k not in srcs:
            continue  # no postings from it: no chip
        picked = sorted(set(srcs) ^ {k})
        src_chips += '<a class="fchip%s" href="%s" aria-pressed="%s">%s<small>%d</small></a>' % (
            " on" if k in srcs else "", e(link(source=",".join(picked))), "true" if k in srcs else "false",
            e(v[0]), scount.get(k, 0))
    hidden_inputs = "".join('<input type="hidden" name="%s" value="%s">' % (k, e(v))
                            for k, v in base_q.items() if k in ("status", "work", "sort", "age", "source") and v)
    more_open = " open" if (text or mn != cfg["min_score"]) else ""
    picked_labels += [e(SOURCES_INFO.get(k, (k,))[0]) for k in srcs]
    if mn != cfg["min_score"]:
        picked_labels.append("≥ %d pts" % mn)
    if text:
        picked_labels.append("“%s”" % e(text))
    filt = """<section class="ui-section ui-filters" aria-label="Filters">
<button type="button" class="flt-toggle" id="flt_btn" aria-expanded="false" aria-controls="flt_panel">%s<b>Filters</b>
<span class="flt-sum">%s</span><span class="flt-chev">%s</span></button>
<div class="flt-panel" id="flt_panel" inert><div class="flt-inner"><div class="ui-body">
<div class="row-chips"><span class="lbl">Status</span>%s</div>
<div class="row-chips"><span class="lbl">Work mode</span>%s</div>
<div class="row-chips"><span class="lbl">Source</span>%s<span class="muted" style="font-size:12.5px">click several to combine</span></div>
<div class="row-chips"><span class="lbl">Sort</span>%s</div>
<div class="row-chips"><span class="lbl">Age</span>%s</div>
<details class="ui-more"%s><summary>More filters: score, search</summary>
<form class="filters" method="get">%s
<label class="ui-inline" style="margin:0;font-weight:400">Min. score <input class="input input-sm" type="number" name="min" value="%d" style="width:80px"></label>
<input class="input input-sm" type="search" name="q" value="%s" placeholder="Search: title, company, location" aria-label="Search" style="width:260px">
<button class="btn btn-sm btn-primary">Filter</button><a class="btn btn-sm btn-ghost" href="/?reset=1">Reset filters</a></form></details></div></div></div></section>
""" % (
        ic("tune"), " · ".join(picked_labels), ic("expand_more"), st_chips, wm_chips, src_chips, sort_chips, age_chips, more_open, hidden_inputs, mn, e(text))
    head = """<div class="listhead"><span><b>%d</b> posting%s · age refers to <b>%s</b> · notifications from %d pts</span>
<form method="post" action="/mark-all-seen"><button class="btn btn-sm btn-ghost">Mark all new as seen</button></form></div>""" % (
        total, " (first 300 shown)" if total > 300 else "", "collected" if sort == "seen" else "posted",
        cfg["notify_min_score"])

    with database.connect() as con:
        has_cv = {r["job_id"] for r in con.execute("SELECT job_id FROM cvs WHERE status='done'")}
    logos = logo_files()
    cards, cur_bucket = [], None
    for r in rows:
        if sort != "score":
            bk = bucket_of(r[key_days])
            if bk != cur_bucket:
                n_in = sum(1 for x in rows if bucket_of(x[key_days]) == bk)
                cards.append('<div class="bucket">%s <span>· %d</span></div>' % (e(bk), n_in))
                cur_bucket = bk
        sc = r["score"]
        cls = "s-hi" if sc >= cfg["notify_min_score"] else ("s-mid" if sc >= cfg["min_score"] else "")
        qid = e(urllib.parse.quote(r["id"], safe=""))
        chips = [mode_picker(r), mode_quick(r)]
        if r["location"] and r["location"].strip().lower() not in ("remote", "(remote)", "anywhere"):
            chips.append('<span class="chip">%s %s</span>' % (ic("location_on"), e(r["location"][:60])))
        if r["_closed"]:
            chips.append('<span class="chip closed">%s probably closed</span>' % ic("event_busy"))
        if r["_posted_known"]:
            cls_age = "age-new" if r["_pdays"] <= 2 else ("age-old" if r["_pdays"] > 30 else "")
            fresh_ = r["_pdays"] <= 2
            chips.append('<span class="chip %s" title="Posted: %s">%s %s</span>' % (
                cls_age, e(r["_posted"].astimezone().strftime("%Y-%m-%d")), ic("fiber_new" if fresh_ else "calendar_today"),
                e(rel_day(r["_pdays"]) if fresh_ else "posted " + rel_day(r["_pdays"]))))
        else:
            chips.append('<span class="chip" title="The source does not give the posting date">%s posted date unknown</span>'
                         % ic("calendar_today"))
        chips.append('<span class="chip" title="Collected: %s">%s %s</span>' % (
            e(fmt_dt(r["first_seen"])), ic("move_to_inbox"), e(rel_day(r["_sdays"]))))
        if r["status"] != "new":
            chips.append('<span class="chip st">%s</span>' % e(STATUS_LABEL.get(r["status"], r["status"])))
        if r.get("also_sources"):
            chips.append('<span class="chip" title="The same posting came from these sources too">' + ic("repeat") + ' also on %s</span>' % e(
                ", ".join(SOURCES_INFO.get(x, (x,))[0] for x in r["also_sources"].split(",") if x)))
        icon_btns = "".join('<button class="btn btn-sm btn-ghost btn-square" name="status" value="%s" title="%s" aria-label="%s">%s</button>'
                            % (s_, t, t, ic(icn)) for s_, icn, t in STATUS_BUTTONS if s_ != r["status"])
        cv_btn = ('<a class="btn btn-sm btn-outline" href="/cv/%s">Documents ' + ic("check") + '</a>' if r["id"] in has_cv else
                  '<a class="btn btn-sm btn-primary" href="/cv/%s">CV + Resume</a>') % qid
        cards.append("""<article class="job st-%s%s" data-href="/job/%s"><div class="score %s" title="Score against your CV">%d<small>pts</small></div>
<div><div class="job-head">%s<div><div class="jt"><a href="/job/%s">%s</a> <a class="ext" href="/open/%s" target="_blank" rel="noopener noreferrer" title="Open the original posting" aria-label="Open the original posting">%s</a></div>
<div class="co"><b>%s</b> <span class="src">· %s</span></div></div></div><div class="chips">%s</div>
<details class="why"><summary>Why this score?</summary>%s</details></div>
<form class="acts" method="post" action="/job/%s">%s<div class="icons">%s</div></form></article>""" % (
            e(r["status"]), " stale" if (r["_closed"] or r["_pdays"] > 30) else "", qid, cls, sc,
            logo_html(r["company"], logos), qid, e(r["title"]), qid, ic("open_in_new"), e(r["company"] or "?"),
            e(SOURCES_INFO.get(r["source"], (r["source"],))[0]),
            "".join(chips), e(r["matched"] or "-"), qid, cv_btn, icon_btns))
    if not cards:
        cards.append('<section class="ui-section"><div class="ui-body ui-empty">No postings match the filters. '
                     'Loosen them, or press “Refresh” to fetch new ones.</div></section>')
    busy = bool(STATE.get("progress") or STATE.get("enrich") or STATE["running"])
    top = ('<div class="ui-head"><h1>Jobs</h1>'
           '<form method="post" action="/add-job" class="join ui-addjob"><input class="input input-sm join-item" type="url" name="url" required '
           'placeholder="Found a posting elsewhere? Paste its link to add it" aria-label="Link of a posting to add">'
           '<button class="btn btn-sm btn-primary btn-outline join-item">Add posting</button></form></div>')
    return page("Jobs", '<div class="ui">%s%s%s%s%s%s</div>' % (top, progress_html(), stats, filt, head, "".join(cards))
                + FILTERS_JS + JD_PREVIEW_JS + JOB_MODAL,
                q.get("msg", ""), refresh=3 if busy else 0)


def job_detail_html(jid, back="/", modal=False):
    """(title, HTML) of one posting in detail: its data, your status and documents, and the full posting text.
    modal: the content of the dialog on the Jobs page (no back link). Opening it marks a new posting seen."""
    with database.connect() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if r and r["status"] == "new":
            JobRepository(con).set_status(jid, "seen")   # opened: seen (remembered)
            r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        has_cv = bool(r and con.execute("SELECT 1 FROM cvs WHERE job_id=? AND status='done'", (jid,)).fetchone())
    if not r:
        return None, ('<div class="ui">%s<section class="ui-section"><div class="ui-body ui-empty">This posting is not in '
                      'the list any more.</div></section></div>') % ("" if modal else '<a class="btn btn-sm btn-ghost" href="/">%s Jobs</a>' % ic("arrow_back"))
    r = dict(r)
    with config_store.lock:
        cfg = config_store.load()
    qid = e(urllib.parse.quote(jid, safe=""))
    sc = r["score"] or 0
    cls = "s-hi" if sc >= cfg["notify_min_score"] else ("s-mid" if sc >= cfg["min_score"] else "")
    chips = [mode_picker(r), mode_quick(r)]
    if r["location"]:
        chips.append('<span class="chip">%s %s</span>' % (ic("location_on"), e(r["location"][:80])))
    posted = parse_dt(r["posted"])
    chips.append('<span class="chip">%s %s</span>' % (ic("calendar_today"), e("posted " + posted.astimezone().strftime("%Y-%m-%d"))
                                                      if posted else "posted date unknown"))
    chips.append('<span class="chip">%s collected %s</span>' % (ic("move_to_inbox"), e(fmt_dt(r["first_seen"]))))
    chips.append('<span class="chip st">%s</span>' % e(STATUS_LABEL.get(r["status"], r["status"])))
    if r.get("also_sources"):
        chips.append('<span class="chip">' + ic("repeat") + ' also on %s</span>' % e(
            ", ".join(SOURCES_INFO.get(x, (x,))[0] for x in r["also_sources"].split(",") if x)))
    acts = "".join('<button class="btn btn-sm btn-ghost" name="status" value="%s">%s %s</button>' % (
        s_, ic(icn), "Seen" if s_ == "seen" else t) for s_, icn, t in STATUS_BUTTONS if s_ != r["status"])
    cv_btn = ('<a class="btn btn-sm btn-outline" href="/cv/%s">Documents ' + ic("check") + '</a>' if has_cv else
              '<a class="btn btn-sm btn-primary" href="/cv/%s">CV + Resume</a>') % qid
    cv_btn += '<a class="btn btn-sm btn-outline" href="/interviews?job=%s#add">%s Interview</a>' % (qid, ic("event"))
    text = r["desc_html"] or clean_posting_html(r["description"] or "")
    loading = not r["desc_html"]
    body = ('<div class="ui jd">%s'
            '<section class="ui-section"><div class="ui-body"><div class="jd-head">%s<div class="jd-t"><h1 id="jd-title">%s</h1>'
            '<div class="co"><b>%s</b> <span class="src">· %s</span></div></div>'
            '<div class="score %s jd-score" title="Score against your CV">%d<small>pts</small></div></div>'
            '<div class="chips">%s</div>'
            '<div class="jd-acts">%s<a class="btn btn-sm btn-outline" href="/open/%s" target="_blank" rel="noopener noreferrer">'
            'Open the original %s</a><form method="post" action="/job/%s" class="jd-status">'
            '<input type="hidden" name="next" value="%s">%s</form></div>'
            '<details class="why"><summary>Why this score?</summary>%s</details></div></section>'
            '<section class="ui-section" aria-labelledby="jd-post-h"><header><div><h2 id="jd-post-h">The posting</h2>'
            '<p class="ui-desc" id="jd-src">%s</p></div></header><div class="ui-body"><div class="jd-text" id="jd-text"%s>%s</div>'
            '</div></section></div>') % (
        "" if modal else '<a class="btn btn-sm btn-ghost" href="%s">%s Back to the list</a>' % (e(back), ic("arrow_back")),
        logo_html(r["company"], logo_files(), 56), e(r["title"]), e(r["company"] or "?"),
        e(SOURCES_INFO.get(r["source"], (r["source"],))[0]), cls, sc, "".join(chips), cv_btn, qid, ic("open_in_new"), qid, e(back), acts,
        e(r["matched"] or "-"),
        "Loading the full posting from its page…" if loading else "From the posting's own page.",
        ' data-preview="/job/%s/preview"' % qid if loading else "",
        text or '<p class="muted">No text stored for this posting yet.</p>')
    return r["title"], body


# The filter panel on the Jobs page is folded by default; the button opens it with an animation. Whether it is open is
# remembered in this browser, so picking a filter (which reloads the page) does not fold it again.
FILTERS_JS = """<script>(function () {
var sec = document.querySelector(".ui-filters"), btn = document.getElementById("flt_btn"), panel = document.getElementById("flt_panel");
if (!sec || !btn) return;
function set(open, animate) {
  if (!animate) sec.classList.add("no-anim");
  sec.classList.toggle("open", open);
  btn.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) panel.removeAttribute("inert"); else panel.setAttribute("inert", "");
  if (!animate) { void sec.offsetHeight; sec.classList.remove("no-anim"); }
}
var saved = null;
try { saved = localStorage.getItem("jw_filters_open"); } catch (x) {}
set(saved === "1", false);
btn.addEventListener("click", function () {
  var open = !sec.classList.contains("open");
  set(open, true);
  try { localStorage.setItem("jw_filters_open", open ? "1" : "0"); } catch (x) {}
});
})();</script>"""


# Loads the full posting into a detail view (the page, or the dialog on the Jobs page).
JD_PREVIEW_JS = """<script>(function () {
window.jdLoadPreview = function (root) {
  var box = root.querySelector("#jd-text[data-preview]"), src = root.querySelector("#jd-src");
  if (!box) return;
  fetch(box.dataset.preview).then(function (r) { return r.ok ? r.text() : ""; }).then(function (h) {
    if (h) { box.innerHTML = h; src.textContent = "From the posting's own page."; }
    else src.textContent = "The full posting could not be read from its page (it may need a login); this is the text jobwatch has. Use \\u201cOpen the original \\u2197\\u201d.";
  }).catch(function () { src.textContent = "The full posting could not be loaded; this is the text jobwatch has."; });
};
})();</script>"""


def job_page(jid, back="/", flash=""):
    """The posting's own page (opened in a new tab, or from a link)."""
    title, body = job_detail_html(jid, back)
    return page((title or "Not found")[:60], body + JD_PREVIEW_JS + "<script>jdLoadPreview(document)</script>", flash)


# Jobs page: a card (or its title) opens the posting in a dialog; Ctrl/Cmd or middle click opens its own page in a new
# tab. The buttons (on a card or in the dialog) save in the background, without reloading the page: a card that no
# longer fits the filters slides away, the others are updated in place, and the counters follow.
JOB_MODAL = """<dialog id="job_dlg" class="modal" aria-labelledby="jd-title"><div class="modal-box jd-modal">
<form method="dialog"><button class="btn btn-sm btn-circle btn-ghost jd-close" aria-label="Close">""" + ic("close") + """</button></form>
<div id="job_dlg_body"></div></div><form method="dialog" class="modal-backdrop"><button aria-label="Close" tabindex="-1"></button></form></dialog>
<div id="jw_live" class="sr-only" aria-live="polite"></div>
<script>(function () {
var dlg = document.getElementById("job_dlg"), body = document.getElementById("job_dlg_body"), live = document.getElementById("jw_live");
var changed = false, cur = null, curCard = null;
var reduce = window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches;
// ---- updating the list in place
function swapCounts(doc) {
  [".stats", ".listhead"].forEach(function (sel) {
    var a = document.querySelector(sel), b = doc.querySelector(sel);
    if (a && b) a.replaceWith(document.importNode(b, true));
  });
  var A = document.querySelectorAll(".ui-filters .row-chips"), B = doc.querySelectorAll(".ui-filters .row-chips");
  if (A.length === B.length) A.forEach(function (x, i) { x.replaceWith(document.importNode(B[i], true)); });
}
function bucketOf(el) {
  while (el && !(el.classList && el.classList.contains("bucket"))) el = el.previousElementSibling;
  return el;
}
function fixBucket(b) {
  if (!b) return;
  var n = 0, x = b.nextElementSibling;
  while (x && !x.classList.contains("bucket")) { if (x.matches("article.job")) n++; x = x.nextElementSibling; }
  if (!n) b.remove(); else { var sp = b.querySelector("span"); if (sp) sp.textContent = "\\u00b7 " + n; }
}
function leave(card) {
  var b = bucketOf(card);
  function done() { card.remove(); fixBucket(b); }
  if (reduce) return done();
  card.style.height = card.offsetHeight + "px";
  void card.offsetHeight;
  card.classList.add("leaving");
  setTimeout(done, 420);
}
function refreshCard(card, what) {
  if (!card || !card.isConnected) return;
  var href = card.dataset.href, title = (card.querySelector(".jt a") || {}).textContent || "";
  card.classList.add("busy");
  fetch(location.href, {cache: "no-store"}).then(function (r) { return r.text(); }).then(function (h) {
    var doc = new DOMParser().parseFromString(h, "text/html");
    swapCounts(doc);
    var fresh = doc.querySelector('article.job[data-href="' + href + '"]');
    if (fresh) {
      fresh = document.importNode(fresh, true);
      card.replaceWith(fresh);
      if (!reduce) { fresh.classList.add("updated"); setTimeout(function () { fresh.classList.remove("updated"); }, 900); }
    } else leave(card);
    if (what) live.textContent = title + ": " + what;
  }).catch(function () { location.reload(); });
}
var LABEL = {hidden: "hidden", seen: "marked seen", saved: "saved", applied: "marked applied", "new": "marked new"};
function post(form, submitter) {
  var fd = new FormData(form, submitter);
  return fetch(form.action, {method: "POST", body: new URLSearchParams(fd)}).then(function () { return fd; });
}
// buttons on the cards
document.addEventListener("submit", function (ev) {
  var form = ev.target, card = form.closest && form.closest("article.job[data-href]");
  if (!card || dlg.contains(form) || (form.method || "").toLowerCase() !== "post") return;
  ev.preventDefault();
  var d = form.closest("details"); if (d) d.open = false;
  post(form, ev.submitter).then(function (fd) {
    refreshCard(card, LABEL[fd.get("status")] || (fd.get("work_mode") ? "work mode set" : ""));
  });
}, true);
// ---- the dialog
function load(href) {
  cur = href;
  return fetch(href + "?partial=1").then(function (r) { if (!r.ok) throw r; return r.text(); })
    .then(function (h) { body.innerHTML = h; if (window.jdLoadPreview) window.jdLoadPreview(body); });
}
function open(href, card) {
  curCard = card; changed = !!(card && card.classList.contains("st-new"));   // opening marks it seen
  body.innerHTML = '<p class="muted" style="padding:20px">Loading\\u2026</p>';
  dlg.showModal();
  load(href).catch(function () { location.href = href; });
}
var lastStatus = "";
dlg.addEventListener("close", function () {
  body.innerHTML = "";
  if (changed) { changed = false; refreshCard(curCard, LABEL[lastStatus] || ""); }
  lastStatus = "";
});
body.addEventListener("submit", function (ev) {
  var f = ev.target;
  if (!f.action || (f.method || "").toLowerCase() !== "post") return;
  ev.preventDefault();
  post(f, ev.submitter).then(function (fd) {
    changed = true;
    if (fd.has("status")) { lastStatus = fd.get("status"); dlg.close(); } else load(cur);   // status: done; work mode: show it
  });
});
function cardOf(ev) {
  var a = ev.target.closest('.jt a[href^="/job/"]');
  if (a) return {card: a.closest("article.job"), link: true};
  var card = ev.target.closest("article.job[data-href]");
  if (!card || ev.target.closest("a,button,summary,details,form,input,select,textarea,label")) return null;
  if (String(window.getSelection && window.getSelection()).trim()) return null;   // selecting text, not clicking
  return {card: card, link: false};
}
document.addEventListener("click", function (ev) {
  if (ev.button !== 0 || dlg.contains(ev.target)) return;
  var t = cardOf(ev);
  if (!t || !t.card) return;
  if (ev.ctrlKey || ev.metaKey || ev.shiftKey) { if (!t.link) window.open(t.card.dataset.href, "_blank"); return; }
  ev.preventDefault();
  open(t.card.dataset.href, t.card);
});
document.addEventListener("auxclick", function (ev) {
  var t = cardOf(ev);
  if (t && t.card && !t.link && ev.button === 1) { ev.preventDefault(); window.open(t.card.dataset.href, "_blank"); }
});
})();</script>"""
