"""What is searched for: the search terms, their keywords and locations, and matching a posting."""

from jobwatch.util.text import term_re


def search_entries(cfg):
    """[(keywords, location or "")] from the search terms."""
    out = []
    for line in cfg.get("search_terms") or []:
        parts = [x.strip() for x in line.split("|")]
        if parts[0]:
            out.append((parts[0], parts[1] if len(parts) > 1 else ""))
    return out


def search_keywords(cfg):
    """The distinct keywords (case-insensitive), in order."""
    out, seen = [], set()
    for kw, _ in search_entries(cfg):
        if kw.lower() not in seen:
            seen.add(kw.lower())
            out.append(kw)
    return out


def linkedin_plan(cfg):
    """[(keywords, location)]: a term with its own location is searched there, the others in each default location."""
    locs = [l.strip() for l in cfg.get("search_locations") or [] if l.strip()] or ["European Union"]
    out, seen = [], set()
    for kw, loc in search_entries(cfg):
        for l in ([loc] if loc else locs):
            if (kw.lower(), l.lower()) not in seen:
                seen.add((kw.lower(), l.lower()))
                out.append((kw, l))
    return out


def matches_search(keywords, it, in_title=False):
    """True if the title or description contains every word of at least one search term.
    in_title: one of those words must also be in the title (for the general boards, where a description alone matches
    too much, e.g. "Unity Catalog" in a data posting)."""
    title = (it.get("title") or "").lower()
    text = " ".join((title, it.get("description") or "")).lower()
    return any(all(term_re(w).search(text) for w in kw.lower().split())
               and (not in_title or any(term_re(w).search(title) for w in kw.lower().split())) for kw in keywords)
