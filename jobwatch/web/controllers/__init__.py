"""The controllers: each handles the requests of one area, calls the services and picks the view."""
from jobwatch.web.controllers.assets import AssetsController
from jobwatch.web.controllers.documents import DocumentsController
from jobwatch.web.controllers.interviews import InterviewsController
from jobwatch.web.controllers.jobs import JobsController
from jobwatch.web.controllers.log import LogController
from jobwatch.web.controllers.outreach import OutreachController
from jobwatch.web.controllers.settings import SettingsController

CONTROLLERS = (AssetsController, JobsController, InterviewsController, OutreachController, DocumentsController,
               SettingsController, LogController)
