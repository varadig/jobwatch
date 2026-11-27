"""The activity log: what jobwatch did, and why, kept in the database so it can be searched later."""
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from jobwatch.env import log
from jobwatch.util.timeutil import now_iso

# kind -> what it means (the Log page shows these)
KINDS = {
    "status": "You (or an action) changed a posting's status: hidden, saved, applied, seen, new",
    "mark_forgot": "A remembered decision about a posting was forgotten (it was set back to new / seen)",
    "job_new": "A posting entered the list for the first time",
    "restored": "A posting came back from a refresh or a cleared cache and got its remembered status back",
    "possible_repeat": "A new posting looks like one you hid, but jobwatch could not tell it is the same one",
    "duplicate": "A posting was recognised as one already in the list (another source) and merged into it",
    "dedupe": "Duplicates already in the list were hidden behind the one you worked on",
    "source_run": "One source was fetched: how many postings, how many new, how many skipped",
    "refresh": "A refresh finished",
    "cache_cleared": "The job cache was cleared (what was deleted, what was kept)",
    "new_search": "The search settings changed: old results were cleared and a new search started",
    "llm": "A call to a language model: which one answered, how long, how many tokens, errors, fallbacks",
    "notify": "A push notification or an e-mail was sent (or failed)",
    "interview": "An interview was added, found in a mailbox, changed or reminded",
    "post": "A form was sent to the server (what was changed from the pages)",
    "error": "Something failed",
}
RETENTION_DAYS = 90


class EventRepository:
    """The events table, on one connection (the caller's transaction: never open a second connection to write while
    one has a write open, they would wait for each other)."""

    def __init__(self, con):
        self.con = con

    def add(self, kind, summary, job_id=None, ref=None, source=None, level="info", **detail):
        """Writes one event; never raises (the log must not break what it observes). ref: the posting's duplicate key,
        so the history of "the same posting" can be read across its copies."""
        try:
            self.con.execute("INSERT INTO events(ts, level, kind, job_id, ref, source, summary, detail) VALUES(?,?,?,?,?,?,?,?)",
                             (now_iso(), level, kind, job_id, ref, source, (summary or "")[:500],
                              json.dumps(detail, ensure_ascii=False, default=str) if detail else None))
        except sqlite3.Error as ex:
            log.warning("Could not write the activity log: %s", ex)
        log.log({"warn": logging.WARNING, "error": logging.ERROR}.get(level, logging.INFO), "[%s] %s", kind, summary)

    def query(self, kind=None, level=None, text=None, job_id=None, ref=None, days=None, before=None, limit=200):
        """Newest first. level "warn" means warnings and errors. text searches the summary and the posting id."""
        where, args = [], []
        if kind:
            where.append("kind=?")
            args.append(kind)
        if level == "warn":
            where.append("level IN ('warn','error')")
        elif level:
            where.append("level=?")
            args.append(level)
        if text:
            where.append("(summary LIKE ? OR job_id LIKE ? OR detail LIKE ?)")
            args += ["%" + text + "%"] * 3
        if job_id or ref:
            where.append("(job_id=? OR ref=?)")
            args += [job_id or "", ref or ""]
        if days:
            where.append("ts >= ?")
            args.append((datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds"))
        if before:
            where.append("id < ?")
            args.append(before)
        rows = self.con.execute("SELECT * FROM events%s ORDER BY id DESC LIMIT ?" % (" WHERE " + " AND ".join(where) if where else ""),
                                args + [limit]).fetchall()
        return [dict(r) for r in rows]

    def counts(self, days=7):
        """{kind: how many} in the last days."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return {r["kind"]: r["n"] for r in self.con.execute(
            "SELECT kind, COUNT(*) n FROM events WHERE ts >= ? GROUP BY kind ORDER BY n DESC", (since,))}

    def prune(self, days=RETENTION_DAYS):
        """Deletes the events older than the retention; returns how many."""
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return self.con.execute("DELETE FROM events WHERE ts < ?", (cutoff,)).rowcount
