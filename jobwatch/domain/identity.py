"""Recognising the same posting and company: normalised keys."""
import re

from jobwatch.util.text import norm

# The same posting often comes from several places (a LinkedIn search and a LinkedIn alert e-mail, a company
# board and Flexa…). Two postings are the same when their link points to the same posting, or when the title
# and the company match after the noise is removed (legal forms, gender tags).
LEGAL_FORMS = re.compile(r"\b(gmbh|ag|ltd|limited|inc|llc|plc|corp|corporation|co|sas|sa|sarl|srl|bv|nv|ab|oy|as|aps|"
                         r"kft|zrt|nyrt|bt|spa|sl|pty|pte|kk|se)\b\.?", re.I)


GENDER_TAGS = re.compile(r"\(\s*(?:[mwfdx]\s*/\s*){1,3}[mwfdx]\s*\)|\(\s*(?:all genders?|alle geschlechter|gn\*?|w\*m\*d)\s*\)|"
                         r"\b[mwf]\s*/\s*[mwf]\s*/\s*[dx]\b", re.I)


def norm_company(c):
    return norm(LEGAL_FORMS.sub(" ", c or ""))


def url_key(url):
    """One key per posting link: the LinkedIn posting id, else the address without query and fragment."""
    m = re.search(r"linkedin\.com/(?:comm/)?jobs/view/(?:[^/?#]*-)?(\d{6,})", url or "")
    if m:
        return "li:" + m.group(1)
    return re.sub(r"[?#].*$", "", (url or "").strip().lower()).rstrip("/")


def dup_key(title, company, url):
    return norm(GENDER_TAGS.sub(" ", title or "")) + "|" + (norm_company(company) or url_key(url))


def mark_keys(jid, urlkey, dupkey):
    return [k for k in ("id:" + (jid or ""), "u:" + (urlkey or ""), "d:" + (dupkey or "")) if len(k) > 3 and not k.endswith(":")]
