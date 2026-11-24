#!/usr/bin/env python3
"""Start jobwatch with one command. Works on macOS, Windows and Linux.

What it does, every time you run it:
  1. checks that Python 3.10 or newer is installed,
  2. creates a private environment in .venv/ and installs the requirements (first run only),
  3. creates your search profile in profiles/ (first run only; asks a few questions),
  4. starts the app and opens it in your browser.

Stop it with Ctrl+C (or close the window).
"""
import os
import subprocess
import sys
import threading
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
MIN_PY = (3, 10)
PORT = 8090
PYTHON_DOWNLOAD = "https://www.python.org/downloads/"

TEMPLATES = {
    "1": ("unity-game", "Unity / játékfejlesztő állások"),
    "2": ("project-manager", "Projekt- és termékmenedzser állások"),
}


def say(msg=""):
    print(msg, flush=True)


def venv_python():
    if os.name == "nt":
        return os.path.join(VENV, "Scripts", "python.exe")
    return os.path.join(VENV, "bin", "python")


def check_python():
    if sys.version_info < MIN_PY:
        say("A jobwatch-hoz Python %d.%d vagy újabb kell, de ez a %d.%d." % (MIN_PY + sys.version_info[:2]))
        say("Töltsd le innen, telepítsd, majd indítsd újra ezt a programot:")
        say("  " + PYTHON_DOWNLOAD)
        if os.name == "nt":
            say("Telepítéskor pipáld be: \"Add python.exe to PATH\".")
        sys.exit(1)


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=HERE, check=True, **kw)


STEP_PREFIXES = ("Collecting", "Downloading", "Installing collected", "Successfully installed")


def run_with_progress(cmd, label):
    """Runs a command and shows a spinner with the elapsed time, plus each install step as it happens."""
    import itertools
    import threading
    import time
    tty = sys.stdout.isatty()
    lock = threading.Lock()
    state = {"done": False, "start": time.time(), "shown": False}

    def clear_line():
        if tty and state["shown"]:
            sys.stdout.write("\r" + " " * 79 + "\r")
            state["shown"] = False

    def spinner():
        for ch in itertools.cycle("|/-\\"):
            if state["done"]:
                break
            with lock:
                if tty:
                    sys.stdout.write("\r%s %s … %3ds" % (ch, label, time.time() - state["start"]))
                    sys.stdout.flush()
                    state["shown"] = True
            time.sleep(0.2)

    proc = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    lines = []
    if tty:
        threading.Thread(target=spinner, daemon=True).start()
    for line in proc.stdout:
        lines.append(line)
        step = line.strip()
        if step.startswith(STEP_PREFIXES):
            with lock:
                clear_line()
                say("  • " + step[:90])
    state["done"] = True
    proc.wait()
    with lock:
        clear_line()
    if proc.returncode:
        say("".join(lines[-30:]))
        raise subprocess.CalledProcessError(proc.returncode, cmd)
    say("  ✓ %s (%ds)" % (label, time.time() - state["start"]))


def setup_environment():
    py = venv_python()
    if not os.path.exists(py):
        say("Első indítás: a környezet létrehozása...")
        run_with_progress([sys.executable, "-m", "venv", VENV], "virtuális környezet")
    stamp = os.path.join(VENV, ".requirements-installed")
    req = os.path.join(HERE, "requirements.txt")
    if not os.path.exists(stamp) or os.path.getmtime(stamp) < os.path.getmtime(req):
        say("Csomagok telepítése (az első alkalommal 1–2 perc)...")
        run_with_progress([py, "-m", "pip", "install", "--disable-pip-version-check", "-r", req], "pip telepítés")
        open(stamp, "w").close()
    return py


def ask(question, default=""):
    try:
        answer = input(question).strip()
    except EOFError:
        answer = ""
    return answer or default


def setup_profile(py):
    existing = [d for d in os.listdir(os.path.join(HERE, "profiles"))
                if os.path.isdir(os.path.join(HERE, "profiles", d))] if os.path.isdir(os.path.join(HERE, "profiles")) else []
    if existing:
        return existing[0]
    say("")
    say("Még nincs keresési profilod. Válassz egyet (később a böngészőben is módosíthatod):")
    for key, (_, label) in TEMPLATES.items():
        say("  %s) %s" % (key, label))
    choice = ask("Melyik? [1/2, alapértelmezett: 2]: ", "2")
    template = TEMPLATES.get(choice, TEMPLATES["2"])[0]
    name = ask("Profil neve (betű, szám, - és _; alapértelmezett: sajat): ", "sajat")
    run([py, os.path.join(HERE, "tools", "new_profile.py"), name, "--template", template, "--port", str(PORT),
         "--profiles-dir", os.path.join(HERE, "profiles")])
    return name


def free_port(start):
    import socket
    for port in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    return start


def main():
    os.chdir(HERE)
    say("jobwatch – állásfigyelő")
    say("-" * 40)
    check_python()
    py = setup_environment()
    name = setup_profile(py)
    port = free_port(PORT)
    url = "http://localhost:%d/" % port
    env = dict(os.environ, JOBWATCH_DATA=os.path.join(HERE, "profiles", name), JOBWATCH_PORT=str(port))
    say("")
    say("Indul: %s" % url)
    say("Leállítás: Ctrl+C (vagy zárd be az ablakot).")
    say("")
    threading.Timer(3, lambda: webbrowser.open(url)).start()
    try:
        subprocess.run([py, os.path.join(HERE, "app.py")], cwd=HERE, env=env)
    except KeyboardInterrupt:
        say("\nLeállítva.")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as ex:
        say("\nHiba történt a telepítés közben (%s). Másold ki a fenti üzenetet, és kérj segítséget." % ex.cmd)
        sys.exit(1)
    except KeyboardInterrupt:
        say("\nLeállítva.")
