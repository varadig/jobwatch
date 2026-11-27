"""Builds a CV + cover letter tailored to a posting with a language model (DeepSeek or Claude, see llm.py),
rendered to PDF (reportlab).
Design: Inter typeface, round photo, clickable links."""
import json
import os
import re

from jobwatch.llm import LlmError

CVError = LlmError   # what the callers catch

# The source material for the CV: the profile (data/profile.json). See DEFAULT_PROFILE below.
# The "contact" section is NOT sent to a model; it only goes into the PDF locally.
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


# ---------------------------------------------------------------- recruiter outreach
# channel -> (label, character limit of the message, what the platform expects)
OUTREACH_CHANNELS = {
    "linkedin_note": ("LinkedIn connection request note", 300,
                      "a LinkedIn connection request note: at most 300 characters including spaces, a greeting, "
                      "one or two sentences and a polite ask, no sign-off block"),
    "linkedin_message": ("LinkedIn message / InMail", 1000,
                         "a LinkedIn direct message or InMail: 70-130 words, a greeting line, a short body, a thank-you "
                         "and a sign-off with the candidate's full name"),
    "linkedin_intro": ("LinkedIn: ask a mutual connection to introduce you", 1000,
                       "a LinkedIn message to a mutual connection (the introducer), asking them to introduce the "
                       "candidate to the recruiter: 50-110 words, friendly and in the tone that fits how they know "
                       "each other, easy to say no to; include one or two sentences the introducer can forward as is"),
    "email": ("E-mail", 2000,
              "an e-mail: a subject line and a 90-160 word body, ending with a sign-off and the candidate's name; "
              "the contact details are added by the app below the text, do not write them"),
}
OUTREACH_SCHEMA = {
    "type": "object",
    "properties": {"subject": _STR, "message": _STR},
    "required": ["subject", "message"],
    "additionalProperties": False,
}
OUTREACH_SYSTEM = """You write a short first message from a job candidate to a recruiter.

Ground rules:
- Use only facts from the candidate profile. Never invent experience, titles, dates, products, metrics or skills.
- Make every message specific to this recruiter; it must not read like a template sent to many people. Use what you know about them: the role they hire for (its title, company, product, engine, platform, seniority, team setup), their focus from the profile (e.g. games, mobile, Unity, a region, a studio), their company. Mention one concrete thing about it in the opening, in your own words, without copying their headline.
- Then connect it to the candidate: pick the one or two facts from the profile that match that role or focus best (e.g. live mobile games for a mobile studio, UI architecture for a UI-heavy role, remote teamwork for a remote team), and say why they fit, concretely. Different recruiters should get different facts when their focus differs.
- If the role's requirements include something the candidate lacks, do not claim it; simply lead with what does match.
- If you know nothing beyond the name and title, keep it short and do not pad it with generic statements.
- Tone: courteous and professional, a little formal but still human, the way one writes to a professional one has not met. Open with a polite greeting by name ("Dear Anna," or "Hello Anna," in English; "Kedves Anna!" in Hungarian; "Sehr geehrte Frau …," or "Hallo Anna," in German as fits), say briefly why you are writing, and thank them for their time at the end. No flattery, no clichés ("I hope this finds you well", "I came across your profile", "passionate", "rockstar"), no emojis, no bullet lists, no placeholders in brackets.
- Never sound pushy: no assumptions that they have a role for the candidate, no pressure or urgency, no request for a call as the first ask, no "I'd love to jump on a call this week". End with one polite, easy-to-decline question, for example whether they would be open to keeping the candidate in mind, or whether they are working on roles that might fit.
- Do not assume the recruiter hires for the company they work at. Many recruiters work for agencies or as freelancers and fill roles at client companies, and in-house recruiters may hire for sister studios. Use the recruiter type when given; otherwise infer it from the title (e.g. "Recruitment Consultant", "Headhunter", "Talent Partner at <agency>" -> agency) and, when unclear, phrase the ask so it fits both (e.g. "if you are working on roles that fit, at your company or for clients").
- If a specific role is given, name it together with the hiring company from the role, which may differ from the recruiter's company. If not, say what kind of role the candidate is looking for, based on the profile's objective.
- Respect the length limit of the channel strictly; count characters for a LinkedIn connection note.
- When an introducer is given, write to the introducer (greet them, not the recruiter) and ask for an introduction to the recruiter; never pretend the introducer knows the candidate better than stated.
- subject: always write one, in the message's language, for every channel (LinkedIn InMail and e-mail use it): at most 70 characters, specific and calm, naming the role or the kind of role and the candidate's main angle, e.g. "Senior Unity developer – open to new roles" or "Senior Unity Developer role at Kolibri Games". No clickbait, no "Quick question", no exclamation marks."""


def outreach_message(llm, profile, recruiter, job=None, channel="linkedin_message", language="auto", note=""):
    """A short message to one recruiter. Returns {"subject", "message", "_model"}."""
    label, limit, how = OUTREACH_CHANNELS.get(channel, OUTREACH_CHANNELS["linkedin_message"])
    facts = {k: v for k, v in profile.items() if k != "contact"}  # contact details are not sent out
    lang = {"en": "Write in English.", "hu": "Write in Hungarian, addressing the recruiter formally (magázás).",
            "de": "Write in German (Sie-Form)."}.get(
        language, "Write in the language the recruiter or the role most likely uses; if unclear, English.")
    parts = ["<candidate_profile>\n%s\n</candidate_profile>" % json.dumps(facts, ensure_ascii=False, indent=1),
             "<recruiter>\nName: %s\nTitle / company: %s\nRecruiter type: %s\n</recruiter>" % (
                 recruiter.get("name") or "-", recruiter.get("title") or "-",
                 {"inhouse": "in-house (hires for the company they work at)",
                  "agency": "agency / external (fills roles at client companies)"}.get(
                     recruiter.get("kind") or "", "unknown, infer it from the title"))]
    if recruiter.get("profile"):
        parts.append("<recruiter_profile>\nFrom the recruiter's public LinkedIn profile (headline, about, current company, "
                     "location):\n%s\n</recruiter_profile>" % recruiter["profile"])
    if recruiter.get("role_text"):  # the role this recruiter hires for: takes priority over the general posting
        job = None
        parts.append("<role>\nThe role this recruiter is hiring for:\n%s\n</role>" % recruiter["role_text"][:7000])
    if recruiter.get("via"):
        parts.append("<introducer>\nThe message goes to this mutual connection, not to the recruiter: %s\n</introducer>"
                     % recruiter["via"])
    if job:
        parts.append("<role>\nTitle: %s\nCompany: %s\nLocation: %s\nURL: %s\n\n%s\n</role>" % (
            job.get("title", ""), job.get("company", ""), job.get("location") or "-", job.get("url", ""),
            (job.get("description") or "")[:6000]))
    if note:
        parts.append("<candidate_note>\n%s\n</candidate_note>" % note)
    parts.append("Channel: %s. Write %s (hard limit: %d characters for the message). %s" % (label, how, limit, lang))
    done = llm.json(system=OUTREACH_SYSTEM, user="\n\n".join(parts), schema=OUTREACH_SCHEMA, effort="medium")
    data = done.data
    data["message"] = data["message"].strip()
    data["_model"] = done.model
    return data


# ---------------------------------------------------------------- job alert e-mails
MAIL_JOBS_SCHEMA = {
    "type": "object",
    "properties": {"jobs": {"type": "array", "items": {
        "type": "object",
        "properties": {"title": _STR, "company": _STR, "location": _STR, "url": _STR,
                       "work_mode": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unknown"]}},
        "required": ["title", "company", "location", "url", "work_mode"], "additionalProperties": False}}},
    "required": ["jobs"],
    "additionalProperties": False,
}
MAIL_JOBS_SYSTEM = """You read one job alert e-mail (from a job board, a recruiting platform or an AI job agent) and list the job postings it recommends.

Rules:
- Only real job postings the e-mail recommends. Not newsletters, tips, the sender's own careers page, "see all jobs" links, settings, surveys or ads.
- title: the job title as written. company: the hiring company ("" if not given). location: as written, including remote/hybrid if stated ("" if not given).
- work_mode: exactly as the e-mail states it for that posting, e.g. "Budapest (Hybrid)" -> hybrid, "(On-site)" -> onsite, "Remote" -> remote. If the e-mail does not state it for that posting, "unknown". Never guess from the city alone.
- url: the link that opens that posting, copied exactly from the <links> list (the link whose text or position belongs to that posting). If the e-mail has no link for a posting, use "".
- Return an empty list if the e-mail recommends no postings."""


# ---------------------------------------------------------------- work mode of a posting
WORK_MODE_SCHEMA = {
    "type": "object",
    "properties": {"work_mode": {"type": "string", "enum": ["remote", "hybrid", "onsite", "unknown"]}, "evidence": _STR},
    "required": ["work_mode", "evidence"],
    "additionalProperties": False,
}
WORK_MODE_SYSTEM = """You decide where the work of one job posting is done. The posting may be in any language.

- remote: fully remote, remote-first, work from anywhere or from home with no regular office days.
- hybrid: a mix of office and home work (regular office days, "x days a week in the office", "flexible home office", partial télétravail…).
- onsite: the work is done at the office, studio or site, with no remote option (for example "office-based", "in our Paris studio, on-site", "no remote", "présentiel").
- unknown: the posting does not say. Do not guess from a city name or a company address alone, and do not count perks like "home office equipment" or remote-related products as the work mode.

evidence: the short quote (max 140 characters, in the posting's language) that decides it; "" for unknown."""


def work_mode_from_text(llm, title, location, description):
    """(work mode, evidence quote) from the posting text, for postings the rules could not decide."""
    user = "<posting>\nTitle: %s\nLocation: %s\n\n%s\n</posting>" % (title, location or "-", (description or "")[:12000])
    data = llm.json(system=WORK_MODE_SYSTEM, user=user, schema=WORK_MODE_SCHEMA, effort="low").data
    mode = data.get("work_mode") if data.get("work_mode") in ("remote", "hybrid", "onsite") else "unknown"
    return mode, (data.get("evidence") or "")[:200] if mode != "unknown" else ""


def jobs_from_email(llm, subject, sender, text, links):
    """The postings in a job alert e-mail. links: [(text, url)]. Returns [{title, company, location, url}]."""
    link_lines = "\n".join("[%d] %s -> %s" % (i, t[:120], u) for i, (t, u) in enumerate(links[:150]))
    user = "<email>\nFrom: %s\nSubject: %s\n\n%s\n</email>\n\n<links>\n%s\n</links>" % (sender, subject, text[:15000], link_lines)
    data = llm.json(system=MAIL_JOBS_SYSTEM, user=user, schema=MAIL_JOBS_SCHEMA, effort="low").data
    known = {u for _, u in links}
    out = []
    for j in data.get("jobs", []):
        if j.get("title") and (not j.get("url") or j["url"] in known):  # never a link the e-mail does not have
            out.append(j)
    return out



# ---------------------------------------------------------------- interviews
INTERVIEW_MAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "is_interview": {"type": "boolean"}, "cancelled": {"type": "boolean"},
        "company": _STR, "role": _STR, "start": _STR, "minutes": {"type": "integer"},
        "place": _STR, "people": _STR, "summary": _STR,
    },
    "required": ["is_interview", "cancelled", "company", "role", "start", "minutes", "place", "people", "summary"],
    "additionalProperties": False,
}
INTERVIEW_MAIL_SYSTEM = """You read one e-mail from the mailbox of a job seeker and decide whether it fixes a job interview for them: a calendar invitation, a booking confirmation (Calendly, GoodTime, Teams, Zoom…), or a recruiter or hiring manager confirming a date and time. Any language.
- is_interview: true only for an interview (screening call, technical interview, test task review, meeting with the hiring team…) with a fixed date and time, or for the cancellation of one. False for job alerts, newsletters, rejections, offers to pick a slot without a fixed time, and meetings that are not about a job.
- cancelled: true if the e-mail cancels or withdraws a scheduled interview.
- start: the start in the job seeker's time zone (given below) as YYYY-MM-DDTHH:MM, converted if the e-mail gives another zone; resolve relative dates ("tomorrow at 3pm") from the e-mail's date. "" if no fixed time.
- minutes: the length; 0 if not stated.
- company: the hiring company (not the scheduling tool or the recruiting agency, unless no company is named); role: the job title, "" if not given.
- place: the meeting link (Zoom, Teams, Meet…) copied exactly, or the address, or the phone number; "".
- people: the interviewers' names and titles as given, comma-separated; "".
- summary: one short line about what the interview is (e.g. "45 min technical interview with the Unity team")."""


def interview_from_email(llm, sender, subject, sent, text, calendar, tz_name, now_local, companies=()):
    """The interview an e-mail fixes (or cancels). calendar: the invitation's events as text."""
    user = ("Job seeker's time zone: %s. Now: %s.\nCompanies the job seeker applied to (for the names): %s\n\n"
            "<email>\nFrom: %s\nDate: %s\nSubject: %s\n\n%s\n</email>" % (
                tz_name, now_local, ", ".join(companies[:60]) or "-", sender, sent, subject, text[:12000]))
    if calendar:
        user += "\n\n<calendar_invitation>\n%s\n</calendar_invitation>" % calendar[:4000]
    return llm.json(system=INTERVIEW_MAIL_SYSTEM, user=user, schema=INTERVIEW_MAIL_SCHEMA, effort="low").data


RESEARCH_SYSTEM = """You prepare a job seeker for an interview. Search the web and write a short, practical briefing about the company and the interview, in English.
Use these sections (## headings), each with short bullet points; leave a section out if you found nothing reliable:
## At a glance — what the company does, size, headquarters and offices, founded, ownership or funding.
## Products — their main products or games, with platforms and how they are doing.
## Recent news — the last 12 months, each with its month and year (launches, funding, acquisitions, layoffs, leadership changes).
## Tech and team — the technology, engines and tools they use, and how the team for this role works, as far as public sources show.
## Culture — how they work (remote/hybrid, values, what employees say), only from sources you found.
## The people you will meet — for each interviewer named: current role and professional background from public professional sources (company site, LinkedIn, talks). Professional information only, nothing private. Leave the section out if no one is named.
## Likely questions — 6 to 8 questions this company is likely to ask for this role, tailored to it.
## Questions to ask them — 5 smart questions that show you did your homework.
## Watch-outs — anything worth checking (layoffs, bad reviews, unclear funding); leave out if none.
Be factual: say "not found" rather than guess, and write the date of anything time-sensitive. No introduction and no closing remarks."""


def company_research(llm, company, role="", posting="", people="", place="", candidate=""):
    """An interview briefing about the company (markdown-like text) and its sources [(title, url)]. It needs a model
    that can search the web (Claude)."""
    user = "Company: %s\nRole: %s\nInterviewers: %s\nWhere: %s" % (company, role or "-", people or "-", place or "-")
    if posting:
        user += "\n\n<posting>\n%s\n</posting>" % posting[:8000]
    if candidate:
        user += "\n\n<candidate>\nThe job seeker, to tailor the likely questions: %s\n</candidate>" % candidate[:1500]
    return llm.research(RESEARCH_SYSTEM, user)


def tailor(llm, profile, job, description):
    """The tailored CV data (dict, same structure as the profile)."""
    facts = {k: v for k, v in profile.items() if k != "contact"}  # contact details are not sent out
    facts = json.loads(json.dumps(facts))  # the model does not need links; the renderer puts them back
    for x in facts.get("experience", []):
        x.pop("links", None)
        for p in x.get("projects", []):
            p.pop("links", None)
    user = ("<candidate_profile>\n%s\n</candidate_profile>\n\n<job_posting>\nTitle: %s\nCompany: %s\nLocation: %s\nURL: %s\n\n%s\n</job_posting>"
            % (json.dumps(facts, ensure_ascii=False, indent=1), job["title"], job["company"], job["location"] or "-",
               job["url"], description or "(The full posting text is not available; work from the title and company.)"))
    done = llm.json(system=SYSTEM, user=user, schema=SCHEMA, effort="high", timeout=300.0)
    data = done.data
    data["_model"] = done.model
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
    from reportlab.platypus import (
        HRFlowable, Image, KeepTogether, ListFlowable, ListItem, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    )

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


def profile_from_text(llm, text):
    """Profile JSON (dict) from the CV text. Same structure as DEFAULT_PROFILE."""
    text = (text or "").strip()
    if not text:
        raise CVError("No CV text.")
    if len(text) > MAX_CV_CHARS:
        raise CVError("The CV text is too long (max. %d characters)." % MAX_CV_CHARS)
    if not llm:
        raise CVError("No AI key is set (Settings → AI).")
    data = llm.json(system=PROFILE_SYSTEM, user="<cv>\n%s\n</cv>" % text, schema=PROFILE_SCHEMA, effort="medium",
                    timeout=300.0, max_tokens=12000).data
    if not data.get("name") or not data.get("experience"):
        raise CVError("Could not extract the name or work experience from the CV.")
    return data
