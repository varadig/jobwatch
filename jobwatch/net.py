"""HTTP: fetching pages with a browser-like client."""
import urllib.request

from jobwatch.env import UA


class HttpClient:
    """Fetches pages as text, presenting itself as a browser (some job boards refuse other clients)."""

    def __init__(self, user_agent=UA):
        self.user_agent = user_agent

    def get(self, url, data=None, headers=None, timeout=30):
        """The page's text; with data, a POST. Raises urllib's errors (HTTPError, URLError) as they come."""
        h = {"User-Agent": self.user_agent, "Accept-Language": "en-US,en;q=0.9"}
        h.update(headers or {})
        req = urllib.request.Request(url, data=data, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")


http = HttpClient()
