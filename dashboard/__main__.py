"""Local dev runner: ``python -m dashboard`` serves BOTH roles from one process
(read on :8080, ingest on :8081) using Werkzeug's dev server. Production uses
``entrypoint.sh`` → two gunicorn processes instead.

Defaults that differ from the container: ``DASHBOARD_DATA`` → ``./data`` and
``JOBS_FILE`` → ``./jobs.yml`` when those env vars are unset and the container
paths don't exist.
"""

from __future__ import annotations

import os
import sys
import threading

from werkzeug.serving import make_server

from . import create_app
from .config import Settings
from .registry import RegistryError


def _dev_settings() -> Settings:
    if "DASHBOARD_DATA" not in os.environ and not os.path.isdir("/app/data"):
        os.environ["DASHBOARD_DATA"] = os.path.abspath("data")
    if "JOBS_FILE" not in os.environ and not os.path.exists("/app/jobs.yml"):
        os.environ["JOBS_FILE"] = os.path.abspath("jobs.yml")
    return Settings.from_env()


def main() -> int:
    settings = _dev_settings()
    host = os.environ.get("DASHBOARD_BIND", "127.0.0.1")
    read_port = int(os.environ.get("READ_PORT", "8080"))
    ingest_port = int(os.environ.get("INGEST_PORT", "8081"))
    try:
        ingest_app = create_app("ingest", settings)
        read_app = create_app("read", settings)
    except RegistryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    ingest_srv = make_server(host, ingest_port, ingest_app, threaded=True)
    read_srv = make_server(host, read_port, read_app, threaded=True)
    t = threading.Thread(target=ingest_srv.serve_forever, daemon=True)
    t.start()
    print(f"read   → http://{host}:{read_port}/   "
          f"ingest → http://{host}:{ingest_port}/api/v1/ping/<job>",
          file=sys.stderr)
    try:
        read_srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ingest_srv.shutdown()
        ingest_app.extensions["scheduler"].stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
