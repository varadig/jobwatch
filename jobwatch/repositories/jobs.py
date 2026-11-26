"""Postings in the database: status and work mode (remembered across clearing), duplicates."""

from jobwatch.domain.identity import dup_key, mark_keys, url_key
from jobwatch.models import KEPT_STATUSES, SOURCES_INFO
from jobwatch.util.timeutil import now_iso


def set_job_status(con, jid, status):
    """Sets a posting's status and remembers it (or forgets it, for new / seen)."""
    con.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))
    r = con.execute("SELECT title, company, urlkey, dupkey FROM jobs WHERE id=?", (jid,)).fetchone()
    keys = mark_keys(jid, r["urlkey"], r["dupkey"]) if r else mark_keys(jid, "", "")
    if status in KEPT_STATUSES:
        con.executemany("INSERT OR REPLACE INTO job_marks(k, status, title, company, at) VALUES(?,?,?,?,?)",
                        [(k, status, r["title"] if r else "", r["company"] if r else "", now_iso()) for k in keys])
    else:  # forget it under every key of the same posting (also the ones from other sources)
        con.executemany("DELETE FROM job_marks WHERE k=?", [(k,) for k in keys])
        if r and r["title"]:
            con.execute("DELETE FROM job_marks WHERE title=? AND COALESCE(company,'')=?", (r["title"], r["company"] or ""))


def remembered_status(con, jid, urlkey, dupkey):
    keys = mark_keys(jid, urlkey, dupkey)
    r = con.execute("SELECT status FROM job_marks WHERE k IN (%s) ORDER BY at DESC LIMIT 1" % ",".join("?" * len(keys)),
                    keys).fetchone() if keys else None
    return r["status"] if r else None


def set_manual_mode(con, jid, mode):
    """Remembers the work mode you set by hand for a posting (mode None: forgets it)."""
    r = con.execute("SELECT title, company, urlkey, dupkey FROM jobs WHERE id=?", (jid,)).fetchone()
    keys = mark_keys(jid, r["urlkey"], r["dupkey"]) if r else mark_keys(jid, "", "")
    if mode:
        con.executemany("INSERT OR REPLACE INTO job_modes(k, mode, title, company, at) VALUES(?,?,?,?,?)",
                        [(k, mode, r["title"] if r else "", r["company"] if r else "", now_iso()) for k in keys])
    else:
        con.executemany("DELETE FROM job_modes WHERE k=?", [(k,) for k in keys])
        if r and r["title"]:
            con.execute("DELETE FROM job_modes WHERE title=? AND COALESCE(company,'')=?", (r["title"], r["company"] or ""))


def remembered_mode(con, jid, urlkey, dupkey):
    keys = mark_keys(jid, urlkey, dupkey)
    r = con.execute("SELECT mode FROM job_modes WHERE k IN (%s) ORDER BY at DESC LIMIT 1" % ",".join("?" * len(keys)),
                    keys).fetchone() if keys else None
    return r["mode"] if r else None


def find_duplicate(con, title, company, url):
    """The posting already in the list that is the same as this one (or None)."""
    return con.execute("SELECT id, source, company, description, also_sources FROM jobs WHERE (dupkey=? OR urlkey=?) "
                       "AND dup_of IS NULL LIMIT 1", (dup_key(title, company, url), url_key(url))).fetchone()


def merge_into(con, row, source, it):
    """Notes that the posting also came from another source, and fills in what the first one lacked."""
    also = [s for s in (row["also_sources"] or "").split(",") if s]
    if source != row["source"] and source not in also:
        also.append(source)
    con.execute("UPDATE jobs SET also_sources=?, company=CASE WHEN COALESCE(company,'')='' THEN ? ELSE company END, "
                "description=CASE WHEN COALESCE(description,'')='' THEN ? ELSE description END WHERE id=?",
                (",".join(also), it.get("company") or "", it.get("description") or "", row["id"]))
    # an exact work mode from the other source (e.g. "(Hybrid)" in a LinkedIn alert e-mail) settles an unknown one
    if it.get("work_mode") in ("remote", "hybrid", "onsite"):
        con.execute("UPDATE jobs SET work_mode=?, mode_src='source', mode_note=? WHERE id=? AND "
                    "COALESCE(work_mode,'unknown')='unknown' AND COALESCE(mode_src,'')!='manual'",
                    (it["work_mode"], "from " + SOURCES_INFO.get(source, (source,))[0], row["id"]))


def dedupe_existing(con):
    """Hides the duplicates already in the list (dup_of = the one kept; nothing is deleted). The one kept is the
    one you worked on most: a CV, then applied > saved > seen > new, then the oldest."""
    rank = {"applied": 4, "saved": 3, "seen": 2, "new": 1}
    with_cv = {r["job_id"] for r in con.execute("SELECT job_id FROM cvs")}
    rows = [dict(r) for r in con.execute("SELECT id, source, status, first_seen, dupkey, urlkey, also_sources FROM jobs WHERE dup_of IS NULL")]
    groups = {}
    for r in rows:
        for k in ("d:" + (r["dupkey"] or ""), "u:" + (r["urlkey"] or "")):
            if len(k) > 2:
                groups.setdefault(k, []).append(r)
    hidden = 0
    for members in groups.values():
        members = [m for m in members if not m.get("_gone")]
        if len(members) < 2:
            continue
        members.sort(key=lambda m: (m["id"] not in with_cv, -rank.get(m["status"], 0), m["first_seen"] or ""))
        keep = members[0]
        also = [s for s in (keep["also_sources"] or "").split(",") if s]
        for m in members[1:]:
            m["_gone"] = True
            con.execute("UPDATE jobs SET dup_of=? WHERE id=?", (keep["id"], m["id"]))
            hidden += 1
            if m["source"] != keep["source"] and m["source"] not in also:
                also.append(m["source"])
        keep["also_sources"] = ",".join(also)
        con.execute("UPDATE jobs SET also_sources=? WHERE id=?", (keep["also_sources"], keep["id"]))
    return hidden
