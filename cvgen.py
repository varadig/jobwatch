"""Builds a CV + cover letter tailored to a posting with the Claude API, rendered to PDF (reportlab).
Design: Inter typeface, round photo, clickable links."""
import json
import os
import re

MODEL = "claude-opus-5-5"

# The source material for the CV: the profile (data/profile.json). See DEFAULT_PROFILE below.
# The "contact" section is NOT sent to the Claude API; it only goes into the PDF locally.
# Base (template) profile: used if the instance has no data/profile.json.
# Write your own data into data/profile.json (see README: "Profile"). This file must not contain
# real personal data.
DEFAULT_PROFILE = {
    "name": "Your Name",
    "headline": "Your headline, e.g. Project Manager | Operations Lead",
    "contact": {
        "phone": "",
        "email": "you@example.com",
        "linkedin": "",
        "portfolio": "",
        "location": "City, Country",
    },
    "summary": "A short professional summary in two or three sentences.",
    "skills": [
        {"group": "Core skills", "items": ["Skill one", "Skill two", "Skill three"]},
    ],
    "experience": [
        {"heading": "Company name", "role": "Job title", "period": "2020 – 2024", "intro": "",
         "bullets": ["One achievement with a measurable result."],
         "projects": [], "links": []},
    ],
    "languages": "English (professional working proficiency)",
    "objective": "Looking for <role> roles, ideally <setting>.",
}

_STR = {"type": "string"}
SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "enum": ["en", "hu", "de"]},
        "headline": _STR,
        "summary": _STR,
        "skills": {"type": "array", "items": {
            "type": "object", "properties": {"group": _STR, "items": {"type": "array", "items": _STR}},
            "required": ["group", "items"], "additionalProperties": False}},
        "experience": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "heading": _STR, "role": _STR, "period": _STR, "intro": _STR,
                "bullets": {"type": "array", "items": _STR},
                "projects": {"type": "array", "items": {
                    "type": "object", "properties": {"name": _STR, "role": _STR, "text": _STR},
                    "required": ["name", "role", "text"], "additionalProperties": False}},
            },
            "required": ["heading", "role", "period", "intro", "bullets", "projects"], "additionalProperties": False}},
        "languages": _STR,
        "resume": {
            "type": "object",
            "properties": {
                "headline": _STR, "summary": _STR,
                "skills": {"type": "array", "items": {
                    "type": "object", "properties": {"group": _STR, "items": {"type": "array", "items": _STR}},
                    "required": ["group", "items"], "additionalProperties": False}},
                "experience": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "heading": _STR, "role": _STR, "period": _STR,
                        "bullets": {"type": "array", "items": _STR},
                        "projects": {"type": "array", "items": {
                            "type": "object", "properties": {"name": _STR, "text": _STR},
                            "required": ["name", "text"], "additionalProperties": False}},
                    },
                    "required": ["heading", "role", "period", "bullets", "projects"], "additionalProperties": False}},
            },
            "required": ["headline", "summary", "skills", "experience"], "additionalProperties": False},
        "cover_letter": _STR,
        "email_subject": _STR,
        "match_notes": {"type": "array", "items": _STR},
    },
    "required": ["language", "headline", "summary", "skills", "experience", "languages", "resume", "cover_letter",
                 "email_subject", "match_notes"],
    "additionalProperties": False,
}

SYSTEM = """You tailor a job candidate's CV and write a cover letter for one specific job posting.

Ground rules:
- Use only facts from the candidate profile. Never invent employers, titles, dates, games, metrics, technologies, team sizes or achievements. You may select, reorder, merge, shorten and rephrase existing facts, and choose which to emphasize.
- Keep every experience entry, in the profile's order, with its heading, role and period copied exactly. You may rewrite, drop or merge bullets and projects that are irrelevant to this posting; keep project names exactly as in the profile.
- List only skills the profile contains, ordered by relevance to the posting; you may regroup and rename the groups.
- If the posting asks for something the candidate lacks (e.g. a specific engine, years of experience, shipped titles), do not claim it. Mention the gap in match_notes so the candidate can decide how to handle it.
- Write the CV and the letter in the posting's language: Hungarian posting -> "hu", German posting -> "de", otherwise "en". Keep headings and project names as they are. match_notes are always in English, because they are for the candidate.
- headline: one line positioning the candidate for this role, still truthful.
- summary: 3-4 sentences that answer "why this candidate for this role".
- resume: a one-page version of the same tailored CV for quick screening, same facts, language and heading rules: summary at most 2 sentences; at most 4 skill groups with only the most relevant items; every experience entry kept, each with at most 3 short bullets; for the freelance entry at most 3 projects with one short line each (no bullets there).
- cover_letter: 150-250 words, plain text with paragraphs separated by blank lines, addressed to the hiring team of the company. Reference the company or its products where the posting gives you something concrete, and the 2-3 most relevant experiences. No clichés, no placeholders in brackets, no salary. End with a sign-off and the candidate's name.
- email_subject: a short application e-mail subject naming the position.
- match_notes: 2-5 short notes for the candidate: what you emphasized and any gaps or risks (time zone, location, missing requirements)."""


class CVError(Exception):
    pass


def tailor(api_key, profile, job, description):
    """Claude API call: returns the tailored CV data (dict, same structure as the profile)."""
    import anthropic

    facts = {k: v for k, v in profile.items() if k != "contact"}  # contact details are not sent out
    facts = json.loads(json.dumps(facts))  # the model does not need links; the renderer puts them back
    for x in facts.get("experience", []):
        x.pop("links", None)
        for p in x.get("projects", []):
            p.pop("links", None)
    user = ("<candidate_profile>\n%s\n</candidate_profile>\n\n<job_posting>\nTitle: %s\nCompany: %s\nLocation: %s\nURL: %s\n\n%s\n</job_posting>"
            % (json.dumps(facts, ensure_ascii=False, indent=1), job["title"], job["company"], job["location"] or "-",
               job["url"], description or "(The full posting text is not available; work from the title and company.)"))
    client = anthropic.Anthropic(api_key=api_key or None, timeout=300.0, max_retries=2)
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "high", "format": {"type": "json_schema", "schema": SCHEMA}},
            system=SYSTEM,
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError:
        raise CVError("Invalid Anthropic API key (Settings → Claude API).")
    except anthropic.PermissionDeniedError:
        raise CVError("The API key has no permission for this model.")
    except anthropic.RateLimitError:
        raise CVError("Claude API: too many requests, try again in a few minutes.")
    except anthropic.BadRequestError as e:
        raise CVError("Claude API bad request: %s" % e.message)
    except anthropic.APIStatusError as e:
        raise CVError("Claude API error (%s): %s" % (e.status_code, e.message))
    except anthropic.APIConnectionError:
        raise CVError("Cannot reach the Claude API (network error).")
    if resp.stop_reason == "refusal":
        raise CVError("The model refused the request.")
    if resp.stop_reason == "max_tokens":
        raise CVError("The response was too long, try again.")
    text = "".join(b.text for b in resp.content if b.type == "text")
    try:
        data = json.loads(text)
    except ValueError:
        raise CVError("The model's response is not valid JSON.")
    data["_model"] = resp.model
    return data


# ---------------------------------------------------------------- foto szabaly
NO_PHOTO_RE = re.compile(
    r"united states|\busa?\b|united kingdom|\buk\b|england|scotland|wales|london|canada|australia|new zealand|"
    r",\s*[A-Z]{2}$|\b(?:CA|NY|TX|WA|OR|MA|IL|CO|FL|GA|NC|UT|AZ)\b(?:\s|,|$)", re.I)


def photo_default(job):
    """No photo for US/UK-style locations (a photo is a disadvantage there), a photo elsewhere."""
    loc = job.get("location") or ""
    return not NO_PHOTO_RE.search(loc)


# ---------------------------------------------------------------- job description
def fetch_description(fetch, strip_tags, job):
    """Full posting text: for LinkedIn from the public jobPosting endpoint, otherwise the stored description."""
    if job["source"] == "linkedin":
        jid = job["id"].split(":", 1)[1]
        try:
            page = fetch("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/%s" % jid)
            m = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', page, re.S)
            if m:
                return strip_tags(m.group(1).replace("<br>", "\n").replace("</li>", "\n").replace("</p>", "\n"))[:12000]
        except Exception:
            pass
    return job["description"] or ""


def find_emails(text):
    bad = ("example.", "noreply", "no-reply", "sentry", "@2x")
    return sorted({m for m in re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text or "")
                   if not any(b in m.lower() for b in bad)})


# ---------------------------------------------------------------- PDF
_fonts_cache = None


def _fonts(font_dir):
    """Font names (regular, bold, semibold, italic, light); Inter if available, otherwise DejaVu/Helvetica."""
    global _fonts_cache
    if _fonts_cache:
        return _fonts_cache
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    def reg(name, fn):
        pdfmetrics.registerFont(TTFont(name, os.path.join(font_dir, fn)))

    try:
        reg("In", "Inter-Regular.ttf"); reg("InB", "Inter-Bold.ttf"); reg("InSB", "Inter-SemiBold.ttf")
        reg("InI", "Inter-Italic.ttf"); reg("InL", "Inter-Light.ttf"); reg("InBI", "Inter-BoldItalic.ttf")
        pdfmetrics.registerFontFamily("In", normal="In", bold="InB", italic="InI", boldItalic="InBI")
        _fonts_cache = ("In", "InB", "InSB", "InI", "InL")
    except Exception:
        try:
            reg("DV", "DejaVuSans.ttf"); reg("DVB", "DejaVuSans-Bold.ttf")
            pdfmetrics.registerFontFamily("DV", normal="DV", bold="DVB", italic="DV", boldItalic="DVB")
            _fonts_cache = ("DV", "DVB", "DVB", "DV", "DV")
        except Exception:
            _fonts_cache = ("Helvetica", "Helvetica-Bold", "Helvetica-Bold", "Helvetica-Oblique", "Helvetica")
    return _fonts_cache


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


LINK = "#1a5dd6"


def _a(url, label):
    return '<a href="%s" color="%s"><u>%s</u></a>' % (_esc(url), LINK, _esc(label))


def _links(links):
    return " &nbsp;|&nbsp; ".join(_a(l["url"], l["label"]) for l in links or [] if l.get("url"))


SECTION = {"en": ("Profile", "Technical Stack & Core Competencies", "Selected References & Experience", "Key Projects:", "Languages"),
           "hu": ("Profil", "Technológiák és kompetenciák", "Kiemelt referenciák és tapasztalat", "Fontosabb projektek:", "Nyelvek"),  # Hungarian CV headings, used for Hungarian postings
           "de": ("Profil", "Technologien & Kernkompetenzen", "Ausgewählte Referenzen & Erfahrung", "Wichtige Projekte:", "Sprachen")}


def _normalize(cv):
    """Regi (fotos dizajn elotti) formatum: role/company/period/context/bullets -> heading/role/period/intro."""
    for x in cv.get("experience", []):
        if "heading" not in x:
            x["heading"] = x.get("company", "")
            x.setdefault("intro", x.pop("context", ""))
        x.setdefault("intro", "")
        x.setdefault("bullets", [])
        x.setdefault("projects", [])
    return cv


def _merge_links(profile, cv):
    """Puts the profile links back into the model output (matched by heading / project name)."""
    by_head = {x["heading"]: x for x in profile.get("experience", [])}
    pexp = profile.get("experience", [])
    for i, x in enumerate(cv.get("experience", [])):
        src = by_head.get(x.get("heading")) or (pexp[i] if i < len(pexp) else {})
        x["links"] = x.get("links") or src.get("links", [])
        plinks = {p["name"]: p.get("links", []) for p in src.get("projects", [])}
        for p in x.get("projects", []):
            p["links"] = p.get("links") or plinks.get(p.get("name"), [])
    return cv


def render_resume_pdf(path, profile, cv, font_dir, photo_path=None):
    """One-page resume from the model's 'resume' section; if it does not fit, re-renders at a smaller size."""
    r = cv.get("resume") or {}
    data = {"language": cv.get("language", "en"), "headline": r.get("headline") or cv.get("headline"),
            "summary": r.get("summary") or cv.get("summary"), "skills": r.get("skills") or cv.get("skills"),
            "experience": [dict(x, intro="") for x in _normalize({"experience": json.loads(json.dumps(
                r.get("experience") or cv.get("experience")))})["experience"]],
            "languages": cv.get("languages") or profile.get("languages")}
    for scale in (1.0, 0.94, 0.88, 0.82, 0.76):
        pages = render_cv_pdf(path, profile, data, font_dir, photo_path, scale=scale, compact=True)
        if pages <= 1:
            break
    return pages


def render_cv_pdf(path, profile, cv, font_dir, photo_path=None, scale=1.0, compact=False):
    """cv: data in profile structure (the profile itself = the base CV). photo_path=None -> no photo. Returns the page count."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (HRFlowable, Image, KeepTogether, ListFlowable, ListItem, Paragraph,
                                    SimpleDocTemplate, Spacer, Table, TableStyle)

    cv = _merge_links(profile, _normalize(json.loads(json.dumps(cv))))
    R, B, SB, I, L = _fonts(font_dir)
    k = scale * (0.93 if compact else 1.0)

    def PS(name, **kw):
        for a in ("fontSize", "leading", "spaceBefore", "spaceAfter"):
            if a in kw:
                kw[a] = kw[a] * (k if a in ("fontSize", "leading") else k * (0.75 if compact else 1.0))
        return ParagraphStyle(name, **kw)
    ink = colors.HexColor("#111111")
    rule = colors.HexColor("#bdbdbd")
    st = {
        "name": PS("name", fontName=B, fontSize=20, leading=24, textColor=ink),
        "head": PS("head", fontName=R, fontSize=10.5, leading=14, textColor=ink),
        "contact": PS("contact", fontName=R, fontSize=10, leading=14, textColor=ink),
        "sec": PS("sec", fontName=L, fontSize=15.5, leading=19, textColor=ink, spaceBefore=12, spaceAfter=2),
        "body": PS("body", fontName=R, fontSize=9.6, leading=13.6, textColor=ink),
        "skill": PS("skill", fontName=R, fontSize=9.6, leading=13.4, textColor=ink, spaceAfter=3.5),
        "eh": PS("eh", fontName=SB, fontSize=12, leading=15.5, textColor=ink, spaceBefore=8),
        "role": PS("role", fontName=I, fontSize=9.4, leading=13, textColor=ink, spaceAfter=3),
        "bul": PS("bul", fontName=R, fontSize=9.6, leading=13.2, textColor=ink),
        "small": PS("small", fontName=R, fontSize=9.4, leading=13, textColor=ink, spaceBefore=2),
        "kp": PS("kp", fontName=B, fontSize=9.6, leading=13.4, spaceBefore=4, spaceAfter=2),
    }
    lab = SECTION.get(cv.get("language"), SECTION["en"])
    c = profile.get("contact", {})
    parts = []
    if c.get("phone"):
        parts.append(_esc(c["phone"]))
    if c.get("email"):
        parts.append(_a("mailto:" + c["email"], c["email"]))
    for k in ("linkedin", "portfolio"):
        if c.get(k):
            parts.append(_a(c[k], re.sub(r"^https?://(www\.)?|/$", "", c[k])))
    contact = " | ".join(parts)
    text_block = [Paragraph(_esc(profile["name"]).upper(), st["name"]),
                  Paragraph(_esc(cv.get("headline") or profile.get("headline", "")), st["head"]),
                  Paragraph(contact, st["contact"])]
    if c.get("location"):
        text_block.append(Paragraph(_esc(c["location"]), st["head"]))

    width = A4[0] - (26 if compact else 36) * mm
    if photo_path and os.path.exists(photo_path):
        ph = (22 if compact else 29) * mm * scale
        img = Image(photo_path, width=ph, height=ph, mask="auto")
        hdr = Table([[img, text_block]], colWidths=[ph + 5 * mm, width - ph - 5 * mm])
        hdr.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
                                 ("BOTTOMPADDING", (0, 0), (-1, -1), 0)]))
        el = [hdr]
    else:
        el = list(text_block)

    def section(title):
        el.append(Paragraph(_esc(title).upper(), st["sec"]))
        el.append(HRFlowable(width="100%", thickness=0.7, color=rule, spaceBefore=1, spaceAfter=6))

    def bullets(items):
        return ListFlowable([ListItem(Paragraph(t, st["bul"]), leftIndent=12, spaceAfter=1.5) for t in items],
                            bulletType="bullet", start="•", leftIndent=12, bulletFontName=R, bulletFontSize=9)

    section(lab[0])
    el.append(Paragraph(_esc(cv["summary"]), st["body"]))
    section(lab[1])
    for g in cv["skills"]:
        el.append(Paragraph("<b>%s:</b> %s" % (_esc(g["group"]), _esc(", ".join(g["items"]))), st["skill"]))
    section(lab[2])
    for x in cv["experience"]:
        head = [Paragraph(_esc(x["heading"]), st["eh"]),
                Paragraph("%s | %s" % (_esc(x["role"]), _esc(x["period"])), st["role"])]
        rest = []
        if x.get("intro"):
            head.append(Paragraph(_esc(x["intro"]), st["body"]))
        if x.get("bullets"):
            bl = [_esc(b) for b in x["bullets"]]
            head.append(bullets(bl[:1]))  # keep the heading with the first bullet
            if bl[1:]:
                rest.append(bullets(bl[1:]))
        if x.get("projects"):
            items = []
            for p in x["projects"]:
                t = ("<b>%s</b> (%s): %s" % (_esc(p["name"]), _esc(p["role"]), _esc(p["text"]))) if p.get("role") \
                    else "<b>%s</b>: %s" % (_esc(p["name"]), _esc(p["text"]))
                if p.get("links"):
                    t += " " + _links(p["links"])
                items.append(t)
            tgt = rest if x.get("bullets") else head
            tgt.append(Paragraph(lab[3], st["kp"]))
            tgt.append(bullets(items[:1]))
            if items[1:]:
                rest.append(bullets(items[1:]))
        if x.get("links"):
            rest.append(Paragraph(_links(x["links"]), st["small"]))
        el.append(KeepTogether(head))
        el += rest
    langs = cv.get("languages") or profile.get("languages")
    if langs:
        section(lab[4])
        el.append(Paragraph(_esc(langs), st["body"]))
    mg = (13 if compact else 18) * mm
    doc = SimpleDocTemplate(path, pagesize=A4, leftMargin=mg, rightMargin=mg, topMargin=(11 if compact else 14) * mm,
                            bottomMargin=(10 if compact else 14) * mm,
                            title="%s – %s" % (profile["name"], "Resume" if compact else "CV"), author=profile["name"])
    doc.build(el)
    return doc.page


def render_letter_pdf(path, profile, cv, font_dir):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    R, B, SB, I, L = _fonts(font_dir)
    c = profile.get("contact", {})
    parts = [_esc(c["phone"])] if c.get("phone") else []
    if c.get("email"):
        parts.append(_a("mailto:" + c["email"], c["email"]))
    if c.get("linkedin"):
        parts.append(_a(c["linkedin"], re.sub(r"^https?://(www\.)?|/$", "", c["linkedin"])))
    el = [Paragraph(_esc(profile["name"]).upper(), ParagraphStyle("n", fontName=B, fontSize=16, leading=20)),
          Paragraph(" | ".join(parts), ParagraphStyle("c", fontName=R, fontSize=9.5, leading=13)), Spacer(1, 16)]
    body = ParagraphStyle("b", fontName=R, fontSize=10.5, leading=15.5, spaceAfter=9)
    for para in re.split(r"\n\s*\n", cv["cover_letter"].strip()):
        el.append(Paragraph(_esc(para).replace("\n", "<br/>"), body))
    SimpleDocTemplate(path, pagesize=A4, leftMargin=22 * mm, rightMargin=22 * mm, topMargin=20 * mm,
                      bottomMargin=20 * mm, title="%s – Cover letter" % profile["name"]).build(el)


# ---------------------------------------------------------------- profile from CV text
_LINKS = {"type": "array", "items": {"type": "object", "additionalProperties": False,
          "required": ["label", "url"], "properties": {"label": _STR, "url": _STR}}}
PROFILE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "headline", "contact", "summary", "skills", "experience", "languages", "objective"],
    "properties": {
        "name": _STR, "headline": _STR, "summary": _STR, "languages": _STR, "objective": _STR,
        "contact": {"type": "object", "additionalProperties": False,
                    "required": ["phone", "email", "linkedin", "portfolio", "location"],
                    "properties": {k: _STR for k in ("phone", "email", "linkedin", "portfolio", "location")}},
        "skills": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                   "required": ["group", "items"],
                   "properties": {"group": _STR, "items": {"type": "array", "items": _STR}}}},
        "experience": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                       "required": ["heading", "role", "period", "intro", "bullets", "projects", "links"],
                       "properties": {
                           "heading": _STR, "role": _STR, "period": _STR, "intro": _STR,
                           "bullets": {"type": "array", "items": _STR},
                           "projects": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                        "required": ["name", "role", "text", "links"],
                                        "properties": {"name": _STR, "role": _STR, "text": _STR, "links": _LINKS}}},
                           "links": _LINKS}}},
    },
}

PROFILE_SYSTEM = """You convert a candidate's CV into a structured profile.
- Use only facts stated in the CV text. Never invent employers, titles, dates, skills, projects or achievements.
- Keep employers, roles and periods exactly as written.
- Keep the language of the CV.
- If the CV does not state a field, use an empty string or an empty list.
- Put contact details only in "contact".
- Output only the JSON object."""

MAX_CV_CHARS = 60000


def profile_from_text(api_key, text):
    """Claude API: profile JSON (dict) from the CV text. Same structure as DEFAULT_PROFILE."""
    import anthropic

    text = (text or "").strip()
    if not text:
        raise CVError("No CV text.")
    if len(text) > MAX_CV_CHARS:
        raise CVError("The CV text is too long (max. %d characters)." % MAX_CV_CHARS)
    if not (api_key or os.environ.get("ANTHROPIC_API_KEY")):
        raise CVError("No Anthropic API key (Settings → Claude API).")
    client = anthropic.Anthropic(api_key=api_key or None, timeout=300.0, max_retries=2)
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=12000,
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": PROFILE_SCHEMA}},
            system=PROFILE_SYSTEM,
            messages=[{"role": "user", "content": "<cv>\n%s\n</cv>" % text}],
        )
    except anthropic.AuthenticationError:
        raise CVError("Invalid Anthropic API key (Settings → Claude API).")
    except anthropic.RateLimitError:
        raise CVError("Claude API: too many requests, try again in a few minutes.")
    except anthropic.APIStatusError as e:
        raise CVError("Claude API error (%s): %s" % (e.status_code, e.message))
    except anthropic.APIConnectionError:
        raise CVError("Cannot reach the Claude API (network error).")
    if resp.stop_reason in ("refusal", "max_tokens"):
        raise CVError("The profile was not created (%s). Try a shorter text." % resp.stop_reason)
    raw = "".join(b.text for b in resp.content if b.type == "text")
    try:
        data = json.loads(raw)
    except ValueError:
        raise CVError("The model's response is not valid JSON.")
    if not data.get("name") or not data.get("experience"):
        raise CVError("Could not extract the name or work experience from the CV.")
    return data
