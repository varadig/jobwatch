"""Files the pages need: icons, the web app manifest, the static CSS / JS / font, company logos, and the health check."""
import json
import os

from jobwatch.env import LOGO_DIR, STATIC_DIR
from jobwatch.services.logos import LOGO_TYPES, logo_files
from jobwatch.web.components import font_version
from jobwatch.web.controllers.base import Controller
from jobwatch.web.http import Response

WEEK = 7 * 86400
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".woff2": "font/woff2",
                ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}
ICON_FILES = {"/favicon.ico": "favicon.ico", "/favicon.svg": "favicon.svg", "/apple-touch-icon.png": "apple-touch-icon.png",
              "/icon-192.png": "icon-192.png", "/material-symbols.woff2": "material-symbols.woff2"}
MANIFEST = {"name": "jobwatch", "short_name": "jobwatch", "start_url": "/", "display": "standalone",
            "background_color": "#1d232a", "theme_color": "#5b5bd6",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/favicon.svg", "sizes": "any", "type": "image/svg+xml"}]}


class AssetsController(Controller):
    def register(self, router):
        for path in ICON_FILES:
            router.get(path.replace(".", r"\."), self.icon, public=True)
        router.get(r"/manifest\.webmanifest", self.manifest, public=True)
        router.get(r"/static/([a-z0-9_-]+(?:/[a-z0-9_-]+)?\.(?:css|js))", self.static, public=True)
        router.get(r"/logo/([a-z0-9]{1,200})", self.logo)
        router.get(r"/health", self.health, public=True)

    def icon(self, req):
        name = ICON_FILES[req.path]
        return self._file(os.path.join(STATIC_DIR, name))

    def manifest(self, req):
        return Response.file(json.dumps(MANIFEST).encode(), "application/manifest+json", max_age=WEEK)

    def static(self, req):
        """The pages' CSS and JS (static/css, static/js). The pages link them with ?v=<checksum>, so they are kept a
        week and a changed file still loads at once."""
        path = os.path.realpath(os.path.join(STATIC_DIR, req.params[0]))
        if not path.startswith(os.path.realpath(STATIC_DIR) + os.sep):
            return Response.empty(404)
        resp = self._file(path)
        if path.endswith(".css") and resp.status == 200:   # the icon font's address carries its checksum
            resp.body = resp.body.replace(b"FONTVER", font_version().encode())
        return resp

    def logo(self, req):
        fname = logo_files().get(req.params[0])
        path = os.path.join(LOGO_DIR, fname) if fname else ""
        if not path or not os.path.isfile(path):
            return Response.empty(404)
        ctype = {v: k for k, v in LOGO_TYPES.items()}.get(os.path.splitext(fname)[1], "application/octet-stream")
        resp = self._file(path, ctype)
        resp.headers.append(("X-Content-Type-Options", "nosniff"))
        return resp

    def health(self, req):
        return Response.html("ok")

    @staticmethod
    def _file(path, ctype=None):
        if not os.path.isfile(path):
            return Response.empty(404)
        with open(path, "rb") as fh:
            data = fh.read()
        return Response.file(data, ctype or STATIC_TYPES.get(os.path.splitext(path)[1], "application/octet-stream"),
                             max_age=WEEK)
