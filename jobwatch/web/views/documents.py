"""The CV page of a posting, the Documents page and the CV profile page."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from jobwatch import cvgen
from jobwatch.db import database
from jobwatch.env import DOCS_ROOT
from jobwatch.net import http
from jobwatch.services.documents import DOC_LABEL, cv_paths, doc_names, load_profile, smb_path
from jobwatch.services.logos import logo_files
from jobwatch.util.text import e, safe_url, strip_tags
from jobwatch.util.timeutil import fmt_dt
from jobwatch.web.components import ic, logo_html, page, wm_html


def cv_page(jid, flash=""):
    with database.connect() as con:
        r = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not r:
            return page("Not found", "<p>No such posting.</p>")
        job = dict(r)
        cv = con.execute("SELECT * FROM cvs WHERE job_id=?", (jid,)).fetchone()
    qid = e(urllib.parse.quote(jid, safe=""))
    head = """<div class="ui-head"><a href="/" style="font-size:13px">%s Jobs</a><div class="job-head" style="margin-top:6px">%s<div><h1>%s</h1>
<p style="margin-top:2px">%s · %s · %s · <b>%d points</b> · <a href="%s" target="_blank" rel="noopener">posting %s</a></p></div></div>
<div class="ui-btns" style="margin-top:12px"><a class="btn btn-sm btn-outline" href="/outreach?job=%s">Message a recruiter about this posting</a></div></div>""" % (
        ic("arrow_back"), logo_html(job["company"], logo_files(), 52), e(job["title"]), e(job["company"] or "?"),
        e(job["location"] or "-"), wm_html(job.get("work_mode") or "unknown"), job["score"], e(safe_url(job["url"])),
        ic("open_in_new"), qid)
    if cv and cv["status"] == "running":
        return page("Generating CV", '<div class="ui">%s<section class="ui-section"><div class="ui-body ui-empty">'
                    '<span class="loading loading-spinner loading-md"></span><p>Claude is writing the CV, Resume and cover letter for this posting '
                    '(about 1–2 minutes). The page refreshes by itself.</p></div></section></div>' % head, flash, refresh=5)
    desc = cv["description"] if cv and cv["description"] else cvgen.fetch_description(http.get, strip_tags, job)
    done = bool(cv and cv["status"] == "done")
    gen_form = """<section class="ui-section"><header><div><h2>%s</h2><p class="ui-desc">Claude works from this posting text: fix or extend it if
something is missing. Only facts from your CV profile go in; your contact details are not sent to the API.</p></div></header>
<form method="post" action="/cv/%s/generate"><div class="ui-body">%s<label for="description">Posting text</label>
<textarea class="textarea" id="description" name="description" rows="12">%s</textarea></div>
<div class="ui-foot"><span class="muted" style="font-size:13px">About 5–15 cents.</span><button class="btn %s">%s</button></div></form></section>""" % (
        "Write it again" if done else "Create documents for this posting", qid,
        ('<div role="alert" class="alert alert-error alert-soft" style="margin-bottom:12px"><span>%s</span></div>' % e(cv["error"]))
        if cv and cv["status"] == "error" else "",
        e(desc), "btn-outline" if done else "btn-primary",
        "Regenerate the documents" if done else "Write CV + Resume + cover letter")
    if not done:
        return page("CV", '<div class="ui">%s%s</div>' % (head, gen_form), flash)
    data = json.loads(cv["data"])
    notes = "".join("<li>%s</li>" % e(n) for n in data.get("match_notes", []))
    emails = cvgen.find_emails(desc)
    sent = ('<div role="status" class="alert alert-success alert-soft" style="margin-bottom:12px"><span>Sent %s → %s</span></div>'
            % (e(fmt_dt(cv["sent_at"])), e(cv["sent_to"]))) if cv["sent_at"] else ""
    paths = cv_paths(jid)
    folder = smb_path(os.path.dirname(paths["cv"])) if paths["cv"] else ""
    photo = data.get("_photo", True)
    docs = """<section class="ui-section"><header><div><h2>Documents</h2><p class="ui-desc">Written by %s, %s.</p></div></header>
<div class="ui-body"><div class="ui-btns"><a class="btn btn-sm btn-primary" href="/cv/%s/cv.pdf" target="_blank">CV (detailed)</a>
<a class="btn btn-sm btn-primary" href="/cv/%s/resume.pdf" target="_blank">Resume (1 page)</a>
<a class="btn btn-sm btn-outline" href="/cv/%s/letter.pdf" target="_blank">Cover letter</a></div>
<form method="post" action="/cv/%s/photo" class="ui-inline"><span>Photo on the CV: <b>%s</b></span>
<button class="btn btn-xs btn-ghost" name="photo" value="%s">%s</button>
<span class="help">By default no photo for US, UK, Canadian and Australian postings.</span></form>
<h3 class="ui-h3">%s</h3><p>%s</p>
<h3 class="ui-h3">Notes for you</h3><ul class="ui-list">%s</ul>
<p class="help">Folder (on a Mac: Finder → Go → Connect to Server): <code>%s</code></p></div></section>""" % (
        e(data.get("_model", "") or "Claude"), e(fmt_dt(cv["created"])), qid, qid, qid, qid,
        "yes" if photo else "no", "off" if photo else "on", "Remove the photo" if photo else "Add a photo",
        e(data["headline"]), e(data["summary"]), notes, e(folder))
    send = """<section class="ui-section"><header><div><h2>Send by e-mail</h2><p class="ui-desc">%s Nothing is sent until you press
the button and confirm. It goes from the e-mail account in Settings.</p></div></header>
<form method="post" action="/cv/%s/send" onsubmit="return confirm('Send the application to ' + this.to.value + '?')"><div class="ui-body">%s<div class="ui-grid">
<div><label for="to">Recipient</label><input class="input" id="to" name="to" value="%s" required type="email"></div>
<div><label for="subject">Subject</label><input class="input" id="subject" name="subject" value="%s"></div>
<div class="wide"><label for="body">Message</label><textarea class="textarea" id="body" name="body" rows="12">%s</textarea></div>
<div class="wide"><label>Attachments</label><div class="ui-checks">
<label><input type="radio" class="radio radio-sm radio-primary" name="attach" value="cv" checked> CV</label>
<label><input type="radio" class="radio radio-sm radio-primary" name="attach" value="resume"> Resume</label>
<label><input type="radio" class="radio radio-sm radio-primary" name="attach" value="both"> Both</label>
<label><input type="checkbox" class="checkbox checkbox-sm checkbox-primary" name="attach_letter"> Cover letter as a PDF too</label></div></div>
</div></div><div class="ui-foot"><span></span><button class="btn btn-primary">Send the application</button></div></form></section>""" % (
        ("E-mail addresses in the posting: " + e(", ".join(emails)) + ".") if emails else
        "The posting has no e-mail address; most companies want the PDF uploaded on their application form.",
        qid, sent, e(emails[0] if emails else ""), e(data.get("email_subject", "")), e(data["cover_letter"]))
    edit = """<section class="ui-section"><header><div><h2>Edit by hand</h2><p class="ui-desc">The CV, the Resume (<code>resume</code>) and the
cover letter in JSON. Saving rebuilds the PDFs, without an API call.</p></div></header>
<form method="post" action="/cv/%s/save"><div class="ui-body"><textarea class="textarea ui-mono" name="data" rows="18" aria-label="Document content (JSON)">%s</textarea></div>
<div class="ui-foot"><span></span><button class="btn btn-outline">Save and rebuild the PDFs</button></div></form></section>""" % (
        qid, e(json.dumps({k: v for k, v in data.items() if not k.startswith("_")}, ensure_ascii=False, indent=2)))
    return page("CV", '<div class="ui">%s%s%s%s%s</div>' % (head, docs, send, gen_form, edit), flash)


def docs_page():
    if not os.path.isdir(DOCS_ROOT):
        return page("Documents", '<div class="ui"><div class="ui-head"><h1>Documents</h1></div><section class="ui-section">'
                                 '<div class="ui-body ui-empty">No documents yet. On a posting in the Jobs list, press “CV + Resume” '
                                 'to create the first one.</div></section></div>')
    with database.connect() as con:
        sent = {r["job_id"]: r for r in con.execute("SELECT job_id, sent_to, sent_at FROM cvs")}
    blocks = []
    for co in sorted(os.listdir(DOCS_ROOT), key=str.lower):
        cdir = os.path.join(DOCS_ROOT, co)
        if not os.path.isdir(cdir):
            continue
        jobs_html = []
        for jd in sorted(os.listdir(cdir)):
            d = os.path.join(cdir, jd)
            if not os.path.isdir(d):
                continue
            info = {}
            try:
                with open(os.path.join(d, "info.json"), encoding="utf-8") as fh:
                    info = json.load(fh)
            except Exception:
                pass
            rel = os.path.relpath(d, DOCS_ROOT)
            files = "".join('<a class="btn btn-sm btn-outline" href="/docs/file?p=%s" target="_blank">%s</a>' % (
                e(urllib.parse.quote(os.path.join(rel, fn))), e(DOC_LABEL[k])) for k, fn in doc_names().items()
                if os.path.exists(os.path.join(d, fn)))
            if os.path.exists(os.path.join(d, "hirdetes.txt")):
                files += '<a class="btn btn-sm btn-ghost" href="/docs/file?p=%s" target="_blank">Posting text</a>' % e(
                    urllib.parse.quote(os.path.join(rel, "hirdetes.txt")))
            older = len(os.listdir(os.path.join(d, "korabbi"))) if os.path.isdir(os.path.join(d, "korabbi")) else 0
            s_ = sent.get(info.get("job_id"))
            status = ('<span class="badge badge-soft badge-success">Sent %s → %s</span>' % (e(fmt_dt(s_["sent_at"])), e(s_["sent_to"]))
                      if s_ and s_["sent_at"] else "")
            jobs_html.append("""<div class="ui-row"><div class="ui-row-main"><div class="ui-row-title">%s %s</div>
<div class="muted" style="font-size:13px">Generated %s%s · %s · <a href="%s" target="_blank" rel="noopener">posting %s</a></div>
<div class="ui-btns" style="margin-top:8px">%s</div></div><div>%s</div></div>""" % (
                e(info.get("title") or jd), status,
                e(fmt_dt(info.get("generated"))), (" · %d earlier version(s)" % older) if older else "",
                wm_html(info.get("work_mode") or "unknown"), e(safe_url(info.get("url", ""))), ic("open_in_new"), files,
                ('<a class="btn btn-sm btn-primary" href="/cv/%s">Open</a>' % e(urllib.parse.quote(info["job_id"], safe="")))
                if info.get("job_id") else ""))
        if jobs_html:
            blocks.append('<section class="ui-section"><header><h2>%s</h2><span class="muted" style="font-size:13px">%d posting%s</span></header>%s</section>'
                          % (e(co), len(jobs_html), "" if len(jobs_html) == 1 else "s", "".join(jobs_html)))
    head = """<div class="ui-head"><h1>Documents</h1><p>Every tailored CV, Resume and cover letter, grouped by company. When you regenerate,
the old version is kept in the <code>korabbi/</code> subfolder. On a Mac: Finder → Go → Connect to Server → <code>%s</code></p></div>""" % e(smb_path(DOCS_ROOT))
    empty = ('<section class="ui-section"><div class="ui-body ui-empty">No documents yet. On a posting in the Jobs list, '
             'press “CV + Resume” to create the first one.</div></section>')
    return page("Documents", '<div class="ui">%s%s</div>' % (head, "".join(blocks) or empty))


def profile_page(flash="", prefill=None):
    current = prefill if prefill is not None else json.dumps(load_profile(), ensure_ascii=False, indent=2)
    body = """<div class="ui"><div class="ui-head"><h1>CV profile</h1><p>The facts Claude works from: only what is here can go into a CV,
cover letter or recruiter message. The <code>contact</code> part is never sent to the API; it only goes into the PDF header.</p>
<div class="ui-btns" style="margin-top:12px"><a class="btn btn-sm btn-primary" href="/base-cv.pdf?photo=1" target="_blank">Base CV with photo (PDF)</a>
<a class="btn btn-sm btn-outline" href="/base-cv.pdf?photo=0" target="_blank">Base CV without photo (for the US)</a></div></div>
<section class="ui-section"><header><div><h2>Create it from your existing CV</h2><p class="ui-desc">Claude reads the CV (contact details included)
and fills in the editor below. Nothing is saved until you check it and press Save. Needs the API key (Settings → Claude API).</p></div></header>
<div class="ui-body"><div class="ui-grid">
<form class="wide" method="post" action="/profile/import-pdf" enctype="multipart/form-data"><label for="pdf">From a PDF</label>
<div class="join" style="width:100%%"><input class="file-input join-item" style="flex:1" type="file" id="pdf" name="pdf" accept="application/pdf">
<button class="btn btn-primary join-item">Read the PDF</button></div></form>
<form class="wide" method="post" action="/profile/import"><label for="cv_text">…or from text</label>
<textarea class="textarea" id="cv_text" name="cv_text" rows="5" placeholder="Paste the text of your CV here"></textarea>
<div style="margin-top:8px"><button class="btn btn-sm btn-primary btn-outline">Read the text</button></div></form>
</div></div></section>
<section class="ui-section"><header><div><h2>Editor</h2><p class="ui-desc">JSON: name, headline, contact, summary, skills, experience, languages, objective.</p></div></header>
<form method="post" action="/profile"><div class="ui-body"><textarea class="textarea ui-mono" name="profile" rows="28" aria-label="CV profile (JSON)">%s</textarea></div>
<div class="ui-foot"><span class="muted" style="font-size:13px">Saved only when you press Save.</span><button class="btn btn-primary">Save</button></div></form></section></div>""" % (
        e(current))
    return page("CV profile", body, flash)
