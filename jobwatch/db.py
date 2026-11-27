"""The SQLite database: connections, the schema and its migrations."""
import os
import sqlite3

from jobwatch.domain.identity import dup_key, mark_keys, url_key
from jobwatch.domain.workmode import classify_mode
from jobwatch.env import DB_PATH, log
from jobwatch.models import KEPT_STATUSES
from jobwatch.repositories.jobs import JobRepository
from jobwatch.util.timeutil import now_iso


class Database:
    """The SQLite database. In WAL mode reading never waits for a write and the other way round, so a CV being
    saved, a refresh and the pages do not lock each other out ("database is locked"); writers take turns, waiting up
    to 60 s. Use a connection as a context manager: `with database.connect() as con:` commits at the end."""

    def __init__(self, path):
        self.path = path

    def connect(self):
        con = sqlite3.connect(self.path, timeout=60)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("PRAGMA synchronous=NORMAL")
        return con


    def init(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        con = sqlite3.connect(self.path, timeout=60)
        try:
            con.execute("PRAGMA journal_mode=WAL")   # stays set in the database file
        except sqlite3.OperationalError as ex:
            log.warning("Could not switch the database to WAL: %s", ex)
        finally:
            con.close()
        with self.connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS jobs(
              id TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT, location TEXT, url TEXT,
              posted TEXT, description TEXT, score INTEGER, matched TEXT, dupkey TEXT,
              first_seen TEXT, last_seen TEXT, status TEXT DEFAULT 'new', notified INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS jobs_dup ON jobs(dupkey);
            CREATE TABLE IF NOT EXISTS cvs(
              job_id TEXT PRIMARY KEY, status TEXT, error TEXT, data TEXT, description TEXT,
              created TEXT, sent_to TEXT, sent_at TEXT);
            CREATE TABLE IF NOT EXISTS outreach(
              id INTEGER PRIMARY KEY AUTOINCREMENT, created TEXT, name TEXT, title TEXT, contact TEXT,
              channel TEXT, language TEXT, job_id TEXT, note TEXT, subject TEXT, message TEXT,
              status TEXT, error TEXT, sent_at TEXT, model TEXT, via TEXT, kind TEXT,
              profile TEXT, role_ref TEXT, role_text TEXT);
            CREATE TABLE IF NOT EXISTS mail_seen(
              msgid TEXT PRIMARY KEY, processed_at TEXT, found INTEGER);
            CREATE TABLE IF NOT EXISTS company_logos(
              ckey TEXT PRIMARY KEY, url TEXT, file TEXT, tried TEXT);
            CREATE TABLE IF NOT EXISTS job_modes(
              k TEXT PRIMARY KEY, mode TEXT, title TEXT, company TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS job_marks(
              k TEXT PRIMARY KEY, status TEXT, title TEXT, company TEXT, at TEXT);
            CREATE TABLE IF NOT EXISTS interviews(
              id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, company TEXT, role TEXT, starts TEXT, minutes INTEGER,
              place TEXT, people TEXT, notes TEXT, source TEXT, uid TEXT, msgid TEXT, status TEXT DEFAULT 'planned',
              reminded_day TEXT, reminded_soon TEXT, research TEXT, research_sources TEXT, research_status TEXT,
              research_at TEXT, research_error TEXT, created TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS interview_mail_seen(
              msgid TEXT PRIMARY KEY, processed_at TEXT, found INTEGER);
            CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, level TEXT, kind TEXT, job_id TEXT, ref TEXT, source TEXT,
          summary TEXT, detail TEXT);
        CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
        CREATE INDEX IF NOT EXISTS events_job ON events(job_id);
        CREATE INDEX IF NOT EXISTS events_ref ON events(ref);
        CREATE INDEX IF NOT EXISTS events_kind ON events(kind);
        CREATE TABLE IF NOT EXISTS source_state(
              source TEXT PRIMARY KEY, last_run TEXT, last_ok TEXT, found INTEGER, new INTEGER,
              error TEXT, seeded INTEGER DEFAULT 0);
            """)


    def migrate(self):
        with self.connect() as con:
            con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
            v = con.execute("SELECT v FROM meta WHERE k='mode_rules'").fetchone()
            if not v or v["v"] != "2":
                # new rules: the LinkedIn filter is unreliable -> re-classify from the text
                if "work_mode" in [r["name"] for r in con.execute("PRAGMA table_info(jobs)")]:
                    for r in con.execute("SELECT id, source, title, location, description FROM jobs WHERE source IN ('linkedin','4dayweek','flexa','rss')").fetchall():
                        con.execute("UPDATE jobs SET work_mode=? WHERE id=?",
                                    (classify_mode(r["title"], r["location"], r["description"]), r["id"]))
                con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('mode_rules','2')")
            ccols = [r["name"] for r in con.execute("PRAGMA table_info(cvs)")]
            if ccols and "doc_dir" not in ccols:
                con.execute("ALTER TABLE cvs ADD COLUMN doc_dir TEXT")
            ocols = [r["name"] for r in con.execute("PRAGMA table_info(outreach)")]
            for col in ("kind", "profile", "role_ref", "role_text"):
                if ocols and col not in ocols:
                    con.execute("ALTER TABLE outreach ADD COLUMN %s TEXT" % col)
            cols = [r["name"] for r in con.execute("PRAGMA table_info(jobs)")]
            if "work_mode" not in cols:
                con.execute("ALTER TABLE jobs ADD COLUMN work_mode TEXT DEFAULT 'unknown'")
                for r in con.execute("SELECT id, source, title, location, description FROM jobs").fetchall():
                    m = "remote" if r["source"] == "remotive" else classify_mode(r["title"], r["location"], r["description"])
                    con.execute("UPDATE jobs SET work_mode=? WHERE id=?", (m, r["id"]))
            # mode_src: where the work mode comes from (source / page / text / claude / manual); mode_note: the evidence
            for col in ("urlkey", "also_sources", "dup_of", "mode_src", "mode_note", "mode_checked", "desc_html"):
                if col not in cols:
                    con.execute("ALTER TABLE jobs ADD COLUMN %s TEXT" % col)
            con.execute("CREATE INDEX IF NOT EXISTS jobs_url ON jobs(urlkey)")
            v = con.execute("SELECT v FROM meta WHERE k='dedupe'").fetchone()
            if not v or v["v"] != "1":
                # new duplicate rules: recompute the keys, then hide the duplicates already in the list
                for r in con.execute("SELECT id, title, company, url FROM jobs").fetchall():
                    con.execute("UPDATE jobs SET dupkey=?, urlkey=? WHERE id=?",
                                (dup_key(r["title"], r["company"], r["url"]), url_key(r["url"]), r["id"]))
                n = JobRepository(con).dedupe()
                if n:
                    log.info("Duplicates: %d posting(s) hidden", n)
                con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('dedupe','1')")
            # remember the decisions already in the list (hidden, saved, applied, seen) so a cache reset keeps them
            for r in con.execute("SELECT id, status, title, company, urlkey, dupkey FROM jobs WHERE status IN (%s)"
                                 % ",".join("?" * len(KEPT_STATUSES)), KEPT_STATUSES).fetchall():
                con.executemany("INSERT OR IGNORE INTO job_marks(k, status, title, company, at) VALUES(?,?,?,?,?)",
                                [(k, r["status"], r["title"], r["company"], now_iso()) for k in mark_keys(r["id"], r["urlkey"], r["dupkey"])])
            for r in con.execute("SELECT id, work_mode, title, company, urlkey, dupkey FROM jobs WHERE mode_src='manual' "
                                 "AND work_mode IN ('remote','hybrid','onsite')").fetchall():
                con.executemany("INSERT OR IGNORE INTO job_modes(k, mode, title, company, at) VALUES(?,?,?,?,?)",
                                [(k, r["work_mode"], r["title"], r["company"], now_iso()) for k in mark_keys(r["id"], r["urlkey"], r["dupkey"])])
            if not con.execute("SELECT 1 FROM meta WHERE k='cv_applied'").fetchone():
                con.execute("UPDATE jobs SET status='applied' WHERE COALESCE(status,'new') IN ('new','seen') "
                            "AND id IN (SELECT job_id FROM cvs WHERE status='done')")
                con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('cv_applied','1')")


database = Database(DB_PATH)
