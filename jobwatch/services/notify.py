"""Notifications: ntfy push and e-mail."""
import html
import json
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

from jobwatch.env import log


class Notifier:
    """Sends notifications: ntfy push to the phone and e-mail (SMTP), as set in Settings → Notifications."""

    def push(self, cfg, title, message, click=None, priority=3):
        n = cfg["ntfy"]
        body = {"topic": n["topic"], "title": title[:200], "message": message[:3500],
                "tags": ["briefcase"], "priority": priority}
        if click:
            body["click"] = click
        req = urllib.request.Request(n["server"].rstrip("/") + "/", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "User-Agent": "jobwatch"})
        urllib.request.urlopen(req, timeout=20).read()


    def email(self, cfg, subject, text, html_body=None, to=None, attachments=()):
        m = cfg["email"]
        if not (m.get("username") and m.get("password")):
            raise RuntimeError("E-mail (SMTP) is not configured in Settings.")
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = m["username"]
        msg["To"] = to or m["to"] or m["username"]
        msg.set_content(text)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        for fname, data in attachments:
            msg.add_attachment(data, maintype="application", subtype="pdf", filename=fname)
        with smtplib.SMTP(m["smtp_host"], int(m["smtp_port"]), timeout=30) as s:
            s.starttls(context=ssl.create_default_context())
            s.login(m["username"], m["password"])
            s.send_message(msg)


    def new_jobs(self, cfg, jobs):
        if not jobs:
            return []
        errors = []
        jobs = sorted(jobs, key=lambda j: -j["score"])
        if cfg["ntfy"].get("enabled") and cfg["ntfy"].get("topic"):
            try:
                if len(jobs) <= 5:
                    for j in jobs:
                        self.push(cfg, "New job (%d pts): %s" % (j["score"], j["title"]),
                                  "%s · %s\n%s" % (j["company"], j["location"] or "-", j["url"]),
                                  click=j["url"], priority=4 if j["score"] >= cfg["notify_min_score"] + 3 else 3)
                else:
                    lines = ["• %s – %s (%d)" % (j["title"], j["company"], j["score"]) for j in jobs[:15]]
                    self.push(cfg, "%d new relevant job(s)" % len(jobs), "\n".join(lines),
                              click=cfg.get("public_url"), priority=4)
            except Exception as ex:
                errors.append("ntfy: %s" % ex)
        if cfg["email"].get("enabled") and cfg["email"].get("username") and cfg["email"].get("password"):
            try:
                rows = "".join(
                    '<li><b>%d</b> · <a href="%s">%s</a> – %s <small>(%s, %s)</small></li>' % (
                        j["score"], html.escape(j["url"]), html.escape(j["title"]), html.escape(j["company"]),
                        html.escape(j["location"] or "-"), html.escape(j["source"])) for j in jobs)
                text = "\n".join("[%d] %s – %s\n%s" % (j["score"], j["title"], j["company"], j["url"]) for j in jobs)
                self.email(cfg, "jobwatch: %d new relevant job(s)" % len(jobs), text,
                           '<p>New postings that match your CV:</p><ul>%s</ul><p><a href="%s">Open jobwatch</a></p>'
                           % (rows, html.escape(cfg.get("public_url", ""))))
            except Exception as ex:
                errors.append("e-mail: %s" % ex)
        for e in errors:
            log.error("Notification error: %s", e)
        return errors


notifier = Notifier()
