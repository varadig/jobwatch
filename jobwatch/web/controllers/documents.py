"""Documents: a posting's tailored CV / resume / cover letter, the Documents page, and the CV profile (with importing
an existing CV)."""
import json
import os
import urllib.parse

from jobwatch import cvgen
from jobwatch.db import database
from jobwatch.env import CV_DIR, DATA, DOCS_ROOT, FONT_DIR, PHOTO_PATH, PROFILE_PATH
from jobwatch.services.documents import (
    MAX_PDF_BYTES, applications, cv_filename, cv_paths, load_profile, owner_prefix, pdf_from_multipart, pdf_text,
    profile_from_cv_text
)
from jobwatch.web.components import page
from jobwatch.web.controllers.base import Controller
from jobwatch.web.http import Response
from jobwatch.web.views.documents import cv_page, docs_page, profile_page

PDF = "application/pdf"
DOC_TYPES = {".pdf": PDF, ".txt": "text/plain; charset=utf-8", ".json": "application/json; charset=utf-8"}


class DocumentsController(Controller):
    def register(self, router):
        router.get(r"/cv/([^/]+)", self.cv)
        router.get(r"/cv/([^/]+)/(cv|resume|letter)\.pdf", self.cv_pdf)
        router.post(r"/cv/([^/]+)/(generate|save|send|photo)", self.cv_action)
        router.get(r"/docs", self.docs)
        router.get(r"/docs/file", self.docs_file)
        router.get(r"/profile", self.profile)
        router.post(r"/profile", self.save_profile)
        router.post(r"/profile/import", self.import_text)
        router.post(r"/profile/import-pdf", self.import_pdf)
        router.get(r"/base-cv\.pdf", self.base_cv)

    # ---- a posting's documents
    def cv(self, req):
        return Response.html(cv_page(urllib.parse.unquote(req.params[0]), req.msg))

    def cv_pdf(self, req):
        jid, kind = urllib.parse.unquote(req.params[0]), req.params[1]
        with database.connect() as con:
            job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        path = cv_paths(jid)[kind]
        if not job or not path or not os.path.exists(path):
            return Response.html(page("Not found", "<p>No PDF yet.</p>"), 404)
        with open(path, "rb") as fh:
            return Response.file(fh.read(), PDF, inline_name=cv_filename(job, kind))

    def cv_action(self, req):
        jid, act = urllib.parse.unquote(req.params[0]), req.params[1]
        f = req.form
        back = "/cv/" + urllib.parse.quote(jid, safe="")
        try:
            if act == "generate":
                msg = applications.generate(jid, f.get("description", ""))
            elif act == "photo":
                msg = applications.set_photo(jid, f.get("photo") == "on")
            elif act == "save":
                msg = applications.save_edit(jid, f.get("data", ""))
            else:
                msg = applications.send(jid, f.get("to", ""), f.get("subject", ""), f.get("body", ""), f.get("attach"),
                                        "attach_letter" in f)
        except LookupError:
            return Response.redirect("/")
        return Response.redirect(back, msg)

    def docs(self, req):
        return Response.html(docs_page())

    def docs_file(self, req):
        """A generated file from the documents folder (never anything outside it)."""
        root = os.path.realpath(DOCS_ROOT)
        full = os.path.realpath(os.path.join(DOCS_ROOT, urllib.parse.unquote(req.query.get("p", ""))))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return Response.html(page("Not found", "<p>No such file.</p>"), 404)
        with open(full, "rb") as fh:
            return Response.file(fh.read(), DOC_TYPES.get(os.path.splitext(full)[1], "application/octet-stream"))

    # ---- the CV profile
    def profile(self, req):
        return Response.html(profile_page(req.msg))

    def save_profile(self, req):
        try:
            prof = json.loads(req.form.get("profile", ""))
            assert isinstance(prof, dict) and prof.get("name") and prof.get("experience")
        except Exception:
            return Response.redirect("/profile", "Invalid JSON, nothing saved.")
        os.makedirs(DATA, exist_ok=True)
        with open(PROFILE_PATH, "w", encoding="utf-8") as fh:
            json.dump(prof, fh, ensure_ascii=False, indent=2)
        return Response.redirect("/profile", "Saved.")

    def import_text(self, req):
        return self._import(lambda: req.form.get("cv_text", ""))

    def import_pdf(self, req):
        if int(req.headers.get("Content-Length") or 0) > MAX_PDF_BYTES + 4096:
            return Response.html(profile_page("The file is too large (max. 10 MB)."))
        data = pdf_from_multipart(req.headers.get("Content-Type", ""), req.body())
        if not data:
            return Response.html(profile_page("No PDF file received."))
        if len(data) > MAX_PDF_BYTES:
            return Response.html(profile_page("The file is too large (max. 10 MB)."))

        def text():
            try:
                return pdf_text(data)
            except Exception as ex:
                raise ValueError("PDF reading error: %s" % ex)
        return self._import(text)

    @staticmethod
    def _import(text_source):
        """Claude turns an existing CV into the profile; it is shown in the editor to check before saving."""
        try:
            prefill = profile_from_cv_text(text_source())
            return Response.html(profile_page("Check the preview, then save it.", prefill=prefill))
        except ValueError as ex:
            return Response.html(profile_page("Failed: %s" % ex))

    def base_cv(self, req):
        """The CV from the profile, not tailored to a posting (?photo=0: without the photo)."""
        os.makedirs(CV_DIR, exist_ok=True)
        photo = req.query.get("photo", "1") == "1"
        out = os.path.join(CV_DIR, "base-%s.pdf" % ("photo" if photo else "nophoto"))
        prof = load_profile()
        cvgen.render_cv_pdf(out, prof, prof, FONT_DIR, PHOTO_PATH if photo else None)
        with open(out, "rb") as fh:
            return Response.file(fh.read(), PDF, inline_name="%s_CV%s.pdf" % (owner_prefix(), "" if photo else "_no_photo"))
