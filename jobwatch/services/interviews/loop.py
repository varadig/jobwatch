"""The interviews' background work: reminders and the mailbox scan."""
import threading
import time

from jobwatch.db import database
from jobwatch.env import log
from jobwatch.services.interviews.core import IV_SCAN_EVERY, reminders
from jobwatch.services.interviews.mailboxes import scan_interview_mails


def interview_loop():
    """Every 30 s: the reminders; every 30 min: the mailbox scan for interview e-mails (in the background)."""
    time.sleep(8)
    while True:
        try:
            reminders.send_due()
            with database.connect() as con:
                last = con.execute("SELECT v FROM meta WHERE k='iv_scan_at'").fetchone()
            if not last or time.time() - float(last["v"]) > IV_SCAN_EVERY:
                with database.connect() as con:
                    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES('iv_scan_at', ?)", (str(time.time()),))
                threading.Thread(target=scan_interview_mails, daemon=True).start()
        except Exception:
            log.exception("Interview loop error")
        time.sleep(30)
