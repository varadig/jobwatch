"""Domain constants shared by the layers: statuses, work modes, source catalogue."""

SOURCES_INFO = {
    "linkedin": ("LinkedIn", "https://www.linkedin.com/jobs/"),
    "meetfrank": ("MeetFrank", "https://meetfrank.com/latest-remote-jobs-in-meetfrankcom"),
    "4dayweek": ("4dayweek.io", "https://4dayweek.io/"),
    "flexa": ("Flexa", "https://flexa.careers/"),
    "remotive": ("Remotive (extra)", "https://remotive.com/"),
    # optional boards (off until switched on in Settings)
    "remoteok": ("Remote OK", "https://remoteok.com/"),
    "weworkremotely": ("We Work Remotely", "https://weworkremotely.com/"),
    "himalayas": ("Himalayas", "https://himalayas.app/jobs"),
    "jobicy": ("Jobicy", "https://jobicy.com/"),
    "workingnomads": ("Working Nomads", "https://www.workingnomads.com/jobs"),
    "arbeitnow": ("Arbeitnow (EU, mostly Germany)", "https://www.arbeitnow.com/"),
    "remotegamejobs": ("Remote Game Jobs", "https://remotegamejobs.com/"),
    "workwithindies": ("Work With Indies", "https://www.workwithindies.com/"),
    "studios": ("Company job boards", ""),
    "rss": ("Own RSS feeds", ""),
    "custom": ("Own sources", ""),
    "mail": ("E-mail alerts", ""),
    "manual": ("Added by hand", ""),
}

FETCHED_SOURCES = [k for k in SOURCES_INFO if k != "manual"]

UNSUPPORTED = [
    ("HiringCafe", "https://hiringcafe.com/", "Cloudflare blocks automated fetching (403)"),
    ("JobLeads", "https://www.jobleads.com/", "Cloudflare blocks automated fetching (403)"),
    ("Cord", "https://cord.com/", "JavaScript app, cannot be read without a browser"),
    ("Jack & Jill", "https://app.jackandjill.ai/", "Login required"),
    ("Company career pages", "", "No public API: check manually, or add the company's board to the Studios list"),
]

# Your decisions on postings (hidden, saved, applied, seen) are kept apart from the job cache, keyed by the posting's id,
# link and title + company: after a cache reset or a new search the same posting, from any source, gets them back.
KEPT_STATUSES = ("hidden", "saved", "applied", "seen")

WORK_MODES = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site", "unknown": "Not stated"}

INTERVIEW_STATUS = {"planned": "Planned", "done": "Done", "cancelled": "Cancelled"}

STATUS_LABEL = {"new": "new", "seen": "seen", "saved": "saved", "applied": "applied", "hidden": "hidden"}
