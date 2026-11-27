"""Filling in what a source leaves out: LinkedIn descriptions and the work mode."""
import html
import re
import threading
import time
from datetime import datetime, timezone

from jobwatch import cvgen
from jobwatch.db import database
from jobwatch.domain.scoring import score_job
from jobwatch.domain.workmode import FLEXA_MODE, FOURDAY_MODE, classify_mode_ex
from jobwatch.env import log
from jobwatch.llm import llm_for
from jobwatch.net import http
from jobwatch.services.state import ENRICH_LOCK, MODE_LOCK, STATE
from jobwatch.sources.linkedin import enrich_linkedin
from jobwatch.util.timeutil import now_iso


def enrich_pending_linkedin(cfg, limit=60):
    """Background job: fetches the description of LinkedIn postings that still have none,
    then updates their work mode and score in the database. Never blocks a refresh."""
    if not ENRICH_LOCK.acquire(blocking=False):
        return
    try:
        with database.connect() as con:
            # every LinkedIn posting, whichever source brought it (a LinkedIn search, an alert e-mail, a pasted link)
            rows = con.execute("SELECT id, urlkey, title, company, location FROM jobs WHERE (urlkey LIKE 'li:%' OR source='linkedin') "
                               "AND dup_of IS NULL AND (description IS NULL OR description='') ORDER BY first_seen DESC LIMIT ?",
                               (limit,)).fetchall()
            items = [{"ext_id": r["urlkey"][3:] if (r["urlkey"] or "").startswith("li:") else r["id"].split(":", 1)[-1],
                      "row_id": r["id"], "title": r["title"], "company": r["company"], "location": r["location"]} for r in rows]
            if not items:
                return
            STATE["enrich"] = {"total": len(items), "done": 0}
            enrich_linkedin(con, items, limit=limit)
            STATE["enrich"]["done"] = sum(1 for it in items if it.get("description"))
        with database.connect() as con:
            for it in items:
                if not it.get("description"):
                    continue
                jid = it["row_id"]
                sc, hits = score_job(cfg, it)
                con.execute("UPDATE jobs SET description=?, score=?, matched=? WHERE id=?",
                            (it["description"], sc, ", ".join(hits), jid))
                if it.get("work_mode", "unknown") != "unknown":  # the text decides only where nothing exact is known
                    con.execute("UPDATE jobs SET work_mode=?, mode_src='text', mode_note=? WHERE id=? AND "
                                "COALESCE(mode_src,'') NOT IN ('manual','source','page')", (it["work_mode"], it.get("mode_note"), jid))
        log.info("LinkedIn descriptions: %d updated", sum(1 for it in items if it.get("description")))
    except Exception:
        log.exception("LinkedIn background enrichment failed")
    finally:
        STATE["enrich"] = None
        ENRICH_LOCK.release()
    threading.Thread(target=enrich_work_modes, args=(cfg,), daemon=True).start()  # descriptions arrived: settle the rest


MODE_CLAUDE_PER_RUN = 40


def mode_from_page(source, url):
    """The work mode a job board states on the posting's own page (4dayweek, Flexa): (mode, note) or (None, "")."""
    if source == "4dayweek":
        pg = http.get(url, timeout=20)
        m = re.search(r"<span><span>([A-Za-z -]{3,20}?) · ([^<]{1,80})</span>", pg)
        if m and m.group(1).strip().lower() in FOURDAY_MODE:
            return FOURDAY_MODE[m.group(1).strip().lower()], "4dayweek: “%s · %s”" % (m.group(1).strip(), html.unescape(m.group(2)))
    elif source == "flexa":
        pg = http.get(url, timeout=20).replace('\\"', '"')
        m = re.search(r'"size":16\}\],"(%s)"\]' % "|".join(map(re.escape, FLEXA_MODE)), pg)
        if m:
            return FLEXA_MODE[m.group(1)], "Flexa: “%s”" % m.group(1)
    return None, ""


def enrich_work_modes(cfg, limit=80):
    """Background job: settles postings whose work mode is unknown — first from the job board's own page
    (4dayweek, Flexa), then from the text in many languages, then (if turned on) a language model reads the posting.
    A manual choice is never touched. Each posting is checked once (again after 3 days if still unknown)."""
    if not MODE_LOCK.acquire(blocking=False):
        return
    try:
        old = datetime.fromtimestamp(time.time() - 3 * 86400, timezone.utc).isoformat()
        with database.connect() as con:
            rows = [dict(r) for r in con.execute(
                "SELECT id, source, url, urlkey, title, location, description FROM jobs WHERE dup_of IS NULL AND "
                "COALESCE(work_mode,'unknown')='unknown' AND COALESCE(mode_src,'')!='manual' AND "
                "NOT ((COALESCE(urlkey,'') LIKE 'li:%' OR source='linkedin') AND COALESCE(description,'')='') AND "
                "(mode_checked IS NULL OR mode_checked < ?) ORDER BY first_seen DESC LIMIT ?", (old, limit))]
        llm = llm_for(cfg)
        use_claude = cfg.get("mode_claude", True) and llm
        settled, asked = 0, 0
        for r in rows:
            mode, src, note = None, None, ""
            linkedin = (r["urlkey"] or "").startswith("li:") or r["source"] == "linkedin"
            if linkedin and not (r["description"] or ""):
                continue  # wait for its text (the LinkedIn description download calls this again); not marked as checked
            page_tried = False
            if r["source"] in ("4dayweek", "flexa"):
                page_tried = True
                try:
                    mode, note = mode_from_page(r["source"], r["url"])
                    src = "page" if mode else None
                except Exception as ex:
                    log.info("Work mode page %s: %s", r["url"], ex)
                time.sleep(1)
            if not mode:
                m2, n2 = classify_mode_ex(r["title"], r["location"], r["description"])
                if m2 != "unknown":
                    mode, src, note = m2, "text", n2
            claude_state = "claude_off" if not cfg.get("mode_claude", True) else ("no_key" if not llm else "")
            if not mode and use_claude and len(r["description"] or "") > 200 and asked < MODE_CLAUDE_PER_RUN:
                asked += 1
                try:
                    m3, n3 = cvgen.work_mode_from_text(llm, r["title"], r["location"], r["description"])
                    claude_state = "claude"
                    if m3 != "unknown":
                        mode, src, note = m3, "claude", n3
                except Exception as ex:
                    claude_state = "claude_error"
                    log.info("Work mode (Claude) %s: %s", r["id"], ex)
            if not mode:  # remember why it stays unknown, to show it on the card
                if linkedin:
                    code = "li_hidden"
                elif page_tried:
                    code = "board_none"
                elif r["source"] == "mail" and not (r["description"] or ""):
                    code = "mail_none"
                elif not (r["description"] or ""):
                    code = "no_text"
                else:
                    code = "text_none"
                note = "?%s|%s" % (code, claude_state)
            with database.connect() as con:
                if mode:
                    settled += 1
                    con.execute("UPDATE jobs SET work_mode=?, mode_src=?, mode_note=?, mode_checked=? WHERE id=? AND "
                                "COALESCE(mode_src,'')!='manual'", (mode, src, note[:300], now_iso(), r["id"]))
                else:
                    con.execute("UPDATE jobs SET mode_checked=?, mode_note=? WHERE id=? AND COALESCE(mode_src,'')!='manual'",
                                (now_iso(), note, r["id"]))
        if rows:
            log.info("Work modes: %d of %d unknown settled (%d read by Claude)", settled, len(rows), asked)
    finally:
        MODE_LOCK.release()
