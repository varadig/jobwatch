"""Environment: where the data, documents and fonts live, the port, and the logger.

Everything comes from environment variables with defaults next to the code, so one copy of the code can run
several profiles (JOBWATCH_DATA) on several ports (JOBWATCH_PORT)."""
import logging
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # the project folder (above the package)
DATA = os.environ.get("JOBWATCH_DATA", os.path.join(BASE, "data"))
PORT = int(os.environ.get("JOBWATCH_PORT", "8090"))

CFG_PATH = os.path.join(DATA, "config.json")
PROFILE_PATH = os.path.join(DATA, "profile.json")
DB_PATH = os.path.join(DATA, "jobs.db")
CV_DIR = os.path.join(DATA, "cv")
LOGO_DIR = os.path.join(DATA, "logos")   # company logos, downloaded once and served from here
PHOTO_PATH = os.path.join(DATA, "photo.png")

FONT_DIR = os.path.join(BASE, "fonts")
TEMPLATE_DIR = os.path.join(BASE, "templates")
STATIC_DIR = os.path.join(BASE, "static")

# Generated documents (CV / resume / cover letter), one folder per company. If it is on a network share, set
# JOBWATCH_SMB_PREFIX (the local path prefix) and JOBWATCH_SMB_BASE (its smb:// address) to get a Finder link too.
DOCS_ROOT = os.environ.get("JOBWATCH_DOCS") or os.path.join(DATA, "docs")
SMB_PREFIX = os.environ.get("JOBWATCH_SMB_PREFIX", "")
SMB_BASE = os.environ.get("JOBWATCH_SMB_BASE", "")

UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jobwatch")
