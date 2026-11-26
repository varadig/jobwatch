"""Refreshing the sources: what is due, fetching in parallel, saving, scoring, notifying."""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from datetime import timezone

from jobwatch.domain.identity import dup_key, url_key
from jobwatch.domain.scoring import score_job
from jobwatch.domain.workmode import classify_mode_ex
from jobwatch.env import log
from jobwatch.repositories.jobs import JobRepository
from jobwatch.services.documents import restore_cv_jobs
from jobwatch.services.enrich import enrich_pending_linkedin, enrich_work_modes
from jobwatch.services.logos import fetch_logos, remember_logo

from jobwatch.services.state import RUN_LOCK, STATE
from jobwatch.sources.mail import mark_mails_done
from jobwatch.sources.registry import SOURCES
from jobwatch.util.timeutil import now_iso
from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.services.notify import notifier


# Settings that decide WHAT is fetched. When one changes, the postings found so far are cleared and fetched again.
SEARCH_SETTINGS = ("sources", "search_terms", "search_locations", "search_filter", "studios", "rss_feeds", "custom_sources")


KEPT_ON_NEW_SEARCH = "kept: saved, applied and hidden postings, the ones with a CV or a recruiter message, e-mail alerts and the ones added by hand"


def search_signature(cfg):
    return json.dumps({k: cfg.get(k) for k in SEARCH_SETTINGS}, sort_keys=True, ensure_ascii=False)


def clear_for_new_search():
    """Deletes the postings the old search found, except what you worked with, and forgets the source state
    (so the first run after it seeds without a flood of notifications). Returns how many were deleted."""
    with database.connect() as con:
        n = con.execute(
            "DELETE FROM jobs WHERE source NOT IN ('mail','manual') AND COALESCE(status,'new') IN ('new','seen') "
            "AND id NOT IN (SELECT job_id FROM cvs WHERE job_id IS NOT NULL) "
            "AND id NOT IN (SELECT job_id FROM outreach WHERE job_id IS NOT NULL)").rowcount
        con.execute("UPDATE jobs SET dup_of=NULL WHERE dup_of IS NOT NULL AND dup_of NOT IN (SELECT id FROM jobs)")
        con.execute("DELETE FROM source_state WHERE source NOT IN ('mail')")
    return n


def search_again():
    """Clears the old results and fetches every source again, in the background (after a running refresh)."""
    n = clear_for_new_search()

    def go():
        while RUN_LOCK.locked():
            time.sleep(2)
        refresher.run(force=True)
    threading.Thread(target=go, daemon=True).start()
    return n


def reset_job_cache():
    """Deletes the found postings and the source status, and forgets which alert e-mails were read, so the
    next refresh reads those e-mails again (otherwise their postings would not come back)."""
    with database.connect() as con:
        con.execute("DELETE FROM jobs")
        con.execute("DELETE FROM source_state")
        con.execute("DELETE FROM mail_seen")
        restore_cv_jobs(con)  # the postings with a CV stay (from their documents)


class Refresher:
    """Refreshes the sources: decides which are due, fetches them in parallel, then saves what each found, one source
    at a time in the order they finish (scored, deduplicated, your earlier decisions applied), and notifies about the
    relevant new postings. One refresh runs at a time; STATE shows its progress on the pages."""

    def __init__(self, sources=SOURCES):
        self.sources = sources

    def run(self, force=False, only=None, custom_ids=None):
        """The due sources (all of them with force). only: just these source keys (forced); custom_ids: just these
        own sources (the "custom" source then runs with only them)."""
        if not RUN_LOCK.acquire(blocking=False):
            return
        STATE["running"] = True
        try:
            with config_store.lock:
                cfg = config_store.load()
            if custom_ids:
                cfg["custom_sources"] = [s for s in cfg.get("custom_sources") or [] if s.get("id") in custom_ids]
                only = set(only or ()) | {"custom"}
            due = self.due(cfg, force, only)
            STATE["progress"] = {"total": len(due), "done": 0, "sources": {s.key: "fetching" for s, _ in due}}
            to_notify = []
            for source, seeded, items, err in self._fetch_all(cfg, due):
                to_notify += self._save(cfg, source, seeded, items, err)
            self._notify(cfg, to_notify)
            self._follow_up(cfg, due)
        finally:
            STATE["running"] = False
            STATE["last_run"] = now_iso()
            STATE["progress"] = None
            RUN_LOCK.release()

    def due(self, cfg, force=False, only=None):
        """[(source, seeded)]: the switched-on sources whose interval has passed (with force or only: all of those)."""
        out = []
        with database.connect() as con:
            for source in self.sources:
                if only is not None and source.key not in only:
                    continue
                if cfg["sources"].get(source.key) and self.is_due(con, cfg, source, force or only is not None):
                    st = con.execute("SELECT seeded FROM source_state WHERE source=?", (source.key,)).fetchone()
                    out.append((source, bool(st and st["seeded"])))
        return out

    @staticmethod
    def is_due(con, cfg, source, force=False):
        if force:
            return True
        row = con.execute("SELECT last_run FROM source_state WHERE source=?", (source.key,)).fetchone()
        if not row or not row["last_run"]:
            return True
        last = datetime.fromisoformat(row["last_run"])
        return (datetime.now(timezone.utc) - last).total_seconds() >= source.interval(cfg) * 60 - 30

    @staticmethod
    def _fetch_all(cfg, due):
        """Fetches the sources in parallel; yields (source, seeded, items, error) as each one finishes."""
        if not due:
            return
        pool = ThreadPoolExecutor(max_workers=len(due))
        futs = {pool.submit(source.fetch, cfg): (source, seeded) for source, seeded in due}
        pool.shutdown(wait=False)
        for fut in as_completed(futs):
            source, seeded = futs[fut]
            try:
                yield source, seeded, fut.result(), None
            except Exception as ex:
                log.exception("Source error: %s", source.key)
                yield source, seeded, [], "%s: %s" % (type(ex).__name__, ex)

    def _save(self, cfg, source, seeded, items, err):
        """Saves one source's postings and its status. Returns the new postings to notify about."""
        ts = now_iso()
        keywords = source.search_filter(cfg)
        new = skipped = 0
        to_notify = []
        with database.connect() as con:
            for it in items:
                if not it.get("title") or not it.get("url", "").startswith("http"):
                    continue
                outcome = self._save_posting(con, cfg, source, seeded, it, ts, keywords)
                if outcome == "skipped":
                    skipped += 1
                elif outcome:
                    new += 1
                    if isinstance(outcome, dict):
                        to_notify.append(outcome)
            con.execute(
                "INSERT INTO source_state(source,last_run,last_ok,found,new,error,seeded) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run, "
                "last_ok=COALESCE(excluded.last_ok, source_state.last_ok), found=excluded.found, "
                "new=excluded.new, error=excluded.error, seeded=MAX(source_state.seeded, excluded.seeded)",
                (source.key, ts, None if err else ts, len(items), new, err, 0 if err else 1))
        if source.key == "mail" and not err:
            mark_mails_done(cfg.pop("_mail_done", []))  # only now that their postings are saved
        prog = STATE["progress"]
        prog["done"] += 1
        prog["sources"][source.key] = ("error" if err else "done") + " (%d)" % len(items)
        log.info("%s: %d postings, %d new%s%s", source.key, len(items), new,
                 (", %d not matching the search" % skipped) if skipped else "", " ERROR: " + err if err else "")
        return to_notify

    @staticmethod
    def _save_posting(con, cfg, source, seeded, it, ts, keywords):
        """Saves one posting. Returns None (already known, or a duplicate), "skipped" (not matching the search),
        True (new) or, for a new one to notify about, its summary."""
        name = source.key
        jobs = JobRepository(con)
        jid = "%s:%s" % (name, it["ext_id"])
        remember_logo(con, it.get("company"), it.get("logo"))
        sc, hits = score_job(cfg, it)
        # without an employer the title alone is not enough to call two postings the same
        dup = dup_key(it["title"], it["company"], it["url"])
        if it.get("work_mode") in ("remote", "hybrid", "onsite"):   # the source says it: exact
            wm, msrc, mnote = it["work_mode"], "source", "from " + source.label
        else:
            wm, mnote = classify_mode_ex(it["title"], it["location"], it.get("description"))
            msrc = "text" if wm != "unknown" else None
        known = con.execute("SELECT id, mode_src FROM jobs WHERE id=?", (jid,)).fetchone()
        if known:
            keep = (known["mode_src"] or "") in ("manual", "source", "page")  # never overwrite what is exact
            if it.get("description") and name == "linkedin":
                con.execute("UPDATE jobs SET last_seen=?, description=?, score=?, matched=? WHERE id=?",
                            (ts, it["description"], sc, ", ".join(hits), jid))
            else:
                con.execute("UPDATE jobs SET last_seen=? WHERE id=?", (ts, jid))
            if wm != "unknown" and (msrc == "source" or not keep) and (known["mode_src"] or "") != "manual":
                con.execute("UPDATE jobs SET work_mode=?, mode_src=?, mode_note=? WHERE id=?", (wm, msrc, mnote, jid))
            return None
        if not source.keeps(keywords, it):
            return "skipped"   # this source lists everything: keep only what matches a search term
        same = jobs.find_duplicate(it["title"], it["company"], it["url"])
        if same:
            jobs.merge_into(same, name, it)   # already listed from another source
            return None
        kept = jobs.remembered_status(jid, url_key(it["url"]), dup)   # hidden / saved / applied before
        hand = jobs.remembered_mode(jid, url_key(it["url"]), dup)     # a work mode you set by hand before
        if hand:
            wm, msrc, mnote = hand, "manual", "set by you"
        notified = 0 if (seeded and not kept and sc >= cfg["notify_min_score"]
                         and wm in cfg.get("notify_work_modes", ["remote", "unknown"])) else 1
        con.execute(
            "INSERT INTO jobs(id,source,title,company,location,url,posted,description,score,matched,"
            "dupkey,first_seen,last_seen,status,notified,work_mode,urlkey,mode_src,mode_note) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (jid, name, it["title"], it["company"], it["location"], it["url"], it["posted"],
             it.get("description", ""), sc, ", ".join(hits), dup, ts, ts, kept or "new", notified, wm,
             url_key(it["url"]), msrc, mnote))
        if notified:
            return True
        return dict(id=jid, source=name, score=sc, **{k: it[k] for k in ("title", "company", "location", "url")})

    @staticmethod
    def _notify(cfg, to_notify):
        """Notifies about the new relevant postings; marks them notified if at least one channel worked."""
        if not to_notify:
            return
        errs = notifier.new_jobs(cfg, to_notify)
        ok = not errs or (cfg["ntfy"].get("enabled") and not any(e.startswith("ntfy") for e in errs)) or \
            (cfg["email"].get("enabled") and not any(e.startswith("e-mail") for e in errs))
        if ok:
            with database.connect() as con:
                con.executemany("UPDATE jobs SET notified=1 WHERE id=?", [(j["id"],) for j in to_notify])

    @staticmethod
    def _follow_up(cfg, due):
        """The work done after a refresh, in the background so the list is not held up: LinkedIn descriptions,
        company logos, and the work modes still unknown."""
        if any(source.key in ("linkedin", "mail") for source, _ in due):
            threading.Thread(target=enrich_pending_linkedin, args=(cfg,), daemon=True).start()
        threading.Thread(target=fetch_logos, daemon=True).start()
        threading.Thread(target=enrich_work_modes, args=(cfg,), daemon=True).start()


refresher = Refresher()


def rescore_all():
    with config_store.lock:
        cfg = config_store.load()
    with database.connect() as con:
        for r in con.execute("SELECT * FROM jobs").fetchall():
            sc, hits = score_job(cfg, dict(r))
            con.execute("UPDATE jobs SET score=?, matched=? WHERE id=?", (sc, ", ".join(hits), r["id"]))


def scheduler():
    time.sleep(5)
    while True:
        try:
            refresher.run()
        except Exception:
            log.exception("Scheduler error")
        time.sleep(60)


