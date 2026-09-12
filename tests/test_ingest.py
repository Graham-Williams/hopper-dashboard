import json

import pytest

from dashboard import db
from dashboard.ingest import (PayloadError, parse_form_payload, parse_json_payload,
                              parse_metrics)
from tests.conftest import auth


# --------------------------------------------------------------------------- #
# Auth / routing
# --------------------------------------------------------------------------- #

def test_healthz_ingest(ingest):
    r = ingest.get("/healthz")
    assert r.status_code == 200 and r.get_json()["role"] == "ingest"


def test_ping_without_token_is_401_with_no_body(ingest):
    r = ingest.post("/api/v1/ping/snap", json={"status": "ok"})
    assert r.status_code == 401
    assert r.data == b""


def test_ping_wrong_token_401(ingest):
    r = ingest.post("/api/v1/ping/snap", json={"status": "ok"},
                    headers=auth("nope"))
    assert r.status_code == 401


def test_ping_basic_scheme_rejected(ingest):
    r = ingest.post("/api/v1/ping/snap", json={"status": "ok"},
                    headers={"Authorization": "Basic dGVzdC1pbmdlc3QtdG9rZW4="})
    assert r.status_code == 401


def test_unknown_job_404_even_with_token(ingest):
    r = ingest.post("/api/v1/ping/phantom", json={"status": "ok"}, headers=auth())
    assert r.status_code == 404
    assert r.get_json() == {"ok": False, "error": "unknown job"}


def test_unknown_job_without_token_is_401_not_404(ingest):
    """Auth first: an unauthenticated caller can't enumerate job ids."""
    r = ingest.post("/api/v1/ping/phantom", json={"status": "ok"})
    assert r.status_code == 401


def test_read_routes_not_on_ingest(ingest):
    assert ingest.get("/").status_code == 404
    assert ingest.get("/api/v1/status").status_code == 404
    assert ingest.get("/api/v1/status").get_json()["ok"] is False


def test_empty_ingest_token_fails_closed(settings, registry, notifier):
    from dashboard import create_app
    settings.ingest_token = ""
    app = create_app("ingest", settings, registry, notifier)
    r = app.test_client().post("/api/v1/ping/snap", json={"status": "ok"},
                               headers=auth(""))
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #

def test_json_ping_records_run_and_returns_state(ingest, core):
    body = {"status": "ok", "started_at": "2026-09-04T10:00:00Z",
            "finished_at": "2026-09-04T10:00:07Z", "reason": "pushed",
            "exit_code": 0, "note": "fine",
            "metrics": {"bytes": 1234, "files": 2, "db_sha256": "ab" * 32}}
    r = ingest.post("/api/v1/ping/snap", json=body, headers=auth())
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "state": "OK"}
    conn = core.connect()
    run = db.last_run(conn, "snap")
    assert run["status"] == "ok" and run["reason"] == "pushed"
    assert run["metrics"]["bytes"] == 1234 and run["source"] == "ping"
    assert db.job_row(conn, "snap")["last_metrics"]["db_sha256"] == "ab" * 32


def test_form_ping_success(ingest, core):
    r = ingest.post("/api/v1/ping/snap", data={"result": "success", "exit": "0"},
                    headers=auth())
    assert r.status_code == 200 and r.get_json()["state"] == "OK"
    run = db.last_run(core.connect(), "snap")
    assert run["status"] == "ok" and run["exit_code"] == 0 and run["source"] == "form"


def test_form_ping_failure_maps_result_to_reason(ingest, core):
    r = ingest.post("/api/v1/ping/snap", data={"result": "timeout", "exit": "KILL"},
                    headers=auth())
    assert r.status_code == 200 and r.get_json()["state"] == "FAIL"
    run = db.last_run(core.connect(), "snap")
    assert run["status"] == "fail" and run["reason"] == "timeout"
    assert run["exit_code"] is None and "exit=KILL" in run["note"]


def test_form_ping_exit_code_failure(ingest, core):
    ingest.post("/api/v1/ping/snap", data={"result": "exit-code", "exit": "1"},
                headers=auth())
    run = db.last_run(core.connect(), "snap")
    assert run["status"] == "fail" and run["exit_code"] == 1


def test_form_ping_without_result_is_400(ingest):
    r = ingest.post("/api/v1/ping/snap", data={"exit": "0"}, headers=auth())
    assert r.status_code == 400


def test_skipped_status_counts_as_heartbeat_and_success(ingest, core):
    r = ingest.post("/api/v1/ping/snap",
                    json={"status": "skipped", "reason": "skipped-unchanged"},
                    headers=auth())
    assert r.get_json()["state"] == "OK"
    conn = core.connect()
    assert db.last_success(conn, "snap")["status"] == "skipped"


def test_metric_status_updates_metrics_but_is_not_a_run(ingest, core):
    r = ingest.post("/api/v1/ping/offload",
                    json={"status": "metric", "metrics": {"lag_bytes": 10}},
                    headers=auth())
    assert r.status_code == 200
    conn = core.connect()
    assert db.last_run(conn, "offload") is None
    assert db.last_success(conn, "offload") is None
    assert db.job_row(conn, "offload")["last_metrics"] == {"lag_bytes": 10}


def test_metrics_are_shallow_merged_across_pings(ingest, core):
    ingest.post("/api/v1/ping/snap", json={"status": "ok",
                "metrics": {"db_sha256": "aa", "bytes": 1}}, headers=auth())
    ingest.post("/api/v1/ping/snap", data={"result": "success"}, headers=auth())
    ingest.post("/api/v1/ping/snap", json={"status": "metric",
                "metrics": {"bytes": 2}}, headers=auth())
    m = db.job_row(core.connect(), "snap")["last_metrics"]
    assert m == {"db_sha256": "aa", "bytes": 2}


def test_note_truncated_to_500(ingest, core):
    ingest.post("/api/v1/ping/snap", json={"status": "ok", "note": "x" * 900},
                headers=auth())
    assert len(db.last_run(core.connect(), "snap")["note"]) == 500


# --------------------------------------------------------------------------- #
# Validation / limits
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("body", [
    {"status": "maybe"}, {}, {"status": "ok", "started_at": "yesterday"},
    {"status": "ok", "exit_code": "0"}, {"status": "ok", "metrics": {"a": {"b": 1}}},
    {"status": "ok", "metrics": "nope"}, {"status": "ok", "note": 5},
    {"status": "ok", "metrics": {"bad key!": 1}},
    {"status": "ok", "metrics": {"s": "x" * 1001}},
    {"status": "ok", "metrics": {"l": [[1]]}},
    [1, 2, 3],
])
def test_bad_json_payloads_are_400(ingest, body):
    r = ingest.post("/api/v1/ping/snap", json=body, headers=auth())
    assert r.status_code == 400, r.get_json()
    assert r.get_json()["ok"] is False


def test_invalid_json_body_is_400(ingest):
    r = ingest.post("/api/v1/ping/snap", data="{not json", headers=auth(),
                    content_type="application/json")
    assert r.status_code == 400


def test_oversize_body_rejected(ingest):
    big = json.dumps({"status": "ok", "note": "x" * (70 * 1024)})
    r = ingest.post("/api/v1/ping/snap", data=big, headers=auth(),
                    content_type="application/json")
    assert r.status_code == 413


def test_rate_limit_per_ip(ingest, settings):
    for _ in range(settings.ping_rate_max):
        r = ingest.post("/api/v1/ping/snap", json={"status": "ok"}, headers=auth())
        assert r.status_code == 200
    r = ingest.post("/api/v1/ping/snap", json={"status": "ok"}, headers=auth())
    assert r.status_code == 429
    # A different client IP is unaffected.
    r = ingest.post("/api/v1/ping/snap", json={"status": "ok"}, headers=auth(),
                    environ_base={"REMOTE_ADDR": "10.9.9.9"})
    assert r.status_code == 200


def test_parse_metrics_accepts_scalars_and_flat_lists():
    m = parse_metrics({"n": 1, "f": 1.5, "s": "x", "b": True, "z": None,
                       "running": ["a", "b"]})
    assert m["running"] == ["a", "b"] and m["b"] is True


def test_parse_metrics_rejects_nan_and_too_many_keys():
    with pytest.raises(PayloadError):
        parse_metrics({"n": float("nan")})
    with pytest.raises(PayloadError):
        parse_metrics({f"k{i}": i for i in range(51)})


def test_parse_metrics_rejects_an_absurdly_large_float():
    """Ints were capped at 2**63 but floats were only checked for finiteness, so
    `disk_free_bytes: 1e308` reached the store and rendered a ~310-character number
    into the gauge heading (and /api/v1/status). Same ceiling for both types now."""
    with pytest.raises(PayloadError, match="out of range"):
        parse_metrics({"disk_free_bytes": 1e308})
    with pytest.raises(PayloadError, match="out of range"):
        parse_metrics({"disk_free_bytes": -1e308})
    # A real multi-TB byte count is nowhere near the ceiling and still goes through.
    assert parse_metrics({"disk_total_bytes": 4e12})["disk_total_bytes"] == 4e12


def test_ping_with_an_absurd_float_metric_is_400(ingest, core):
    r = ingest.post("/api/v1/ping/disk", headers=auth(),
                    json={"status": "metric", "metrics": {"disk_free_bytes": 1e308}})
    assert r.status_code == 400
    assert db.job_row(core.connect(), "disk")["last_metrics"] in (None, {})


def test_parse_json_payload_normalises_status_case():
    assert parse_json_payload({"status": "OK"})["status"] == "ok"


def test_parse_form_payload_shapes():
    ok = parse_form_payload({"result": "success", "exit": "0"})
    assert ok["status"] == "ok" and ok["reason"] is None and ok["exit_code"] == 0
    bad = parse_form_payload({"result": "signal", "exit": "TERM"})
    assert bad["status"] == "fail" and bad["reason"] == "signal"


def test_form_ping_accepts_systemd_exit_code_field(ingest, core):
    """systemd's ExecStopPost curl also sends exit_code=exited|killed|dumped."""
    r = ingest.post("/api/v1/ping/snap",
                    data={"result": "success", "exit": "0", "exit_code": "exited"},
                    headers=auth())
    assert r.status_code == 200 and r.get_json()["state"] == "OK"
    assert db.last_run(core.connect(), "snap")["note"] is None
    r = ingest.post("/api/v1/ping/snap",
                    data={"result": "timeout", "exit": "9", "exit_code": "killed"},
                    headers=auth())
    assert r.status_code == 200
    run = db.last_run(core.connect(), "snap")
    assert run["status"] == "fail" and run["exit_code"] == 9 and "exit_code=killed" in run["note"]


def test_long_running_list_string_accepted(ingest, core):
    names = ",".join(f"km-tracker-service-{i}-1" for i in range(12))  # ~300 chars
    assert 220 < len(names) < 1000
    r = ingest.post("/api/v1/ping/containers",
                    json={"status": "ok", "metrics": {"running": names}}, headers=auth())
    assert r.status_code == 200
    assert db.job_row(core.connect(), "containers")["last_metrics"]["running"] == names


def test_metric_after_ok_keeps_last_run_and_last_success(ingest, core):
    ingest.post("/api/v1/ping/tree", json={"status": "ok", "reason": "pushed"}, headers=auth())
    conn = core.connect()
    before_run = db.last_run(conn, "tree")
    before_success = db.last_success(conn, "tree")
    ingest.post("/api/v1/ping/tree", json={"status": "metric",
                "metrics": {"missing_bytes": 0, "disk_free_bytes": 5}}, headers=auth())
    assert db.last_run(conn, "tree") == before_run
    assert db.last_success(conn, "tree") == before_success
    assert len(db.recent_runs(conn, "tree")) == 1
