"""Runtime state shared by the background work and the pages."""
import threading


RUN_LOCK = threading.Lock()


STATE = {"running": False, "last_run": None, "next_run": None, "progress": None, "enrich": None}


ENRICH_LOCK = threading.Lock()


MODE_LOCK = threading.Lock()


IV_LOCK = threading.Lock()        # one e-mail scan at a time
