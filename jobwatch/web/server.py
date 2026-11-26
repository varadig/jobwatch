"""The HTTP server: the routes of every controller, the password check, and the adapter to the standard library's
HTTP server. Run it with main()."""
import base64
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jobwatch.config import config_store
from jobwatch.db import database
from jobwatch.env import PORT, log
from jobwatch.services.documents import backfill_docs, restore_cv_jobs
from jobwatch.services.interviews.loop import interview_loop
from jobwatch.services.refresh import scheduler
from jobwatch.util.text import e
from jobwatch.web.components import REQ, page
from jobwatch.web.controllers import CONTROLLERS
from jobwatch.web.http import Request, Response
from jobwatch.web.router import Router

AUTH_PASSWORD = os.environ.get("JOBWATCH_PASSWORD", "")   # empty: no password (a trusted network)


def auth_ok(header, password):
    """True if no password is set, or the Basic auth header carries it (the user name is ignored)."""
    if not password:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[len("Basic "):]).decode("utf-8")
    except Exception:
        return False
    return secrets.compare_digest(decoded.partition(":")[2].encode("utf-8"), password.encode("utf-8"))


def build_router():
    router = Router()
    for controller in CONTROLLERS:
        controller().register(router)
    return router


ROUTER = build_router()


def dispatch(req, router=ROUTER):
    """The response to a request: the route's action, or 404; an error in an action is a 500 page (and logged)."""
    route, params = router.match(req.method, req.path)
    if not route:
        return Response.html(page("Not found", "<p>404</p>"), 404)
    req.params = params
    try:
        return route.action(req)
    except Exception as ex:
        log.exception("%s %s failed", req.method, req.path)
        return Response.html(page("Error", "<p class=err>%s</p>" % e(ex)), 500)


class Handler(BaseHTTPRequestHandler):
    """Turns an HTTP request into a Request, checks the password, and writes the Response back."""

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method):
        req = Request(method, self.path, self.headers, self.rfile.read)
        REQ.path = req.path   # the active tab in the page header
        if not (ROUTER.is_public(method, req.path) or auth_ok(self.headers.get("Authorization"), AUTH_PASSWORD)):
            resp = Response(401, b"Login required.", [("Content-Type", "text/plain; charset=utf-8"),
                                                      ("WWW-Authenticate", 'Basic realm="jobwatch", charset="UTF-8"')])
        else:
            resp = dispatch(req)
        self.send_response(resp.status)
        for name, value in resp.headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        self.wfile.write(resp.body)


def main():
    database.init()
    database.migrate()
    restore_cv_jobs()
    threading.Thread(target=backfill_docs, daemon=True).start()
    with config_store.lock:
        cfg = config_store.load()
    log.info("jobwatch starting: port %d, ntfy topic: %s", PORT, cfg["ntfy"]["topic"])
    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=interview_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
