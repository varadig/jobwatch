"""The Recruiters page."""
import re

from jobwatch import cvgen

from jobwatch.services.outreach import ROLE_UNREAD, outreach_role_label
from jobwatch.util.text import e, safe_url
from jobwatch.util.timeutil import fmt_dt
from jobwatch.web.components import ic, page
from jobwatch.config import config_store
from jobwatch.db import database


OUTREACH_CSS = ""  # the styles are in CSS (shared ui-* blocks + rc-* for this page)


OUTREACH_JS = """<script>
function ocount(id, lim) { var t = document.getElementById('om' + id), c = document.getElementById('on' + id);
  if (!c) return; c.textContent = t.value.length + ' / ' + lim + ' characters';
  c.className = 'rc-count' + (t.value.length > lim ? ' over' : ''); }
function ocopy(id, subj) {  // subj: copy the subject line instead of the message
  var t = document.getElementById((subj ? 'os' : 'om') + id), b = document.getElementById((subj ? 'ocs' : 'oc') + id), ok = false;
  var label = b.textContent; t.focus(); t.select(); try { ok = document.execCommand('copy'); } catch (err) {}
  if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(t.value); ok = true; }
  b.textContent = ok ? '✓ Copied' : 'Press Ctrl+C'; setTimeout(function () { b.textContent = label; }, 1800); }
function oview(id) { document.getElementById('dlg' + id).showModal(); }
</script>"""


OUTREACH_FORM_JS = """<script>
var RL = [];
function rval(id) { return document.getElementById(id).value.trim(); }
function rcell(tr, text, cls) { var td = document.createElement('td'); td.textContent = text; if (cls) td.className = cls; tr.appendChild(td); }
function rdraw() {
  var body = document.getElementById('rlist'), empty = document.getElementById('rempty'), table = document.getElementById('rtable');
  body.innerHTML = '';
  RL.forEach(function (r, i) {
    var tr = document.createElement('tr');
    rcell(tr, r.name + (r.about ? '  ℹ️' : ''));
    rcell(tr, r.title || '—', 'hide-sm');
    rcell(tr, r.role ? (r.role.length > 48 ? r.role.slice(0, 48) + '…' : r.role) : '—', 'hide-sm');
    rcell(tr, r.kind === 'agency' ? 'Agency' : r.kind === 'inhouse' ? 'In-house' : 'Auto');
    var td = document.createElement('td'), b = document.createElement('button');
    b.type = 'button'; b.className = 'btn btn-ghost btn-xs'; b.textContent = 'Remove'; b.setAttribute('aria-label', 'Remove ' + r.name);
    b.onclick = function () { RL.splice(i, 1); rdraw(); }; td.style.textAlign = 'right'; td.appendChild(b); tr.appendChild(td);
    body.appendChild(tr);
  });
  table.style.display = RL.length ? '' : 'none'; empty.style.display = RL.length ? 'none' : '';
  document.getElementById('rjson').value = JSON.stringify(RL);
  var go = document.getElementById('rgo'); go.disabled = !RL.length;
  go.textContent = RL.length ? 'Write ' + RL.length + ' message' + (RL.length === 1 ? '' : 's') : 'Write messages';
}
var RLOOK = null;
function rlookup() {  // LinkedIn link -> name + title from the profile's public link preview
  var url = rval('r_contact'), hint = document.getElementById('rhint');
  if (!/linkedin\\.com\\/in\\//i.test(url)) return Promise.resolve();
  hint.textContent = 'Reading the profile…';
  RLOOK = fetch('/outreach/lookup?url=' + encodeURIComponent(url)).then(function (r) { return r.json(); }).then(function (d) {
    if (!d.ok) { hint.textContent = d.error || ''; return; }
    if (rval('r_contact') === url) document.getElementById('r_contact').value = d.url;
    if (!rval('r_name')) document.getElementById('r_name').value = d.name || '';
    if (!rval('r_title')) document.getElementById('r_title').value = d.title || '';
    document.getElementById('r_about').value = d.about || '';
    hint.textContent = d.from === 'profile' ? '✓ Filled in from the profile. Check it, then add the person.'
      : 'The profile is not public: the name was guessed from the link. Check it and add the title by hand.';
  }).catch(function () { hint.textContent = 'Could not read the link. Fill in the name by hand.'; })
    .then(function () { RLOOK = null; });
  return RLOOK;
}
function radd() {
  if (!rval('r_name') && /linkedin\\.com\\/in\\//i.test(rval('r_contact'))) {  // only the link was given
    (RLOOK || rlookup()).then(function () { if (rval('r_name')) radd(); });
    return false;
  }
  if (!rval('r_name')) { document.getElementById('rhint').textContent = 'Give a LinkedIn link or a name first.';
    document.getElementById('r_contact').focus(); return false; }
  RL.push({name: rval('r_name'), title: rval('r_title'), contact: rval('r_contact'), kind: document.getElementById('r_kind').value,
           role: rval('r_role'), about: rval('r_about')});
  ['r_name', 'r_title', 'r_contact', 'r_kind', 'r_role', 'r_about'].forEach(function (id) { document.getElementById(id).value = ''; });
  document.getElementById('rhint').textContent = '';
  rdraw(); document.getElementById('r_contact').focus(); return true;
}
function renter(ev) { if (ev.key === 'Enter') { ev.preventDefault(); radd(); } }
function rsubmit() {  // a filled-in but not yet added recruiter counts too
  if (rval('r_name')) radd();
  if (!RL.length) { document.getElementById('rhint').textContent = 'Add at least one recruiter.'; return false; }
  return true;
}
function rchannel() {  // the introducer field matters only for introduction requests
  var intro = document.getElementById('channel').value === 'linkedin_intro';
  document.getElementById('viawrap').style.display = intro ? '' : 'none';
  if (intro) document.getElementById('rmore').open = true;
}
rdraw(); rchannel();
</script>"""


def outreach_initials(name):
    parts = [p for p in re.split(r"\s+", name or "") if p]
    return e("".join(p[0] for p in parts[:2]).upper() or "?")


def outreach_page(q, flash=""):
    with config_store.lock:
        cfg = config_store.load()
    with database.connect() as con:
        jobs = con.execute("SELECT id, title, company FROM jobs WHERE status != 'hidden' AND score >= ? "
                           "ORDER BY first_seen DESC LIMIT 100", (cfg["min_score"],)).fetchall()
        rows = con.execute("SELECT o.*, j.title AS job_title, j.company AS job_company FROM outreach o "
                           "LEFT JOIN jobs j ON j.id = o.job_id ORDER BY o.id DESC LIMIT 200").fetchall()
        picked = q.get("job", "")
        if picked and not any(j["id"] == picked for j in jobs):
            extra = con.execute("SELECT id, title, company FROM jobs WHERE id=?", (picked,)).fetchone()
            jobs = ([extra] if extra else []) + list(jobs)
    job_opts = '<option value="">None: a general message</option>' + "".join(
        '<option value="%s"%s>%s – %s</option>' % (e(j["id"]), " selected" if j["id"] == picked else "",
                                                   e(j["title"]), e(j["company"] or "?")) for j in jobs)
    ch_opts = "".join('<option value="%s">%s</option>' % (k, e(v[0])) for k, v in cvgen.OUTREACH_CHANNELS.items())
    drafts = [r for r in rows if r["status"] != "sent"]
    sent = [r for r in rows if r["status"] == "sent"]

    head = """<div class="ui-head"><h1>Recruiters</h1>
<p>Claude writes a short, personal first message to each recruiter, based on your CV profile and the role they hire for.
LinkedIn does not allow sending from other programs: copy the message and open the profile with one click. E-mails can be sent from here.</p>
<div class="ui-stats"><span class="badge badge-soft badge-primary">%d draft%s</span><span class="badge badge-soft badge-success">%d sent</span></div></div>""" % (
        len(drafts), "" if len(drafts) == 1 else "s", len(sent))

    form = """<section class="ui-section" aria-labelledby="new-h"><header><h2 id="new-h">New messages</h2></header>
<form method="post" action="/outreach/generate" onsubmit="return rsubmit()"><div class="ui-body">
<div class="ui-step"><span class="ui-num">1</span><div><h3>Who do you write to?</h3>
<div class="ui-grid">
<div class="wide"><label for="r_contact">LinkedIn profile link or e-mail</label>
<input class="input" id="r_contact" placeholder="https://www.linkedin.com/in/…" onkeydown="renter(event)" onpaste="setTimeout(rlookup, 0)" onchange="rlookup()" autocomplete="off">
<span class="help">Paste a LinkedIn link and the name and title fill in by themselves.</span></div>
<div><label for="r_name">Name</label><input class="input" id="r_name" placeholder="Anna Kovács" onkeydown="renter(event)" autocomplete="off"></div>
<div><label for="r_title">Title and company</label><input class="input" id="r_title" placeholder="Talent Acquisition @ Supercell" onkeydown="renter(event)" autocomplete="off"></div>
<div class="wide"><label for="r_role">Role they hire for <span class="help" style="display:inline">(optional)</span></label>
<input class="input" id="r_role" placeholder="LinkedIn job link, another job page, or a few words" onkeydown="renter(event)" autocomplete="off">
<span class="help">Makes the message specific to that role. Tip: the “Meet the hiring team” box on a LinkedIn posting shows who hires for it.</span></div>
<div><label for="r_kind">Recruiter works</label><select class="select" id="r_kind"><option value="">Decide from the title</option>
<option value="inhouse">In-house: hires for their own company</option><option value="agency">Agency or freelance: hires for clients</option></select></div>
<div style="align-self:end"><button type="button" class="btn btn-primary btn-outline" onclick="radd()">Add to the list</button></div>
</div>
<input type="hidden" id="r_about"><div class="ui-hint" id="rhint" role="status" aria-live="polite"></div>
<div class="rc-people"><div class="empty" id="rempty">No recruiters yet. Add them one by one above (Enter works too).</div>
<table class="table table-sm" id="rtable"><thead><tr><th>Name</th><th class="hide-sm">Title</th><th class="hide-sm">Role</th><th>Type</th><th></th></tr></thead>
<tbody id="rlist"></tbody></table></div>
<input type="hidden" name="recruiters_json" id="rjson" value="[]"></div></div>
<div class="ui-step"><span class="ui-num">2</span><div><h3>Message</h3>
<div class="ui-grid3">
<div><label for="channel">Channel</label><select class="select" name="channel" id="channel" onchange="rchannel()">%s</select></div>
<div><label for="language">Language</label><select class="select" name="language" id="language"><option value="auto">Automatic</option>
<option value="en">English</option><option value="hu">Hungarian</option><option value="de">German</option></select></div>
<div><label for="job_id">Posting for everyone</label><select class="select" name="job_id" id="job_id">%s</select></div>
</div>
<details class="ui-more" id="rmore"><summary>More options</summary><div class="ui-grid">
<div class="wide" id="viawrap"><label for="via">Mutual connection who introduces you</label>
<input class="input" name="via" id="via" placeholder="Péter Nagy | we worked together at Gameloft 2015–2017" autocomplete="off">
<span class="help">On the recruiter's LinkedIn profile, “mutual connections” shows who knows both of you.</span></div>
<div class="wide"><label for="note">Note for Claude</label><input class="input" name="note" id="note" placeholder="e.g. LinkedIn suggested them for the Unity Developer role" autocomplete="off"></div>
</div></details></div></div>
</div>
<div class="ui-foot"><span class="muted" style="font-size:13px">About 1–3 cents per message. Your contact details are not sent to the API.</span>
<button class="btn btn-primary" id="rgo">Write messages</button></div></form></section>""" % (ch_opts, job_opts)

    draft_html = []
    for r in drafts:
        label, limit, _ = cvgen.OUTREACH_CHANNELS.get(r["channel"], ("?", 1000, ""))
        oid, role = r["id"], outreach_role_label(r)
        badge = {"running": '<span class="badge badge-soft badge-info"><span class="loading loading-spinner loading-xs"></span> Writing</span>',
                 "error": '<span class="badge badge-soft badge-error">Error</span>',
                 "draft": '<span class="badge badge-soft badge-primary">Draft</span>'}.get(r["status"], "")
        who = """<div class="rc-who"><span class="rc-avatar" aria-hidden="true">%s</span><div><div class="name">%s</div><div class="sub">%s</div></div>
<div class="badges"><span class="badge badge-ghost">%s</span>%s</div></div>""" % (
            outreach_initials(r["name"]), e(r["name"]), e(r["title"] or ""), e(label), badge)
        if r["via"]:
            who += '<div class="rc-role">Goes to <b>%s</b>, who introduces you.</div>' % e(r["via"])
        if role:
            who += '<div class="rc-role">Role: %s</div>' % e(role)
        if r["role_ref"] and ROLE_UNREAD in (r["role_text"] or ""):
            who += ('<div role="alert" class="alert alert-warning alert-soft rc-alert"><span>The posting could not be read (LinkedIn limits requests), '
                    'so this message is not tailored to it. Paste the posting text into “Role they hire for” and write it again.</span></div>')
        if r["status"] == "running":
            draft_html.append('<article class="rc-draft">%s<p class="rc-role">Claude is writing the message…</p></article>' % who)
            continue
        if r["status"] == "error":
            draft_html.append("""<article class="rc-draft">%s<div role="alert" class="alert alert-error alert-soft rc-alert"><span>%s</span></div>
<form method="post" action="/outreach/%d" class="rc-actions"><div><button class="btn btn-sm btn-primary" name="act" value="regen">Try again</button></div>
<div><button class="btn btn-sm btn-ghost text-error" name="act" value="delete">Delete</button></div></form></article>""" % (who, e(r["error"]), oid))
            continue
        is_mail = "@" in (r["contact"] or "") and not (r["contact"] or "").startswith("http")
        open_btn = ('<a class="btn btn-sm btn-outline" href="%s" target="_blank" rel="noopener noreferrer">Open profile %s</a>' % (e(safe_url(r["contact"])), ic("open_in_new"))
                    if (r["contact"] or "").startswith("http") else "")
        send_btn = ('<button class="btn btn-sm btn-outline" name="act" value="send" title="Send to %s with the e-mail settings">Send e-mail</button>'
                    % e(r["contact"]) if is_mail and r["channel"] == "email" else "")
        draft_html.append("""<article class="rc-draft">%s<form method="post" action="/outreach/%d"><div class="rc-fields">
<div><label for="os%d">Subject</label><div class="join" style="width:100%%"><input class="input join-item" id="os%d" name="subject" value="%s" style="flex:1">
<button type="button" class="btn join-item" id="ocs%d" onclick="ocopy(%d, 1)">Copy</button></div></div>
<div><label for="om%d">Message</label><textarea class="textarea" id="om%d" name="message" rows="%d" oninput="ocount(%d,%d)">%s</textarea>
<div class="rc-count" id="on%d"></div><script>ocount(%d,%d)</script></div></div>
<div class="rc-actions"><div><button type="button" class="btn btn-sm btn-primary" id="oc%d" onclick="ocopy(%d)">Copy message</button>%s%s
<button class="btn btn-sm btn-success btn-soft" name="act" value="sent">Mark as sent</button></div>
<div><button class="btn btn-sm btn-ghost" name="act" value="save">Save edits</button><button class="btn btn-sm btn-ghost" name="act" value="regen">Rewrite</button>
<button class="btn btn-sm btn-ghost text-error" name="act" value="delete">Delete</button></div></div></form></article>""" % (
            who, oid, oid, oid, e(r["subject"] or ""), oid, oid, oid, oid, 4 if limit <= 300 else 9, oid, limit,
            e(r["message"] or ""), oid, oid, limit, oid, oid, open_btn, send_btn))
    drafts_sec = ('<section class="ui-section" aria-labelledby="dr-h"><header><h2 id="dr-h">Drafts</h2>'
                  '<span class="muted" style="font-size:13px">Edit, copy, send, then mark as sent.</span></header>%s</section>'
                  % "".join(draft_html)) if drafts else ""

    sent_rows, dialogs = [], []
    for r in sent:
        label = cvgen.OUTREACH_CHANNELS.get(r["channel"], ("?",))[0]
        oid, role = r["id"], outreach_role_label(r)
        sent_rows.append("""<tr onclick="oview(%d)"><td><button type="button" class="link link-hover" style="font-weight:600;text-align:left"
onclick="event.stopPropagation(); oview(%d)">%s</button><div class="muted" style="font-size:12.5px">%s</div></td>
<td class="hide-sm">%s</td><td class="hide-sm">%s</td><td class="when">%s</td></tr>""" % (
            oid, oid, e(r["name"]), e(r["title"] or ""), e(role or "—"), e(label), e(fmt_dt(r["sent_at"]))))
        open_btn = ('<a class="btn btn-sm btn-outline" href="%s" target="_blank" rel="noopener noreferrer">Open profile %s</a>' % (e(safe_url(r["contact"])), ic("open_in_new"))
                    if (r["contact"] or "").startswith("http") else "")
        dialogs.append("""<dialog class="modal" id="dlg%d" aria-labelledby="dlgt%d"><div class="modal-box rc-modal ui">
<form method="dialog"><button class="btn btn-sm btn-circle btn-ghost" style="position:absolute;right:10px;top:10px" aria-label="Close">%s</button></form>
<h3 id="dlgt%d">%s</h3><div class="sub">%s%s · %s · sent %s</div>
<label for="os%d">Subject</label><div class="join" style="width:100%%;margin-bottom:12px"><input class="input join-item" id="os%d" value="%s" readonly style="flex:1">
<button type="button" class="btn join-item" id="ocs%d" onclick="ocopy(%d, 1)">Copy</button></div>
<label for="om%d">Message</label><textarea class="textarea" id="om%d" rows="10" readonly>%s</textarea>
<form method="post" action="/outreach/%d" class="modal-action"><div><button type="button" class="btn btn-sm btn-primary" id="oc%d" onclick="ocopy(%d)">Copy message</button>%s</div>
<div><button class="btn btn-sm btn-ghost" name="act" value="unsent">Move back to drafts</button>
<button class="btn btn-sm btn-ghost text-error" name="act" value="delete">Delete</button></div></form></div>
<form method="dialog" class="modal-backdrop"><button aria-label="Close" tabindex="-1"></button></form></dialog>""" % (
            oid, oid, ic("close"), oid, e(r["name"]), e(r["title"] or ""), (" · " + e(role)) if role else "", e(label), e(fmt_dt(r["sent_at"])),
            oid, oid, e(r["subject"] or ""), oid, oid, oid, oid, e(r["message"] or ""), oid, oid, oid, open_btn))
    sent_sec = ("""<section class="ui-section" aria-labelledby="se-h"><header><h2 id="se-h">Sent</h2>
<span class="muted" style="font-size:13px">Select a row to see the message.</span></header>
<div style="overflow-x:auto"><table class="table rc-sent"><thead><tr><th>Recruiter</th><th class="hide-sm">Role</th><th class="hide-sm">Channel</th><th>Sent</th></tr></thead>
<tbody>%s</tbody></table></div></section>%s""" % ("".join(sent_rows), "".join(dialogs))) if sent else ""

    busy = any(r["status"] == "running" for r in rows)
    body = '<div class="ui rc">%s%s%s%s%s</div>%s' % (head, form, drafts_sec, sent_sec, OUTREACH_JS, OUTREACH_FORM_JS)
    return page("Recruiters", body, flash, refresh=3 if busy else 0)
