#!/usr/bin/env python3
"""jobwatch - watches job postings, scores them against your CV, sends ntfy push / e-mail notifications,
and serves a web interface. Run: python3 app.py  (port: JOBWATCH_PORT, default 8090; data: JOBWATCH_DATA)

The code is in the jobwatch package; this file only starts it."""
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)   # the package is found under "python -I" too

from jobwatch.web.server import main  # noqa: E402

if __name__ == "__main__":
    main()
