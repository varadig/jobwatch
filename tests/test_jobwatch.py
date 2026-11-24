"""Tests for jobwatch. Run from the repo root:  python3 -m unittest discover -s tests -v"""
import importlib
import io
import json
import os
import contextlib
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
# app.py reads its data folder when it is imported, so point it at a throwaway one.
os.environ.setdefault("JOBWATCH_DATA", tempfile.mkdtemp(prefix="jobwatch-test-"))
app = importlib.import_module("app")
new_profile = importlib.import_module("new_profile")


def template(name):
    with open(os.path.join(ROOT, "templates", name + ".json"), encoding="utf-8") as f:
        return json.load(f)


class DocumentNames(unittest.TestCase):
    def test_accents_become_ascii_underscored_prefix(self):
        self.assertEqual(app.owner_prefix({"name": "Zoé Ünal"}), "Zoe_Unal")

    def test_missing_name_falls_back(self):
        self.assertEqual(app.owner_prefix({"name": ""}), "Candidate")

    def test_doc_names_follow_the_prefix(self):
        names = app.doc_names({"name": "Jane Doe"})
        self.assertEqual(names["cv"], "Jane_Doe_CV.pdf")
        self.assertEqual(names["letter"], "Jane_Doe_Cover_Letter.pdf")


class Markup(unittest.TestCase):
    def test_known_classes_become_daisyui_components(self):
        out = app.daisify('<div class="card">x</div><span class="badge">b</span><button class="primary sm">go</button>')
        self.assertIn('class="card bg-base-100 shadow-sm"', out)
        self.assertIn('class="badge badge-soft badge-primary"', out)
        self.assertIn('class="btn btn-primary btn-sm"', out)

    def test_bare_form_controls_get_base_classes(self):
        out = app.daisify('<button>x</button><input name="q"><select name="s"></select><textarea></textarea>'
                          '<input type="checkbox" name="c">')
        self.assertIn('<button class="btn btn-sm">', out)
        self.assertIn('<input class="input input-sm" name="q">', out)
        self.assertIn('<select class="select select-sm"', out)
        self.assertIn('<textarea class="textarea">', out)
        self.assertIn('<input class="checkbox checkbox-sm" type="checkbox"', out)

    def test_unknown_classes_are_kept(self):
        self.assertIn('class="job st-new"', app.daisify('<li class="job st-new">'))


class Scoring(unittest.TestCase):
    unity_job = {"title": "Senior Unity Developer", "company": "Studio", "location": "Remote - European Union",
                 "description": "Unity and C# on a live game, live ops, remote team."}
    pm_job = {"title": "Project Manager", "company": "Acme", "location": "Budapest, Hungary",
              "description": "Own the roadmap with stakeholders; agile delivery and budget."}

    def test_unity_profile_scores_unity_jobs_high(self):
        score, _ = app.score_job(template("unity-game"), self.unity_job)
        self.assertGreaterEqual(score, template("unity-game")["notify_min_score"])

    def test_project_profile_scores_project_jobs_high(self):
        cfg = template("project-manager")
        score, hits = app.score_job(cfg, self.pm_job)
        self.assertGreaterEqual(score, cfg["min_score"], hits)

    def test_profiles_do_not_mix(self):
        # A unity profile hides a project-manager posting, and the reverse.
        self.assertLess(app.score_job(template("unity-game"), self.pm_job)[0], template("unity-game")["min_score"])
        self.assertLess(app.score_job(template("project-manager"), self.unity_job)[0],
                        template("project-manager")["min_score"])

    def test_defaults_are_generic(self):
        # The shipped defaults must not carry game-specific keywords any more.
        self.assertEqual(app.DEFAULT_CONFIG["title_keywords"], {})
        self.assertEqual(app.DEFAULT_CONFIG["linkedin_searches"], [])


class NewProfileTool(unittest.TestCase):
    def test_creates_an_isolated_profile(self):
        with tempfile.TemporaryDirectory() as root:
            with contextlib.redirect_stdout(io.StringIO()):
                code = new_profile.main(["test-pm", "--template", "project-manager", "--port", "8123",
                                         "--profiles-dir", root])
            self.assertEqual(code, 0)
            folder = os.path.join(root, "test-pm")
            with open(os.path.join(folder, "config.json"), encoding="utf-8") as f:
                cfg = json.load(f)
            self.assertTrue(cfg["ntfy"]["topic"].startswith("jobwatch-"))
            self.assertEqual(cfg["public_url"], "http://localhost:8123/")
            self.assertEqual(cfg["email"]["password"], "")
            self.assertFalse(any(k.startswith("_") for k in cfg), "template comments must not be copied")
            self.assertTrue(os.path.isdir(os.path.join(folder, "cv")))
            with open(os.path.join(folder, "profile.json"), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["name"], "Your Name")

    def test_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                new_profile.main(["dup", "--profiles-dir", root])
                with self.assertRaises(SystemExit):
                    new_profile.main(["dup", "--profiles-dir", root])

    def test_rejects_bad_names(self):
        with tempfile.TemporaryDirectory() as root, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                new_profile.main(["../evil", "--profiles-dir", root])


class CvDefaults(unittest.TestCase):
    def test_placeholder_profile_has_the_fields_the_renderer_uses(self):
        import cvgen
        for key in ("name", "headline", "contact", "summary", "skills", "experience", "languages", "objective"):
            self.assertIn(key, cvgen.DEFAULT_PROFILE)
        self.assertEqual(cvgen.DEFAULT_PROFILE["name"], "Your Name")


if __name__ == "__main__":
    unittest.main()


class PasswordProtection(unittest.TestCase):
    def test_auth_ok_without_password_is_always_true(self):
        self.assertTrue(app.auth_ok(None, ""))

    def test_auth_ok_accepts_only_the_right_password(self):
        import base64
        good = "Basic " + base64.b64encode(b"anyone:s3cret").decode()
        bad = "Basic " + base64.b64encode(b"anyone:wrong").decode()
        self.assertTrue(app.auth_ok(good, "s3cret"))
        self.assertFalse(app.auth_ok(bad, "s3cret"))
        self.assertFalse(app.auth_ok(None, "s3cret"))
        self.assertFalse(app.auth_ok("Basic !!!not-base64", "s3cret"))

    def test_server_asks_for_login_and_lets_health_through(self):
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        import base64
        app.init_db()  # the front page reads the database; main() also runs the migrations
        app.migrate_db()
        old = app.AUTH_PASSWORD
        app.AUTH_PASSWORD = "s3cret"
        srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/")
            self.assertEqual(ctx.exception.code, 401)
            self.assertIn("Basic", ctx.exception.headers.get("WWW-Authenticate", ""))
            self.assertEqual(urllib.request.urlopen(base + "/health").status, 200)
            req = urllib.request.Request(base + "/", headers={
                "Authorization": "Basic " + base64.b64encode(b"u:s3cret").decode()})
            self.assertEqual(urllib.request.urlopen(req).status, 200)
        finally:
            app.AUTH_PASSWORD = old
            srv.shutdown()
            srv.server_close()


class CvImport(unittest.TestCase):
    def _pdf(self, lines):
        import io
        from reportlab.pdfgen import canvas
        buf = io.BytesIO()
        c = canvas.Canvas(buf)
        y = 800
        for line in lines:
            c.drawString(72, y, line)
            y -= 20
        c.save()
        return buf.getvalue()

    def test_multipart_pdf_is_extracted(self):
        pdf = self._pdf(["Jane Doe", "Senior Engineer at Acme"])
        ctype = "multipart/form-data; boundary=XYZ"
        body = (b"--XYZ\r\nContent-Disposition: form-data; name=\"pdf\"; filename=\"cv.pdf\"\r\n"
                b"Content-Type: application/pdf\r\n\r\n" + pdf + b"\r\n--XYZ--\r\n")
        self.assertEqual(app.pdf_from_multipart(ctype, body), pdf)

    def test_multipart_without_pdf_field_is_empty(self):
        body = b"--XYZ\r\nContent-Disposition: form-data; name=\"other\"\r\n\r\nx\r\n--XYZ--\r\n"
        self.assertEqual(app.pdf_from_multipart("multipart/form-data; boundary=XYZ", body), b"")

    def test_pdf_text_reads_the_text_layer(self):
        text = app.pdf_text(self._pdf(["Jane Doe", "Senior Engineer at Acme"]))
        self.assertIn("Jane Doe", text)
        self.assertIn("Acme", text)

    def test_scanned_pdf_without_text_gives_a_clear_error(self):
        import io
        from pypdf import PdfWriter
        buf = io.BytesIO()
        w = PdfWriter()
        w.add_blank_page(width=200, height=200)
        w.write(buf)
        with self.assertRaises(ValueError):
            app.pdf_text(buf.getvalue())

    def test_profile_from_cv_text_uses_the_model_result(self):
        import cvgen
        fake = {"name": "Jane Doe", "headline": "", "contact": {}, "summary": "", "skills": [],
                "experience": [{"heading": "Acme", "role": "Engineer", "period": "2020", "intro": "",
                                "bullets": [], "projects": [], "links": []}],
                "languages": "", "objective": ""}
        old = cvgen.profile_from_text
        cvgen.profile_from_text = lambda key, text: fake
        try:
            out = json.loads(app.profile_from_cv_text("Jane Doe, Engineer at Acme"))
        finally:
            cvgen.profile_from_text = old
        self.assertEqual(out["name"], "Jane Doe")

    def test_schema_requires_the_fields_the_profile_needs(self):
        import cvgen
        self.assertTrue({"name", "experience", "contact"} <= set(cvgen.PROFILE_SCHEMA["required"]))
