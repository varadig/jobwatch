#!/usr/bin/env python3
"""Creates a new jobwatch profile: its own data folder, config and CV profile, on its own port.

One profile = one running jobwatch instance. Each instance has its own searches, keywords,
job database, CVs and notification topic, so a Unity search and a project-manager search
never mix.

    python3 tools/new_profile.py my-pm --template project-manager
    python3 app.py                       # see the printed command for the exact env vars
"""
import argparse
import json
import os
import secrets
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(ROOT, "templates")


def available_templates():
    return sorted(f[:-5] for f in os.listdir(TEMPLATES) if f.endswith(".json") and f != "profile.example.json")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("name", help="folder name of the profile, e.g. my-pm (letters, digits, - and _)")
    ap.add_argument("--template", default="project-manager", help="one of: " + ", ".join(available_templates()))
    ap.add_argument("--port", type=int, default=8090, help="port for this instance (default 8090)")
    ap.add_argument("--profiles-dir", default=os.path.join(ROOT, "profiles"), help="where profile folders live")
    args = ap.parse_args(argv)

    if not args.name or not all(c.isalnum() or c in "-_" for c in args.name):
        ap.error("name may only contain letters, digits, '-' and '_'")
    tpl_path = os.path.join(TEMPLATES, args.template + ".json")
    if not os.path.exists(tpl_path):
        ap.error("unknown template %r; choose from: %s" % (args.template, ", ".join(available_templates())))

    data_dir = os.path.join(args.profiles_dir, args.name)
    if os.path.exists(data_dir):
        ap.error("%s already exists" % data_dir)
    os.makedirs(os.path.join(data_dir, "cv"))

    with open(tpl_path, encoding="utf-8") as f:
        cfg = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    cfg["public_url"] = "http://localhost:%d/" % args.port
    cfg["ntfy"] = {"enabled": True, "server": "https://ntfy.sh", "topic": "jobwatch-" + secrets.token_hex(6)}
    cfg["anthropic_api_key"] = ""
    cfg["email"] = {"enabled": False, "smtp_host": "smtp.gmail.com", "smtp_port": 587,
                    "username": "", "password": "", "to": ""}
    cfg_path = os.path.join(data_dir, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(cfg_path, 0o600)  # holds the ntfy topic (it is also the password) and, later, the API key

    shutil.copy(os.path.join(TEMPLATES, "profile.example.json"), os.path.join(data_dir, "profile.json"))

    print("Created %s" % data_dir)
    print("  1. Edit %s/profile.json: your name, contact and experience (used for the CVs)." % data_dir)
    print("  2. Open the Settings page and paste your Anthropic API key if you want CV generation.")
    print("  3. Start it:")
    print("     JOBWATCH_DATA=%s JOBWATCH_PORT=%d python3 %s" % (data_dir, args.port, os.path.join(ROOT, "app.py")))
    print("  4. Subscribe to the ntfy topic in the app: %s" % cfg["ntfy"]["topic"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
