"""All the sources by name, and how often each may be fetched."""

from jobwatch.sources.custom import src_custom
from jobwatch.sources.feeds import src_4dayweek, src_arbeitnow, src_flexa, src_himalayas, src_jobicy, src_meetfrank, src_remotegamejobs, src_remoteok, src_remotive, src_rss, src_weworkremotely, src_workingnomads, src_workwithindies
from jobwatch.sources.linkedin import src_linkedin
from jobwatch.sources.mail import src_mail
from jobwatch.sources.studios import src_studios


SOURCE_FUNCS = {"linkedin": src_linkedin, "meetfrank": src_meetfrank, "4dayweek": src_4dayweek,
                "flexa": src_flexa, "remotive": src_remotive, "studios": src_studios,
                "rss": src_rss, "custom": src_custom, "mail": lambda cfg: src_mail(cfg),
                "remoteok": src_remoteok, "weworkremotely": src_weworkremotely, "himalayas": src_himalayas,
                "jobicy": src_jobicy, "workingnomads": src_workingnomads, "arbeitnow": src_arbeitnow,
                "remotegamejobs": src_remotegamejobs, "workwithindies": src_workwithindies}


MIN_INTERVAL = {"remotive": 360,  # perc; a Remotive API max ~4 hivast ker naponta
                "custom": 120,    # own sources (e.g. munka.hu): gentle, so the site does not block us
                # the optional public boards: gentle, they are free APIs that ask not to be hammered
                "remoteok": 120, "himalayas": 120, "jobicy": 120, "workingnomads": 120, "arbeitnow": 120}
