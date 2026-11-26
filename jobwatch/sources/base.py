"""The base of every source: what it is, how its postings are narrowed to the search, and how often it may run."""
from abc import ABC, abstractmethod
from enum import Enum

from jobwatch.models import SOURCES_INFO
from jobwatch.search import matches_search, search_keywords


class Filtering(Enum):
    """How a source's postings are narrowed to the search terms."""
    NONE = "none"           # the source searches with the terms itself
    OPTIONAL = "optional"   # it lists everything; Settings decides whether only the matching postings are kept
    ALWAYS = "always"       # it lists everything (all trades): only the matching postings are kept
    TITLE = "title"         # as ALWAYS, and a word of the term must be in the title (a description alone matches
                            # too much, e.g. "Unity Catalog" in a data posting)


class JobSource(ABC):
    """A place postings come from. A subclass sets its key (see SOURCES_INFO for its name and link) and fetches."""
    key = ""
    filtering = Filtering.NONE
    optional = False        # an extra board, off until switched on in Settings
    min_interval = None     # minutes between two fetches, when the site asks for fewer calls than the refresh interval

    @property
    def label(self):
        return SOURCES_INFO[self.key][0]

    @property
    def url(self):
        return SOURCES_INFO[self.key][1]

    @abstractmethod
    def fetch(self, cfg):
        """The postings found now: [{ext_id, title, company, location, url, posted, description, work_mode?, logo?}]."""

    def interval(self, cfg):
        """Minutes to wait between two fetches."""
        return max(int(cfg["interval_minutes"]), self.min_interval or 0)

    def search_filter(self, cfg):
        """The search keywords this source's postings must match, or [] to keep them all."""
        on = self.filtering in (Filtering.ALWAYS, Filtering.TITLE) or (
            self.filtering is Filtering.OPTIONAL and (cfg.get("search_filter") or {}).get(self.key))
        return search_keywords(cfg) if on else []

    def keeps(self, keywords, posting):
        """Whether a posting stays, given search_filter()'s keywords."""
        return not keywords or matches_search(keywords, posting, in_title=self.filtering is Filtering.TITLE)

    def __repr__(self):
        return "<%s %s>" % (type(self).__name__, self.key)
