"""The Sources page."""

from jobwatch.models import FETCHED_SOURCES, SOURCES_INFO, UNSUPPORTED
from jobwatch.services.state import STATE
from jobwatch.sources.custom import CUSTOM_TYPES
from jobwatch.util.text import e
from jobwatch.util.timeutil import fmt_dt
from jobwatch.web.components import ic, page, progress_html
from jobwatch.config import config_store
from jobwatch.db import database


def sources_page(flash=""):
    with database.connect() as con:
        st = {r["source"]: r for r in con.execute("SELECT * FROM source_state")}
        cnt = {r["source"]: r["c"] for r in con.execute("SELECT source, COUNT(*) c FROM jobs GROUP BY source")}
    with config_store.lock:
        cfg = config_store.load()
    busy = bool(STATE["running"])
    rows = []
    for k in FETCHED_SOURCES:
        name, url = SOURCES_INFO[k]
        s = st.get(k)
        on = cfg["sources"].get(k)
        if not on:
            status = '<span class="badge badge-soft badge-neutral">Off</span>'
        elif s and s["error"]:
            status = '<span class="badge badge-soft badge-error">Error</span><div class="ui-err">%s</div>' % e(s["error"][:200])
        else:
            status = ('<span class="badge badge-soft badge-success">OK</span>' if s else
                      '<span class="badge badge-soft badge-neutral">Not run yet</span>')
        tick = ('<input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="run_%s" aria-label="Select %s"%s>'
                % (k, e(name), "" if on else " disabled title=\"Turned off in Settings\""))
        one = ('<button class="btn btn-xs btn-ghost" name="one" value="%s"%s>Refresh</button>'
               % (k, "" if on and not busy else " disabled"))
        rows.append('<tr%s><td>%s</td><td>%s</td><td class="when">%s</td><td class="num">%s</td><td class="num">%s</td><td class="num">%s</td><td>%s</td><td>%s</td></tr>' % (
            "" if on else ' class="off"', tick,
            ('<a href="%s" target="_blank" rel="noopener">%s</a>' % (e(url), e(name))) if url else e(name),
            e(fmt_dt(s["last_run"])) if s else "—", s["found"] if s else "—", s["new"] if s else "—", cnt.get(k, 0), status, one))
        if k == "custom":  # each own source can be refreshed on its own
            for src in cfg.get("custom_sources") or []:
                son = on and src.get("enabled", True)
                rows.append('<tr class="sub%s"><td><input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="run_cs_%s" '
                            'aria-label="Select %s"%s></td><td colspan="6">%s %s <span class="muted">· %s</span></td>'
                            '<td><button class="btn btn-xs btn-ghost" name="one" value="cs:%s"%s>Refresh</button></td></tr>' % (
                                "" if son else " off", e(src["id"]), e(src.get("name") or src["url"]), "" if son else " disabled",
                                ic("subdirectory_arrow_right"), e(src.get("name") or src["url"]), e(CUSTOM_TYPES.get(src.get("type"), src.get("type"))),
                                e(src["id"]), "" if son and not busy else " disabled"))
    uns = "".join('<tr><td>%s</td><td class="muted">%s</td></tr>' % (
        ('<a href="%s" target="_blank" rel="noopener">%s %s</a>' % (e(u), e(n), ic("open_in_new"))) if u else e(n), e(why)) for n, u, why in UNSUPPORTED)
    body = """<div class="ui"><div class="ui-head"><h1>Sources</h1><p>Where postings come from, and how the last refresh went.
Sources are turned on and off in <a href="/settings#search">Settings → Job search</a>.</p></div>%s
<section class="ui-section" id="refresh"><header><div><h2>Watched sources</h2><p class="ui-desc">Refreshed automatically every %d minutes
(Remotive at most every 6 hours). To fetch now, tick the sources you want, or use a row's Refresh button.</p></div></header>
<form method="post" action="/run"><div style="overflow-x:auto"><table class="table ui-table"><thead><tr><th style="width:36px">
<input type="checkbox" class="checkbox checkbox-sm" aria-label="Select all" onclick="document.querySelectorAll('#refresh tbody input[type=checkbox]:not(:disabled)').forEach(function (c) { c.checked = this.checked; }, this)"></th>
<th>Source</th><th>Last run</th><th class="num">Fetched</th><th class="num">New</th><th class="num">In the list</th><th>Status</th><th></th></tr></thead>
<tbody>%s</tbody></table></div>
<div class="ui-foot"><span class="muted" style="font-size:13px">%s</span><div class="ui-btns">
<button class="btn btn-sm btn-outline" name="all" value="1"%s>Refresh all</button>
<button class="btn btn-sm btn-primary" name="selected" value="1"%s>Refresh selected</button></div></div></form></section>
<section class="ui-section"><header><div><h2>Sites that cannot be watched automatically</h2><p class="ui-desc">Check these by hand now and then.</p></div></header>
<div style="overflow-x:auto"><table class="table ui-table"><thead><tr><th>Site</th><th>Why</th></tr></thead><tbody>%s</tbody></table></div></section></div>""" % (
        progress_html(), cfg["interval_minutes"], "".join(rows),
        "A refresh is running…" if busy else "Only the sources that are turned on can be selected.",
        " disabled" if busy else "", " disabled" if busy else "", uns)
    return page("Sources", body, flash, refresh=3 if busy else 0)


def custom_source_fields(i, src):
    """One editable own-source block of the Settings form (i = index, "new" for the empty one)."""
    src = src or {}
    opts = "".join('<option value="%s"%s>%s</option>' % (k, " selected" if src.get("type", "vmp") == k else "", e(v))
                   for k, v in CUSTOM_TYPES.items())
    f = lambda name, label, val, extra="", wide=False: (
        '<div%s><label for="cs_%s_%s">%s</label><input class="input" id="cs_%s_%s" name="cs_%s_%s" value="%s"%s></div>'
        % (' class="wide"' if wide else "", i, name, label, i, name, i, name, e(val), extra))
    if i == "new":
        head = '<div class="ui-subhead"><b>Add a new source</b><span class="muted">Fill in the URL to add it; it is saved with the page.</span></div>'
    else:
        head = ('<div class="ui-subhead"><b>%s</b><span class="ui-checks">'
                '<label><input type="checkbox" class="toggle toggle-sm toggle-primary" name="cs_%s_enabled"%s> Enabled</label>'
                '<label><input type="checkbox" class="checkbox checkbox-sm checkbox-error" name="cs_%s_delete"> Delete on save</label></span></div>'
                % (e(src.get("name") or src.get("url", "")), i, " checked" if src.get("enabled", True) else "", i))
    return ('<div class="ui-sub">%s<input type="hidden" name="cs_%s_id" value="%s"><div class="ui-grid">'
            '<div><label for="cs_%s_type">Type</label><select class="select" id="cs_%s_type" name="cs_%s_type">%s</select></div>%s%s%s'
            '<div class="wide"><span class="help">Login (only if the site needs it):</span></div>%s%s'
            '<div><label for="cs_%s_password">Password %s</label><input class="input" type="password" id="cs_%s_password" name="cs_%s_password" '
            'autocomplete="new-password" placeholder="Leave empty to keep"></div></div></div>') % (
        head, i, e(src.get("id", "")), i, i, i, opts,
        f("name", "Name", src.get("name", ""), ' placeholder="e.g. munka.hu"'),
        f("url", "URL: search result page, web page or feed", src.get("url", ""),
          ' placeholder="https://vmp.munka.hu/allas/talalatok?kulcsszo=…"', wide=True),
        f("pattern", "Link pattern (only for “any web page”)", src.get("pattern", ""), ' placeholder="/job/"'),
        f("login_url", "Login page (empty = the default for VMP)", src.get("login_url", "")),
        f("username", "Username", src.get("username", ""), ' autocomplete="off"'),
        i, '<span class="badge badge-soft badge-success">set</span>' if src.get("password") else "", i, i)


def custom_sources_card(cfg):
    blocks = "".join(custom_source_fields(i, s) for i, s in enumerate(cfg.get("custom_sources") or []))
    return ('<p class="ui-lead"><b>munka.hu VMP</b>: search on <a href="https://vmp.munka.hu/allas/egyszeru_kereses" target="_blank" '
            'rel="noopener">vmp.munka.hu</a> and paste the address of the result page; with your VMP login the employer is shown too. '
            '<b>Any web page</b>: every link whose address contains the pattern becomes a posting. Passwords stay on this machine.</p>'
            '%s%s<input type="hidden" name="cs_count" value="%d">') % (
        blocks, custom_source_fields("new", None), len(cfg.get("custom_sources") or []))
