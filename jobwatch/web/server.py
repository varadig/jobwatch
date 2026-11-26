"""The HTTP server: login, routing, and the request handler."""
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from datetime import timedelta
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

from jobwatch import cvgen

from jobwatch.env import CV_DIR, DATA, DOCS_ROOT, FONT_DIR, LOGO_DIR, PHOTO_PATH, PORT, PROFILE_PATH, STATIC_DIR, log
from jobwatch.models import STATUS_LABEL
from jobwatch.repositories.jobs import JobRepository
from jobwatch.services.documents import MAX_PDF_BYTES, backfill_docs, cv_filename, cv_generate, cv_paths, load_profile, owner_prefix, pdf_from_multipart, pdf_text, profile_from_cv_text, render_cv_files, restore_cv_jobs, write_job_info
from jobwatch.services.enrich import enrich_work_modes
from jobwatch.services.interviews.core import interview_cfg
from jobwatch.services.interviews.ics import ics_filename, interviews_ics
from jobwatch.services.interviews.loop import interview_loop
from jobwatch.services.logos import LOGO_TYPES, logo_files

from jobwatch.services.outreach import linkedin_preview, outreach_action, outreach_create
from jobwatch.services.postings import add_job_by_link, posting_preview
from jobwatch.services.refresh import refresher, reset_job_cache, run_selection, scheduler
from jobwatch.services.settings import preset_action, save_settings, saved_message
from jobwatch.services.state import STATE
from jobwatch.sources.custom import test_custom_sources
from jobwatch.sources.mail import test_mail_source
from jobwatch.util.text import e, safe_url
from jobwatch.util.timeutil import now_iso
from jobwatch.web.components import REQ, page, with_msg
from jobwatch.web.views.documents import cv_page, docs_page, profile_page
from jobwatch.web.views.interviews import interview_page, interview_post, interviews_page
from jobwatch.web.views.jobs import job_detail_html, job_page, jobs_page
from jobwatch.web.views.outreach import outreach_page
from jobwatch.web.views.settings import settings_page
from jobwatch.web.views.sources import sources_page
from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.services.notify import notifier


AUTH_PASSWORD = os.environ.get("JOBWATCH_PASSWORD", "")


AUTH_PUBLIC_PATHS = ("/health",)  # the platform's health check must get through without a password


ICON_FILES = {"/favicon.ico": ("favicon.ico", "image/x-icon"), "/favicon.svg": ("favicon.svg", "image/svg+xml"),
              "/material-symbols.woff2": ("material-symbols.woff2", "font/woff2"),
              "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"), "/icon-192.png": ("icon-192.png", "image/png")}


MANIFEST = json.dumps({"name": "jobwatch", "short_name": "jobwatch", "start_url": "/", "display": "standalone",
                       "background_color": "#1d232a", "theme_color": "#5b5bd6",
                       "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                                 {"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml"}]}).encode()


AUTH_PUBLIC_PATHS = AUTH_PUBLIC_PATHS + tuple(ICON_FILES) + ("/manifest.webmanifest",)


def auth_ok(header, password):
    """True if no password is set, or the Basic auth header carries it (the user name is ignored)."""
    if not password:
        return True
    import base64
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except Exception:
        return False
    given = decoded.partition(":")[2]
    return secrets.compare_digest(given.encode("utf-8"), password.encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def require_login(self):
        """Sends 401 and returns False unless the request is allowed."""
        path = urllib.parse.urlparse(self.path).path
        if path in AUTH_PUBLIC_PATHS or re.fullmatch(r"/calendar/[A-Za-z0-9_-]{20,}\.ics", path) or auth_ok(self.headers.get("Authorization"), AUTH_PASSWORD):
            return True
        body = "Login required.".encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="jobwatch", charset="UTF-8"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return False

    def send_html(self, s, code=200):
        b = s.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def send_bytes(self, data, ctype, filename=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if filename:
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def form(self):
        n = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[-1] for k, v in urllib.parse.parse_qs(data, keep_blank_values=True).items()}

    def back(self):
        ref = self.headers.get("Referer") or "/"
        p = urllib.parse.urlparse(ref)
        return (p.path or "/") + ("?" + p.query if p.query else "")

    def do_GET(self):
        if not self.require_login():
            return
        u = urllib.parse.urlparse(self.path)
        REQ.path = u.path
        q = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        if u.path in ICON_FILES or u.path == "/manifest.webmanifest":
            if u.path == "/manifest.webmanifest":
                data, ctype = MANIFEST, "application/manifest+json"
            else:
                fname, ctype = ICON_FILES[u.path]
                with open(os.path.join(STATIC_DIR, fname), "rb") as fh:
                    data = fh.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=604800")
            self.end_headers()
            return self.wfile.write(data)
        try:
            if u.path == "/":
                return self.send_html(jobs_page(q))
            if u.path == "/sources":
                return self.send_html(sources_page(q.get("msg", "")))
            if u.path == "/interviews":
                return self.send_html(interviews_page(q, q.get("msg", "")))
            m = re.fullmatch(r"/interviews/(\d+)(\.ics)?", u.path)
            if m:
                if m.group(2):
                    with database.connect() as con:
                        rows = [dict(r) for r in con.execute("SELECT * FROM interviews WHERE id=?", (int(m.group(1)),))]
                    with config_store.lock:
                        cfg_ = config_store.load()
                    return self.send_bytes(interviews_ics(rows, cfg_).encode("utf-8"), "text/calendar; charset=utf-8",
                                           ics_filename(rows[0]) if rows else "Interview.ics")
                if q.get("partial"):
                    return self.send_html(interview_page(int(m.group(1)), modal=True))
                return self.send_html(interview_page(int(m.group(1)), q.get("msg", "")))
            m = re.fullmatch(r"/calendar/([A-Za-z0-9_-]{20,})\.ics", u.path)
            if m:
                with config_store.lock:
                    cfg_ = config_store.load()
                if not secrets.compare_digest(m.group(1), interview_cfg(cfg_).get("cal_token") or "-"):
                    return self.send_html("Not found", 404)
                with database.connect() as con:
                    rows = [dict(r) for r in con.execute("SELECT * FROM interviews WHERE status<>'cancelled' OR "
                                                          "starts > ? ORDER BY starts", (
                                                              (datetime.now() - timedelta(days=60)).isoformat(),))]
                return self.send_bytes(interviews_ics(rows, cfg_).encode("utf-8"), "text/calendar; charset=utf-8")
            if u.path == "/outreach":
                return self.send_html(outreach_page(q, q.get("msg", "")))
            if u.path == "/outreach/lookup":
                body = json.dumps(linkedin_preview(q.get("url", "")), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            if u.path == "/settings":
                return self.send_html(settings_page(q.get("msg", "")))
            m = re.match(r"^/logo/([a-z0-9]{1,200})$", u.path)
            if m:
                fname = logo_files().get(m.group(1))
                path = os.path.join(LOGO_DIR, fname) if fname else ""
                if not path or not os.path.isfile(path):
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                ctype = {v: k for k, v in LOGO_TYPES.items()}.get(os.path.splitext(fname)[1], "application/octet-stream")
                data = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "max-age=604800")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                return self.wfile.write(data)
            if u.path.startswith("/open/"):
                jid = urllib.parse.unquote(u.path[6:])
                with database.connect() as con:
                    r = con.execute("SELECT url,status FROM jobs WHERE id=?", (jid,)).fetchone()
                    if r and r["status"] == "new":
                        JobRepository(con).set_status(jid, "seen")
                return self.redirect(safe_url(r["url"]) if r else "/")
            if u.path == "/profile":
                return self.send_html(profile_page(q.get("msg", "")))
            if u.path == "/base-cv.pdf":
                os.makedirs(CV_DIR, exist_ok=True)
                photo = q.get("photo", "1") == "1"
                out = os.path.join(CV_DIR, "base-%s.pdf" % ("photo" if photo else "nophoto"))
                prof = load_profile()
                cvgen.render_cv_pdf(out, prof, prof, FONT_DIR, PHOTO_PATH if photo else None)
                b = open(out, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'inline; filename="%s_CV%s.pdf"' % (owner_prefix(), "" if photo else "_no_photo"))
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if u.path == "/docs":
                return self.send_html(docs_page())
            if u.path == "/docs/file":
                full = os.path.realpath(os.path.join(DOCS_ROOT, urllib.parse.unquote(q.get("p", ""))))
                if not full.startswith(os.path.realpath(DOCS_ROOT) + os.sep) or not os.path.isfile(full):
                    return self.send_html(page("Not found", "<p>No such file.</p>"), 404)
                ctype = {".pdf": "application/pdf", ".txt": "text/plain; charset=utf-8",
                         ".json": "application/json; charset=utf-8"}.get(os.path.splitext(full)[1], "application/octet-stream")
                b = open(full, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            m = re.match(r"^/job/([^/]+)(/preview)?$", u.path)
            if m:
                jid = urllib.parse.unquote(m.group(1))
                if m.group(2):
                    h = posting_preview(jid)
                    return self.send_html(h or "", 200 if h else 404)
                if q.get("partial"):
                    return self.send_html(job_detail_html(jid, modal=True)[1])
                back = self.back()
                return self.send_html(job_page(jid, back if not back.startswith("/job/") else "/", q.get("msg", "")))
            m = re.match(r"^/cv/([^/]+?)(?:/(cv|resume|letter)\.pdf)?$", u.path)
            if m:
                jid = urllib.parse.unquote(m.group(1))
                if not m.group(2):
                    return self.send_html(cv_page(jid, q.get("msg", "")))
                with database.connect() as con:
                    job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                path = cv_paths(jid)[m.group(2)]
                if not job or not path or not os.path.exists(path):
                    return self.send_html(page("Not found", "<p>No PDF yet.</p>"), 404)
                b = open(path, "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'inline; filename="%s"' % cv_filename(job, m.group(2)))
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                return self.wfile.write(b)
            if u.path == "/api/jobs":
                with database.connect() as con:
                    rows = [dict(r) for r in con.execute(
                        "SELECT id,source,title,company,location,url,posted,score,matched,first_seen,status "
                        "FROM jobs ORDER BY first_seen DESC LIMIT 500")]
                b = json.dumps(rows, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                return self.wfile.write(b)
            if u.path == "/health":
                return self.send_html("ok")
            self.send_html(page("Not found", "<p>404</p>"), 404)
        except Exception as ex:
            log.exception("GET hiba")
            self.send_html(page("Hiba", "<p class=err>%s</p>" % e(ex)), 500)

    def profile_import_result(self, text_source, flash_ok):
        try:
            prefill = profile_from_cv_text(text_source())
            return self.send_html(profile_page(flash_ok, prefill=prefill))
        except ValueError as ex:
            return self.send_html(profile_page("Failed: %s" % ex))

    def profile_import_text(self, text):
        return self.profile_import_result(lambda: text, "Check the preview, then save it.")

    def profile_import_pdf(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_PDF_BYTES + 4096:
            return self.send_html(profile_page("The file is too large (max. 10 MB)."))
        body = self.rfile.read(length)
        data = pdf_from_multipart(self.headers.get("Content-Type", ""), body)
        if not data:
            return self.send_html(profile_page("No PDF file received."))
        if len(data) > MAX_PDF_BYTES:
            return self.send_html(profile_page("The file is too large (max. 10 MB)."))
        def source():
            try:
                return pdf_text(data)
            except Exception as ex:
                raise ValueError("PDF reading error: %s" % ex)
        return self.profile_import_result(source, "Check the preview, then save it.")

    def do_POST(self):
        if not self.require_login():
            return
        u = urllib.parse.urlparse(self.path)
        REQ.path = u.path
        try:
            if u.path == "/profile/import-pdf":
                return self.profile_import_pdf()
            f = self.form()
            if u.path == "/profile/import":
                return self.profile_import_text(f.get("cv_text", ""))
            if u.path == "/interviews" or u.path.startswith("/interviews/"):
                to, msg = interview_post(u.path, f)
                path_, _, frag = to.partition("#")
                return self.redirect(path_ + "?msg=" + urllib.parse.quote(msg) + ("#" + frag if frag else ""))
            if u.path == "/reset-jobs":
                reset_job_cache()
                if f.get("refetch"):
                    threading.Thread(target=refresher.run, kwargs={"force": True}, daemon=True).start()
                    msg = "Job cache cleared and a new search started: the list fills up again in a few minutes."
                else:
                    msg = "Job cache cleared. Press Refresh all to fetch again."
                return self.redirect(with_msg(self.back(), msg + " The postings you made a CV for stay, marked applied."))
            if u.path == "/run":
                only, cids = run_selection(f)
                if only is None and not cids and not f.get("all") and ("one" in f or "selected" in f):
                    return self.redirect("/sources?msg=" + urllib.parse.quote("Tick at least one source to refresh."))
                if STATE["running"]:
                    return self.redirect(self.back() + ("&" if "?" in self.back() else "?") + "msg=" +
                                         urllib.parse.quote("A refresh is already running; try again when it has finished."))
                threading.Thread(target=refresher.run, kwargs={"force": True, "only": only, "custom_ids": cids}, daemon=True).start()
                time.sleep(0.3)
                return self.redirect(self.back())
            if u.path.startswith("/job/"):
                jid = urllib.parse.unquote(u.path[5:])
                st = f.get("status")
                if st in STATUS_LABEL:
                    with database.connect() as con:
                        JobRepository(con).set_status(jid, st)
                wm = f.get("work_mode")
                if wm in ("remote", "hybrid", "onsite"):   # set by hand: never overwritten, remembered
                    with database.connect() as con:
                        con.execute("UPDATE jobs SET work_mode=?, mode_src='manual', mode_note='set by you' WHERE id=?", (wm, jid))
                        JobRepository(con).set_manual_mode(jid, wm)
                elif wm == "auto":                           # back to automatic: decided again in the background
                    with database.connect() as con:
                        JobRepository(con).set_manual_mode(jid, None)
                        con.execute("UPDATE jobs SET work_mode='unknown', mode_src=NULL, mode_note=NULL, mode_checked=NULL WHERE id=?", (jid,))
                    with config_store.lock:
                        cfg_ = config_store.load()
                    threading.Thread(target=enrich_work_modes, args=(cfg_,), daemon=True).start()
                nxt = f.get("next", "")
                if st in ("hidden", "seen") and nxt.startswith("/") and not nxt.startswith("//") and not nxt.startswith("/job/"):
                    return self.redirect(nxt)   # done with it on its page: back to the list
                return self.redirect(self.back())
            if u.path == "/profile":
                try:
                    prof = json.loads(f.get("profile", ""))
                    assert isinstance(prof, dict) and prof.get("name") and prof.get("experience")
                except Exception:
                    return self.redirect("/profile?msg=" + urllib.parse.quote("Invalid JSON, nothing saved."))
                os.makedirs(DATA, exist_ok=True)
                with open(PROFILE_PATH, "w", encoding="utf-8") as fh:
                    json.dump(prof, fh, ensure_ascii=False, indent=2)
                return self.redirect("/profile?msg=" + urllib.parse.quote("Saved."))
            m = re.match(r"^/cv/([^/]+)/(generate|save|send|photo)$", u.path)
            if m:
                jid, act = urllib.parse.unquote(m.group(1)), m.group(2)
                back = "/cv/" + urllib.parse.quote(jid, safe="")
                with database.connect() as con:
                    job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
                    if not job:
                        return self.redirect("/")
                    job = dict(job)
                    if act == "generate":
                        desc = f.get("description", "")[:20000]
                        con.execute("INSERT INTO cvs(job_id,status,description) VALUES(?, 'running', ?) "
                                    "ON CONFLICT(job_id) DO UPDATE SET status='running', error=NULL, description=excluded.description",
                                    (jid, desc))
                if act == "generate":
                    threading.Thread(target=cv_generate, args=(jid, desc), daemon=True).start()
                    return self.redirect(back)
                if act == "photo":
                    with database.connect() as con:
                        data = json.loads(con.execute("SELECT data FROM cvs WHERE job_id=?", (jid,)).fetchone()["data"])
                        data["_photo"] = f.get("photo") == "on"
                        render_cv_files(jid, data)
                        con.execute("UPDATE cvs SET data=? WHERE job_id=?", (json.dumps(data, ensure_ascii=False), jid))
                    return self.redirect(back)
                if act == "save":
                    try:
                        data = json.loads(f.get("data", ""))
                        for k in ("headline", "summary", "experience", "skills", "cover_letter"):
                            assert k in data
                        with database.connect() as con:
                            old = json.loads(con.execute("SELECT data FROM cvs WHERE job_id=?", (jid,)).fetchone()["data"])
                        data["_model"] = old.get("_model", "").replace(" (edited by hand)", "") + " (edited by hand)"
                        data["_photo"] = old.get("_photo", True)
                        render_cv_files(jid, data)
                        with database.connect() as con:
                            con.execute("UPDATE cvs SET data=? WHERE job_id=?", (json.dumps(data, ensure_ascii=False), jid))
                        msg = "Saved, PDF rebuilt."
                    except Exception as ex:
                        msg = "Not saved: %s" % ex
                    return self.redirect(back + "?msg=" + urllib.parse.quote(msg))
                if act == "send":
                    to = f.get("to", "").strip()
                    if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):
                        return self.redirect(back + "?msg=" + urllib.parse.quote("Invalid recipient."))
                    paths = cv_paths(jid)
                    kinds = {"cv": ["cv"], "resume": ["resume"], "both": ["cv", "resume"]}.get(f.get("attach"), ["cv"])
                    if "attach_letter" in f:
                        kinds.append("letter")
                    att = [(cv_filename(job, k), open(paths[k], "rb").read()) for k in kinds]
                    with config_store.lock:
                        cfg = config_store.load()
                    try:
                        notifier.email(cfg, f.get("subject", "").strip() or "Application: " + job["title"],
                                   f.get("body", ""), to=to, attachments=att)
                        with database.connect() as con:
                            con.execute("UPDATE cvs SET sent_to=?, sent_at=? WHERE job_id=?", (to, now_iso(), jid))
                            JobRepository(con).set_status(jid, "applied")
                            cvrow = con.execute("SELECT * FROM cvs WHERE job_id=?", (jid,)).fetchone()
                        try:
                            write_job_info(os.path.dirname(paths["cv"]), job, json.loads(cvrow["data"]), cvrow)
                        except Exception:
                            log.exception("info.json update")
                        msg = "Sent: %s" % to
                    except Exception as ex:
                        msg = "Sending error: %s" % ex
                    return self.redirect(back + "?msg=" + urllib.parse.quote(msg))
            if u.path == "/outreach/generate":
                return self.redirect("/outreach?msg=" + urllib.parse.quote(outreach_create(f)))
            m = re.match(r"^/outreach/(\d+)$", u.path)
            if m:
                return self.redirect("/outreach?msg=" + urllib.parse.quote(outreach_action(int(m.group(1)), f)))
            if u.path == "/add-job":
                jid, msg = add_job_by_link(f.get("url", ""))
                if jid:
                    return self.redirect("/cv/%s?msg=%s" % (urllib.parse.quote(jid, safe=""), urllib.parse.quote(msg)))
                return self.redirect("/?msg=" + urllib.parse.quote(msg))
            if u.path == "/mark-all-seen":
                with database.connect() as con:
                    for r in con.execute("SELECT id FROM jobs WHERE status='new'").fetchall():
                        JobRepository(con).set_status(r["id"], "seen")
                return self.redirect(self.back())
            if u.path == "/settings":
                msg = urllib.parse.quote(saved_message(save_settings(f)))
                nxt = f.get("next", "")
                if nxt.startswith("/") and not nxt.startswith("//"):  # saved from the "unsaved changes" dialog
                    return self.redirect(nxt + ("&" if "?" in nxt else "?") + "msg=" + msg)
                return self.redirect("/settings?msg=" + msg)
            if u.path == "/preset":
                return self.redirect("/settings?msg=" + urllib.parse.quote(preset_action(f) or "Nothing to do.") + "#presets")
            if u.path == "/test":
                if "ntfy_server" in f:  # sent from the settings form: save what is on the screen first
                    save_settings(f)
                with config_store.lock:
                    cfg = config_store.load()
                if f.get("what") == "custom":
                    return self.redirect("/settings?msg=" + urllib.parse.quote(test_custom_sources(cfg)[:1500]))
                if f.get("what") == "mail":
                    return self.redirect("/settings?msg=" + urllib.parse.quote(test_mail_source(cfg)[:1500]) + "#mail")
                try:
                    if f.get("what") == "email":
                        notifier.email(cfg, "jobwatch test", "E-mail notifications work.",
                                   "<p>E-mail notifications work. ✅</p>")
                    else:
                        notifier.push(cfg, "jobwatch test", "Push notifications work ✅",
                                  click=cfg.get("public_url"))
                    msg = "Test sent."
                except Exception as ex:
                    msg = "Test error: %s" % ex
                return self.redirect("/settings?msg=" + urllib.parse.quote(msg))
            self.send_html(page("Not found", "<p>404</p>"), 404)
        except Exception as ex:
            log.exception("POST hiba")
            self.send_html(page("Hiba", "<p class=err>%s</p>" % e(ex)), 500)


def main():
    database.init()
    database.migrate()
    restore_cv_jobs()
    threading.Thread(target=backfill_docs, daemon=True).start()
    with config_store.lock:
        cfg = config_store.load()
    log.info("jobwatch starting: port %d, ntfy topic: %s", PORT, cfg["ntfy"]["topic"])
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=interview_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
