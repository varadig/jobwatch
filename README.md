# jobwatch

A self-hosted job watcher. It collects job postings from several sources, scores them against your
keywords, sends a push or e-mail when a strong match appears, and can generate a CV and cover letter
tailored to a posting with the Claude API. The web interface is in Hungarian and uses daisyUI.

![jobwatch screenshot: the job list with scores, filters and the English interface](docs/screenshot.png)

It is built on the Python standard library. CV PDFs need `reportlab`.

> **Status:** works for its author daily. Shared as-is for others who want to run it for their own
> search. Expect rough edges.

## What it does

- **Sources:** LinkedIn search pages, MeetFrank, 4dayweek.io, Flexa, Remotive, company job boards
  (Ashby, Greenhouse, Lever, BambooHR, SmartRecruiters), and your own RSS feeds. Each source can be
  switched off in the Settings page.
- **Scoring:** every posting gets points from title keywords, description keywords, location and
  negative keywords (for example "junior" or "sales"). Postings below *Min. score in list* are hidden,
  and postings at or above *Min. score for notifications* trigger a notification.
- **Work mode:** postings are classified as remote, hybrid, office or unknown from their text, with a
  filter on the list page.
- **Notifications:** [ntfy](https://ntfy.sh) push and/or e-mail (SMTP).
- **Documents:** a CV, a one-page resume and a cover letter per posting, with a PDF renderer and
  Claude-written tailoring. Only the facts in your profile are used.
- **Tracking:** mark postings as saved, applied or hidden, and see what is new since your last visit.

## Easy start (no terminal needed)

1. Install **Python 3.10 or newer** from [python.org/downloads](https://www.python.org/downloads/).
   On Windows, tick **"Add python.exe to PATH"** during installation.
2. Download this project (green **Code** button → **Download ZIP**) and unzip it.
3. Start it:
   - **Mac:** double-click `start-mac.command`. The first time, right-click it and choose **Open**.
   - **Windows:** double-click `start-windows.bat`.

The first start installs what it needs (a minute or two), asks which kind of jobs you are looking for,
and opens the app in your browser at `http://localhost:8090`. Later starts open it straight away.
To stop it, press Ctrl+C in its window. Everything it creates stays inside the project folder.

Only you can see the app: it runs on your computer, not on the internet.

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

**Creating the CV profile from your existing CV:** on the CV-profil page, upload a PDF or paste the text.
Claude turns it into the profile structure and shows the result in the editor. Check it, fix what is wrong,
then press Save. Your full CV text, contact details included, is sent to the Anthropic API for this step.
A scanned PDF with no text layer cannot be read; paste the text instead. The API key is set in Settings.

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
| `JOBWATCH_PASSWORD` | empty | if set, every page needs HTTP Basic auth with this password |

The first CV generation needs an Anthropic API key. Without one the rest of the app works.

## Hosting a demo instance (Docker, Fly.io)

The image runs one profile. Data lives in `/data` (a volume). The first start seeds an empty volume with
the project-manager template and a blank CV profile, so a demo never contains personal data.

```sh
docker build -t jobwatch .
docker run -p 8080:8080 -v jobwatch-data:/data -e JOBWATCH_PASSWORD='change-me' jobwatch
```

Fly.io (see `fly.toml`; change the app name first):

```sh
fly volumes create jobwatch_data --size 1 --region ams
fly secrets set JOBWATCH_PASSWORD='<long random password>'
fly deploy
```

Notes:

- **Password:** `JOBWATCH_PASSWORD` turns on HTTP Basic auth for every page. The user name is ignored; any
  name with the right password gets in. Without the variable the app is open, as on a home network.
  `/health` stays open for the platform's health check.
- **One machine only.** The refresh scheduler runs inside the app, and the config says `auto_stop_machines = false`.
  Do not scale to two machines on one volume.
- **Container runs as root** on purpose: a freshly mounted Fly volume is root-owned.
- Check the Fly pricing and free allowance before deploying; they change.

## Privacy and secrets

- `config.json` holds the ntfy topic (it acts as a password for push notifications), the e-mail
  password and the API key. It is written with mode `600`. Never commit it.
- `profile.json`, the CVs, the job database and photos are personal. The `.gitignore` excludes
  `profiles/` and `data/`. Keep it that way.
- The source code contains no personal data. The CV renderer uses only what is in `profile.json`.

## License

MIT, see [LICENSE](LICENSE).

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
| `Dockerfile`, `docker-entrypoint.sh`, `fly.toml` | container image and Fly.io config |
| `fonts/` | Inter (SIL OFL 1.1) and DejaVu Sans fonts used by the PDF renderer |
