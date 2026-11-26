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
app.init_db()
app.migrate_db()  # the tests share one throwaway database with the current columns
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
        self.assertEqual(app.DEFAULT_CONFIG["scoring"]["interests"], [])
        self.assertEqual(app.DEFAULT_CONFIG["search_terms"], [])


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
        self.assertEqual(app.load_config()["scoring"], cfg_before["scoring"])


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
        self.assertIn("Open profile", html)
        oid = rows[-2]["id"]
        self.assertEqual(app.outreach_action(oid, {"act": "save", "message": "edited"}), "Saved.")
        self.assertIn("Marked as sent", app.outreach_action(oid, {"act": "sent", "message": "edited"}))
        html = app.outreach_page({})
        self.assertIn("1 sent", html)
        self.assertEqual(html.count("<dialog") - html.count('<dialog id="clear_dlg"'), 1)  # + the clear-cache one on every page
        self.assertEqual(app.outreach_action(oid, {"act": "unsent"}), "Moved back to drafts.")
        self.assertNotIn("<dialog", app.outreach_page({}).replace(app.CLEAR_DIALOG, ""))
        app.outreach_action(oid, {"act": "sent"})
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


class OutreachSpecific(unittest.TestCase):
    def test_profile_summary_drops_linkedin_boilerplate(self):
        from unittest import mock
        pg = ('<meta property="og:title" content="Tom Baker - Games Recruiter | LinkedIn">'
              '<meta property="og:description" content="Games Recruiter · Hiring Unity devs for mobile studios in the '
              'Nordics. · Experience: Amiqus · Location: London · 500+ connections on LinkedIn. View Tom Baker’s profile on LinkedIn">')
        with mock.patch.object(app, "fetch", lambda u, **k: pg):
            r = app.linkedin_preview("https://www.linkedin.com/in/tom-baker/")
        self.assertEqual(r["about"], "Games Recruiter · Hiring Unity devs for mobile studios in the Nordics. · "
                                     "Experience: Amiqus · Location: London")

    def test_job_links_are_recognised(self):
        for u in ("https://www.linkedin.com/jobs/view/4494793948/",
                  "https://hu.linkedin.com/jobs/view/senior-unity-developer-at-kolibri-4494793948?trk=x",
                  "https://www.linkedin.com/jobs/collections/recommended/?currentJobId=4494793948"):
            self.assertEqual(app.LINKEDIN_JOB_RE.search(u).group(1), "4494793948", u)

    def test_role_from_linkedin_job_and_plain_text(self):
        from unittest import mock
        app.init_db()
        page = ('<h2 class="top-card-layout__title topcard__title">Senior Unity Developer</h2>'
                '<a class="topcard__org-name-link" href="#">\n Kolibri Games \n</a>'
                '<span class="topcard__flavor topcard__flavor--bullet">Berlin</span>'
                '<div class="show-more-less-html__markup">Build our idle games in Unity.</div>')
        with mock.patch.object(app, "fetch", lambda u, **k: page):
            t = app.resolve_role("https://www.linkedin.com/jobs/view/4494793948/")
        self.assertIn("Title: Senior Unity Developer\nCompany: Kolibri Games\nLocation: Berlin", t)
        self.assertIn("idle games", t)
        self.assertEqual(app.resolve_role("Senior Unity dev, mobile"), "Senior Unity dev, mobile")

    def test_list_items_carry_profile_and_role(self):
        r = app.recruiters_from_form({"recruiters_json": json.dumps(
            [{"name": "Tom", "about": "Hiring Unity devs", "role": "https://www.linkedin.com/jobs/view/1234567/"}])})
        self.assertEqual((r[0]["profile"], r[0]["role_ref"]), ("Hiring Unity devs", "https://www.linkedin.com/jobs/view/1234567/"))

    def test_prompt_uses_the_recruiters_own_role_over_the_general_posting(self):
        import cvgen
        from unittest import mock
        cap = {}
        sdk = Outreach().fake_sdk(cap)
        with mock.patch.dict(sys.modules, {"anthropic": sdk}):
            cvgen.outreach_message("k", {"name": "G"}, {"name": "Tom", "profile": "Hiring for Nordic mobile studios",
                                                        "role_text": "Title: Senior Unity Developer\nCompany: Kolibri"},
                                   {"title": "General posting", "company": "Other"})
        user = cap["messages"][0]["content"]
        self.assertIn("Hiring for Nordic mobile studios", user)
        self.assertIn("Kolibri", user)
        self.assertNotIn("General posting", user)

    def test_role_from_the_database_needs_no_request_and_429_is_retried(self):
        import urllib.error
        from unittest import mock
        app.init_db()
        with app.db() as con:
            con.execute("INSERT OR REPLACE INTO jobs(id,source,title,company,location,url,description,score,status) "
                        "VALUES('linkedin:7777777','linkedin','Unity Dev','Acme','Remote','u','Make games',9,'new')")
        calls = []

        def busy(u, **k):
            calls.append(u)
            raise urllib.error.HTTPError(u, 429, "Too Many", {}, None)
        with mock.patch.object(app, "fetch", busy):
            t = app.resolve_role("https://www.linkedin.com/jobs/view/7777777/")
            self.assertIn("Company: Acme", t)
            self.assertEqual(calls, [])
            t = app.resolve_role("https://www.linkedin.com/jobs/view/8888888/", waits=(0, 0))
        self.assertIn(app.ROLE_UNREAD, t)
        self.assertEqual(len(calls), 3)  # first try + 2 retries


class LinkedInHeadless(unittest.TestCase):
    def test_location_in_place_of_a_headline_is_not_a_title(self):
        from unittest import mock
        pg = ('<meta property="og:title" content="Rita Tóth - Budapest, Budapest, Hungary | Professional Profile | LinkedIn">'
              '<meta property="og:description" content="Experience: Amiqus · Location: Budapest, Budapest, Hungary · 500+ connections on LinkedIn.">')
        with mock.patch.object(app, "fetch", lambda u, **k: pg):
            r = app.linkedin_preview("https://www.linkedin.com/in/rita-toth/")
        self.assertEqual((r["name"], r["title"]), ("Rita Tóth", "@ Amiqus"))


class Logos(unittest.TestCase):
    def setUp(self):
        app.init_db()
        with app.db() as con:
            con.execute("DELETE FROM company_logos")

    def test_one_logo_per_company_and_download(self):
        from unittest import mock
        import io
        with app.db() as con:
            app.remember_logo(con, "Super Cell Oy", "https://cdn.example/a.png")
            app.remember_logo(con, "supercell oy", "https://cdn.example/b.png")  # same company, kept first
            app.remember_logo(con, "NoLogo Ltd", "")
            app.remember_logo(con, "Bad", "http://insecure.example/x.png")
        png = b"\x89PNG\r\n\x1a\n" + b"0" * 100

        class Resp(io.BytesIO):
            headers = mock.Mock(get_content_type=lambda: "image/png")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with mock.patch.object(app.urllib.request, "urlopen", lambda req, timeout=0: Resp(png)), \
                mock.patch.object(app.time, "sleep", lambda s: None):
            app.fetch_logos()
        files = app.logo_files()
        self.assertEqual(list(files), ["supercelloy"])
        self.assertTrue(open(os.path.join(app.LOGO_DIR, files["supercelloy"]), "rb").read().startswith(b"\x89PNG"))
        self.assertIn('src="/logo/supercelloy"', app.logo_html("Super Cell Oy", files))

    def test_svg_and_html_are_not_stored(self):
        from unittest import mock
        import io
        with app.db() as con:
            app.remember_logo(con, "Evil", "https://cdn.example/x.svg")

        class Resp(io.BytesIO):
            headers = mock.Mock(get_content_type=lambda: "image/svg+xml")

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        with mock.patch.object(app.urllib.request, "urlopen", lambda req, timeout=0: Resp(b"<svg onload=alert(1)>")), \
                mock.patch.object(app.time, "sleep", lambda s: None):
            app.fetch_logos()
        self.assertEqual(app.logo_files(), {})

    def test_initials_when_there_is_no_logo(self):
        h = app.logo_html("Kolibri Games GmbH", {})
        self.assertIn(">KG</span>", h)
        self.assertIn("hsl(", h)
        self.assertEqual(h, app.logo_html("Kolibri Games GmbH", {}))  # same colour every time


class RefreshSelection(unittest.TestCase):
    def test_form_to_selection(self):
        self.assertEqual(app.run_selection({}), (None, set()))                       # header button: all
        self.assertEqual(app.run_selection({"all": "1", "run_flexa": "on"}), (None, set()))
        self.assertEqual(app.run_selection({"one": "flexa"}), ({"flexa"}, set()))
        self.assertEqual(app.run_selection({"one": "cs:ab12"}), (set(), {"ab12"}))
        self.assertEqual(app.run_selection({"selected": "1", "run_flexa": "on", "run_linkedin": "on"}), ({"flexa", "linkedin"}, set()))
        self.assertEqual(app.run_selection({"selected": "1", "run_custom": "on", "run_cs_ab12": "on"}), (set(), {"ab12"}))
        self.assertEqual(app.run_selection({"selected": "1"}), (None, set()))        # nothing ticked: caller refuses

    def test_only_the_picked_sources_run(self):
        from unittest import mock
        app.init_db()
        ran = []
        fake = {k: (lambda cfg, k=k: ran.append((k, [s["id"] for s in cfg.get("custom_sources") or []])) or [])
                for k in app.SOURCE_FUNCS}
        cfg = app.load_config()
        cfg["sources"] = {k: True for k in app.SOURCES_INFO}
        cfg["custom_sources"] = [{"id": "a1", "url": "x"}, {"id": "b2", "url": "y"}]
        app.save_config(cfg)
        with mock.patch.dict(app.SOURCE_FUNCS, fake), mock.patch.object(app, "fetch_logos", lambda: None):
            app.run_all(force=True, only={"flexa"})
            self.assertEqual(ran, [("flexa", ["a1", "b2"])])
            ran.clear()
            app.run_all(force=True, only=set(), custom_ids={"b2"})
            self.assertEqual(ran, [("custom", ["b2"])])

    def test_jobs_list_filters_by_several_sources(self):
        app.init_db()
        with app.db() as con:
            con.execute("DELETE FROM jobs")
            for jid, src in (("flexa:1", "flexa"), ("linkedin:2", "linkedin"), ("remotive:3", "remotive")):
                con.execute("INSERT INTO jobs(id,source,title,company,url,score,status,first_seen,work_mode) "
                            "VALUES(?,?,?,?,?,9,'new','2026-11-25T10:00:00+00:00','remote')", (jid, src, "T " + src, "C " + src, "u"))
        page_ = app.jobs_page({"source": "flexa,remotive", "work": "all"})
        self.assertIn("T flexa", page_)
        self.assertIn("T remotive", page_)
        self.assertNotIn("T linkedin", page_)


class MailAlerts(unittest.TestCase):
    """Job alert e-mails from a fake mailbox; the mailbox must be opened read-only and fetched with PEEK."""
    MAIL = (b"From: Jack <jack@jackandjill.ai>\r\nSubject: New roles for you\r\nMessage-ID: <m1@jj>\r\n"
            b"Date: Wed, 25 Nov 2026 08:00:00 +0000\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
            b'<p>Hi!</p><a href="https://app.jackandjill.ai/jobs/123">Senior Unity Developer</a> at Supercell'
            b'<a href="https://www.linkedin.com/comm/jobs/view/4494498619/?trackingId=x">Unity Engineer</a>'
            b'<a href="https://app.jackandjill.ai/settings">Manage your settings</a>'
            b'<a href="https://jackandjill.ai/unsubscribe">Unsubscribe</a>')

    def fake_imap(self, calls):
        mail = self.MAIL

        class M:
            def __init__(self, host, port, timeout=0):
                calls.append(("connect", host, port))

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def login(self, u, p):
                calls.append(("login", u))

            def select(self, folder, readonly=False):
                calls.append(("select", folder, readonly))
                return "OK", [b"1"]

            def search(self, charset, *crit):
                calls.append(("search",) + crit)
                return "OK", [b"7"]

            def fetch(self, num, what):
                calls.append(("fetch", num, what))
                return "OK", [(b"7 (BODY[] {1}", mail)]
        return M

    def cfg(self, claude=False):
        cfg = app.load_config()
        cfg["email"].update(username="me@gmail.com", password="app-pw")
        cfg["mail_source"] = dict(app.DEFAULT_CONFIG["mail_source"], senders=["jackandjill.ai"], use_claude=claude)
        cfg["anthropic_api_key"] = "k"
        return cfg

    def setUp(self):
        app.init_db()
        with app.db() as con:
            con.execute("DELETE FROM mail_seen")

    def test_links_mode_read_only_and_once(self):
        from unittest import mock
        calls = []
        with mock.patch("imaplib.IMAP4_SSL", self.fake_imap(calls)):
            cfg = self.cfg()
            jobs = app.src_mail(cfg)
            app.mark_mails_done(cfg.pop("_mail_done"))  # what run_all does once the postings are saved
            again = app.src_mail(self.cfg())
        self.assertIn(("select", '"INBOX"', True), calls)                      # read-only
        self.assertTrue(all(c[2] == "(BODY.PEEK[])" for c in calls if c[0] == "fetch"))  # not marked as read
        self.assertEqual(sorted(j["title"] for j in jobs), ["Senior Unity Developer", "Unity Engineer"])
        self.assertIn("https://www.linkedin.com/jobs/view/4494498619/", [j["url"] for j in jobs])  # tracking removed
        self.assertEqual(again, [])                                              # each e-mail once

    def test_claude_mode_only_keeps_links_from_the_mail(self):
        import cvgen
        from unittest import mock
        calls = []
        fake = lambda key, subj, sender, text, links: [
            {"title": "Senior Unity Developer", "company": "Supercell", "location": "Helsinki", "url": "https://app.jackandjill.ai/jobs/123"}]
        with mock.patch("imaplib.IMAP4_SSL", self.fake_imap(calls)), mock.patch.object(cvgen, "jobs_from_email", fake):
            jobs = app.src_mail(self.cfg(claude=True))
        self.assertEqual([(j["title"], j["company"]) for j in jobs], [("Senior Unity Developer", "Supercell")])

    def test_claude_filter_drops_made_up_links(self):
        import cvgen
        from unittest import mock
        cap = {}
        sdk = Outreach().fake_sdk(cap, text='{"jobs": [{"title": "A", "company": "", "location": "", "url": "https://evil.example/x"},'
                                            '{"title": "B", "company": "", "location": "", "url": "https://ok.example/j/1"}]}')
        with mock.patch.dict(sys.modules, {"anthropic": sdk}):
            out = cvgen.jobs_from_email("k", "s", "f", "t", [("B", "https://ok.example/j/1")])
        self.assertEqual([j["title"] for j in out], ["B"])


class AddByLink(unittest.TestCase):
    PAGE = ('<html><head><title>Careers</title><script type="application/ld+json">{"@context": "https://schema.org", '
            '"@type": "JobPosting", "title": "Senior Unity Developer", "datePosted": "2026-11-20", '
            '"hiringOrganization": {"@type": "Organization", "name": "Kolibri Games", "logo": "https://cdn.example/k.png"}, '
            '"jobLocation": {"@type": "Place", "address": {"addressLocality": "Berlin", "addressCountry": "DE"}}, '
            '"jobLocationType": "TELECOMMUTE", "description": "&lt;p&gt;Build idle games in Unity&lt;/p&gt;"}</script></head></html>')

    def test_structured_data_and_no_duplicates(self):
        from unittest import mock
        app.init_db()
        app.migrate_db()
        with app.db() as con:
            con.execute("DELETE FROM jobs WHERE source='manual'")
        with mock.patch.object(app, "fetch", lambda u, **k: self.PAGE), mock.patch.object(app, "fetch_logos", lambda: None):
            jid, msg = app.add_job_by_link("https://careers.kolibrigames.com/jobs/42")
            again, msg2 = app.add_job_by_link("https://careers.kolibrigames.com/jobs/42")
        with app.db() as con:
            r = dict(con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
            logo = con.execute("SELECT url FROM company_logos WHERE ckey='kolibrigames'").fetchone()
        self.assertEqual((r["title"], r["company"], r["location"], r["source"]), ("Senior Unity Developer", "Kolibri Games", "Berlin, DE, Remote", "manual"))
        self.assertIn("idle games", r["description"])
        self.assertEqual(r["work_mode"], "remote")
        self.assertEqual(logo["url"], "https://cdn.example/k.png")
        self.assertEqual((again, msg2), (jid, "This posting is already in the list."))

    def test_bad_link(self):
        self.assertEqual(app.add_job_by_link("not a link")[0], None)


class Duplicates(unittest.TestCase):
    def setUp(self):
        app.init_db()
        app.migrate_db()
        with app.db() as con:
            con.execute("DELETE FROM jobs")
            con.execute("DELETE FROM source_state")

    def run_sources(self, results):
        from unittest import mock
        fake = {k: (lambda cfg, k=k: results.get(k, [])) for k in app.SOURCE_FUNCS}
        cfg = app.load_config()
        cfg["sources"] = {k: True for k in app.SOURCES_INFO}
        app.save_config(cfg)
        with mock.patch.dict(app.SOURCE_FUNCS, fake), mock.patch.object(app, "fetch_logos", lambda: None),                 mock.patch.object(app, "enrich_pending_linkedin", lambda cfg: None):
            app.run_all(force=True, only=set(results))

    def test_keys(self):
        self.assertEqual(app.dup_key("Senior Unity Developer (m/w/d)", "Kolibri Games GmbH", "u"),
                         app.dup_key("Senior Unity Developer", "Kolibri Games", "x"))
        self.assertEqual(app.url_key("https://www.linkedin.com/comm/jobs/view/4494498619/?trackingId=x"),
                         app.url_key("https://hu.linkedin.com/jobs/view/unity-dev-at-acme-4494498619"))

    def test_same_posting_from_two_sources_is_one_card(self):
        li = {"ext_id": "4494498619", "title": "Unity Engineer", "company": "Acme", "location": "Remote", "posted": "",
              "url": "https://www.linkedin.com/jobs/view/4494498619/", "description": ""}
        mail = {"ext_id": "m1", "title": "Unity Engineer", "company": "", "location": "linkedin.com", "posted": "",
                "url": "https://www.linkedin.com/jobs/view/4494498619/", "description": ""}           # same link, no company
        flexa = {"ext_id": "f1", "title": "Unity Engineer (f/m/d)", "company": "ACME Ltd", "location": "", "posted": "",
                 "url": "https://flexa.careers/jobs/x", "description": "Full text"}                    # same title+company
        self.run_sources({"linkedin": [li]})
        self.run_sources({"mail": [mail], "flexa": [flexa]})
        with app.db() as con:
            rows = con.execute("SELECT source, also_sources, description FROM jobs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "linkedin")
        self.assertEqual(sorted(rows[0]["also_sources"].split(",")), ["flexa", "mail"])
        self.assertEqual(rows[0]["description"], "Full text")   # filled in from the other source
        self.assertIn("also on", app.jobs_page({"work": "all", "min": "-99"}))

    def test_existing_duplicates_are_hidden_keeping_the_one_worked_on(self):
        with app.db() as con:
            for jid, src, status in (("flexa:1", "flexa", "new"), ("linkedin:2", "linkedin", "applied")):
                con.execute("INSERT INTO jobs(id,source,title,company,url,score,status,first_seen,dupkey,urlkey) "
                            "VALUES(?,?,?,?,?,9,?,'2026-11-25',?,?)", (jid, src, "Unity Dev", "Acme", "u/" + jid, status,
                                                                     app.dup_key("Unity Dev", "Acme", ""), "u/" + jid))
            self.assertEqual(app.dedupe_existing(con), 1)
            kept = con.execute("SELECT id, also_sources FROM jobs WHERE dup_of IS NULL").fetchall()
        self.assertEqual([(r["id"], r["also_sources"]) for r in kept], [("linkedin:2", "flexa")])

    def test_mails_are_marked_done_only_after_saving(self):
        from unittest import mock
        with app.db() as con:
            con.execute("DELETE FROM mail_seen")

        def fake_mail(cfg):
            cfg.setdefault("_mail_done", []).append(("<m9@x>", 1))
            with app.db() as con:
                self.assertIsNone(con.execute("SELECT 1 FROM mail_seen").fetchone())  # not yet
            return [{"ext_id": "z", "title": "Unity Dev", "company": "Z", "location": "", "posted": "",
                     "url": "https://z.example/jobs/1", "description": ""}]
        fake = {k: (lambda cfg: []) for k in app.SOURCE_FUNCS}
        fake["mail"] = fake_mail
        cfg = app.load_config()
        cfg["sources"] = {k: True for k in app.SOURCES_INFO}
        app.save_config(cfg)
        with mock.patch.dict(app.SOURCE_FUNCS, fake), mock.patch.object(app, "fetch_logos", lambda: None):
            app.run_all(force=True, only={"mail"})
        with app.db() as con:
            self.assertEqual(con.execute("SELECT msgid FROM mail_seen").fetchall()[0][0], "<m9@x>")
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs WHERE source='mail'").fetchone()[0], 1)


class EnrichAnySource(unittest.TestCase):
    def test_linkedin_posting_from_an_alert_email_gets_its_description(self):
        from unittest import mock
        app.init_db()
        app.migrate_db()
        with app.db() as con:
            con.execute("DELETE FROM jobs")
            con.execute("INSERT INTO jobs(id,source,title,company,location,url,score,status,first_seen,urlkey) VALUES"
                        "('mail:x','mail','Mobile Engineer','ElevenLabs','Hungary (Remote)','https://www.linkedin.com/jobs/view/4486152955/',"
                        "2,'new','2026-11-25','li:4486152955')")
        cfg = app.load_config()
        cfg["scoring"]["desc"] = ["unity"]
        with mock.patch.object(app, "linkedin_detail", lambda jid: "We build our app in Unity. Fully remote."), \
                mock.patch.object(app.time, "sleep", lambda s: None):
            app.enrich_pending_linkedin(cfg)
        with app.db() as con:
            r = con.execute("SELECT description, work_mode, score FROM jobs WHERE id='mail:x'").fetchone()
        self.assertIn("Unity", r["description"])
        self.assertEqual(r["work_mode"], "remote")
        self.assertGreater(r["score"], 2)


class WorkModes(unittest.TestCase):
    def test_text_rules_in_many_languages(self):
        cases = [
            ("", "", "Poste basé à Paris, en présentiel du lundi au vendredi.", "onsite"),
            ("", "", "Vous bénéficiez de 2 jours de télétravail par semaine.", "hybrid"),
            ("", "", "This role is fully remote within the EU.", "remote"),
            ("", "", "Please note: no remote, the team works from our Lisbon office.", "onsite"),
            ("", "", "Oferujemy pracę stacjonarną w biurze w Krakowie.", "onsite"),
            ("", "", "Oferujemy praca zdalna z dowolnego miejsca.", "remote"),
            ("", "", "Du arbetar på plats på vårt kontor i Stockholm.", "onsite"),
            ("", "", "Wir bieten 2 Tage pro Woche im Büro, sonst Homeoffice.", "hybrid"),
            ("", "", "Heti 2 nap home office, a többi napon irodában.", "hybrid"),
            ("", "", "Perks: home office equipment budget. Our Remote Monitoring product is great.", "unknown"),
            ("Unity Developer (Hybrid)", "Budapest", "", "hybrid"),
            ("Mobile Engineer", "Hungary (Remote)", "", "remote"),
        ]
        for t, loc, d, want in cases:
            got, ev = app.classify_mode_ex(t, loc, d)
            self.assertEqual(got, want, d or t)
            self.assertEqual(bool(ev), want != "unknown")

    def test_mode_from_the_job_boards_own_page(self):
        from unittest import mock
        fourday = '<div><span><span>Onsite · Coventry, UK</span></span></div> other <span><span>Hybrid · London, UK</span></span>'
        flexa = r'x":"i6l2r4"}]]},\"className\":\"mt-0.5\",\"size\":16}],\"Hybrid\"]}],[\"$\",\"div\"'
        with mock.patch.object(app, "fetch", lambda u, **k: fourday):
            self.assertEqual(app.mode_from_page("4dayweek", "u")[0], "onsite")   # the posting's own label, not the list below
        with mock.patch.object(app, "fetch", lambda u, **k: flexa):
            self.assertEqual(app.mode_from_page("flexa", "u")[0], "hybrid")
        with mock.patch.object(app, "fetch", lambda u, **k: "<p>nothing</p>"):
            self.assertEqual(app.mode_from_page("flexa", "u"), (None, ""))

    def test_background_settling_order_and_manual_is_kept(self):
        import cvgen
        from unittest import mock
        app.init_db()
        app.migrate_db()
        rows = [("flexa:1", "flexa", "Designer", "", "Nothing here"),
                ("linkedin:2", "linkedin", "Engineer", "Paris", "Le poste est en présentiel à Paris."),
                ("linkedin:3", "linkedin", "Programmer", "Paris", "Join our studio team. " * 20),
                ("linkedin:4", "linkedin", "Artist", "Paris", "Join our studio team. " * 20)]
        with app.db() as con:
            con.execute("DELETE FROM jobs")
            for jid, src, title, loc, d in rows:
                con.execute("INSERT INTO jobs(id,source,title,location,url,description,score,status,first_seen,work_mode) "
                            "VALUES(?,?,?,?,?,?,5,'new','2026-11-25','unknown')", (jid, src, title, loc, "https://x/" + jid, d))
            con.execute("UPDATE jobs SET mode_src='manual' WHERE id='linkedin:4'")
        cfg = app.load_config()
        cfg["anthropic_api_key"] = "k"
        flexa_page = r'\"size\":16}],\"Remote-first\"]'
        claude_calls = []

        def fake_claude(key, title, loc, d):
            claude_calls.append(title)
            return "onsite", "Join our studio team."
        with mock.patch.object(app, "fetch", lambda u, **k: flexa_page), mock.patch.object(app.time, "sleep", lambda s: None), \
                mock.patch.object(cvgen, "work_mode_from_text", fake_claude):
            app.enrich_work_modes(cfg)
        with app.db() as con:
            got = {r["id"]: (r["work_mode"], r["mode_src"]) for r in con.execute("SELECT id, work_mode, mode_src FROM jobs")}
        self.assertEqual(got["flexa:1"], ("remote", "page"))        # the board's own label
        self.assertEqual(got["linkedin:2"], ("onsite", "text"))     # rules, no API call
        self.assertEqual(got["linkedin:3"], ("onsite", "claude"))   # only the rest goes to Claude
        self.assertEqual(got["linkedin:4"], ("unknown", "manual"))  # never touched
        self.assertEqual(claude_calls, ["Programmer"])

    def test_exact_mode_from_a_duplicate_settles_an_unknown_one(self):
        app.init_db()
        app.migrate_db()
        with app.db() as con:
            con.execute("DELETE FROM jobs")
            con.execute("INSERT INTO jobs(id,source,title,company,url,score,status,first_seen,work_mode,dupkey,urlkey) "
                        "VALUES('linkedin:9','linkedin','Unity Dev','Acme','https://www.linkedin.com/jobs/view/9999999/',5,'new','2026-11-25',"
                        "'unknown',?, 'li:9999999')", (app.dup_key("Unity Dev", "Acme", ""),))
            row = app.find_duplicate(con, "Unity Dev", "", "https://www.linkedin.com/comm/jobs/view/9999999/?x=1")
            app.merge_into(con, row, "mail", {"work_mode": "hybrid"})
            r = con.execute("SELECT work_mode, mode_src FROM jobs WHERE id='linkedin:9'").fetchone()
        self.assertEqual((r["work_mode"], r["mode_src"]), ("hybrid", "source"))


class UnknownReasons(unittest.TestCase):
    def test_reason_shown_for_each_case(self):
        base = {"id": "x", "work_mode": "unknown", "mode_src": None}
        cases = [
            (dict(base, source="linkedin", urlkey="li:1", description=""), "LinkedIn hides it, text not downloaded yet"),
            (dict(base, source="linkedin", urlkey="li:1", description="long text", mode_note="?li_hidden|claude"),
             "LinkedIn hides it, text doesn't say"),
            (dict(base, source="flexa", urlkey="u", description="", mode_note="?board_none|"), "Flexa doesn't give it"),
            (dict(base, source="mail", urlkey="u", description="", mode_note="?mail_none|"), "the e-mail doesn't say"),
            (dict(base, source="rss", urlkey="u", description="some text"), "not checked yet"),
        ]
        for r, short in cases:
            self.assertEqual(app.unknown_reason(r)[0], short)
            self.assertIn(app.e(short), app.mode_picker(r))
        long_ = app.unknown_reason(cases[1][0])[1]
        self.assertIn("rules and Claude", long_)
        self.assertNotIn("· ", app.mode_picker(dict(base, source="linkedin", urlkey="li:1", description="", mode_src="manual",
                                                     work_mode="remote")).split("</summary>")[0].replace("▾", ""))

    def test_linkedin_waiting_for_text_is_not_marked_checked_and_reasons_are_stored(self):
        import cvgen
        from unittest import mock
        app.init_db()
        app.migrate_db()
        with app.db() as con:
            con.execute("DELETE FROM jobs")
            for jid, src, uk, d in (("linkedin:1", "linkedin", "li:1", ""), ("linkedin:2", "linkedin", "li:2", "Great team. " * 30),
                                    ("flexa:3", "flexa", "u3", "")):
                con.execute("INSERT INTO jobs(id,source,title,url,urlkey,description,score,status,first_seen,work_mode) "
                            "VALUES(?,?,'T','https://x/'||?,?,?,5,'new','2026-11-25','unknown')", (jid, src, jid, uk, d))
        cfg = app.load_config()
        cfg["anthropic_api_key"] = "k"
        with mock.patch.object(app, "fetch", lambda u, **k: "<p>no badge</p>"), mock.patch.object(app.time, "sleep", lambda s: None), \
                mock.patch.object(cvgen, "work_mode_from_text", lambda *a: ("unknown", "")):
            app.enrich_work_modes(cfg)
        with app.db() as con:
            got = {r["id"]: (r["mode_checked"] is not None, r["mode_note"]) for r in con.execute("SELECT id, mode_checked, mode_note FROM jobs")}
        self.assertEqual(got["linkedin:1"], (False, None))              # waits for its text
        self.assertEqual(got["linkedin:2"], (True, "?li_hidden|claude"))
        self.assertEqual(got["flexa:3"], (True, "?board_none|"))


class ResetForgetsMails(unittest.TestCase):
    def test_reset_also_forgets_read_alert_emails(self):
        app.init_db()
        app.migrate_db()
        with app.db() as con:
            con.execute("INSERT OR REPLACE INTO mail_seen(msgid, processed_at, found) VALUES('<a@b>', '2026-11-25', 3)")
            con.execute("INSERT OR REPLACE INTO jobs(id,source,title,url,score,status,first_seen) VALUES('mail:z','mail','T','u',5,'new','x')")
        app.reset_job_cache()
        with app.db() as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM mail_seen").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)


class UnifiedSearch(unittest.TestCase):
    def test_old_lists_become_one_search(self):
        cfg = {"linkedin_searches": ["unity developer | European Union | remote", "unity | Hungary | any", "mobile engineer"],
               "remotive_queries": ["unity", "game developer"]}
        self.assertTrue(app.migrate_search(cfg))
        self.assertEqual(cfg["search_terms"], ["unity developer | European Union", "unity | Hungary", "mobile engineer",
                                               "game developer"])
        self.assertNotIn("linkedin_searches", cfg)
        self.assertNotIn("remotive_queries", cfg)
        self.assertFalse(app.migrate_search(cfg))  # only once

    def test_every_source_uses_the_terms(self):
        cfg = {"search_terms": ["unity | Hungary", "game developer", "Unity"], "search_locations": ["European Union", "Worldwide"]}
        self.assertEqual(app.search_keywords(cfg), ["unity", "game developer"])           # Remotive, Flexa
        self.assertEqual(app.linkedin_plan(cfg), [("unity", "Hungary"), ("game developer", "European Union"),
                                                  ("game developer", "Worldwide"), ("Unity", "European Union"),
                                                  ("Unity", "Worldwide")])

    def test_match_needs_every_word_of_a_term(self):
        kws = ["game developer", "c#"]
        self.assertTrue(app.matches_search(kws, {"title": "Senior Game Developer"}))
        self.assertTrue(app.matches_search(kws, {"title": "Backend engineer", "description": "We use C# and .NET"}))
        self.assertFalse(app.matches_search(kws, {"title": "Game Designer", "description": "Design levels for our studio"}))
        self.assertFalse(app.matches_search(kws, {"title": "Gameplay programmer"}))

    def test_listing_sources_keep_only_matches(self):
        from unittest import mock
        app.init_db()
        with app.db() as con:
            con.execute("DELETE FROM jobs")
        cfg = app.load_config()
        cfg["sources"] = {k: True for k in app.SOURCES_INFO}
        cfg["search_terms"] = ["unity"]
        cfg["search_filter"] = {"meetfrank": True, "studios": False}
        app.save_config(cfg)
        items = [{"ext_id": "1", "title": "Unity Developer", "company": "A", "location": "", "url": "https://a/1", "posted": ""},
                 {"ext_id": "2", "title": "Accountant", "company": "B", "location": "", "url": "https://b/2", "posted": ""}]
        fake = {"meetfrank": lambda c: [dict(i) for i in items],
                "studios": lambda c: [dict(i, ext_id="s" + i["ext_id"], company="S" + i["company"], url=i["url"] + "s") for i in items]}
        with mock.patch.dict(app.SOURCE_FUNCS, fake), mock.patch.object(app, "fetch_logos", lambda: None), \
                mock.patch.object(app, "notify_jobs", lambda c, j: []):
            app.run_all(force=True, only={"meetfrank", "studios"})
        with app.db() as con:
            ids = {r[0] for r in con.execute("SELECT id FROM jobs")}
        self.assertEqual(ids, {"meetfrank:1", "studios:s1", "studios:s2"})


class ChipScoring(unittest.TestCase):
    def test_order_is_the_score(self):
        cfg = {"scoring": {"interests": ["unity / unity3d", "game", "senior"], "desc": ["c#"], "less": ["artist"],
                           "never": ["producer"], "loc_good": ["hungary"], "loc_bad": ["india"]}}
        self.assertEqual(app.score_job(cfg, {"title": "Senior Unity3D Developer"})[0], 6 + 4)
        self.assertEqual(app.score_job(cfg, {"title": "Game Producer"})[0], 5 - 8)
        self.assertEqual(app.score_job(cfg, {"title": "Unity Artist", "location": "Budapest, Hungary"})[0], 6 - 3 + 3)
        self.assertEqual(app.score_job(cfg, {"title": "Game Dev", "location": "Pune, India"})[0], 5 - 5)
        # a description chip counts once, and not again when the title already has the word
        self.assertEqual(app.score_job(cfg, {"title": "Unity Dev", "description": "C# and Unity"})[0], 6 + 3)

    def test_old_lists_become_ordered_chips(self):
        saved = {"title_keywords": {"c#": 3, "unity": 6, "game": 3}, "body_keywords": {"unity": 3},
                 "negative_keywords": {"junior": -6, "designer": -3}, "location_keywords": {"hungary": 3, "india": -5}}
        cfg = dict(saved)
        self.assertTrue(app.migrate_scoring(cfg, saved))
        self.assertEqual(cfg["scoring"], {"interests": ["unity", "c#", "game"], "desc": ["unity"], "never": ["junior"],
                                          "less": ["designer"], "loc_good": ["hungary"], "loc_bad": ["india"]})
        self.assertNotIn("title_keywords", cfg)

    def test_chips_from_the_form(self):
        self.assertEqual(app.parse_chips(" Unity \n\nunity\nC#,\n  game   designer "), ["Unity", "C#", "game designer"])

    def test_settings_page_has_the_chip_editor(self):
        h = app.settings_page()
        self.assertIn('class="textarea ui-mono chips-src"', h)
        self.assertIn('id="score_try"', h)
        self.assertIn('id="presets"', h)


class Presets(unittest.TestCase):
    def test_save_switch_and_templates(self):
        from unittest import mock
        app.init_db()
        patcher = mock.patch.object(app, "search_again", lambda: 0)  # no real fetching in the tests
        patcher.start()
        self.addCleanup(patcher.stop)
        cfg = app.load_config()
        cfg["search_terms"] = ["unity developer"]
        cfg["scoring"]["interests"] = ["unity"]
        cfg["presets"], cfg["active_preset"] = {}, ""
        app.save_config(cfg)
        self.assertIn("Saved", app.preset_action({"preset_do": "save", "preset_name": "Unity"}))
        self.assertIn("game-designer", app.template_presets())
        self.assertIn("Switched", app.preset_action({"preset_do": "load", "preset_pick": "tpl:game-designer"}))
        cfg = app.load_config()
        self.assertIn("game designer", cfg["scoring"]["interests"])
        self.assertEqual(cfg["active_preset"], "tpl:game-designer")   # the template stays selected
        self.assertEqual(cfg["presets"]["Unity"]["search_terms"], ["unity developer"])  # the saved one is untouched
        app.preset_action({"preset_do": "load", "preset_pick": "Unity"})
        cfg = app.load_config()
        self.assertEqual((cfg["search_terms"], cfg["scoring"]["interests"], cfg["active_preset"]), (["unity developer"], ["unity"], "Unity"))
        self.assertIn("deleted", app.preset_action({"preset_do": "delete", "preset_pick": "Unity"}))
        self.assertEqual(app.load_config()["active_preset"], "")


class NewSearch(unittest.TestCase):
    def test_clear_keeps_what_you_worked_with(self):
        app.init_db()
        with app.db() as con:
            for t in ("jobs", "cvs", "outreach"):
                con.execute("DELETE FROM %s" % t)
            rows = [("linkedin:1", "linkedin", "new"), ("linkedin:2", "linkedin", "saved"), ("flexa:3", "flexa", "seen"),
                    ("flexa:4", "flexa", "hidden"), ("mail:5", "mail", "new"), ("manual:6", "manual", "new"),
                    ("studios:7", "studios", "new"), ("studios:8", "studios", "new")]
            for jid, src, st in rows:
                con.execute("INSERT INTO jobs(id,source,title,url,score,status,first_seen) VALUES(?,?,?,?,5,?,'x')", (jid, src, jid, "u", st))
            con.execute("INSERT INTO cvs(job_id,status) VALUES('studios:7','done')")
            con.execute("INSERT INTO outreach(job_id,name) VALUES('studios:8','A')")
            con.execute("UPDATE jobs SET dup_of='linkedin:1' WHERE id='mail:5'")
        self.assertEqual(app.clear_for_new_search(), 2)
        with app.db() as con:
            left = {r[0] for r in con.execute("SELECT id FROM jobs")}
            self.assertIsNone(con.execute("SELECT dup_of FROM jobs WHERE id='mail:5'").fetchone()[0])  # shown again
        self.assertEqual(left, {"linkedin:2", "flexa:4", "mail:5", "manual:6", "studios:7", "studios:8"})

    def test_only_a_search_change_searches_again(self):
        from unittest import mock
        app.init_db()
        cfg = app.load_config()
        self.addCleanup(app.save_config, json.loads(json.dumps(cfg)))  # the other tests share this config
        cfg["presets"], cfg["active_preset"] = {}, ""
        app.save_config(cfg)
        form = {"ntfy_server": "https://ntfy.sh", "interval": "60", "search_terms": "unity", "search_locations": "European Union",
                "sc_interests": "unity", "min_score": "4", "src_linkedin": "on"}
        calls = []
        with mock.patch.object(app, "search_again", lambda: calls.append(1) or 0):
            app.save_settings(form)                               # first save may change the search
            calls.clear()
            self.assertFalse(app.save_settings(dict(form, sc_interests="unity\ngame")))  # scoring only
            self.assertEqual(calls, [])
            self.assertTrue(app.save_settings(dict(form, search_terms="game designer")))
            self.assertEqual(calls, [1])

    def test_settings_page_has_the_leave_dialog(self):
        h = app.settings_page()
        self.assertIn('id="leave_dlg"', h)
        self.assertIn('id="settings_next"', h)


class PresetPickedInTheForm(unittest.TestCase):
    def setUp(self):
        from unittest import mock
        app.init_db()
        cfg = app.load_config()
        self.addCleanup(app.save_config, json.loads(json.dumps(cfg)))
        p = mock.patch.object(app, "search_again", lambda: 0)
        p.start()
        self.addCleanup(p.stop)
        self.unity = {"search_terms": ["unity"], "scoring": {"interests": ["unity"]}}
        cfg["presets"], cfg["active_preset"] = {"Unity": self.unity}, "Unity"
        app.apply_preset(cfg, self.unity)
        app.save_config(cfg)
        self.form = {"ntfy_server": "https://ntfy.sh", "interval": "60", "search_terms": "unity", "sc_interests": "unity",
                     "min_score": "4", "active_preset_set": "Unity", "preset_name": ""}

    def test_unchanged_keeps_the_preset(self):
        res = app.save_settings(self.form)
        cfg = app.load_config()
        self.assertEqual((cfg["active_preset"], list(cfg["presets"]), res.preset_note), ("Unity", ["Unity"], ""))

    def test_changed_becomes_a_new_preset(self):
        res = app.save_settings(dict(self.form, sc_interests="unity\ngame"))
        cfg = app.load_config()
        self.assertEqual(cfg["active_preset"], "New preset")
        self.assertEqual(cfg["presets"]["Unity"], self.unity)                 # the picked one stays as it was
        self.assertEqual(cfg["presets"]["New preset"]["scoring"]["interests"], ["unity", "game"])
        self.assertIn("New preset", res.preset_note)
        app.save_settings(dict(self.form, active_preset_set="New preset", sc_interests="unity\nc#"))
        self.assertEqual(app.load_config()["active_preset"], "New preset 2")
        app.save_settings(dict(self.form, active_preset_set="Unity", sc_interests="c#", preset_name="Unity"))  # named: updated
        cfg = app.load_config()
        self.assertEqual((cfg["active_preset"], cfg["presets"]["Unity"]["scoring"]["interests"]), ("Unity", ["c#"]))

    def test_a_picked_template_stays_selected(self):
        tpl = app.template_presets()["game-designer"]
        form = dict(self.form, active_preset_set="tpl:game-designer", search_terms="\n".join(tpl["search_terms"]),
                    search_locations="\n".join(tpl["search_locations"]), min_score=str(tpl["min_score"]),
                    **{"sc_" + k: "\n".join(v) for k, v in tpl["scoring"].items()},
                    **{"sf_" + k: "on" for k, v in tpl["search_filter"].items() if v})
        app.save_settings(form)
        cfg = app.load_config()
        self.assertEqual(cfg["active_preset"], "tpl:game-designer")
        self.assertIn('value="tpl:game-designer" selected', app.settings_page())

    def test_rename(self):
        self.assertIn("renamed", app.preset_action({"preset_do": "rename", "preset_name": "Unity dev"}))
        cfg = app.load_config()
        self.assertEqual((cfg["active_preset"], list(cfg["presets"])), ("Unity dev", ["Unity dev"]))

    def test_page_carries_the_presets_for_the_form(self):
        h = app.settings_page()
        self.assertIn('id="preset_data"', h)
        self.assertIn('"tpl:game-designer"', h)
        self.assertNotIn('value="load"', h)   # no server-side switch button any more


class CvPostingsStay(unittest.TestCase):
    def test_reset_brings_back_the_postings_with_a_cv(self):
        app.init_db()
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "info.json"), "w", encoding="utf-8") as fh:
            json.dump({"job_id": "linkedin:777", "title": "Senior Unity Developer", "company": "Acme", "location": "Remote",
                       "url": "https://www.linkedin.com/jobs/view/777/", "work_mode": "remote", "posted": "2026-11-20"}, fh)
        with app.db() as con:
            con.execute("DELETE FROM cvs")
            con.execute("INSERT INTO cvs(job_id,status,description,created,doc_dir) VALUES('linkedin:777','done','We use Unity.','2026-11-21T10:00:00+00:00',?)", (d,))
            con.execute("INSERT INTO cvs(job_id,status) VALUES('linkedin:888','error')")   # no documents: nothing to bring back
        app.reset_job_cache()
        with app.db() as con:
            rows = [dict(r) for r in con.execute("SELECT id, status, title, company, work_mode, urlkey FROM jobs")]
        self.assertEqual(rows, [{"id": "linkedin:777", "status": "applied", "title": "Senior Unity Developer", "company": "Acme",
                                 "work_mode": "remote", "urlkey": app.url_key("https://www.linkedin.com/jobs/view/777/")}])
        # fetched again later (e.g. from an alert e-mail): merged into the one with the CV, not a second card
        with app.db() as con:
            same = app.find_duplicate(con, "Senior Unity Developer", "Acme", "https://www.linkedin.com/jobs/view/777/?trk=x")
        self.assertEqual(same["id"], "linkedin:777")
        with app.db() as con:
            con.execute("DELETE FROM cvs")
            con.execute("DELETE FROM jobs")


class ClearCacheButton(unittest.TestCase):
    def test_every_page_has_the_button_and_the_dialog(self):
        h = app.page("X", "<p>x</p>")
        self.assertIn("Clear cache</button>", h)
        self.assertIn('<dialog id="clear_dlg"', h)
        self.assertIn('action="/reset-jobs"', h)
        self.assertNotIn("confirm(", app.settings_page())   # no browser confirm any more

    def test_back_with_message(self):
        self.assertEqual(app.with_msg("/?work=all&msg=old", "Done"), "/?work=all&msg=Done")
        self.assertEqual(app.with_msg("//evil.example/x", "Hi"), "/?msg=Hi")


class DecisionsSurviveReset(unittest.TestCase):
    def test_hidden_stays_hidden_after_a_reset(self):
        from unittest import mock
        app.init_db()
        app.migrate_db()
        with app.db() as con:
            for t in ("jobs", "job_marks", "cvs"):
                con.execute("DELETE FROM %s" % t)
        cfg = app.load_config()
        self.addCleanup(app.save_config, json.loads(json.dumps(cfg)))
        cfg["sources"] = {k: True for k in app.SOURCES_INFO}
        cfg["search_filter"] = {}
        app.save_config(cfg)
        items = [{"ext_id": "1", "title": "Unity Developer", "company": "Acme", "location": "", "url": "https://acme.example/jobs/1", "posted": ""},
                 {"ext_id": "2", "title": "Game Programmer", "company": "Beta", "location": "", "url": "https://beta.example/jobs/2", "posted": ""}]
        run = lambda srcs: app.run_all(force=True, only=srcs)
        with mock.patch.dict(app.SOURCE_FUNCS, {"meetfrank": lambda c: [dict(i) for i in items],
                                                "flexa": lambda c: [dict(i, ext_id="f" + i["ext_id"]) for i in items[:1]]}), \
                mock.patch.object(app, "fetch_logos", lambda: None), mock.patch.object(app, "notify_jobs", lambda c, j: []):
            run({"meetfrank"})
            with app.db() as con:
                app.set_job_status(con, "meetfrank:1", "hidden")
                app.set_job_status(con, "meetfrank:2", "saved")
            app.reset_job_cache()
            run({"meetfrank"})      # mark all new as seen survives a reset too
            with app.db() as con:
                for r in con.execute("SELECT id FROM jobs WHERE status='new'").fetchall():
                    app.set_job_status(con, r["id"], "seen")
            app.reset_job_cache()
            run({"flexa"})          # the same posting from another source
            run({"meetfrank"})
        with app.db() as con:
            st = {r["id"]: r["status"] for r in con.execute("SELECT id, status FROM jobs WHERE dup_of IS NULL")}
        self.assertEqual(st, {"flexa:f1": "hidden", "meetfrank:2": "saved"})
        with app.db() as con:                    # unhiding forgets it
            app.set_job_status(con, "flexa:f1", "new")
            self.assertIsNone(app.remembered_status(con, "meetfrank:1", app.url_key(items[0]["url"]), ""))
            for t in ("jobs", "job_marks"):
                con.execute("DELETE FROM %s" % t)


class SeenIsRemembered(unittest.TestCase):
    def test_seen_survives_and_new_again_forgets(self):
        app.init_db()
        app.migrate_db()
        url = "https://gamma.example/jobs/9"
        with app.db() as con:
            con.execute("DELETE FROM job_marks")
            con.execute("INSERT OR REPLACE INTO jobs(id,source,title,company,url,score,status,first_seen,urlkey,dupkey) "
                        "VALUES('rss:9','rss','Tools Programmer','Gamma',?,5,'new','x',?,?)",
                        (url, app.url_key(url), app.dup_key("Tools Programmer", "Gamma", url)))
            app.set_job_status(con, "rss:9", "seen")
            con.execute("DELETE FROM jobs WHERE id='rss:9'")                  # a cache reset
            self.assertEqual(app.remembered_status(con, "flexa:x", app.url_key(url), ""), "seen")
            con.execute("INSERT INTO jobs(id,source,title,company,url,score,status,first_seen,urlkey,dupkey) "
                        "VALUES('rss:9','rss','Tools Programmer','Gamma',?,5,'seen','x',?,?)",
                        (url, app.url_key(url), app.dup_key("Tools Programmer", "Gamma", url)))
            app.set_job_status(con, "rss:9", "new")                           # "Mark new again"
            self.assertIsNone(app.remembered_status(con, "rss:9", app.url_key(url), ""))
            con.execute("DELETE FROM jobs WHERE id='rss:9'")
