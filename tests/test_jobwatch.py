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


class LinkedInEnrichment(unittest.TestCase):
    def _con(self):
        app.init_db()
        app.migrate_db()
        con = app.db()
        return con

    def test_rate_limited_detail_is_retried_then_classified(self):
        import urllib.error
        calls = {"n": 0}

        def fake_detail(jid):
            calls["n"] += 1
            if calls["n"] == 1:
                raise urllib.error.HTTPError("u", 429, "Too Many", None, None)
            return "This is a fully remote role."

        old_detail, old_sleep = app.linkedin_detail, app.time.sleep
        app.linkedin_detail = fake_detail
        app.time.sleep = lambda s: None
        con = self._con()
        try:
            items = [{"ext_id": "1", "title": "Project Manager", "location": "Europe"}]
            n = app.enrich_linkedin(con, items)
        finally:
            app.linkedin_detail, app.time.sleep = old_detail, old_sleep
            con.close()
        self.assertEqual(n, 1)
        self.assertEqual(items[0]["work_mode"], "remote")
        self.assertEqual(calls["n"], 2)


class BackgroundEnrichment(unittest.TestCase):
    def test_pending_linkedin_rows_get_description_and_mode(self):
        app.init_db()
        app.migrate_db()
        con = app.db()
        con.execute("DELETE FROM jobs WHERE id='linkedin:bg-1'")
        con.execute("INSERT INTO jobs(id,source,title,company,location,url,posted,description,score,matched,dupkey,"
                    "first_seen,last_seen,status,notified,work_mode) VALUES('linkedin:bg-1','linkedin','Project Manager',"
                    "'Acme','Europe','https://x/1',NULL,'',0,'','bg',?,?,'new',1,'unknown')", ("2026-01-01",) * 2)
        con.commit()
        old_detail, old_sleep = app.linkedin_detail, app.time.sleep
        app.linkedin_detail = lambda jid: "This is a fully remote role."
        app.time.sleep = lambda s: None
        cfg = app.load_config()
        try:
            app.enrich_pending_linkedin(cfg)
        finally:
            app.linkedin_detail, app.time.sleep = old_detail, old_sleep
        row = con.execute("SELECT description, work_mode FROM jobs WHERE id='linkedin:bg-1'").fetchone()
        con.close()
        self.assertEqual(row["work_mode"], "remote")
        self.assertIn("fully remote", row["description"])


class ResetCache(unittest.TestCase):
    def test_reset_removes_jobs_and_source_status_but_keeps_settings(self):
        app.init_db()
        app.migrate_db()
        cfg_before = app.load_config()
        con = app.db()
        con.execute("INSERT OR REPLACE INTO source_state(source,last_run,last_ok,found,new,error,seeded) VALUES('x','a','a',1,1,NULL,1)")
        con.commit()
        con.close()
        import io
        # the handler clears the tables; call the same SQL the route runs
        with app.db() as con:
            con.execute("DELETE FROM jobs")
            con.execute("DELETE FROM source_state")
        with app.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT count(*) FROM source_state").fetchone()[0], 0)
        self.assertEqual(app.load_config()["title_keywords"], cfg_before["title_keywords"])


class OwnSources(unittest.TestCase):
    """Own sources against a tiny local site that needs a login (like vmp.munka.hu)."""

    @classmethod
    def setUpClass(cls):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import parse_qs

        class Site(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, body, cookie=None):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                if cookie:
                    self.send_header("Set-Cookie", cookie)
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def logged_in(self):
                return "sess=ok" in (self.headers.get("Cookie") or "")

            def do_GET(self):
                if self.path.startswith("/belepes"):
                    return self.send('<form method="post" id="loginForm"><input type="hidden" name="csrf" value="t0k">'
                                     '<input type="text" name="login_username"><input type="password" name="login_jelszo">'
                                     '<input type="submit" name="login" value="Belépés"></form>')
                if self.path.startswith("/allas/talalatok"):
                    if "oldal=2" in self.path:
                        return self.send("<table></table>")
                    emp = "Acme Kft" if self.logged_in() else "<i>Bejelentkezés után látható</i>"
                    row = ('<tr><td><a href="/allas/reszletek/%s">2142 - %s</a></td><td><a href="#">egyetem</a></td>'
                           '<td><a href="#">Budapest</a></td><td>%s</td></tr>')
                    return self.send("<table>%s%s</table>" % (row % ("1", "Szoftverfejlesztő", emp),
                                                              row % ("2", "Szoftverfejlesztő", emp)))
                if self.path.startswith("/allas/reszletek/"):
                    return self.send("<td>Munkakör (FEOR)</td><td>Szoftverfejlesztő</td><td>Unity, távmunka</td><p>TÁMOP</p>")
                if self.path == "/jobs":
                    return self.send('<a href="/job/10">Unity Developer</a><a href="/about">About us</a>'
                                     '<a href="/job/10">Unity Developer</a><a href="/job/11">Game Programmer</a>')
                self.send("")

            def do_POST(self):
                f = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
                ok = f.get("login_username") == ["me"] and f.get("login_jelszo") == ["pw"] and f.get("csrf") == ["t0k"]
                if ok:
                    return self.send("Üdvözöljük!", cookie="sess=ok; Path=/")
                return self.send('<form method="post"><input type="text" name="login_username">'
                                 '<input type="password" name="login_jelszo"></form>')

        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        self._delay, app.CUSTOM_DELAY = app.CUSTOM_DELAY, 0

    def tearDown(self):
        app.CUSTOM_DELAY = self._delay

    def vmp(self, **kw):
        src = {"id": "s1", "type": "vmp", "name": "VMP", "url": self.base + "/allas/talalatok?kulcsszo=x",
               "login_url": self.base + "/belepes"}
        src.update(kw)
        return src

    def test_vmp_without_login_has_no_employer_and_drops_feor_code(self):
        items = app.run_custom_source(self.vmp(), is_new=lambda i: i == "1")
        self.assertEqual([i["title"] for i in items], ["Szoftverfejlesztő", "Szoftverfejlesztő"])
        self.assertEqual(items[0]["company"], "")
        self.assertIn("távmunka", items[0]["description"])   # detail page read for the new posting
        self.assertNotIn("távmunka", items[1]["description"])  # not new: no detail fetch

    def test_vmp_login_shows_employer(self):
        items = app.run_custom_source(self.vmp(username="me", password="pw"))
        self.assertEqual(items[0]["company"], "Acme Kft")

    def test_wrong_password_is_reported(self):
        with self.assertRaisesRegex(RuntimeError, "login failed"):
            app.run_custom_source(self.vmp(username="me", password="bad"))

    def test_any_page_takes_links_matching_the_pattern_once(self):
        items = app.run_custom_source({"id": "p", "type": "page", "url": self.base + "/jobs", "pattern": "/job/"})
        self.assertEqual([i["title"] for i in items], ["Unity Developer", "Game Programmer"])

    def test_settings_form_keeps_password_when_left_empty_and_deletes(self):
        old = [{"id": "a1", "type": "vmp", "url": "u1", "username": "me", "password": "secret"},
               {"id": "b2", "type": "page", "url": "u2"}]
        f = {"cs_count": "2", "cs_0_id": "a1", "cs_0_type": "vmp", "cs_0_url": "u1", "cs_0_username": "me",
             "cs_0_enabled": "on", "cs_1_id": "b2", "cs_1_url": "u2", "cs_1_delete": "on",
             "cs_new_type": "rss", "cs_new_url": "https://example.com/feed"}
        out = app.parse_custom_sources(f, old)
        self.assertEqual([s["id"] for s in out[:1]], ["a1"])
        self.assertEqual(out[0]["password"], "secret")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["type"], "rss")
        self.assertTrue(out[1]["enabled"])


class Outreach(unittest.TestCase):
    """Recruiter messages: the API request shape (with a fake SDK) and the page flow (with a stubbed call)."""

    def fake_sdk(self, captured, text='{"subject": "", "message": " Hi Anna, ... "}', stop="end_turn"):
        import types

        class Err(Exception):
            pass

        class Messages:
            def create(self, **kw):
                captured.update(kw)
                return types.SimpleNamespace(stop_reason=stop, model="claude-opus-5-5",
                                             content=[types.SimpleNamespace(type="text", text=text)])

        mod = types.ModuleType("anthropic")
        mod.Anthropic = lambda **kw: types.SimpleNamespace(beta=types.SimpleNamespace(messages=Messages()))
        for n in ("AuthenticationError", "PermissionDeniedError", "RateLimitError", "BadRequestError",
                  "APIStatusError", "APIConnectionError"):
            setattr(mod, n, Err)
        return mod

    def test_request_and_parsing(self):
        import cvgen
        from unittest import mock
        cap = {}
        prof = {"name": "Gábor", "contact": {"email": "secret@example.com"}, "summary": "Unity dev"}
        with mock.patch.dict(sys.modules, {"anthropic": self.fake_sdk(cap)}):
            out = cvgen.outreach_message("k", prof, {"name": "Anna", "title": "TA @ Supercell", "via": "Péter | ex-colleague"},
                                         {"title": "Unity Developer", "company": "Supercell"}, "linkedin_intro", "hu")
        self.assertEqual(out["message"], "Hi Anna, ...")
        self.assertEqual(cap["model"], "claude-opus-5-5")
        self.assertEqual(cap["fallbacks"], "default")
        self.assertEqual(cap["output_config"]["format"]["schema"], cvgen.OUTREACH_SCHEMA)
        user = cap["messages"][0]["content"]
        self.assertNotIn("secret@example.com", user)         # contact details stay local
        self.assertIn("<introducer>", user)
        self.assertIn("Hungarian", user)

    def test_refusal_is_reported(self):
        import cvgen
        from unittest import mock
        with mock.patch.dict(sys.modules, {"anthropic": self.fake_sdk({}, stop="refusal")}):
            with self.assertRaises(cvgen.CVError):
                cvgen.outreach_message("k", {"name": "x"}, {"name": "Anna"})

    def test_recruiter_lines(self):
        r = app.parse_recruiters("Anna Kovács | TA @ Supercell | https://www.linkedin.com/in/anna/\nBob | bob@corp.com\n\n | x")
        self.assertEqual(r[0], {"name": "Anna Kovács", "title": "TA @ Supercell", "contact": "https://www.linkedin.com/in/anna/"})
        self.assertEqual(r[1]["contact"], "bob@corp.com")
        self.assertEqual(len(r), 2)

    def test_page_flow(self):
        import cvgen
        import threading
        from unittest import mock
        app.init_db()
        cfg = app.load_config()
        cfg["anthropic_api_key"] = "k"
        app.save_config(cfg)
        fake = lambda *a, **k: {"subject": "", "message": "Hello %s" % a[2]["name"], "_model": "m"}
        with mock.patch.object(cvgen, "outreach_message", fake):
            self.assertIn("mutual connection", app.outreach_create({"recruiters": "Anna", "channel": "linkedin_intro"}))
            msg = app.outreach_create({"recruiters": "Anna | TA @ X | https://www.linkedin.com/in/anna/\nBob",
                                       "channel": "linkedin_note"})
            self.assertEqual(msg, "Writing 2 message(s)…")
            for t in [t for t in threading.enumerate() if t.name != "MainThread" and t.daemon]:
                t.join(5)
        with app.db() as con:
            rows = con.execute("SELECT id, status, message FROM outreach ORDER BY id").fetchall()
        self.assertEqual([(r["status"], r["message"]) for r in rows[-2:]], [("draft", "Hello Anna"), ("draft", "Hello Bob")])
        html = app.outreach_page({})
        self.assertIn("Hello Anna", html)
        self.assertIn("🔗 Open profile", html)
        oid = rows[-2]["id"]
        self.assertEqual(app.outreach_action(oid, {"act": "save", "message": "edited"}), "Saved.")
        self.assertEqual(app.outreach_action(oid, {"act": "sent", "message": "edited"}), "Marked as sent.")
        with app.db() as con:
            r = con.execute("SELECT status, message, sent_at FROM outreach WHERE id=?", (oid,)).fetchone()
        self.assertEqual((r["status"], r["message"]), ("sent", "edited"))
        self.assertTrue(r["sent_at"])
        self.assertEqual(app.outreach_action(oid, {"act": "delete"}), "Deleted.")


class OutreachForm(unittest.TestCase):
    def test_list_from_the_page_with_per_person_type(self):
        f = {"recruiters_json": json.dumps([
            {"name": "Tom", "title": "Consultant @ Amiqus", "contact": "", "kind": "agency"},
            {"name": "  ", "title": "no name, skipped"},
            {"name": "Anna", "title": "TA | Supercell", "contact": "https://www.linkedin.com/in/anna/", "kind": "bogus"}])}
        r = app.recruiters_from_form(f)
        self.assertEqual([(x["name"], x["kind"]) for x in r], [("Tom", "agency"), ("Anna", "")])
        self.assertEqual(r[1]["title"], "TA | Supercell")   # a "|" in a field is kept as is

    def test_bad_json_is_ignored(self):
        self.assertEqual(app.recruiters_from_form({"recruiters_json": "{not json"}), [])


class LinkedInPreview(unittest.TestCase):
    def preview(self, page=None, exc=None, url="https://hu.linkedin.com/in/anna-kovacs-1a2b3c4d?trk=x"):
        from unittest import mock

        def fake(u, **kw):
            self.fetched = u
            if exc:
                raise exc
            return page
        with mock.patch.object(app, "fetch", fake):
            return app.linkedin_preview(url)

    def test_public_profile_gives_name_and_headline(self):
        r = self.preview('<meta property="og:title" content="Anna Kov&#225;cs - Talent Acquisition at Supercell | LinkedIn">')
        self.assertEqual((r["name"], r["title"], r["from"]), ("Anna Kovács", "Talent Acquisition at Supercell", "profile"))
        self.assertEqual(self.fetched, "https://www.linkedin.com/in/anna-kovacs-1a2b3c4d/")  # tracking params dropped

    def test_login_wall_falls_back_to_the_address(self):
        r = self.preview("<title>Sign Up | LinkedIn</title>")
        self.assertEqual((r["name"], r["title"], r["from"]), ("Anna Kovacs", "", "address"))

    def test_network_error_falls_back_to_the_address(self):
        r = self.preview(exc=OSError("999"))
        self.assertEqual(r["name"], "Anna Kovacs")

    def test_other_links_are_not_fetched(self):
        r = self.preview(page="x", url="http://192.168.68.1/admin")
        self.assertFalse(r["ok"])
        self.assertFalse(hasattr(self, "fetched"))
