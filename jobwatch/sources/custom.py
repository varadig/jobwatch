"""Own sources: job sites behind a login form, read with a session."""
import hashlib
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from jobwatch.sources.base import Filtering, JobSource
from jobwatch.env import UA, log
from jobwatch.sources.feeds import parse_rss
from jobwatch.util.text import html_attr, strip_tags
from jobwatch.db import database


# Each entry of cfg["custom_sources"]: {"id", "enabled", "type", "name", "url", "pattern",
# "login_url", "username", "password"}. A source with a username + password logs in first, through the
# site's own login form (the form that has a password field), and keeps the cookies for that run only.
CUSTOM_TYPES = {
    "vmp": "munka.hu VMP (search result URL)",
    "page": "Any web page (links matching a pattern)",
    "rss": "RSS / Atom feed",
}


VMP_BASE = "https://vmp.munka.hu"


CUSTOM_DETAIL_LIMIT = 8   # per source and run: new postings whose detail page is fetched


CUSTOM_DELAY = 3          # seconds between requests to the same site (government sites block fast clients)


class WebSession:
    """Cookie-keeping HTTP client, so a source can log in and then read pages as that user."""

    def __init__(self):
        import http.cookiejar
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def get(self, url, data=None, referer=None, timeout=30):
        """GET (or POST when data is given). Returns (final url, text)."""
        h = {"User-Agent": UA, "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8"}
        if referer:
            h["Referer"] = referer
        if isinstance(data, dict):
            data = urllib.parse.urlencode(data).encode()
        with self.opener.open(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
            return r.geturl(), r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")


def form_login(sess, login_url, username, password):
    """Logs in through the first form on login_url that has a password field. Hidden fields are sent back,
    the text field before the password field gets the username. Raises RuntimeError if it did not work."""
    url, pg = sess.get(login_url)
    for fm in re.finditer(r"<form\b([^>]*)>(.*?)</form>", pg, re.S | re.I):
        inputs = [m.group(1) for m in re.finditer(r"<input\b([^>]*)>", fm.group(2), re.I)]
        if not any((html_attr(a, "type") or "").lower() == "password" for a in inputs):
            continue
        fields, user_f, pass_f, submit = {}, None, None, None
        for a in inputs:
            name, typ = html_attr(a, "name"), (html_attr(a, "type") or "text").lower()
            if not name:
                continue
            if typ == "password":
                pass_f = pass_f or name
            elif typ in ("text", "email") and pass_f is None:
                user_f = name  # the last text field before the password field
            elif typ == "hidden":
                fields[name] = html_attr(a, "value") or ""
            elif typ == "submit" and submit is None:
                submit = (name, html_attr(a, "value") or "")
        if not user_f:
            raise RuntimeError("login form on %s has no username field" % login_url)
        fields[user_f], fields[pass_f] = username, password
        if submit:
            fields[submit[0]] = submit[1]
        action = urllib.parse.urljoin(url, html_attr(fm.group(1), "action") or url)
        _, resp = sess.get(action, data=fields, referer=url)
        if re.search(r"""name\s*=\s*["']?%s["'\s>]""" % re.escape(pass_f), resp, re.I):
            raise RuntimeError("login failed (wrong username or password?)")
        return
    raise RuntimeError("no login form (with a password field) on %s" % login_url)


def with_query(url, **params):
    u = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(u.query, keep_blank_values=True))
    q.update({k: str(v) for k, v in params.items()})
    return urllib.parse.urlunparse(u._replace(query=urllib.parse.urlencode(q)))


def custom_vmp(sess, src, is_new):
    """munka.hu Virtual Labour Market: the URL is a search result page (vmp.munka.hu/allas/talalatok?...).
    The employer is shown only to logged-in users; descriptions come from the detail pages of new postings."""
    out, seen = [], set()
    for n in range(1, 4):  # at most 3 result pages per run
        _, pg = sess.get(with_query(src["url"], oldal=n))
        added = 0
        for row in re.findall(r"<tr\b[^>]*>(.*?)</tr>", pg, re.S | re.I):
            m = re.search(r'href="(/allas/reszletek/(\d+))"', row)
            cells = re.findall(r"<td\b[^>]*>(.*?)</td>", row, re.S | re.I)
            if not m or len(cells) < 3 or m.group(2) in seen:
                continue
            seen.add(m.group(2))
            added += 1
            employer = strip_tags(cells[3]) if len(cells) > 3 else ""
            if "bejelentkez" in employer.lower():
                employer = ""
            out.append({"ext_id": m.group(2), "url": urllib.parse.urljoin(src["url"], m.group(1)), "posted": "",
                        "title": re.sub(r"^\d{3,4}\s*-\s*", "", strip_tags(cells[0])),  # drop the FEOR code
                        "company": employer, "location": strip_tags(cells[2]),
                        "description": "Végzettség: " + strip_tags(cells[1])})
        if not added:
            break
        time.sleep(CUSTOM_DELAY)
    fetched = 0
    for it in out:
        if fetched >= CUSTOM_DETAIL_LIMIT or not is_new(it["ext_id"]):
            continue
        fetched += 1
        time.sleep(CUSTOM_DELAY)
        try:
            _, pg = sess.get(it["url"])
            text = strip_tags(pg.replace("</td>", " </td>").replace("<br", " <br"))
            a, b = text.find("Munkakör (FEOR)"), text.find("TÁMOP")
            if a < 0:
                continue  # not a posting page (e.g. "Nincs ilyen állásajánlat"): keep the list data
            it["description"] = text[a: b if b > a else None][:8000]
            if not it["company"]:
                m = re.search(r"Foglalkoztató neve\s+(.{2,80}?)\s{2,}|Foglalkoztató neve\s+(\S.{1,80}?)\s+(?:Cím|Székhely)", text)
                if m:
                    it["company"] = (m.group(1) or m.group(2)).strip()
        except Exception as ex:
            log.warning("VMP detail %s: %s", it["ext_id"], ex)
    return out


def custom_page(sess, src, is_new):
    """Any page: every link whose address contains the pattern (or matches it as a regex) is a posting."""
    url, pg = sess.get(src["url"])
    pat = (src.get("pattern") or "").strip()
    try:
        rx = re.compile(pat, re.I) if pat else None
    except re.error:
        rx = re.compile(re.escape(pat), re.I)
    out, seen = [], set()
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", pg, re.S | re.I):
        href = html_attr(m.group(1), "href")
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        full = urllib.parse.urljoin(url, href)
        if (rx and not rx.search(full)) or full in seen:
            continue
        title = strip_tags(m.group(2)) or (html_attr(m.group(1), "title") or "").strip()
        if len(title) < 3:
            continue
        seen.add(full)
        out.append({"ext_id": hashlib.sha1(full.encode()).hexdigest()[:16], "title": title[:200],
                    "company": "", "location": "", "url": full, "posted": "", "description": ""})
    return out


def custom_rss(sess, src, is_new):
    return parse_rss(sess.get(src["url"])[1])


CUSTOM_FUNCS = {"vmp": custom_vmp, "page": custom_page, "rss": custom_rss}


def custom_login_url(src):
    return (src.get("login_url") or "").strip() or (VMP_BASE + "/belepes" if src.get("type") == "vmp" else "")


def run_custom_source(src, is_new=lambda ext_id: True):
    """Fetches one own source (logging in first when it has a username and password)."""
    if src.get("type") not in CUSTOM_FUNCS:
        raise RuntimeError("unknown type '%s'" % src.get("type"))
    sess = WebSession()
    if src.get("username") and src.get("password"):
        if not custom_login_url(src):
            raise RuntimeError("username given but no login page URL")
        form_login(sess, custom_login_url(src), src["username"], src["password"])
    return CUSTOM_FUNCS[src["type"]](sess, src, is_new)


class CustomSource(JobSource):
    """Your own sources from Settings (sites behind a login form); gentle, so the site does not block us."""

    key = "custom"
    filtering = Filtering.OPTIONAL
    min_interval = 120

    def fetch(self, cfg):
        out, errs = [], []
        for src in cfg.get("custom_sources") or []:
            if not src.get("enabled", True) or not src.get("url"):
                continue
            prefix = src["id"] + ":"

            def is_new(ext_id, prefix=prefix):
                with database.connect() as con:
                    return not con.execute("SELECT 1 FROM jobs WHERE id=?", ("custom:" + prefix + ext_id,)).fetchone()
            try:
                for it in run_custom_source(src, is_new):
                    it["ext_id"] = prefix + it["ext_id"]
                    it["via"] = src.get("name") or src["type"]
                    out.append(it)
            except Exception as ex:
                errs.append("%s: %s" % (src.get("name") or src["type"], ex))
        if errs:
            log.warning("Own source errors: %s", "; ".join(errs))
            if not out:
                raise RuntimeError("; ".join(errs))
        return out


def kv_text(d):
    return "\n".join("%s = %s" % (k, v) for k, v in d.items())


def parse_kv(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.rsplit("=", 1)
            try:
                out[k.strip().lower()] = int(v.strip())
            except ValueError:
                pass
    return out


def parse_custom_sources(f, old):
    """Rebuilds cfg["custom_sources"] from the Settings form. An empty password keeps the stored one."""
    by_id = {s.get("id"): s for s in old or []}
    out = []
    try:
        count = int(f.get("cs_count", "0") or 0)
    except ValueError:
        count = 0
    for i in list(range(count)) + ["new"]:
        g = lambda k: (f.get("cs_%s_%s" % (i, k)) or "").strip()
        if i != "new" and "cs_%s_delete" % i in f:
            continue
        if not g("url"):
            continue
        prev = by_id.get(g("id")) or {}
        src = {"id": prev.get("id") or secrets.token_hex(4),
               "enabled": True if i == "new" else ("cs_%s_enabled" % i) in f,
               "type": g("type") if g("type") in CUSTOM_TYPES else "page",
               "name": g("name") or urllib.parse.urlparse(g("url")).netloc,
               "url": g("url"), "pattern": g("pattern"), "login_url": g("login_url"),
               "username": g("username"),
               "password": f.get("cs_%s_password" % i) or prev.get("password", "")}
        if not src["username"]:
            src["password"] = ""
        out.append(src)
    return out


def test_custom_sources(cfg):
    """Runs every own source once (no saving). Returns a short report line per source."""
    lines = []
    for src in cfg.get("custom_sources") or []:
        name = src.get("name") or src.get("type")
        try:
            items = run_custom_source(src, is_new=lambda ext_id: False)
            sample = "; ".join("%s (%s)" % (it["title"], it["company"] or it["location"] or "-") for it in items[:3])
            lines.append("%s: %d posting(s)%s%s" % (name, len(items), " – logged in" if src.get("username") else "",
                                                     (" – e.g. " + sample) if sample else ""))
        except Exception as ex:
            lines.append("%s: ERROR %s" % (name, ex))
    return " | ".join(lines) or "No own sources yet."
