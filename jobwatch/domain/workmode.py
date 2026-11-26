"""Remote, hybrid or on-site: the rules that read it from a posting, and why it is unknown."""
import re

from jobwatch.models import SOURCES_INFO


WT_LINKEDIN = {"2": "remote", "3": "hybrid", "1": "onsite"}


# Work-mode wording in the languages the postings come in (en, hu, de, fr, es, it, pl, sv, nl).
RE_HYB = re.compile(
    r"\bhybrid\w*|\bhibrid\w*|\bhybride\b|\bhíbrid[oa]\b|\bibrid[oa]\b|\bhybrydow\w*|teilweise remote|"
    r"\b\d\s*(?:-\s*\d\s*)?days?\s*(?:a|per|each)\s*week\s*(?:in|at|from)\s*(?:the|our)?\s*(?:office|studio)|"
    r"(?:in|at|from)\s*(?:the|our)\s*(?:office|studio)\s*\d\s*(?:-\s*\d\s*)?days|"
    r"\d\s*(?:-\s*\d\s*)?(?:jours?|days?)\s*(?:de\s*)?(?:télétravail|remote|home ?office)|télétravail\s*(?:partiel|possible|\d)|"
    r"possibilité de télétravail|\d\s*tage?\s*(?:pro woche\s*)?(?:im büro|vor ort|homeoffice|home office)|homeoffice[- ]möglichkeit|"
    r"home office lehetőség|heti\s*\d\s*nap|teletrabajo parcial|smart working|flexible (?:office|home) (?:days|working)", re.I)


RE_ONS = re.compile(
    r"\bon-?site\b|\bonsite\b|\bin-office\b|office-based|studio-based|fully in[- ]office|"
    r"\birodai\b|irodában|helyszíni munka|vor ort|präsenz\w*|\bsur site\b|\ben présentiel\b|présentiel|"
    r"\bpresencial\b|in sede|stacjonarn\w*|w biurze|på plats|på kontoret|op kantoor|"
    r"work(?:ing)? from (?:our|the) (?:office|studio)|relocat(?:e|ion) to|"
    r"\b(?:no|not a|not)[- ]remote\b|remote (?:work )?(?:is )?not (?:possible|available|an option|offered)|"
    r"this (?:role|position) is not remote", re.I)


RE_REM_STRONG = re.compile(
    r"fully remote|full[- ]remote|100\s*%\s*remote|remote[- ]first|\bfully distributed|work from anywhere|"
    r"remote work is possible|remote (?:position|role|job|opportunity)|work(?:ing)? remotely|"
    r"távmunka|teljesen remote|100\s*%\s*(?:télétravail|homeoffice|home office)|full télétravail|télétravail complet|"
    r"vollständig remote|komplett remote|praca zdalna|w pełni zdaln\w*|totalmente remoto|en remoto|100\s*%\s*remoto|"
    r"helt på distans|distansarbete", re.I)


RE_REM_TL = re.compile(r"\bremote\b|anywhere|home office|\bwfh\b|télétravail|zdaln\w*|remoto", re.I)


RE_NOT_REMOTE = re.compile(r"\b(?:no|not a|not)[- ]remote\b|remote (?:work )?(?:is )?not (?:possible|available|an option|offered)|"
                           r"this (?:role|position) is not remote", re.I)


def _evidence(text, m):
    a, b = max(0, m.start() - 50), min(len(text), m.end() + 50)
    return ("…" if a else "") + re.sub(r"\s+", " ", text[a:b]).strip() + ("…" if b < len(text) else "")


def classify_mode_ex(title="", location="", description=""):
    """(work mode, evidence quote). Title/location are strong signals; in the description only unambiguous
    phrases count; "no remote" style negations mean on-site."""
    tl = " ".join(x or "" for x in (title, location))
    for rx, mode in ((RE_HYB, "hybrid"), (RE_NOT_REMOTE, "onsite"), (re.compile(r"\bon-?site\b|in-office|\bonsite\b", re.I), "onsite"),
                     (RE_REM_TL, "remote")):
        m = rx.search(tl)
        if m:
            return mode, _evidence(tl, m)
    d = description or ""
    neg = RE_NOT_REMOTE.search(d)
    if neg:
        return "onsite", _evidence(d, neg)
    rem, hyb, ons = RE_REM_STRONG.search(d), RE_HYB.search(d), RE_ONS.search(d)
    if rem and not hyb:
        return "remote", _evidence(d, rem)
    if hyb:
        return "hybrid", _evidence(d, hyb)
    if ons:
        return "onsite", _evidence(d, ons)
    return "unknown", ""


def classify_mode(title="", location="", description=""):
    return classify_mode_ex(title, location, description)[0]


def norm_mode(v):
    v = (v or "").lower().replace("-", "").replace("_", "").replace(" ", "")
    return {"remote": "remote", "fully": "remote", "hybrid": "hybrid", "partially": "hybrid",
            "onsite": "onsite", "office": "onsite", "inoffice": "onsite"}.get(v)


FLEXA_MODE = {"On-site": "onsite", "Hybrid": "hybrid", "Remote-first": "remote", "Remote": "remote", "Field-based": "onsite",
              "Work from anywhere": "remote"}


FOURDAY_MODE = {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite", "on site": "onsite",
                "office": "onsite", "in-office": "onsite", "in office": "onsite"}


# Why a posting's work mode is not known: code -> (short text on the card, full explanation in the menu)
UNKNOWN_WHY = {
    "pending": ("not checked yet",
                "Not checked yet: it is checked in the background right after the refresh."),
    "li_wait": ("LinkedIn hides it, text not downloaded yet",
                "LinkedIn shows the work mode only to logged-in users, so it is not in what jobwatch can read. "
                "The posting text is still being downloaded (LinkedIn limits how fast); the text is checked once it arrives."),
    "li_hidden": ("LinkedIn hides it, text doesn't say",
                  "LinkedIn shows the work mode only to logged-in users, and the posting text does not state it either "
                  "(checked by the rules%s). Set it here, or let a LinkedIn job alert e-mail bring it: those state the mode."),
    "board_none": ("%s doesn't give it",
                   "%s does not state the work mode for this posting, and the text does not say it either (checked by the rules%s)."),
    "mail_none": ("the e-mail doesn't say",
                  "The alert e-mail did not state the work mode for this posting, and there is no posting text to check."),
    "no_text": ("no posting text",
                "The source gives no posting text, so there is nothing to check."),
    "text_none": ("the text doesn't say",
                  "The posting text does not state the work mode (checked by the rules%s)."),
}


def unknown_reason(r):
    """(short, long) reason why the work mode of r is unknown."""
    note = r.get("mode_note") or ""
    code, _, extra = note[1:].partition("|") if note.startswith("?") else ("", "", "")
    linkedin = (r.get("urlkey") or "").startswith("li:") or r.get("source") == "linkedin"
    if not code:  # not checked yet: say what it is waiting for
        code = "li_wait" if linkedin and not (r.get("description") or "") else "pending"
    by = {"claude": " and Claude", "claude_off": "; Claude is turned off in Settings", "no_key": "; no Claude API key",
          "claude_error": "; Claude could not be reached", "": ""}.get(extra, "")
    board = SOURCES_INFO.get(r.get("source"), (r.get("source") or "",))[0]
    short, long_ = UNKNOWN_WHY.get(code, UNKNOWN_WHY["pending"])
    if code == "board_none":
        return short % board, long_ % (board, by)
    return short, (long_ % by) if "%s" in long_ else long_


def mode_why(r):
    src, note = r.get("mode_src") or "", r.get("mode_note") or ""
    if src == "manual":
        return "Set by you."
    if src in ("source", "page"):
        return "Exact, %s." % (note or "from the source")
    if src == "text":
        return "From the posting text: %s" % note
    if src == "claude":
        return "Claude read the posting: %s" % note
    if (r.get("work_mode") or "unknown") == "unknown":
        long_ = unknown_reason(r)[1]
        return long_ if "Set it here" in long_ else long_ + " You can set it here."
    return "From the posting text."
