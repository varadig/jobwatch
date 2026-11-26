"""Postings in the database: your decisions on them (remembered across a cleared cache), and duplicates."""
from jobwatch.domain.identity import dup_key, mark_keys, url_key
from jobwatch.models import KEPT_STATUSES, SOURCES_INFO
from jobwatch.util.timeutil import now_iso


class JobRepository:
    """The jobs table and what is remembered about postings, on one connection (the caller's transaction).

    Your decisions (hidden, saved, applied, seen) and the work modes you set by hand are kept apart from the job
    cache, under every key of the posting (its id, link and title + company): after a cache reset or a new search the
    same posting, from any source, gets them back."""

    def __init__(self, con):
        self.con = con

    def _keys_of(self, jid):
        r = self.con.execute("SELECT title, company, urlkey, dupkey FROM jobs WHERE id=?", (jid,)).fetchone()
        return r, (mark_keys(jid, r["urlkey"], r["dupkey"]) if r else mark_keys(jid, "", ""))

    def _remember(self, table, column, jid, value):
        """Stores (value) or forgets (None) a remembered value under every key of the posting."""
        r, keys = self._keys_of(jid)
        if value:
            self.con.executemany("INSERT OR REPLACE INTO %s(k, %s, title, company, at) VALUES(?,?,?,?,?)" % (table, column),
                                 [(k, value, r["title"] if r else "", r["company"] if r else "", now_iso()) for k in keys])
            return
        self.con.executemany("DELETE FROM %s WHERE k=?" % table, [(k,) for k in keys])
        if r and r["title"]:   # also the copies of the same posting from other sources
            self.con.execute("DELETE FROM %s WHERE title=? AND COALESCE(company,'')=?" % table, (r["title"], r["company"] or ""))

    def _recall(self, table, column, jid, urlkey, dupkey):
        keys = mark_keys(jid, urlkey, dupkey)
        if not keys:
            return None
        r = self.con.execute("SELECT %s FROM %s WHERE k IN (%s) ORDER BY at DESC LIMIT 1" % (
            column, table, ",".join("?" * len(keys))), keys).fetchone()
        return r[column] if r else None

    def set_status(self, jid, status):
        """Sets a posting's status and remembers it (new / seen: forgets it)."""
        self.con.execute("UPDATE jobs SET status=? WHERE id=?", (status, jid))
        self._remember("job_marks", "status", jid, status if status in KEPT_STATUSES else None)

    def remembered_status(self, jid, urlkey, dupkey):
        return self._recall("job_marks", "status", jid, urlkey, dupkey)

    def set_manual_mode(self, jid, mode):
        """Remembers the work mode you set by hand for a posting (None: forgets it)."""
        self._remember("job_modes", "mode", jid, mode)

    def remembered_mode(self, jid, urlkey, dupkey):
        return self._recall("job_modes", "mode", jid, urlkey, dupkey)

    def find_duplicate(self, title, company, url):
        """The posting already in the list that is the same as this one (or None)."""
        return self.con.execute("SELECT id, source, company, description, also_sources FROM jobs WHERE (dupkey=? OR urlkey=?) "
                                "AND dup_of IS NULL LIMIT 1", (dup_key(title, company, url), url_key(url))).fetchone()

    def merge_into(self, row, source, it):
        """Notes that the posting also came from another source, and fills in what the first one lacked."""
        also = [s for s in (row["also_sources"] or "").split(",") if s]
        if source != row["source"] and source not in also:
            also.append(source)
        self.con.execute("UPDATE jobs SET also_sources=?, company=CASE WHEN COALESCE(company,'')='' THEN ? ELSE company END, "
                         "description=CASE WHEN COALESCE(description,'')='' THEN ? ELSE description END WHERE id=?",
                         (",".join(also), it.get("company") or "", it.get("description") or "", row["id"]))
        # an exact work mode from the other source (e.g. "(Hybrid)" in a LinkedIn alert e-mail) settles an unknown one
        if it.get("work_mode") in ("remote", "hybrid", "onsite"):
            self.con.execute("UPDATE jobs SET work_mode=?, mode_src='source', mode_note=? WHERE id=? AND "
                             "COALESCE(work_mode,'unknown')='unknown' AND COALESCE(mode_src,'')!='manual'",
                             (it["work_mode"], "from " + SOURCES_INFO.get(source, (source,))[0], row["id"]))

    def dedupe(self):
        """Hides the duplicates already in the list (dup_of = the one kept; nothing is deleted). The one kept is the
        one you worked on most: a CV, then applied > saved > seen > new, then the oldest. Returns how many."""
        rank = {"applied": 4, "saved": 3, "seen": 2, "new": 1}
        with_cv = {r["job_id"] for r in self.con.execute("SELECT job_id FROM cvs")}
        rows = [dict(r) for r in self.con.execute(
            "SELECT id, source, status, first_seen, dupkey, urlkey, also_sources FROM jobs WHERE dup_of IS NULL")]
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
                self.con.execute("UPDATE jobs SET dup_of=? WHERE id=?", (keep["id"], m["id"]))
                hidden += 1
                if m["source"] != keep["source"] and m["source"] not in also:
                    also.append(m["source"])
            keep["also_sources"] = ",".join(also)
            self.con.execute("UPDATE jobs SET also_sources=? WHERE id=?", (keep["also_sources"], keep["id"]))
        return hidden
