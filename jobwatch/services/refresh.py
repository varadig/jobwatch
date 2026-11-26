"""Refreshing the sources: what is due, fetching in parallel, saving, scoring, notifying."""
import json
import threading
import time
from datetime import datetime
from datetime import timezone

from jobwatch.domain.identity import dup_key, url_key
from jobwatch.domain.scoring import score_job
from jobwatch.domain.workmode import classify_mode_ex
from jobwatch.env import log
from jobwatch.models import ALWAYS_FILTERED, FETCHED_SOURCES, SOURCES_INFO, TITLE_FILTERED
from jobwatch.repositories.jobs import find_duplicate, merge_into, remembered_mode, remembered_status
from jobwatch.search import matches_search, search_keywords
from jobwatch.services.documents import restore_cv_jobs
from jobwatch.services.enrich import enrich_pending_linkedin, enrich_work_modes
from jobwatch.services.logos import fetch_logos, remember_logo

from jobwatch.services.state import RUN_LOCK, STATE
from jobwatch.sources.mail import mark_mails_done
from jobwatch.sources.registry import MIN_INTERVAL, SOURCE_FUNCS
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
        run_all(force=True)
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


def source_due(con, cfg, name, force):
    if force:
        return True
    row = con.execute("SELECT last_run FROM source_state WHERE source=?", (name,)).fetchone()
    if not row or not row["last_run"]:
        return True
    interval = max(int(cfg["interval_minutes"]), MIN_INTERVAL.get(name, 0))
    last = datetime.fromisoformat(row["last_run"])
    return (datetime.now(timezone.utc) - last).total_seconds() >= interval * 60 - 30


def run_all(force=False, only=None, custom_ids=None):
    """Refreshes the due sources (all of them with force). only: just these source names (forced);
    custom_ids: just these own sources (the "custom" source then runs with only them)."""
    if not RUN_LOCK.acquire(blocking=False):
        return
    STATE["running"] = True
    try:
        with config_store.lock:
            cfg = config_store.load()
        if custom_ids:
            cfg["custom_sources"] = [s for s in cfg.get("custom_sources") or [] if s.get("id") in custom_ids]
            only = set(only or ()) | {"custom"}
        to_notify = []
        # 1) decide which sources are due, 2) fetch them in parallel, 3) save them one by one in order
        due = []
        with database.connect() as con:
            for name, fn in SOURCE_FUNCS.items():
                if only is not None and name not in only:
                    continue
                if cfg["sources"].get(name) and source_due(con, cfg, name, force or only is not None):
                    st = con.execute("SELECT seeded FROM source_state WHERE source=?", (name,)).fetchone()
                    due.append((name, fn, bool(st and st["seeded"])))
        from concurrent.futures import ThreadPoolExecutor, as_completed
        STATE["progress"] = {"total": len(due), "done": 0, "sources": {n: "fetching" for n, _, _ in due}}
        completed = iter(())
        if due:
            pool = ThreadPoolExecutor(max_workers=len(due))
            futs = {pool.submit(fn, cfg): (name, seeded) for name, fn, seeded in due}
            pool.shutdown(wait=False)
            completed = as_completed(futs)
        for fut in completed:
            name, seeded = futs[fut]
            try:
                items, err = fut.result(), None
            except Exception as ex:
                log.exception("Source error: %s", name)
                items, err = [], "%s: %s" % (type(ex).__name__, ex)
            ts = now_iso()
            new = skipped = 0
            kws = search_keywords(cfg) if (name in ALWAYS_FILTERED or (cfg.get("search_filter") or {}).get(name)) else []
            with database.connect() as con:
                for it in items:
                    if not it.get("title") or not it.get("url", "").startswith("http"):
                        continue
                    jid = "%s:%s" % (name, it["ext_id"])
                    remember_logo(con, it.get("company"), it.get("logo"))
                    sc, hits = score_job(cfg, it)
                    # without an employer the title alone is not enough to call two postings the same
                    dup = dup_key(it["title"], it["company"], it["url"])
                    if it.get("work_mode") in ("remote", "hybrid", "onsite"):   # the source says it: exact
                        wm, msrc, mnote = it["work_mode"], "source", "from " + SOURCES_INFO.get(name, (name,))[0]
                    else:
                        wm, mnote = classify_mode_ex(it["title"], it["location"], it.get("description"))
                        msrc = "text" if wm != "unknown" else None
                    ex_row = con.execute("SELECT id, mode_src FROM jobs WHERE id=?", (jid,)).fetchone()
                    if ex_row:
                        keep = (ex_row["mode_src"] or "") in ("manual", "source", "page")  # never overwrite what is exact
                        if it.get("description") and name == "linkedin":
                            sc2, hits2 = score_job(cfg, it)
                            con.execute("UPDATE jobs SET last_seen=?, description=?, score=?, matched=? WHERE id=?",
                                        (ts, it["description"], sc2, ", ".join(hits2), jid))
                        else:
                            con.execute("UPDATE jobs SET last_seen=? WHERE id=?", (ts, jid))
                        if wm != "unknown" and (msrc == "source" or not keep) and (ex_row["mode_src"] or "") != "manual":
                            con.execute("UPDATE jobs SET work_mode=?, mode_src=?, mode_note=? WHERE id=?", (wm, msrc, mnote, jid))
                        continue
                    if kws and not matches_search(kws, it, in_title=name in TITLE_FILTERED):
                        skipped += 1  # this source lists everything: keep only what matches a search term
                        continue
                    same = find_duplicate(con, it["title"], it["company"], it["url"])
                    if same:
                        merge_into(con, same, name, it)  # already listed from another source
                        continue
                    kept = remembered_status(con, jid, url_key(it["url"]), dup)  # hidden / saved / applied before
                    hand = remembered_mode(con, jid, url_key(it["url"]), dup)    # a work mode you set by hand before
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
                    new += 1
                    if not notified:
                        to_notify.append(dict(id=jid, source=name, score=sc, **{
                            k: it[k] for k in ("title", "company", "location", "url")}))
                con.execute(
                    "INSERT INTO source_state(source,last_run,last_ok,found,new,error,seeded) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(source) DO UPDATE SET last_run=excluded.last_run, "
                    "last_ok=COALESCE(excluded.last_ok, source_state.last_ok), found=excluded.found, "
                    "new=excluded.new, error=excluded.error, seeded=MAX(source_state.seeded, excluded.seeded)",
                    (name, ts, None if err else ts, len(items), new, err, 0 if err else 1))
            if name == "mail" and not err:
                mark_mails_done(cfg.pop("_mail_done", []))  # only now that their postings are saved
            prog = STATE["progress"]
            prog["done"] += 1
            prog["sources"][name] = ("error" if err else "done") + " (%d)" % len(items)
            log.info("%s: %d postings, %d new%s%s", name, len(items), new,
                     (", %d not matching the search" % skipped) if skipped else "", " ERROR: " + err if err else "")
        errs = notifier.new_jobs(cfg, to_notify)
        if to_notify and not errs:
            with database.connect() as con:
                con.executemany("UPDATE jobs SET notified=1 WHERE id=?", [(j["id"],) for j in to_notify])
        elif to_notify:
            # if at least one channel worked, do not send again
            ok_channels = (cfg["ntfy"].get("enabled") and not any(e.startswith("ntfy") for e in errs)) or \
                          (cfg["email"].get("enabled") and not any(e.startswith("e-mail") for e in errs))
            if ok_channels:
                with database.connect() as con:
                    con.executemany("UPDATE jobs SET notified=1 WHERE id=?", [(j["id"],) for j in to_notify])
        if any(name in ("linkedin", "mail") for name, _, _ in due):
            # descriptions for LinkedIn postings come later, in the background, so the list is not held up
            threading.Thread(target=enrich_pending_linkedin, args=(cfg,), daemon=True).start()
        threading.Thread(target=fetch_logos, daemon=True).start()
        threading.Thread(target=enrich_work_modes, args=(cfg,), daemon=True).start()
    finally:
        STATE["running"] = False
        STATE["last_run"] = now_iso()
        STATE["progress"] = None
        RUN_LOCK.release()


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
            run_all()
        except Exception:
            log.exception("Scheduler error")
        time.sleep(60)


def run_selection(f):
    """From the Sources form: (source names or None for all, own-source ids). "one" = a single row's button."""
    one = f.get("one", "")
    if one.startswith("cs:"):
        return set(), {one[3:]}
    if one:
        return {one}, set()
    if f.get("all") or "selected" not in f:
        return None, set()
    names = {k for k in FETCHED_SOURCES if ("run_" + k) in f}
    cids = {k[len("run_cs_"):] for k in f if k.startswith("run_cs_")}
    if cids:
        names.discard("custom")  # only the ticked own sources, not all of them
    return (names or (set() if cids else None)), cids
