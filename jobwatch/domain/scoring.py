"""Scoring a posting against the scoring chips (the order of the chips is the score)."""
import re

from jobwatch.util.text import term_re


# Scoring: what interests you, as chips. In the ordered lists the position is the score (first = most points);
# the other lists give the same points to every chip. "unity / unity3d": words that count as one.
SCORING_KINDS = {  # kind: (points by position, points after the table / for every chip)
    "interests": ((6, 5, 4, 4, 3, 3, 2, 2, 2), 1),   # in the job title
    "desc": ((3, 2), 1),                              # in the description, at most DESC_MAX together
    "less": ((), -3),                                 # in the title: less relevant
    "never": ((), -8),                                # in the title: hides the posting
    "loc_good": ((3, 2), 1),                          # in the location, only for a fitting title
    "loc_bad": ((), -5),                              # in the location
}


DESC_MAX = 4


def chip_points(kind, rank):
    table, rest = SCORING_KINDS[kind]
    return table[rank] if rank < len(table) else rest


def chip_alts(chip):
    return [a.strip().lower() for a in re.split(r"\s+/\s+", chip or "") if a.strip()]


def scoring_rules(cfg):
    """{kind: [(alternatives, points)]} from the chips, or from the old term = points lists (templates, old configs)."""
    sc = cfg.get("scoring")
    if sc is not None:
        return {k: [(chip_alts(c), chip_points(k, i)) for i, c in enumerate(sc.get(k) or []) if chip_alts(c)]
                for k in SCORING_KINDS}
    d = lambda k: [([t.lower()], int(w)) for t, w in (cfg.get(k) or {}).items()]
    title, loc = d("title_keywords"), d("location_keywords")
    return {"interests": [x for x in title if x[1] > 0], "desc": d("body_keywords"),
            "less": [x for x in title if x[1] < 0] + d("negative_keywords"), "never": [],
            "loc_good": [x for x in loc if x[1] > 0], "loc_bad": [x for x in loc if x[1] < 0]}


def _hit(alts, text):
    for a in alts:
        if term_re(a).search(text):
            return a
    return None


def score_job(cfg, j):
    rules = scoring_rules(cfg)
    title = (j.get("title") or "").lower()
    body = " ".join([(j.get("description") or ""), (j.get("company") or "")]).lower()
    score, hits, seen = 0, [], set()
    for alts, w in rules["interests"]:
        a = _hit(alts, title)
        if a:
            score += w
            hits.append(a)
            seen.update(alts)
    title_pts = score
    body_pts = 0
    for alts, w in rules["desc"]:
        a = None if seen.intersection(alts) else _hit(alts, body)
        if a:
            body_pts += w
            hits.append(a)
    score += min(body_pts, DESC_MAX)  # the description can give at most DESC_MAX points
    for alts, w in rules["less"] + rules["never"]:
        a = _hit(alts, title)
        if a:
            score += w
            hits.append("%s(%d)" % (a, w))
    loc = (j.get("location") or "").lower()
    for alts, w in rules["loc_good"] + rules["loc_bad"]:
        a = _hit(alts, loc)
        if a:
            if w > 0 and title_pts < 4:
                continue  # location bonus only for a relevant position
            score += w
            hits.append("📍%s(%+d)" % (a, w))
    if re.search(r",\s*[A-Z]{2}$", j.get("location") or ""):  # US allam (pl. "Louisville, CO")
        score -= 4
        hits.append("📍USA(-4)")
    score += j.get("bonus", 0)
    return score, hits


def parse_chips(text):
    """One chip per line, in order; duplicates (case-insensitive) dropped."""
    out, seen = [], set()
    for line in (text or "").splitlines():
        c = re.sub(r"\s+", " ", line).strip().strip(",")
        if c and c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out
