"""hopper-dashboard probes — stdlib-only heartbeat/metrics senders.

Everything in this package must run under macOS's stock ``/usr/bin/python3`` (3.9) with no
venv and no third-party imports, because launchd invokes it with a minimal environment.
Pure logic lives in importable functions so it can be unit-tested with fixtures and no network.
"""
