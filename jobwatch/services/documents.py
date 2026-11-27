"""The CV profile and the generated documents (CV, resume, cover letter)."""
import copy
import json
import os
import re
import shutil
import unicodedata
from datetime import datetime

from jobwatch import cvgen
from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.domain.identity import dup_key, url_key
from jobwatch.domain.scoring import score_job
from jobwatch.domain.workmode import classify_mode_ex
from jobwatch.env import DOCS_ROOT, FONT_DIR, PHOTO_PATH, PROFILE_PATH, SMB_BASE, SMB_PREFIX, log
from jobwatch.llm import llm_for
from jobwatch.repositories.jobs import JobRepository
from jobwatch.util.text import safe_name
from jobwatch.util.timeutil import now_iso


def restore_cv_jobs(con=None):
    """The postings you made a CV for stay in the list for good: a deleted one (cache reset, new search) comes back
    from its documents (info.json in the documents folder, the posting text in the CV record), marked "applied".
    A copy of it fetched again later is merged into it as a duplicate. Returns how many came back."""
    own = con is None
    con = con or database.connect()
    try:
        with config_store.lock:
            cfg = config_store.load()
        back = 0
        for cv in con.execute("SELECT c.* FROM cvs c LEFT JOIN jobs j ON j.id=c.job_id "
                              "WHERE c.status='done' AND j.id IS NULL").fetchall():
            info = {}
            if cv["doc_dir"] and os.path.isfile(os.path.join(cv["doc_dir"], "info.json")):
                try:
                    with open(os.path.join(cv["doc_dir"], "info.json"), encoding="utf-8") as fh:
                        info = json.load(fh)
                except (OSError, ValueError):
                    info = {}
            it = {"title": info.get("title") or "", "company": info.get("company") or "", "location": info.get("location") or "",
                  "url": info.get("url") or "", "posted": info.get("posted") or "", "description": cv["description"] or ""}
            if not it["title"] or not it["url"]:
                continue
            sc, hits = score_job(cfg, it)
            wm = info.get("work_mode") if info.get("work_mode") in ("remote", "hybrid", "onsite") else None
            if wm:
                msrc, mnote = "source", "from the saved documents"
            else:
                wm, mnote = classify_mode_ex(it["title"], it["location"], it["description"])
                msrc = "text" if wm != "unknown" else None
            hand = JobRepository(con).remembered_mode(cv["job_id"], url_key(it["url"]), dup_key(it["title"], it["company"], it["url"]))
            if hand:
                wm, msrc, mnote = hand, "manual", "set by you"
            ts = cv["created"] or info.get("generated") or now_iso()
            con.execute(
                "INSERT INTO jobs(id,source,title,company,location,url,posted,description,score,matched,dupkey,first_seen,"
                "last_seen,status,notified,work_mode,urlkey,mode_src,mode_note) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'applied',1,?,?,?,?)",
                (cv["job_id"], cv["job_id"].split(":", 1)[0], it["title"], it["company"], it["location"], it["url"], it["posted"],
                 it["description"], sc, ", ".join(hits), dup_key(it["title"], it["company"], it["url"]), ts, ts, wm,
                 url_key(it["url"]), msrc, mnote))
            back += 1
        if back:
            JobRepository(con).dedupe()  # a copy fetched meanwhile is hidden behind the one with the CV
            log.info("Postings with a CV restored: %d", back)
        if own:
            con.commit()
        return back
    finally:
        if own:
            con.close()


MAX_PDF_BYTES = 10 * 1024 * 1024


def pdf_from_multipart(content_type, body):
    """Extracts the "pdf" field from a multipart/form-data body. Empty bytes if missing."""
    from email.parser import BytesParser
    from email.policy import default
    if not content_type or "multipart/form-data" not in content_type:
        return b""
    msg = BytesParser(policy=default).parsebytes(b"Content-Type: " + content_type.encode("utf-8") + b"\r\n\r\n" + body)
    for part in msg.iter_parts():
        if part.get_param("name", header="content-disposition") == "pdf":
            return part.get_payload(decode=True) or b""
    return b""


def pdf_text(data):
    """Reads the text of a PDF (pypdf). A scanned PDF saved as an image has no text."""
    import io

    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if not text:
        raise ValueError("No text can be read from the PDF (probably a scanned image). Paste the text instead.")
    return text


def profile_from_cv_text(text):
    """Builds the profile JSON from the CV text with the Claude API. On error raises ValueError with a user message."""
    with config_store.lock:
        cfg = config_store.load()
    try:
        prof = cvgen.profile_from_text(llm_for(cfg), text)
    except cvgen.CVError as ex:
        raise ValueError(str(ex))
    return json.dumps(prof, ensure_ascii=False, indent=2)


def load_profile():
    if os.path.exists(PROFILE_PATH):
        with open(PROFILE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return copy.deepcopy(cvgen.DEFAULT_PROFILE)


def owner_prefix(prof=None):
    """File-name prefix from the profile name, converted to ASCII: "Zoé Ünal" -> "Zoe_Unal"."""
    name = (prof or load_profile()).get("name") or "Candidate"
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    words = re.findall(r"[A-Za-z0-9]+", ascii_name)
    return "_".join(words) or "Candidate"


def doc_names(prof=None):
    p = owner_prefix(prof)
    return {"cv": p + "_CV.pdf", "resume": p + "_Resume.pdf", "letter": p + "_Cover_Letter.pdf"}


DOC_LABEL = {"cv": "CV (detailed)", "resume": "Resume (1 page)", "letter": "Cover letter"}


def job_doc_dir(job):
    short = re.sub(r"[^A-Za-z0-9]+", "", job["id"].split(":", 1)[-1])[-10:]
    return os.path.join(DOCS_ROOT, safe_name(job["company"] or "Unknown company"), "%s [%s]" % (safe_name(job["title"], 80), short))


def cv_paths(jid):
    with database.connect() as con:
        r = con.execute("SELECT doc_dir FROM cvs WHERE job_id=?", (jid,)).fetchone()
    d = r["doc_dir"] if r and r["doc_dir"] else None
    return {k: (os.path.join(d, v) if d else None) for k, v in doc_names().items()}


def cv_filename(job, kind):
    base = re.sub(r"[^A-Za-z0-9]+", "_", (job["company"] or "ceg")).strip("_")[:40]
    return "%s_%s_%s.pdf" % (owner_prefix(), {"cv": "CV", "resume": "Resume", "letter": "Cover_Letter"}[kind], base)


def write_job_info(d, job, data, cvrow=None):
    info = {"job_id": job["id"], "title": job["title"], "company": job["company"], "location": job["location"],
            "url": job["url"], "work_mode": job.get("work_mode"), "score": job["score"], "posted": job["posted"],
            "generated": now_iso(), "model": data.get("_model"), "photo": data.get("_photo", True),
            "language": data.get("language"), "match_notes": data.get("match_notes", []),
            "sent_to": cvrow["sent_to"] if cvrow else None, "sent_at": cvrow["sent_at"] if cvrow else None}
    with open(os.path.join(d, "info.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2)


def render_cv_files(jid, data, archive=False):
    """CV + Resume + cover letter into the company folder. archive=True: the previous version goes to korabbi/<time>/."""
    with database.connect() as con:
        job = dict(con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        cvrow = con.execute("SELECT * FROM cvs WHERE job_id=?", (jid,)).fetchone()
    d = job_doc_dir(job)
    os.makedirs(d, exist_ok=True)
    names = doc_names(load_profile())
    if archive and any(os.path.exists(os.path.join(d, v)) for v in names.values()):
        old = os.path.join(d, "korabbi", datetime.now().strftime("%Y-%m-%d_%H%M"))
        os.makedirs(old, exist_ok=True)
        for fn in list(names.values()) + ["Cover_Letter.txt", "info.json"]:
            if os.path.exists(os.path.join(d, fn)):
                shutil.move(os.path.join(d, fn), os.path.join(old, fn))
    prof = load_profile()
    photo = PHOTO_PATH if data.get("_photo", True) else None
    cvgen.render_cv_pdf(os.path.join(d, names["cv"]), prof, data, FONT_DIR, photo)
    cvgen.render_resume_pdf(os.path.join(d, names["resume"]), prof, data, FONT_DIR, photo)
    cvgen.render_letter_pdf(os.path.join(d, names["letter"]), prof, data, FONT_DIR)
    with open(os.path.join(d, "Cover_Letter.txt"), "w", encoding="utf-8") as fh:
        fh.write(data.get("cover_letter", ""))
    if cvrow and cvrow["description"]:
        with open(os.path.join(d, "hirdetes.txt"), "w", encoding="utf-8") as fh:
            fh.write("%s\n%s · %s\n%s\n\n%s" % (job["title"], job["company"], job["location"] or "", job["url"], cvrow["description"]))
    write_job_info(d, job, data, cvrow)
    with database.connect() as con:
        con.execute("UPDATE cvs SET doc_dir=? WHERE job_id=?", (d, jid))
    return d


def cv_generate(jid, description):
    with config_store.lock:
        cfg = config_store.load()
    llm = llm_for(cfg)
    with database.connect() as con:
        job = dict(con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
    try:
        if not llm:
            raise cvgen.CVError("No AI key is set (Settings → AI).")
        data = cvgen.tailor(llm, load_profile(), job, description)
        data["_photo"] = cvgen.photo_default(job)
        render_cv_files(jid, data, archive=True)
        with database.connect() as con:
            con.execute("UPDATE cvs SET status='done', error=NULL, data=?, created=? WHERE job_id=?",
                        (json.dumps(data, ensure_ascii=False), now_iso(), jid))
            # documents made: you have (probably) applied
            r = con.execute("SELECT status FROM jobs WHERE id=?", (jid,)).fetchone()
            if r and (r["status"] or "new") in ("new", "seen"):
                JobRepository(con).set_status(jid, "applied")
    except Exception as ex:
        log.exception("CV generation error")
        with database.connect() as con:
            con.execute("UPDATE cvs SET status='error', error=? WHERE job_id=?", (str(ex), jid))


def backfill_docs():
    """Moves old generations (from before the document folder existed) into the per-company folder, with the Resume. No API call."""
    with database.connect() as con:
        rows = con.execute("SELECT job_id, data FROM cvs WHERE status='done' AND (doc_dir IS NULL OR doc_dir='')").fetchall()
    for r in rows:
        try:
            d = render_cv_files(r["job_id"], json.loads(r["data"]))
            log.info("Documents moved: %s", d)
        except Exception:
            log.exception("Document move error: %s", r["job_id"])


def smb_path(d):
    if SMB_BASE and SMB_PREFIX and d and d.startswith(SMB_PREFIX):
        return SMB_BASE + d[len(SMB_PREFIX):]
    return d


class Applications:
    """What you do with a posting's documents: generate them, switch the photo, save a hand edit (the PDFs are
    rebuilt), send them by e-mail. Each returns the message to show, or raises LookupError for an unknown posting."""

    @staticmethod
    def _job(con, jid):
        job = con.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not job:
            raise LookupError(jid)
        return dict(job)

    @staticmethod
    def _data(con, jid):
        return json.loads(con.execute("SELECT data FROM cvs WHERE job_id=?", (jid,)).fetchone()["data"])

    def generate(self, jid, description):
        """Starts writing the documents in the background (Claude)."""
        import threading
        description = (description or "")[:20000]
        with database.connect() as con:
            self._job(con, jid)
            con.execute("INSERT INTO cvs(job_id,status,description) VALUES(?, 'running', ?) "
                        "ON CONFLICT(job_id) DO UPDATE SET status='running', error=NULL, description=excluded.description",
                        (jid, description))
        threading.Thread(target=cv_generate, args=(jid, description), daemon=True).start()
        return None

    def set_photo(self, jid, on):
        with database.connect() as con:
            self._job(con, jid)
            data = self._data(con, jid)
            data["_photo"] = bool(on)
            render_cv_files(jid, data)
            con.execute("UPDATE cvs SET data=? WHERE job_id=?", (json.dumps(data, ensure_ascii=False), jid))
        return None

    def save_edit(self, jid, data_json):
        """Saves the documents' text edited by hand and rebuilds the PDFs."""
        try:
            data = json.loads(data_json or "")
            for k in ("headline", "summary", "experience", "skills", "cover_letter"):
                assert k in data, "missing: %s" % k
            with database.connect() as con:
                self._job(con, jid)
                old = self._data(con, jid)
            data["_model"] = old.get("_model", "").replace(" (edited by hand)", "") + " (edited by hand)"
            data["_photo"] = old.get("_photo", True)
            render_cv_files(jid, data)
            with database.connect() as con:
                con.execute("UPDATE cvs SET data=? WHERE job_id=?", (json.dumps(data, ensure_ascii=False), jid))
            return "Saved, PDF rebuilt."
        except LookupError:
            raise
        except Exception as ex:
            return "Not saved: %s" % ex

    def send(self, jid, to, subject, body, attach="cv", with_letter=False):
        """Sends the documents by e-mail and marks the posting applied."""
        from jobwatch.services.notify import notifier
        to = (to or "").strip()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", to):
            return "Invalid recipient."
        with database.connect() as con:
            job = self._job(con, jid)
        paths = cv_paths(jid)
        kinds = {"cv": ["cv"], "resume": ["resume"], "both": ["cv", "resume"]}.get(attach, ["cv"])
        if with_letter:
            kinds.append("letter")
        files = [(cv_filename(job, k), open(paths[k], "rb").read()) for k in kinds]
        with config_store.lock:
            cfg = config_store.load()
        try:
            notifier.email(cfg, (subject or "").strip() or "Application: " + job["title"], body or "", to=to,
                           attachments=files)
        except Exception as ex:
            return "Sending error: %s" % ex
        with database.connect() as con:
            con.execute("UPDATE cvs SET sent_to=?, sent_at=? WHERE job_id=?", (to, now_iso(), jid))
            JobRepository(con).set_status(jid, "applied")
            cvrow = con.execute("SELECT * FROM cvs WHERE job_id=?", (jid,)).fetchone()
        try:
            write_job_info(os.path.dirname(paths["cv"]), job, json.loads(cvrow["data"]), cvrow)
        except Exception:
            log.exception("info.json update")
        return "Sent: %s" % to


applications = Applications()
