"""Every source, in the order they are shown, and what the pages need to know about them."""
from jobwatch.sources.base import Filtering
from jobwatch.sources.custom import CustomSource
from jobwatch.sources.feeds import (
    ArbeitnowSource, FlexaSource, FourDayWeekSource, HimalayasSource, JobicySource, MeetFrankSource,
    RemoteGameJobsSource, RemoteOkSource, RemotiveSource, RssSource, WeWorkRemotelySource, WorkingNomadsSource,
    WorkWithIndiesSource
)
from jobwatch.sources.linkedin import LinkedInSource
from jobwatch.sources.mail import MailAlertSource
from jobwatch.sources.studios import StudiosSource

SOURCES = (LinkedInSource(), MeetFrankSource(), FourDayWeekSource(), FlexaSource(), RemotiveSource(), RemoteOkSource(), WeWorkRemotelySource(), HimalayasSource(), JobicySource(), WorkingNomadsSource(), ArbeitnowSource(), RemoteGameJobsSource(), WorkWithIndiesSource(), StudiosSource(), RssSource(), CustomSource(), MailAlertSource())
BY_KEY = {s.key: s for s in SOURCES}

OPTIONAL_BOARDS = [s.key for s in SOURCES if s.optional]
SEARCH_FILTERABLE = [s.key for s in SOURCES if s.filtering is Filtering.OPTIONAL]   # the search filter is a setting
