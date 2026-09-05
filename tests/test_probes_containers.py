"""box-containers: `docker ps --format '{{.Names}} {{.Status}}'` parsing + ping shape."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from probes import containers, containers_probe  # noqa: E402
from probes.common import ProbeError, build_ping  # noqa: E402

# real output captured on the box 2026-09-04 + synthetic bad states
DOCKER_PS = """km-tracker-app-1 Up 2 weeks
km-tracker-staging-app-1 Up 2 weeks
todoist-points-todoist-points-1 Up 4 weeks
jjho-fan-almanac Up 5 weeks (healthy)
hopper-messaging-gmessages-1 Up 5 weeks
hopper-messaging-synapse-1 Up 5 weeks (healthy)
hopper-messaging-postgres-1 Up 5 weeks (healthy)
baby-pool Up 5 weeks (healthy)
taste-twin Up 5 weeks (healthy)
km-tracker-cloudflared-1 Up 5 weeks

sick Up 3 hours (unhealthy)
dead Exited (1) 2 hours ago
flap Restarting (1) 5 seconds ago
starting Up 2 seconds (health: starting)
"""


def test_parse_docker_ps():
    s = containers.parse_docker_ps(DOCKER_PS)
    assert len(s.running) == 12 and "sick" in s.running and "starting" in s.running
    assert s.unhealthy == ["sick"]
    assert sorted(s.not_running) == ["dead", "flap"]
    assert s.restarting == ["flap"]
    assert s.total == 14


def test_metrics_shape_flat_strings():
    m = containers.parse_docker_ps(DOCKER_PS).metrics()
    assert m["running_count"] == 12 and m["unhealthy_count"] == 1 and m["not_running_count"] == 2
    assert m["unhealthy"] == "sick" and m["not_running"] == "dead,flap" and m["restarting"] == "flap"
    assert m["running"].startswith("baby-pool,") and "," in m["running"]
    body = build_ping("ok", metrics=m)
    assert body["metrics"]["running"] == m["running"]  # 14 names ≈ 240 chars must survive untruncated


def test_real_box_running_list_is_not_truncated():
    # The box already runs 10 containers whose names total > 200 chars; the metric must carry them all.
    real = "\n".join(l for l in DOCKER_PS.splitlines()[:10])
    m = containers.parse_docker_ps(real).metrics()
    assert m["running_count"] == 10 and len(m["running"]) > 200
    assert build_ping("ok", metrics=m)["metrics"]["running"] == m["running"]


def test_parse_empty_and_nameless():
    s = containers.parse_docker_ps("")
    assert s.total == 0 and s.metrics()["running"] == ""
    s = containers.parse_docker_ps("lonely\n")
    assert s.not_running == ["lonely"]


def test_summary_note():
    assert containers.summary_note(containers.parse_docker_ps("a Up 1s\n")) == "1 running"
    n = containers.summary_note(containers.parse_docker_ps(DOCKER_PS))
    assert n.startswith("12 running; unhealthy: sick; not running: dead,flap")


def test_gather_raises_when_docker_fails(monkeypatch):
    monkeypatch.setattr(containers_probe, "run_cmd", lambda argv, timeout, cwd=None: (1, "", "permission denied while trying to connect to the Docker daemon socket"))
    try:
        containers_probe.gather("docker")
    except ProbeError as e:
        assert "permission denied" in str(e)
    else:
        raise AssertionError("expected ProbeError")


def test_main_dry_run_from_stdin(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO("a Up 1s\nb Exited (0)\n"))
    monkeypatch.delenv("INGEST_TOKEN", raising=False)
    rc = containers_probe.main(["--dry-run", "--stdin"])
    out = capsys.readouterr().out
    assert rc == 0 and "/api/v1/ping/box-containers" in out and '"running": "a"' in out and '"status": "ok"' in out


def test_main_dry_run_docker_missing_is_fail_ping(capsys):
    rc = containers_probe.main(["--dry-run", "--docker", "/nonexistent/docker"])
    out = capsys.readouterr().out
    assert rc == 0 and '"status": "fail"' in out and "docker ps failed" in out
