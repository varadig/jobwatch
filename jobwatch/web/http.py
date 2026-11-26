"""The request and the response, independent of the HTTP server: what a controller gets and what it returns."""
import json
import urllib.parse


class Request:
    """One HTTP request: method, path, query, form and headers. The form is read on first use (a file upload reads
    the raw body instead)."""

    def __init__(self, method, target, headers, body_reader):
        u = urllib.parse.urlparse(target)
        self.method = method
        self.path = u.path
        self.query = {k: v[-1] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.headers = headers
        self._read = body_reader
        self._form = None
        self.params = ()   # the groups the route's pattern captured

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self._read(n) if n else b""

    @property
    def form(self):
        if self._form is None:
            data = self.body().decode("utf-8")
            self._form = {k: v[-1] for k, v in urllib.parse.parse_qs(data, keep_blank_values=True).items()}
        return self._form

    @property
    def msg(self):
        """The message a redirect brought along (?msg=…)."""
        return self.query.get("msg", "")

    def back(self):
        """The page the request came from (path and query), or the start page."""
        ref = urllib.parse.urlparse(self.headers.get("Referer") or "/")
        return (ref.path or "/") + ("?" + ref.query if ref.query else "")


class Response:
    """What goes back: a status, headers and a body. Made with the class methods."""

    def __init__(self, status=200, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or []

    @classmethod
    def html(cls, text, status=200):
        return cls(status, text.encode("utf-8"), [("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    @classmethod
    def redirect(cls, to, msg=None, fragment=None):
        """A redirect (303); msg is shown on the next page, fragment scrolls to a part of it."""
        if msg is not None:
            to += ("&" if "?" in to else "?") + "msg=" + urllib.parse.quote(msg)
        if fragment:
            to += "#" + fragment
        return cls(303, b"", [("Location", to)])

    @classmethod
    def file(cls, data, content_type, download_name=None, inline_name=None, max_age=None):
        headers = [("Content-Type", content_type)]
        if download_name:
            headers.append(("Content-Disposition", 'attachment; filename="%s"' % download_name))
        elif inline_name:
            headers.append(("Content-Disposition", 'inline; filename="%s"' % inline_name))
        headers.append(("Cache-Control", "max-age=%d" % max_age if max_age else "no-store"))
        return cls(200, data, headers)

    @classmethod
    def json(cls, obj):
        return cls(200, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")])

    @classmethod
    def empty(cls, status=404):
        return cls(status, b"")
