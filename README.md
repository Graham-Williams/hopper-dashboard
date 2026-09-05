# hopper-dashboard

A small self-hosted dashboard that shows the health of every backup and scheduled job across one home server
and one Mac — last run, last success, whether the destination really has fresh bytes, and how far behind the
manual jobs are. API-first (JSON) with an HTML board on top, dead-man's-switch heartbeats, destination probes
via rclone, and push alerts via ntfy.

See `DESIGN.md` for the design and API contract, `CLAUDE.md` for how to run and test.
