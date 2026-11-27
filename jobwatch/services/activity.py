"""Writing to the activity log from places that have no database transaction open (a refresh finishing, a notification
sent, a model answering). Inside a transaction use EventRepository on its connection instead."""
import sqlite3

from jobwatch.db import database
from jobwatch.env import log
from jobwatch.repositories.events import EventRepository

BUSY_MS = 3000   # a log line is never worth waiting for: drop it rather than hold the app up


def record(kind, summary, **fields):
    """Writes one event on its own connection. Never raises, never waits long."""
    try:
        con = database.connect()
        try:
            con.execute("PRAGMA busy_timeout=%d" % BUSY_MS)
            with con:
                EventRepository(con).add(kind, summary, **fields)
        finally:
            con.close()
    except sqlite3.Error as ex:
        log.warning("Could not write the activity log: %s", ex)


def model_call(provider, purpose, seconds, error=None, completion=None, fell_back_from=None):
    """The observer of jobwatch.llm: one event per call to a language model."""
    ok = error is None
    parts = ["%s: %s" % (provider.label, purpose or "request"), "%.1f s" % seconds]
    if completion is not None and (completion.input_tokens or completion.output_tokens):
        parts.append("%d in / %d out tokens" % (completion.input_tokens, completion.output_tokens))
    if fell_back_from:
        parts.append("after %s failed" % fell_back_from)
    if error:
        parts.append("FAILED: %s" % error)
    record("llm", ", ".join(parts), level="info" if ok else "warn", source=provider.name,
           purpose=purpose, model=getattr(completion, "model", None), seconds=round(seconds, 2), ok=ok, error=error,
           input_tokens=getattr(completion, "input_tokens", 0), output_tokens=getattr(completion, "output_tokens", 0),
           fell_back_from=fell_back_from)
