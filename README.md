# jobwatch

A self-hosted job watcher. It collects job postings from several sources, scores them against your
keywords, sends a push or e-mail when a strong match appears, and can generate a CV and cover letter
tailored to a posting with the Claude API. The web interface is in Hungarian and uses daisyUI.

It is built on the Python standard library. CV PDFs need `reportlab`.

> **Status:** works for its author daily. Shared as-is for others who want to run it for their own
> search. Expect rough edges.

## What it does

- **Sources:** LinkedIn search pages, MeetFrank, 4dayweek.io, Flexa, Remotive, company job boards
  (Ashby, Greenhouse, Lever, BambooHR, SmartRecruiters), and your own RSS feeds. Each source can be
  switched off in the Settings page.
- **Scoring:** every posting gets points from title keywords, description keywords, location and
  negative keywords (for example "junior" or "sales"). Postings below *Listában min. pont* are hidden,
  and postings at or above *Értesítés min. pont* trigger a notification.
- **Work mode:** postings are classified as remote, hybrid, office or unknown from their text, with a
  filter on the list page.
- **Notifications:** [ntfy](https://ntfy.sh) push and/or e-mail (SMTP).
- **Documents:** a CV, a one-page resume and a cover letter per posting, with a PDF renderer and
  Claude-written tailoring. Only the facts in your profile are used.
- **Tracking:** mark postings as saved, applied or hidden, and see what is new since your last visit.

## Profiles: one search per instance

A **profile** is one search: its own searches, keywords, job database, CV profile and notification topic.
Run one instance per profile on its own port. A Unity search and a project-manager search then never mix.

```sh
python3 tools/new_profile.py my-pm --template project-manager --port 8091
JOBWATCH_DATA=profiles/my-pm JOBWATCH_PORT=8091 python3 app.py
```

The tool creates `profiles/my-pm/` with:

- `config.json`: the template's searches and keywords, a new ntfy topic, and empty secrets.
- `profile.json`: your CV profile. Edit it: name, contact, experience. Until you do, the CVs say "Your Name".
- `cv/`: generated documents.

Templates live in `templates/`:

| Template | For |
| --- | --- |
| `unity-game` | Unity / game development roles |
| `project-manager` | project, programme and product roles (an example to adapt) |

To change what a profile searches for, edit the Settings page, or the `linkedin_searches`,
`remotive_queries` and keyword lists in its `config.json`.

## Running and settings

Environment variables (all optional):

| Variable | Default | Meaning |
| --- | --- | --- |
| `JOBWATCH_DATA` | `./data` | the profile folder (config, database, CVs, profile) |
| `JOBWATCH_PORT` | `8090` | HTTP port |
| `JOBWATCH_DOCS` | `<data>/docs` | where the per-company document folders go |
| `JOBWATCH_SMB_PREFIX`, `JOBWATCH_SMB_BASE` | empty | optional: show a network-share link for the document folder, for example `/srv/share` → `smb://host/share` |
| `ANTHROPIC_API_KEY` | empty | the Claude API key, if not set in Settings |

The first CV generation needs an Anthropic API key. Without one the rest of the app works.

## Privacy and secrets

- `config.json` holds the ntfy topic (it acts as a password for push notifications), the e-mail
  password and the API key. It is written with mode `600`. Never commit it.
- `profile.json`, the CVs, the job database and photos are personal. The `.gitignore` excludes
  `profiles/` and `data/`. Keep it that way.
- The source code contains no personal data. The CV renderer uses only what is in `profile.json`.

## Development

```sh
python3 -m unittest discover -s tests -v
```

The tests cover scoring per profile, document names, the daisyUI markup helper, and the profile tool.

## Notes on sources

- LinkedIn is read through its public job-search pages, the same way a browser shows them. LinkedIn's
  terms restrict automated access; whether and how you use this source is your decision. Switch it off
  in Settings if you do not want it.
- Company boards use the public APIs of Ashby, Greenhouse, Lever, BambooHR and SmartRecruiters. Add a
  company in the Studios list as `system | board-id | name`.
- Some sites (Cloudflare-protected or login-only pages) cannot be read automatically. The Sources page
  lists them.

## Layout

| File | Purpose |
| --- | --- |
| `app.py` | HTTP server, scheduler, sources, scoring, notifications, UI |
| `cvgen.py` | Claude tailoring and PDF rendering (reportlab) |
| `tools/new_profile.py` | creates a profile folder from a template |
| `templates/` | search and keyword templates; `profile.example.json` for a CV profile |
| `tests/` | unit tests |
| `fonts/` | Inter (SIL OFL 1.1) and DejaVu Sans fonts used by the PDF renderer |
