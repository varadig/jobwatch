"""The Recruiters page: messages to recruiters about a role."""
from jobwatch.services.outreach import linkedin_preview, outreach_action, outreach_create
from jobwatch.web.controllers.base import Controller
from jobwatch.web.http import Response
from jobwatch.web.views.outreach import outreach_page


class OutreachController(Controller):
    def register(self, router):
        router.get(r"/outreach", self.index)
        router.get(r"/outreach/lookup", self.lookup)
        router.post(r"/outreach/generate", self.generate)
        router.post(r"/outreach/(\d+)", self.update)

    def index(self, req):
        return Response.html(outreach_page(req.query, req.msg))

    def lookup(self, req):
        """What a LinkedIn profile link tells about the person (for the form), as JSON."""
        return Response.json(linkedin_preview(req.query.get("url", "")))

    def generate(self, req):
        return Response.redirect("/outreach", outreach_create(req.form))

    def update(self, req):
        return Response.redirect("/outreach", outreach_action(int(req.params[0]), req.form))
