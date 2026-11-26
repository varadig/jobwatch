"""Text helpers: HTML escaping and stripping, normalising, word matching, safe names."""
import html
import re


_re_cache = {}


def term_re(term):
    t = term.lower().strip()
    if t not in _re_cache:
        _re_cache[t] = re.compile(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9#+])")
    return _re_cache[t]


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def strip_tags(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def html_attr(attrs, name):
    m = re.search(r"""\b%s\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""" % name, attrs, re.I)
    return html.unescape(next(g for g in m.groups() if g is not None)) if m else None


def e(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def safe_url(u):
    return u if isinstance(u, str) and u.startswith(("http://", "https://")) else "#"


def safe_name(x, n=70):
    x = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", x or "").strip().strip(".")
    return re.sub(r"\s+", " ", x)[:n].strip() or "unknown"
