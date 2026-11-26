"""Company logos: found once, stored, served from disk."""
import hashlib
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from jobwatch.db import database
from jobwatch.env import LOGO_DIR, UA, log
from jobwatch.util.text import norm
from jobwatch.util.timeutil import now_iso

LOGO_MAX_BYTES = 300 * 1024


# no SVG: served from this origin, an SVG could run scripts
LOGO_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp"}


LOGO_LOCK = threading.Lock()


def remember_logo(con, company, url):
    """Notes the logo address of a company (once per company; a later source may fill in a missing one)."""
    key = norm(company)
    if not key or not url or not url.startswith("https://"):
        return
    row = con.execute("SELECT url, file FROM company_logos WHERE ckey=?", (key,)).fetchone()
    if not row:
        con.execute("INSERT INTO company_logos(ckey, url) VALUES(?, ?)", (key, url))
    elif not row["file"] and row["url"] != url:
        con.execute("UPDATE company_logos SET url=?, tried=NULL WHERE ckey=?", (url, key))


def fetch_logos(limit=200):
    """Background job: downloads the logos not downloaded yet (a failed one is retried after a day)."""
    if not LOGO_LOCK.acquire(blocking=False):
        return
    try:
        day_ago = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat()
        with database.connect() as con:
            rows = con.execute("SELECT ckey, url FROM company_logos WHERE file IS NULL AND (tried IS NULL OR tried < ?) "
                               "LIMIT ?", (day_ago, limit)).fetchall()
        os.makedirs(LOGO_DIR, exist_ok=True)
        done = 0
        for r in rows:
            fname = None
            try:
                req = urllib.request.Request(r["url"], headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=20) as resp:
                    ctype = (resp.headers.get_content_type() or "").lower()
                    data = resp.read(LOGO_MAX_BYTES + 1)
                if ctype in LOGO_TYPES and 0 < len(data) <= LOGO_MAX_BYTES:
                    fname = hashlib.sha1(r["ckey"].encode()).hexdigest()[:16] + LOGO_TYPES[ctype]
                    with open(os.path.join(LOGO_DIR, fname), "wb") as fh:
                        fh.write(data)
                    done += 1
            except Exception as ex:
                log.info("Logo %s: %s", r["ckey"], ex)
            with database.connect() as con:
                con.execute("UPDATE company_logos SET file=?, tried=? WHERE ckey=?", (fname, now_iso(), r["ckey"]))
            time.sleep(0.5)
        if rows:
            log.info("Logos: %d of %d downloaded", done, len(rows))
    finally:
        LOGO_LOCK.release()


def logo_files():
    with database.connect() as con:
        return {r["ckey"]: r["file"] for r in con.execute("SELECT ckey, file FROM company_logos WHERE file IS NOT NULL")}
