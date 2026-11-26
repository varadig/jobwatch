"""Routing: which controller method answers a request."""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Route:
    method: str
    pattern: re.Pattern
    action: object          # the controller method: action(request) -> Response
    public: bool = False    # reachable without the password (health check, icons, the secret calendar feed)


class Router:
    """Routes are matched in the order they were added; a pattern matches the whole path, its groups become
    request.params."""

    def __init__(self):
        self.routes = []

    def add(self, method, pattern, action, public=False):
        self.routes.append(Route(method, re.compile(pattern), action, public))

    def get(self, pattern, action, public=False):
        self.add("GET", pattern, action, public)

    def post(self, pattern, action, public=False):
        self.add("POST", pattern, action, public)

    def match(self, method, path):
        """(route, groups) for the request, or (None, ())."""
        for route in self.routes:
            if route.method == method:
                m = route.pattern.fullmatch(path)
                if m:
                    return route, m.groups()
        return None, ()

    def is_public(self, method, path):
        route, _ = self.match(method, path)
        return bool(route and route.public)
