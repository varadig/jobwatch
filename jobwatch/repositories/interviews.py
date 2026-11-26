"""Interviews in the database."""
from jobwatch.repositories.jobs import JobRepository
from jobwatch.util.timeutil import now_iso


class InterviewRepository:
    """The interviews table, on one connection (the caller's transaction)."""

    def __init__(self, con):
        self.con = con

    def get(self, iid):
        r = self.con.execute("SELECT * FROM interviews WHERE id=?", (iid,)).fetchone()
        return dict(r) if r else None

    def all(self):
        return [dict(r) for r in self.con.execute("SELECT * FROM interviews ORDER BY starts")]

    def job_for(self, company):
        """The posting of a company (the one you worked with first), for linking an interview to it."""
        if not company:
            return None
        r = self.con.execute("SELECT id FROM jobs WHERE lower(company)=lower(?) ORDER BY CASE status WHEN 'applied' THEN 0 "
                             "WHEN 'saved' THEN 1 ELSE 2 END, first_seen DESC LIMIT 1", (company.strip(),)).fetchone()
        return r["id"] if r else None

    def add(self, company, role, start, minutes=60, place="", people="", notes="", job_id=None, source="manual",
            uid="", msgid=""):
        """Saves an interview and returns its id. The posting it belongs to is marked applied."""
        job_id = job_id or self.job_for(company)
        cur = self.con.execute(
            "INSERT INTO interviews(job_id, company, role, starts, minutes, place, people, notes, source, uid, msgid, "
            "status, created, updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,'planned',?,?)",
            (job_id, (company or "")[:120], (role or "")[:200], start.isoformat(timespec="minutes"), int(minutes or 60),
             (place or "")[:500], (people or "")[:500], (notes or "")[:4000], source, uid or "", msgid or "",
             now_iso(), now_iso()))
        if job_id:
            st = self.con.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if st and st["status"] in ("new", "seen", "saved"):
                JobRepository(self.con).set_status(job_id, "applied")
        return cur.lastrowid

    def update(self, iid, **fields):
        """Changes an interview. A new time starts the reminders again. False if there is no such interview."""
        r = self.get(iid)
        if not r:
            return False
        if "starts" in fields and fields["starts"] != r["starts"]:
            fields.update(reminded_day=None, reminded_soon=None)
        fields["updated"] = now_iso()
        self.con.execute("UPDATE interviews SET %s WHERE id=?" % ", ".join("%s=?" % k for k in fields),
                         list(fields.values()) + [iid])
        return True

    def delete(self, iid):
        self.con.execute("DELETE FROM interviews WHERE id=?", (iid,))
