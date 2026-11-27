"""The Log page and its export."""
import json
from datetime import datetime

from jobwatch.web.controllers.base import Controller
from jobwatch.web.http import Response
from jobwatch.web.views.log import log_page, query_events


class LogController(Controller):
    def register(self, router):
        router.get(r"/log", self.index)
        router.get(r"/log/export", self.export)

    def index(self, req):
        return Response.html(log_page(req.query, req.msg))

    def export(self, req):
        """The events that match the filters (up to 5000), oldest first, as JSON lines: one event per line."""
        rows = query_events(req.query, limit=5000)
        lines = []
        for ev in reversed(rows):
            ev = dict(ev)
            if ev.get("detail"):
                try:
                    ev["detail"] = json.loads(ev["detail"])
                except ValueError:
                    pass
            lines.append(json.dumps(ev, ensure_ascii=False))
        body = "\n".join(lines) + "\n"
        return Response.file(body.encode("utf-8"), "application/x-ndjson; charset=utf-8",
                             download_name="jobwatch-log-%s.jsonl" % datetime.now().strftime("%Y-%m-%d"))
