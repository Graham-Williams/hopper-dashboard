"""Alert thresholds: ntfy hears about a SUSTAINED problem, not a transition.

The incident these exist for (2026-09-11): `dashboard-probes` flapped FAIL→OK
27 times in four days on rclone timeouts against a rate-limited Drive API, and
each blip sent two pushes. Nothing was ever actually broken.

The shared fixtures in conftest use `alert_after_s: 0` so those suites can keep
asserting dispatch at transition time; here every job gets a realistic
threshold, so these are the tests that prove the episode clock itself.

**Every test below is a silence path.** The governing rule of the feature is
that ambiguity resolves toward paging, never toward silence — so when one of
these fails, the failure is "Graham's phone stayed quiet", not "an extra push".
"""

import copy

import pytest

from dashboard import db, probes
from dashboard.probes import ProbeResult
from dashboard.registry import parse_registry
from dashboard.state import ALERTABLE_STATES
from dashboard.services import (BAD_WINDOW_MULTIPLE, COOLDOWN_FLOOR_S, Core,
                                alert_severity, bad_window_s, cooldown_s,
                                ok_dwell_s)
from tests.conftest import JOBS_DOC, pin_created_at

NOW = 1_800_000_000.0
DAY = 86400
# Shortest gap between two ntfy attempts for the same episode (services).
RETRY_MIN = 300


def dwell(core, job_id) -> float:
    """The seconds of unbroken OK THIS job needs before its episode is over.

    Derived from the job, never hard-coded. The dwell is cadence-aware
    (``services.ok_dwell_s``), so a flat constant here would quietly stop being
    long enough the moment a job's cadence or threshold moved — and "the test
    advanced the clock, but not past the dwell" fails as *silence*, which is
    indistinguishable from the bug these tests exist to catch.
    """
    return ok_dwell_s(core.registry.get(job_id))


def core_with(settings, notifier, policy, mutate=None):
    """Core over the shared test registry with a per-job alert policy applied.
    Any job not named opts out entirely, so one job's episode is under test and
    the rest cannot add noise to `notifier.sent`."""
    doc = copy.deepcopy(JOBS_DOC)
    for raw in doc["jobs"]:
        raw.pop("alert_after_s", None)
        raw.pop("alert", None)
        raw.update(policy.get(raw["id"], {"alert": "never"}))
    if mutate is not None:
        mutate(doc)
    core = Core(settings, parse_registry(doc), notifier)
    core.init_store()
    pin_created_at(settings)
    return core


def row(core, job_id):
    conn = core.connect()
    try:
        return db.job_row(conn, job_id)
    finally:
        conn.close()


def changes(core, job_id, limit=200):
    conn = core.connect()
    try:
        return db.recent_state_changes(conn, job_id, limit)
    finally:
        conn.close()


def titles(notifier):
    return [t for t, _, _ in notifier.sent]


# --------------------------------------------------------------------------- #
# The episode
# --------------------------------------------------------------------------- #

def test_episode_starts_once_on_the_first_non_ok_recompute(settings, notifier):
    core = core_with(settings, notifier, {"snap": {"alert_after_s": DAY}})
    job = core.registry.get("snap")                      # deadline 600
    core.record_ping(job, {"status": "ok"}, now=NOW)
    assert row(core, "snap")["bad_since"] is None        # healthy: no episode
    core.recompute_all(now=NOW + 601)                    # LATE
    started = row(core, "snap")["bad_since"]
    assert started == db.to_iso(NOW + 601)
    # Every later tick of the same episode leaves the start alone.
    core.recompute_all(now=NOW + 900)
    core.recompute_all(now=NOW + 1200)
    assert row(core, "snap")["bad_since"] == started
    assert notifier.sent == []


def test_the_page_fires_with_no_state_transition_at_all(settings, notifier):
    """DECISION 1 — dispatch is LEVEL-triggered, not edge-triggered.

    `db.set_state` returns None when nothing changed, and the old `_dispatch`
    iterated only the transitions. "Still FAIL, and now past a day" is exactly
    the event this feature exists to send, and it is not a transition: the last
    thing that changed was a day ago. The proof is the state_changes table —
    ONE row, written at NOW, and a push that arrives 24 h later."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")                      # deadline 90000: stays FAIL
    core.record_ping(job, {"status": "fail"}, now=NOW)
    assert notifier.sent == []
    for i in range(1, 6):                                # ticks with nothing to report
        core.recompute_all(now=NOW + 60 * i)
    assert notifier.sent == []
    core.recompute_all(now=NOW + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]
    assert [(c["from_state"], c["to_state"], c["changed_at"]) for c in changes(core, "tree")] == \
        [("UNKNOWN", "FAIL", db.to_iso(NOW))]            # nothing changed at the tick that paged


def test_the_page_also_fires_from_the_ingest_path_not_only_the_ticker(settings, notifier):
    """`recompute_all` and `record_ping` are separate entry points and both
    dispatch. A threshold crossed by a ping that changes nothing (the same
    `fail` again) must page from there too, or the alert waits for a tick."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.record_ping(job, {"status": "fail"}, now=NOW + DAY)   # no transition: FAIL → FAIL
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]
    assert len(changes(core, "tree")) == 1


def test_mid_episode_state_change_does_not_reset_the_clock(settings, notifier):
    """LATE → FAIL is still the same outage. If the change restarted the clock a
    job could rot forever by rotating between bad states."""
    core = core_with(settings, notifier, {"snap": {"alert_after_s": 3600}})
    job = core.registry.get("snap")                      # deadline 600
    core.record_ping(job, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 601)                    # silence → LATE; episode starts
    started = row(core, "snap")["bad_since"]
    assert started == db.to_iso(NOW + 601)
    core.record_ping(job, {"status": "fail"}, now=NOW + 700)   # LATE → FAIL, same episode
    assert row(core, "snap")["state"] == "FAIL"
    assert row(core, "snap")["bad_since"] == started
    assert notifier.sent == []                           # still inside the threshold
    # Keep failing on schedule so the state stays FAIL (silence would outrank it).
    for t in range(int(NOW) + 1000, int(NOW) + 4200, 300):
        core.record_ping(job, {"status": "fail"}, now=float(t))
    assert notifier.sent == []                           # 3399 s into the episode: not yet
    # The page lands 3600 s after the episode STARTED, not 3600 s after LATE → FAIL.
    core.record_ping(job, {"status": "fail"}, now=NOW + 601 + 3600)
    assert titles(notifier) == ["[dashboard] Snap DB → FAIL"]


def test_unparseable_bad_since_is_healed_not_trusted(settings, notifier):
    """A `bad_since` that won't parse must restart the clock, not disable the
    job's alert forever — late, never silent."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    conn = core.connect()
    with conn:
        conn.execute("UPDATE jobs SET bad_since='not-a-timestamp' WHERE id='tree'")
    conn.close()
    core.recompute_all(now=NOW + 10)
    assert row(core, "tree")["bad_since"] == db.to_iso(NOW + 10)     # healed
    assert notifier.sent == []
    core.recompute_all(now=NOW + 10 + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]


def test_unknown_never_starts_an_episode_or_pages(settings, notifier):
    """UNKNOWN means "no data yet", not "broken" — real silence is the
    never-pinged → LATE rule, which IS alertable."""
    core = core_with(settings, notifier, {"snap": {"alert_after_s": 0},
                                          "containers": {"alert_after_s": 0}})
    states = core.recompute_all(now=NOW)                 # nothing has ever pinged
    assert states["snap"] == "UNKNOWN" and states["containers"] == "UNKNOWN"
    for _ in range(5):
        core.recompute_all(now=NOW + 60)
    assert row(core, "snap")["bad_since"] is None
    assert row(core, "snap")["alerted_at"] is None
    assert notifier.sent == []                           # even at a 0 threshold


# --------------------------------------------------------------------------- #
# One page per episode
# --------------------------------------------------------------------------- #

def test_one_alert_at_the_threshold_and_never_again(settings, notifier):
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")                      # deadline 90000: stays FAIL, never LATE here
    core.record_ping(job, {"status": "fail"}, now=NOW)
    assert notifier.sent == []                           # one failed run is not yet news
    core.recompute_all(now=NOW + DAY - 60)               # one tick short
    assert notifier.sent == []
    assert row(core, "tree")["alerted_at"] is None
    core.recompute_all(now=NOW + DAY)                    # exactly at the threshold
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]
    _, body, priority = notifier.sent[0]
    assert body == "tree: FAIL for over 1d" and priority == "high"
    assert row(core, "tree")["alerted_at"] == db.to_iso(NOW + DAY)
    # The 60 s ticker keeps running; the episode stays paged exactly once.
    for i in range(1, 20):
        core.recompute_all(now=NOW + DAY + 60 * i)
    assert len(notifier.sent) == 1


def test_recovery_only_for_an_episode_that_was_paged(settings, notifier):
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    # Episode 1: under the threshold → no page, and therefore no "recovered".
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.record_ping(job, {"status": "ok"}, now=NOW + 300)
    assert notifier.sent == []
    core.recompute_all(now=NOW + 300 + dwell(core, "tree"))        # OK held → episode over
    assert row(core, "tree")["bad_since"] is None and row(core, "tree")["alerted_at"] is None
    # Episode 2: past the threshold → one page, then one recovery.
    core.record_ping(job, {"status": "fail"}, now=NOW + 900)
    core.recompute_all(now=NOW + 900 + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]
    core.record_ping(job, {"status": "ok"}, now=NOW + 1000 + DAY)
    assert len(notifier.sent) == 1                       # the OK has to be held first
    core.recompute_all(now=NOW + 1000 + DAY + dwell(core, "tree"))  # episode closes → one recovery
    assert titles(notifier)[-1] == "[dashboard] Tree copy → OK"
    assert notifier.sent[-1][1] == "tree: FAIL → OK" and notifier.sent[-1][2] == "default"
    assert row(core, "tree")["bad_since"] is None and row(core, "tree")["alerted_at"] is None
    for i in range(1, 10):                               # ...and only one
        core.recompute_all(now=NOW + 1000 + DAY + dwell(core, "tree") + 60 * i)
    assert len(notifier.sent) == 2


def _flap(core, job, t, period=3600):
    """One blip of the real incident: a failed rclone listing flips the job to
    FAIL for one 5-minute cycle, then every later cycle succeeds and it is OK
    for the next hour. The probe cycle keeps recording a run every 300 s
    throughout (that is what an otherwise-healthy watcher looks like), so the
    OK dwell elapses and the episode actually ENDS. Returns the next blip."""
    core.record_ping(job, {"status": "fail", "reason": "probe-error"}, now=t)
    core.recompute_all(now=t + 60)                       # a ticker tick inside the blip
    u = t + 300                                          # ~5 min later: recovered
    while u < t + period:
        core.record_ping(job, {"status": "ok"}, now=u)
        u += 300
    return t + period


def test_flapping_below_the_threshold_sends_nothing(settings, notifier):
    """The actual bug: 27 short FAIL→OK cycles used to be ~55 pushes."""
    core = core_with(settings, notifier, {"dashboard-probes": {"alert_after_s": 21600}})
    job = core.registry.get("dashboard-probes")
    t = NOW
    for _ in range(27):
        t = _flap(core, job, t)
    assert notifier.sent == []
    # ...but the board saw every one of them.
    assert len(changes(core, "dashboard-probes")) == 54
    assert row(core, "dashboard-probes")["bad_since"] is None


def test_sustained_failure_after_flapping_still_pages(settings, notifier):
    """Muting the flaps must not mute the real thing that follows them.

    Since the accumulator (issue #15) the flaps also do not COST anything: each
    blip's 300 s of FAIL is still inside the 12 h window when the real outage
    crosses the bar, so the page arrives that much earlier than the threshold
    alone would give. Unpaged badness is carried, not discarded — and only
    unpaged badness can bring a page forward, because anything that already
    paged runs into the cooldown instead."""
    core = core_with(settings, notifier, {"dashboard-probes": {"alert_after_s": 21600}})
    job = core.registry.get("dashboard-probes")
    t = NOW
    for _ in range(5):
        t = _flap(core, job, t)
    assert notifier.sent == []
    assert row(core, "dashboard-probes")["bad_since"] is None  # each blip ended cleanly
    # Now it breaks for real: the probe cycle keeps recording a failing run every
    # 300 s (that is what a broken rclone remote looks like), for six hours.
    broke = t
    credit = 5 * 300                                     # five blips, 300 s of FAIL each
    while t < broke + 21600 - credit:
        core.record_ping(job, {"status": "fail"}, now=t)
        t += 300
    assert notifier.sent == []                           # still holding fire
    core.record_ping(job, {"status": "fail"}, now=broke + 21600 - credit)
    assert titles(notifier) == ["[dashboard] Probe cycle → FAIL"]
    # ...and the body says which of the two rules spoke, rather than claiming six
    # unbroken hours it did not see.
    assert notifier.sent[0][1] == "dashboard-probes: FAIL for over 6h in the last 12h"
    # The episode's own clock is untouched: it still starts at the break.
    assert row(core, "dashboard-probes")["bad_since"] == db.to_iso(broke)


# --------------------------------------------------------------------------- #
# alert: never, and the `informational` resolution (DECISION 3)
# --------------------------------------------------------------------------- #

def test_alert_never_pages_in_no_state_but_records_everything(settings, notifier):
    core = core_with(settings, notifier, {"macprobe": {"alert": "never"}})
    job = core.registry.get("macprobe")                  # deadline 10800
    core.record_ping(job, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 20_000)                 # LATE
    core.record_ping(job, {"status": "fail"}, now=NOW + 20_100)          # FAIL
    core.recompute_all(now=NOW + 20_100 + 10 * DAY)      # long past any threshold
    core.record_ping(job, {"status": "ok"}, now=NOW + 20_200 + 10 * DAY)  # recovered
    assert notifier.sent == []
    # the transitions are all on the board
    assert [c["to_state"] for c in changes(core, "macprobe")] == \
        ["OK", "LATE", "FAIL", "LATE", "OK"]
    assert row(core, "macprobe")["alerted_at"] is None


def test_alert_never_still_tracks_the_episode(settings, notifier):
    """`bad_since` is state, not dispatch: the card must still be able to say
    "not OK since …" for a job that never pages."""
    core = core_with(settings, notifier, {"macprobe": {"alert": "never"}})
    core.record_ping(core.registry.get("macprobe"), {"status": "fail"}, now=NOW)
    assert row(core, "macprobe")["bad_since"] == db.to_iso(NOW)
    assert row(core, "macprobe")["alerted_at"] is None


def test_an_informational_job_that_declares_nothing_really_never_pages(settings, notifier):
    """DECISION 3. `Job.informational` documented itself as "shown, never
    alerted on" and NOTHING consulted it — such a job pushed on every
    transition like any other. It is now resolved at parse time. `info` is the
    conftest job that declares no thresholds and no alert policy at all."""
    core = core_with(settings, notifier, {"info": {}})   # declares nothing whatsoever
    job = core.registry.get("info")
    assert job.informational and job.alert_never
    assert job.alert_source == "informational"
    core.record_ping(job, {"status": "fail", "reason": "drill"}, now=NOW)
    assert row(core, "info")["state"] == "FAIL"
    core.recompute_all(now=NOW + 30 * DAY)               # past any conceivable threshold
    assert notifier.sent == []
    assert row(core, "info")["bad_since"] == db.to_iso(NOW)   # ...but the episode is tracked
    assert row(core, "info")["alerted_at"] is None


def test_an_explicit_threshold_wins_over_informational(settings, notifier):
    """The escape hatch: an informational job Graham DOES want paged for.
    Explicit beats implicit, or `informational` would be a trap of its own."""
    core = core_with(settings, notifier, {"info": {"alert_after_s": 3600}})
    job = core.registry.get("info")
    assert job.informational and not job.alert_never
    assert job.alert_source == "alert_after_s"
    core.record_ping(job, {"status": "fail", "reason": "drill"}, now=NOW)
    assert notifier.sent == []
    core.recompute_all(now=NOW + 3600)
    assert titles(notifier) == ["[dashboard] Info-only → FAIL"]


def test_a_job_that_declares_nothing_and_is_not_informational_gets_the_default(settings, notifier):
    """The conservative default: 24 h, never "off". A job nobody thought about
    must page LATE, not never."""
    from dashboard.registry import DEFAULT_ALERT_AFTER_S
    core = core_with(settings, notifier, {"tree": {}})   # no alert keys at all
    job = core.registry.get("tree")
    assert not job.alert_never and job.alert_after_s == DEFAULT_ALERT_AFTER_S == DAY
    assert job.alert_source == "default"
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.recompute_all(now=NOW + DAY - 60)
    assert notifier.sent == []
    core.recompute_all(now=NOW + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]


# --------------------------------------------------------------------------- #
# Interaction with the machine-offline rule (DECISION 5)
# --------------------------------------------------------------------------- #

def test_offline_suppression_does_not_consume_the_episodes_one_alert(settings, notifier):
    """A suppressed alert must not stamp `alerted_at`: if it did, the job would
    be permanently un-pageable for the rest of the episode AND its recovery
    would fire for something Graham was never told about."""
    core = core_with(settings, notifier, {"mirror": {"alert_after_s": 3600},
                                          "macprobe": {"alert_after_s": 3 * DAY}})
    mac, mirror = core.registry.get("macprobe"), core.registry.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 20_000)                 # Mac asleep: both LATE
    assert row(core, "mirror")["state"] == "LATE" and row(core, "macprobe")["state"] == "LATE"
    core.recompute_all(now=NOW + 20_000 + 3600)          # mirror crosses its threshold
    assert notifier.sent == []                           # muted: one fact, the Mac is asleep
    assert row(core, "mirror")["alerted_at"] is None     # ...and the page was NOT burned
    # The Mac wakes (real order: the siblings ping, then the probe's own heartbeat).
    core.record_ping(mirror, {"status": "ok", "metrics": {"pending": 0, "mismatch": 0}},
                     now=NOW + 30_000)
    core.record_ping(mac, {"status": "ok"}, now=NOW + 30_002)
    assert notifier.sent == []                           # nothing was paged, so nothing "recovers"
    core.recompute_all(now=NOW + 30_000 + dwell(core, "mirror"))     # OK held: the episode ends
    assert row(core, "mirror")["bad_since"] is None
    # A real problem now opens a clean episode — and the page the outage never
    # spent is still there to be spent.
    core.record_ping(mirror, {"status": "ok", "metrics": {"pending": 4}}, now=NOW + 30_400)
    assert row(core, "mirror")["state"] == "BEHIND"
    assert notifier.sent == []
    core.recompute_all(now=NOW + 30_400 + 3600)
    assert titles(notifier) == ["[dashboard] Drive mirror → BEHIND"]


def test_a_probe_job_that_cannot_page_does_not_suppress_its_siblings(settings, notifier):
    """DECISION 5's guard: the machine-offline rule mutes every sibling on the
    premise that the machine's probe job sends "one alert for the machine
    instead" — so a probe job that can never page deletes that one alert and
    the whole machine goes dark. Reproduction: a Mac gone for days, `mirror`
    LATE the entire time, and NOT ONE push. Suppression may only borrow an
    alert that actually exists."""
    core = core_with(settings, notifier, {"mirror": {"alert_after_s": 3600},
                                          "macprobe": {"alert": "never"}})
    mac, mirror = core.registry.get("macprobe"), core.registry.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW)
    t = NOW + 20_000
    while t < NOW + 5 * DAY:                             # days of silence, ticking away
        core.recompute_all(now=t)
        t += 600
    assert row(core, "macprobe")["state"] == "LATE" and row(core, "mirror")["state"] == "LATE"
    # The probe stays silent by its own policy, but the sibling is no longer
    # muted behind it — exactly once, at its own threshold.
    assert titles(notifier) == ["[dashboard] Drive mirror → LATE"]
    assert row(core, "macprobe")["alerted_at"] is None


# --------------------------------------------------------------------------- #
# A failed push must not burn the episode's only page
# --------------------------------------------------------------------------- #

def test_a_failed_push_gives_the_page_back_and_is_retried_on_a_backoff(settings, notifier):
    """One page per episode means one failed POST used to buy PERMANENT silence:
    `alerted_at` was stamped inside the transaction, before the POST, and
    `_dispatch` threw the result away. Reproduction: ntfy down for the single
    attempt, then healthy for 30 h of continuous LATE → zero deliveries, zero
    retries, and then a bare recovery for an alert that never arrived.

    The retry is spaced by `ALERT_RETRY_MIN_S`: "the next tick" is the 60 s
    ticker *plus* every ping that lands, and each attempt is a blocking 5 s
    urllib call in the scheduler thread and in the ingest request. Nothing is
    lost by waiting — the episode keeps its clock and its unspent page."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    notifier.fail = True                                 # ntfy is down for this one tick
    core.recompute_all(now=NOW + DAY)
    assert notifier.attempts and notifier.sent == []     # tried, did not land
    assert row(core, "tree")["alerted_at"] is None       # ...so the page was handed back
    assert row(core, "tree")["bad_since"] == db.to_iso(NOW)   # same episode, same clock
    notifier.fail = False                                # ntfy comes back
    for i in range(1, RETRY_MIN // 60):                  # the ticker keeps ticking...
        core.recompute_all(now=NOW + DAY + 60 * i)
    assert len(notifier.attempts) == 1                   # ...and does NOT hammer ntfy
    assert row(core, "tree")["alerted_at"] is None       # the page is still unspent
    core.recompute_all(now=NOW + DAY + RETRY_MIN)        # backoff served: retry
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]
    assert row(core, "tree")["alerted_at"] == db.to_iso(NOW + DAY + RETRY_MIN)
    for i in range(1, 20):                               # and still only once
        core.recompute_all(now=NOW + DAY + RETRY_MIN + 60 * i)
    assert len(notifier.sent) == 1
    # The recovery is for an alert that really did arrive.
    core.record_ping(job, {"status": "ok"}, now=NOW + DAY + 3600)
    core.recompute_all(now=NOW + DAY + 3600 + dwell(core, "tree"))
    assert titles(notifier)[-1] == "[dashboard] Tree copy → OK"


def test_the_retry_backoff_never_delays_a_new_episodes_first_page(settings, notifier):
    """The backoff is keyed on the EPISODE, not the job. A failed push must not
    hold up the first page of whatever breaks next — that would be the
    threshold quietly getting longer after every ntfy hiccup.

    `offload` on purpose: a cadence-less job, so its dwell is 0 and the second
    episode can open INSIDE the 5-minute retry window, which is the only place
    this property is observable. The per-job cooldown is not in the way either,
    because a POST that never landed leaves no cooldown behind — asserted below,
    because that is the half of the rollback that is easiest to forget."""
    core = core_with(settings, notifier, {"offload": {"alert_after_s": 0}})
    job = core.registry.get("offload")
    notifier.fail = True
    core.record_ping(job, {"status": "fail"}, now=NOW)   # episode 1: POST fails
    notifier.fail = False
    assert notifier.attempts and notifier.sent == []
    assert row(core, "offload")["alerted_at"] is None
    assert row(core, "offload")["last_paged_at"] is None
    core.record_ping(job, {"status": "ok"}, now=NOW + 30)     # ...and it recovers
    assert row(core, "offload")["bad_since"] is None     # dwell is 0 with no cadence
    # A brand-new failure well inside the 5-minute retry window pages at once.
    core.record_ping(job, {"status": "fail"}, now=NOW + 60)
    assert titles(notifier) == ["[dashboard] Offload → FAIL"]


def test_a_refused_push_is_retried_too(settings, notifier):
    """A 429 or a 503 answers without raising — `send` returns False just the
    same, and the rate-limited case is the one most likely to hit us."""
    notifier.refuse = True
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.recompute_all(now=NOW + DAY)
    assert len(notifier.attempts) == 1 and notifier.sent == []
    assert row(core, "tree")["alerted_at"] is None
    notifier.refuse = False
    core.recompute_all(now=NOW + DAY + RETRY_MIN)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]


def test_a_recovered_job_does_not_get_its_page_back(settings, notifier):
    """The rollback is per EPISODE. If the job recovered between the stamp and
    the failed POST, the page belongs to whatever comes next, not to a stretch
    that is already over."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    real_dispatch = core._dispatch
    reentered = []

    def dispatch_after_recovery(intents):
        if reentered:                                    # the inner recompute
            return real_dispatch(intents)
        reentered.append(True)
        # The job comes back between COMMIT and the POST (a heartbeat landing
        # on the ingest worker while the ticker thread is mid-dispatch).
        core.record_ping(job, {"status": "ok"}, now=NOW + DAY + 1)
        core.recompute_all(now=NOW + DAY + 1 + dwell(core, "tree"))   # OK held: episode closed
        notifier.fail = True
        try:
            real_dispatch(intents)
        finally:
            notifier.fail = False

    core._dispatch = dispatch_after_recovery
    core.recompute_all(now=NOW + DAY)
    core._dispatch = real_dispatch
    assert row(core, "tree")["state"] == "OK"
    assert row(core, "tree")["alerted_at"] is None       # closed by the recovery, not re-armed
    assert row(core, "tree")["bad_since"] is None


def test_an_ok_ping_during_the_failed_post_still_hands_the_page_back(settings, notifier):
    """The rollback must key on EPISODE IDENTITY, never on "is the job
    currently not-OK". An earlier version skipped any job whose state was no
    longer alertable — "the episode must be over" — which the OK dwell and the
    unverified-OK hold both made false: a job can read OK with its episode wide
    open.

    Reproduction (box-containers' own shape, `restart: unless-stopped`
    backoff): the 5-minutely `docker ps` cron lands while the ntfy POST is
    timing out and the container happens to be up right then. The rollback was
    skipped, the container broke again inside the dwell, and the episode ran on
    for two days — stamped, undelivered, unable to page again."""
    core = core_with(settings, notifier, {"containers": {"alert_after_s": 1200}})
    job = core.registry.get("containers")                # dwell 120 s
    down = {"status": "ok", "metrics": {"running": "app-1"}}
    up = {"status": "ok", "metrics": {"running": "app-1,tunnel-1"}}
    core.record_ping(job, down, now=NOW)                 # tunnel-1 gone: episode starts
    real_dispatch = core._dispatch
    reentered = []

    def dispatch_with_a_concurrent_up_ping(intents):
        if reentered:
            return real_dispatch(intents)
        reentered.append(True)
        core.record_ping(job, up, now=NOW + 1202)        # two seconds, not five minutes
        notifier.fail = True
        try:
            real_dispatch(intents)
        finally:
            notifier.fail = False

    core._dispatch = dispatch_with_a_concurrent_up_ping
    core.recompute_all(now=NOW + 1200)                   # threshold crossed: stamp, POST, fail
    core._dispatch = real_dispatch
    assert notifier.attempts and notifier.sent == []     # tried, did not land
    assert row(core, "containers")["state"] == "OK"      # ...while the job reads OK
    assert row(core, "containers")["bad_since"] == db.to_iso(NOW)   # episode still open
    assert row(core, "containers")["alerted_at"] is None            # so the page came back
    # It falls over again inside the dwell and stays down for an hour.
    t = NOW + 1264
    while t < NOW + 1264 + 3600:
        core.record_ping(job, down, now=t)
        t += 300
    assert titles(notifier) == ["[dashboard] Containers → FAIL"]
    assert row(core, "containers")["bad_since"] == db.to_iso(NOW)   # one unbroken episode


def test_a_disabled_notifier_never_rewrites_the_episode_row(settings):
    """`notify_alert` also returns False when ntfy is simply switched off.
    Rolling back on that would rewrite `alerted_at` every 60 s forever, and
    re-page the moment it was switched on."""
    from dashboard.notify import Notifier
    core = core_with(settings, Notifier("", ""), {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.recompute_all(now=NOW + DAY)
    stamped = row(core, "tree")["alerted_at"]
    assert stamped == db.to_iso(NOW + DAY)               # the episode is spent, quietly
    for i in range(1, 10):
        core.recompute_all(now=NOW + DAY + 60 * i)
    assert row(core, "tree")["alerted_at"] == stamped    # and the row is left alone


# --------------------------------------------------------------------------- #
# An OK we could not verify must not end the episode
# --------------------------------------------------------------------------- #

def _probe(core, job_id, at, ok=True, **kw):
    conn = core.connect()
    try:
        with conn:
            db.insert_probe(conn, job_id, probed_at=db.to_iso(at), ok=ok, **kw)
    finally:
        conn.close()


def test_a_failing_destination_probe_does_not_reset_the_episode_clock(settings, notifier):
    """A db_snapshot whose destination is genuinely 10 days stale, probed by an
    rclone that times out once a day against a rate-limited Drive.

    A failed probe leaves `db_snapshot_stale` with nothing to compare, so
    `compute_state` falls through to OK — and that OK used to clear
    `bad_since`. Reproduction: 21 STALE_DEST↔OK transitions on the board and
    ZERO pushes, because a 24 h clock was reset every 24 h. Transient rclone
    failures against a rate-limited Drive are the exact phenomenon this whole
    feature was built for."""
    core = core_with(settings, notifier, {"snap": {"alert_after_s": DAY}})
    job = core.registry.get("snap")
    stale = db.to_iso(NOW - 10 * DAY)                    # nothing new on Drive for 10 days
    t = NOW
    while t <= NOW + 10 * DAY:
        timed_out = t > NOW and (t - NOW) % DAY == 0      # one rclone timeout per day
        _probe(core, "snap", t, ok=not timed_out, newest_iso=stale, state_sha="bbb",
               error="rclone timed out after 240s" if timed_out else None)
        core.record_ping(job, {"status": "ok", "metrics": {"db_sha256": "aaa"}}, now=t)
        t += 1800
    # the board saw the flapping either way
    to_states = [c["to_state"] for c in changes(core, "snap")]
    assert len(to_states) == 20 and to_states.count("OK") == 10   # ten flips on the board...
    assert titles(notifier) == ["[dashboard] Snap DB → STALE_DEST"]   # ...and ONE push, not zero
    assert row(core, "snap")["bad_since"] == db.to_iso(NOW)   # one unbroken 10-day episode


def test_an_episode_held_open_by_a_blind_probe_still_pages_at_its_threshold(settings, notifier):
    """DECISION 2's HARD CEILING. The unverified-OK hold keeps the episode
    alive, but the page sits behind a timer — so an episode that spends its
    whole threshold inside a blind window would never get to speak, and the
    hold would then expire and close it SILENTLY. Net effect without the
    ceiling: a genuinely stale backup, zero pushes, for ever.

    The episode clock is authoritative: past `alert_after_s`, an open episode
    pages whatever the job currently reads, and says what it is really about
    (STALE_DEST, not the OK on the card)."""
    core = core_with(settings, notifier, {"snap": {"alert_after_s": 3600}})
    job = core.registry.get("snap")
    ping = {"status": "ok", "metrics": {"db_sha256": "aaa"}}
    stale = db.to_iso(NOW - 30 * DAY)
    _probe(core, "snap", NOW, ok=True, newest_iso=stale, state_sha="bbb")
    core.record_ping(job, ping, now=NOW)                 # STALE_DEST: the episode opens
    assert row(core, "snap")["state"] == "STALE_DEST"
    assert notifier.sent == []                           # under the threshold
    # From here the probe is blind: state falls through to OK on the heartbeat
    # alone, and stays there for the whole of the remaining threshold.
    t = NOW + 300
    while t < NOW + 3600:
        _probe(core, "snap", t, ok=False, error="rclone timed out after 240s")
        core.record_ping(job, ping, now=t)
        assert row(core, "snap")["state"] == "OK"        # nothing bad to report...
        assert row(core, "snap")["bad_since"] == db.to_iso(NOW)   # ...but the episode is open
        t += 300
    assert notifier.sent == []
    _probe(core, "snap", NOW + 3600, ok=False, error="rclone timed out after 240s")
    core.record_ping(job, ping, now=NOW + 3600)          # the episode turns one hour old
    assert titles(notifier) == ["[dashboard] Snap DB → STALE_DEST"]
    assert notifier.sent[0][1] == "snap: STALE_DEST for over 1h"
    assert row(core, "snap")["alerted_at"] == db.to_iso(NOW + 3600)
    # Still exactly one page, and the hold is still holding.
    for i in range(1, 6):
        core.recompute_all(now=NOW + 3600 + 60 * i)
    assert len(notifier.sent) == 1


def test_a_blind_probe_that_arrives_seconds_in_still_pages_at_the_threshold(settings, notifier):
    """THE HARD CEILING, at the one instant the old scoping could not reach.

    The ceiling used to live INSIDE `held < ok_hold_s(job)`, and for every
    threshold over five minutes `ok_hold_s == alert_after_s` — so it was live
    only while `began + A <= now < since + A`, a window exactly as long as the
    job was OBSERVED not-OK before the probe went blind. Here that window is ONE
    SECOND: the destination is genuinely a month stale, the probe times out a
    second after the episode opens and never sees the destination again, and the
    heartbeat keeps arriving on cadence, so the card reads OK for ever. No
    recompute could land in the window, the hold then expired, and the episode
    closed SILENTLY: a month-stale backup, zero pushes, `bad_since` NULL,
    `alerted_at` NULL, card green.

    The rule has no window: an open episode past its threshold pages on ANY
    pass, including the pass that gives the hold up."""
    core = core_with(settings, notifier, {"snap": {"alert_after_s": 3600}})
    job = core.registry.get("snap")
    ping = {"status": "ok", "metrics": {"db_sha256": "aaa"}}
    stale = db.to_iso(NOW - 30 * DAY)                    # nothing on Drive for a month
    _probe(core, "snap", NOW, ok=True, newest_iso=stale, state_sha="bbb")
    core.record_ping(job, ping, now=NOW)
    assert row(core, "snap")["state"] == "STALE_DEST"    # the episode opens...
    blind = NOW + 1                                      # ...and is observed for 1 s
    _probe(core, "snap", blind, ok=False, error="rclone timed out after 240s")
    core.record_ping(job, ping, now=blind)
    assert row(core, "snap")["state"] == "OK"            # blind, not fine
    t = blind + 60
    while t <= NOW + 3 * 3600:                           # three hours of ticks + heartbeats
        _probe(core, "snap", t, ok=False, error="rclone timed out after 240s")
        if (t - blind) % 300 == 0:
            core.record_ping(job, ping, now=t)           # still alive, so never LATE
        else:
            core.recompute_all(now=t)
        t += 60
    assert titles(notifier) == ["[dashboard] Snap DB → STALE_DEST"]
    assert notifier.sent[0][1] == "snap: STALE_DEST for over 1h"
    assert row(core, "snap")["bad_since"] is None        # the hold expired afterwards
    assert row(core, "snap")["state"] == "OK"            # ...and the card still reads OK


def test_an_unverifiable_ok_holds_the_recovery_too(settings, notifier):
    """"STALE_DEST → OK" when the OK only means "the probe failed" is false
    comfort, so the recovery waits for a probe that can actually see the
    destination — and arrives naming the state the episode really was about."""
    core = core_with(settings, notifier, {"snap": {"alert_after_s": 0}})
    job = core.registry.get("snap")
    stale = db.to_iso(NOW - 10 * DAY)
    _probe(core, "snap", NOW, ok=True, newest_iso=stale, state_sha="bbb")
    core.record_ping(job, {"status": "ok", "metrics": {"db_sha256": "aaa"}}, now=NOW)
    assert titles(notifier) == ["[dashboard] Snap DB → STALE_DEST"]
    # The probe starts failing. State goes OK, but nothing has been verified.
    _probe(core, "snap", NOW + 300, ok=False, error="rclone timed out after 240s")
    core.record_ping(job, {"status": "ok", "metrics": {"db_sha256": "aaa"}}, now=NOW + 300)
    assert row(core, "snap")["state"] == "OK"
    assert len(notifier.sent) == 1                       # no "recovered" yet
    assert row(core, "snap")["alerted_at"] is not None   # the episode is still open
    # A probe that CAN see the destination, and the backup really did land.
    _probe(core, "snap", NOW + 600, ok=True, newest_iso=db.to_iso(NOW + 590),
           state_sha="aaa")
    core.record_ping(job, {"status": "ok", "metrics": {"db_sha256": "aaa"}}, now=NOW + 600)
    # Evidence restored — but the OK still has to be HELD. The job turned OK at
    # NOW+300, so the episode closes one dwell after that, not on the first
    # believable probe.
    assert len(notifier.sent) == 1
    core.recompute_all(now=NOW + 300 + dwell(core, "snap"))
    assert titles(notifier)[-1] == "[dashboard] Snap DB → OK"
    assert notifier.sent[-1][1] == "snap: STALE_DEST → OK"   # not "OK → OK"
    assert row(core, "snap")["alerted_at"] is None


def test_a_destination_probe_that_never_comes_back_gives_the_page_up(settings, notifier):
    """The hold must be BOUNDED, or a destination we can never probe again pins
    `alerted_at` for good — and with it every later failure of that job.

    Reproduction (`km-backup`'s shape): the destination really is stale, so it
    pages once; then the `gdrive:` probe breaks permanently (remote revoked, or
    rclone's shared Drive OAuth client retired — that one would break all four
    probes at once). The heartbeat still arrives, so the state reads OK, the
    episode is held, and when the backup itself dies and sits LATE for ten days
    there is no page left to send. The pre-threshold code paged on `OK → LATE`.

    The hold is bounded by the job's own threshold: at most one page per
    threshold is spent on a destination we cannot see, and the episode then
    closes SILENTLY — no recovery, because nothing was verified fixed."""
    core = core_with(settings, notifier, {"snap": {"alert_after_s": 3600}})
    job = core.registry.get("snap")
    ping = {"status": "ok", "metrics": {"db_sha256": "aaa"}}
    stale = db.to_iso(NOW - 30 * DAY)                    # nothing new on Drive for a month
    t = NOW
    while t <= NOW + 3600:                               # a stale destination, probed fine
        _probe(core, "snap", t, ok=True, newest_iso=stale, state_sha="bbb")
        core.record_ping(job, ping, now=t)
        t += 300
    assert titles(notifier) == ["[dashboard] Snap DB → STALE_DEST"]
    assert row(core, "snap")["alerted_at"] is not None
    # The probe now breaks for good. State falls through to OK on the heartbeat
    # alone, and the episode is held open — until it has been held a threshold.
    broke = t
    while t <= broke + 3600:
        _probe(core, "snap", t, ok=False, error="rclone: didn't find section in config file")
        core.record_ping(job, ping, now=t)
        if t < broke + 3600:
            assert row(core, "snap")["bad_since"] is not None, t   # still held
        t += 300
    assert row(core, "snap")["state"] == "OK"            # OK only for want of evidence
    assert row(core, "snap")["bad_since"] is None        # the hold expired...
    assert row(core, "snap")["alerted_at"] is None       # ...and gave the page up
    assert len(notifier.sent) == 1                       # silently: no "→ OK" for a lie
    # Now the backup itself dies: no more heartbeats at all.
    while t <= broke + 3600 + 2 * DAY:
        _probe(core, "snap", t, ok=False, error="rclone: didn't find section in config file")
        core.recompute_all(now=t)
        t += 1800
    assert row(core, "snap")["state"] == "LATE"
    assert titles(notifier) == ["[dashboard] Snap DB → STALE_DEST",
                                "[dashboard] Snap DB → LATE"]


def test_a_late_episode_still_recovers_when_the_destination_is_unprobed(settings, notifier):
    """The hold is only for episodes the destination could speak to. A LATE
    episode is about SILENCE, and the heartbeat coming back settles that on its
    own — a job with no usable probe must still recover normally."""
    core = core_with(settings, notifier, {"snap": {"alert_after_s": 0}})
    job = core.registry.get("snap")                      # db_snapshot, never probed
    core.record_ping(job, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 601)
    assert titles(notifier) == ["[dashboard] Snap DB → LATE"]
    core.record_ping(job, {"status": "ok"}, now=NOW + 700)
    core.recompute_all(now=NOW + 700 + dwell(core, "snap"))   # OK held -> episode over
    assert titles(notifier)[-1] == "[dashboard] Snap DB → OK"


# --------------------------------------------------------------------------- #
# The damped-OK interaction with PR #8 (DECISION 2)
# --------------------------------------------------------------------------- #

def _cycle(core, monkeypatch, t, ok):
    """One probe cycle in which every probed destination answers ``ok``."""
    monkeypatch.setattr(
        probes, "probe_job",
        lambda job, **kw: ProbeResult(
            ok=ok, count=1 if ok else None,
            error=None if ok else "rclone exit 7: rateLimitExceeded",
            transient=not ok))
    core.run_probe_cycle(now=t)


def test_a_damped_probe_failure_is_not_a_verified_recovery(settings, notifier, monkeypatch):
    """DECISION 2. PR #8 damps a transient probe failure by recording the
    self-job's run as `ok`, which computes to state OK — and a naive episode
    model reads that as a recovery, closes the episode and resets the clock.

    Under the alternating pattern below (fail, fail, ok, fail, fail, ok, …
    against `PROBE_FAIL_THRESHOLD` = 2) the clock never gets older than ~15
    minutes, so a 1 h threshold is NEVER reached: the probes are broken most of
    the time and the phone never hears about it. `PROBE_NO_SUCCESS_S`
    guarantees the STATE reaches FAIL; it guarantees nothing about a page once
    pages sit behind a timer.

    The fix: `dashboard-probes` writes its own heartbeat, so its `ok` is not
    evidence of anything while a probed destination is in a failing state. The
    episode is held, the clock keeps running, and the hard ceiling pages."""
    core = core_with(settings, notifier, {"dashboard-probes": {"alert_after_s": 3600}})
    t = NOW
    pattern = []                                         # fail, fail, ok, repeating
    while t <= NOW + 4200:
        ok = (len(pattern) % 3) == 2
        pattern.append(ok)
        _cycle(core, monkeypatch, t, ok)
        t += 300
    # The state really did oscillate (that is PR #8 working as designed)...
    to_states = [c["to_state"] for c in changes(core, "dashboard-probes")]
    assert to_states.count("OK") >= 4 and to_states.count("FAIL") >= 4
    # ...but the EPISODE survived every damped `ok`, so the clock got old enough.
    assert titles(notifier) == ["[dashboard] Probe cycle → FAIL"]
    assert notifier.sent[0][1] == "dashboard-probes: FAIL for over 1h"


def test_a_genuinely_clean_probe_cycle_does_close_the_episode(settings, notifier, monkeypatch):
    """The other half: the hold must not make `dashboard-probes` un-recoverable.
    A cycle where every destination actually listed IS verified health, so once
    it has held for the dwell the episode closes and the recovery goes out."""
    core = core_with(settings, notifier, {"dashboard-probes": {"alert_after_s": 0}})
    _cycle(core, monkeypatch, NOW, ok=False)
    _cycle(core, monkeypatch, NOW + 300, ok=False)       # streak 2 → FAIL → page
    assert titles(notifier) == ["[dashboard] Probe cycle → FAIL"]
    _cycle(core, monkeypatch, NOW + 600, ok=True)        # one good listing clears the FAIL
    assert row(core, "dashboard-probes")["state"] == "OK"
    # ...but one is not yet a recovery: the cadence-aware dwell wants two probe
    # cycles of unbroken OK before it believes the episode is over.
    assert titles(notifier) == ["[dashboard] Probe cycle → FAIL"]
    _cycle(core, monkeypatch, NOW + 600 + dwell(core, "dashboard-probes"), ok=True)
    assert titles(notifier)[-1] == "[dashboard] Probe cycle → OK"
    assert row(core, "dashboard-probes")["bad_since"] is None


def test_a_future_dated_probe_row_cannot_mask_the_failures_after_it(settings, notifier,
                                                                    monkeypatch):
    """`probes.probed_at` is the writer's wall clock. A box whose RTC boots ahead
    — or an NTP step backwards afterwards, the same step this branch already
    heals for `bad_since` — leaves one row dated in the FUTURE, and ordered by
    `probed_at` that row outranks every real probe after it permanently: the
    "newest" row reads ok, so `failing_probe_job_ids` is empty, the streak is 0,
    the damped-OK hold is switched off and PR #8's damping can never un-damp.
    One poisoned row, and the job that watches the watchers goes quiet for good.

    Insert order (`id`) is the only monotonic sequence we control — and clamping
    `probed_at` at insert cannot help, because at insert time the value IS now;
    it only becomes the future when the clock later steps back."""
    core = core_with(settings, notifier, {"dashboard-probes": {"alert_after_s": 3600}})
    _probe(core, "snap", NOW + 7200, ok=True, newest_iso=db.to_iso(NOW), count=1)
    _probe(core, "snap", NOW, ok=False, error="rclone timed out after 240s")
    conn = core.connect()
    try:
        assert db.last_probe(conn, "snap")["ok"] == 0            # the LATER row wins
        assert db.probe_fail_streak(conn, "snap") == 1
        assert db.failing_probe_job_ids(conn, ["snap"]) == {"snap"}
    finally:
        conn.close()
    # ...and the consequence, end to end: exactly the damped-failure scenario
    # below, with that one row sitting in the table. The hold must still hold.
    t = NOW
    seen = 0
    while t <= NOW + 4200:                                       # fail, fail, ok, repeating
        _cycle(core, monkeypatch, t, ok=(seen % 3) == 2)
        seen += 1
        t += 300
    assert titles(notifier) == ["[dashboard] Probe cycle → FAIL"]
    assert notifier.sent[0][1] == "dashboard-probes: FAIL for over 1h"


def test_the_damped_hold_is_bounded_like_every_other_hold(settings, notifier, monkeypatch):
    """A destination that stays broken for ever must not pin `alerted_at` for
    ever. After one threshold of being unable to verify anything, the episode
    closes silently and the job can page again on a fresh clock."""
    core = core_with(settings, notifier, {"dashboard-probes": {"alert_after_s": 600}})
    _cycle(core, monkeypatch, NOW, ok=False)
    _cycle(core, monkeypatch, NOW + 300, ok=False)       # streak 2 → FAIL, episode opens
    core.recompute_all(now=NOW + 900)                    # threshold crossed → one page
    assert titles(notifier) == ["[dashboard] Probe cycle → FAIL"]
    # A single damped failure keeps the state OK while a destination is failing.
    _cycle(core, monkeypatch, NOW + 1200, ok=True)       # streak cleared → OK since here
    _cycle(core, monkeypatch, NOW + 1500, ok=False)      # ...then one damped failure
    assert row(core, "dashboard-probes")["state"] == "OK"
    assert row(core, "dashboard-probes")["bad_since"] is not None   # held, not closed
    # Ticks only from here (the scheduler's 60 s state ticker): the newest probe
    # row stays a failure, so every one of these is another held-open recompute.
    for t in range(int(NOW) + 1560, int(NOW) + 1800, 60):
        core.recompute_all(now=float(t))
        assert row(core, "dashboard-probes")["bad_since"] is not None, t
    core.recompute_all(now=NOW + 1800)                   # one ok_hold_s of OK: give it up
    assert row(core, "dashboard-probes")["state"] == "OK"
    assert row(core, "dashboard-probes")["bad_since"] is None
    assert row(core, "dashboard-probes")["alerted_at"] is None
    assert len(notifier.sent) == 1                       # closed silently, no "→ OK"


# --------------------------------------------------------------------------- #
# Oscillation just under the threshold
# --------------------------------------------------------------------------- #

def test_oscillating_just_under_the_threshold_still_pages(settings, notifier):
    """A `restart: unless-stopped` container flapping on backoff: 19 minutes
    down, 1 minute up, for as long as you like. One OK tick used to clear the
    whole episode, so a job that was broken 95% of the day never paged."""
    core = core_with(settings, notifier, {"containers": {"alert_after_s": 1200}})
    job = core.registry.get("containers")                # expects app-1 + tunnel-1
    down = {"status": "ok", "metrics": {"running": "app-1"}}
    up = {"status": "ok", "metrics": {"running": "app-1,tunnel-1"}}
    t = NOW
    for _ in range(50):                                  # 16.7 h of this
        for _ in range(19):
            core.record_ping(job, down, now=t)
            t += 60
        core.record_ping(job, up, now=t)                 # a minute of hope
        t += 60
    assert titles(notifier) == ["[dashboard] Containers → FAIL"]
    assert notifier.sent[0][1] == "containers: FAIL for over 20m"
    assert len(notifier.sent) == 1                       # one page per episode, still
    # The episode never closed, so the board can still say how long it has been
    # going — and nothing ever announced a recovery that was about to be undone.
    assert row(core, "containers")["bad_since"] == db.to_iso(NOW)


def test_an_outage_past_the_threshold_pages_even_though_it_then_recovers(settings, notifier):
    """The dwell branch is a way for an episode to END, so the ceiling applies
    there too. Without it the dwell swallowed the page outright: `ok_dwell_s` is
    `min(5 min, A/10)`, so ANY recovery longer than the dwell closed the episode
    — and a container 19 min down / 3 min up against a 20 min threshold was
    broken 86% of the time, for ever, and paged NOTHING (measured: 0 pushes over
    20 cycles).

    The job crossed the bar Graham set, so the outage is news even though it is
    over by the time we say so. The page and its recovery therefore arrive close
    together: accepted deliberately, and rare — a job has to outlive its whole
    threshold and then recover inside one dwell to produce it."""
    core = core_with(settings, notifier, {"containers": {"alert_after_s": 1200}})
    job = core.registry.get("containers")                # expects app-1 + tunnel-1
    down = {"status": "ok", "metrics": {"running": "app-1"}}
    up = {"status": "ok", "metrics": {"running": "app-1,tunnel-1"}}
    t = NOW
    while t < NOW + 1140:                                # 19 min down: a minute short
        core.record_ping(job, down, now=t)
        t += 60
    assert notifier.sent == []                           # under the threshold, still quiet
    while t < NOW + 1140 + 180:                          # 3 min up
        core.record_ping(job, up, now=t)
        t += 60
    # The ceiling has spoken: the episode outlived the 20 min bar Graham set, so
    # it is news whatever the job reads now.
    assert titles(notifier) == ["[dashboard] Containers → FAIL"]
    assert notifier.sent[0][1] == "containers: FAIL for over 20m"
    # But 3 minutes is no longer a RECOVERY. `box-containers` is sampled every
    # 300 s by the host cron, so its dwell is two samples (600 s) and 180 s of OK
    # is ONE sample — indistinguishable from the up-phase of a crash loop. The
    # episode stays open, which is what stops the next 19 minutes down from
    # opening a fresh one and paging all over again.
    assert row(core, "containers")["bad_since"] is not None
    while t < NOW + 1140 + dwell(core, "containers") + 60:
        core.record_ping(job, up, now=t)
        t += 60
    assert titles(notifier) == ["[dashboard] Containers → FAIL",
                                "[dashboard] Containers → OK"]
    assert row(core, "containers")["bad_since"] is None   # and the episode really closed


def test_a_short_blip_is_still_muted_and_a_real_recovery_still_ends_the_episode(settings, notifier):
    """The dwell must not resurrect an episode that genuinely ended: a 5-minute
    blip against a 6 h threshold stays silent, the episode closes once the job
    has held OK, and a later failure starts a brand-new clock."""
    core = core_with(settings, notifier, {"dashboard-probes": {"alert_after_s": 21600}})
    job = core.registry.get("dashboard-probes")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.record_ping(job, {"status": "ok"}, now=NOW + 300)
    assert row(core, "dashboard-probes")["bad_since"] == db.to_iso(NOW)   # dwell not served
    core.recompute_all(now=NOW + 300 + dwell(core, "dashboard-probes"))
    assert row(core, "dashboard-probes")["bad_since"] is None
    assert notifier.sent == []
    # Hours later it breaks for good. The episode CLOCK starts at the break, not
    # back at the blip — but the blip's 300 s of FAIL is still inside the 12 h
    # accumulator window, so the bar is reached 300 s sooner than the threshold
    # alone (issue #15). Late-and-honest, in the paging direction, and the body
    # says which rule spoke.
    broke = NOW + 4 * 3600
    t = broke
    while t < broke + 21600 - 300:
        core.record_ping(job, {"status": "fail"}, now=t)
        t += 300
    assert notifier.sent == []
    assert row(core, "dashboard-probes")["bad_since"] == db.to_iso(broke)
    core.record_ping(job, {"status": "fail"}, now=broke + 21600 - 300)
    assert titles(notifier) == ["[dashboard] Probe cycle → FAIL"]
    assert notifier.sent[0][1] == "dashboard-probes: FAIL for over 6h in the last 12h"


# --------------------------------------------------------------------------- #
# Clocks and non-alertable states
# --------------------------------------------------------------------------- #

def test_a_bad_since_in_the_future_is_healed_not_trusted(settings, notifier):
    """NTP steps the clock backwards, or a box RTC boots a year ahead: `now -
    began` is permanently negative, so the threshold can never be reached and
    the job is silent forever. Only *unparseable* values used to be healed."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    conn = core.connect()
    with conn:
        conn.execute("UPDATE jobs SET bad_since=? WHERE id='tree'",
                     (db.to_iso(NOW + 400 * DAY),))
    conn.close()
    core.recompute_all(now=NOW + 10)
    assert row(core, "tree")["bad_since"] == db.to_iso(NOW + 10)      # healed
    assert notifier.sent == []
    core.recompute_all(now=NOW + 10 + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]


def test_healing_the_clock_also_drops_a_stale_alerted_at(settings, notifier):
    """Healing `bad_since` while keeping `alerted_at` restarts the clock on a
    job that can never page again — the worst of both."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    conn = core.connect()
    with conn:
        conn.execute("UPDATE jobs SET bad_since='not-a-timestamp', alerted_at=? "
                     "WHERE id='tree'", (db.to_iso(NOW),))
    conn.close()
    core.recompute_all(now=NOW + 10)
    assert row(core, "tree")["alerted_at"] is None
    core.recompute_all(now=NOW + 10 + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]


def test_an_alerted_at_with_no_episode_is_healed_not_trusted(settings, notifier):
    """The other unusable shape: `alerted_at` set with `bad_since` NULL. Nothing
    in `Core` ever writes that pair (closing an episode clears both), so it is a
    corrupt or hand-edited row — but left alone it is a spent page attached to
    nothing, and that is exactly the shape that makes a job un-pageable.

    The job here is deliberately in a healthy, VERIFIED OK. An earlier version
    of this test left it in UNKNOWN, where the non-alertable branch clears the
    same pair — so it passed with the heal deleted and proved nothing. In OK the
    heal is the only thing standing between a corrupt row and a "Tree copy → OK"
    push for an outage that never happened and an alert that was never sent."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "ok"}, now=NOW)
    assert row(core, "tree")["state"] == "OK"            # healthy, not UNKNOWN
    conn = core.connect()
    with conn:
        conn.execute("UPDATE jobs SET bad_since=NULL, alerted_at=? WHERE id='tree'",
                     (db.to_iso(NOW),))
    conn.close()
    core.recompute_all(now=NOW + 600)                    # OK held well past the dwell
    assert row(core, "tree")["alerted_at"] is None       # healed, with no recovery sent
    assert notifier.sent == []                           # ...not "tree: OK → OK"
    # ...and the job pages normally on the next real episode.
    core.record_ping(job, {"status": "fail"}, now=NOW + 700)
    core.recompute_all(now=NOW + 700 + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]


def test_ok_held_s_reads_a_future_since_as_zero_seconds_held(settings, notifier):
    """`_ok_held_s` is the dwell's clock, and `jobs.since` is written from the
    same wall clock that can step. A future `since` makes `now - since`
    negative, which is not a length of time at all.

    Honest about what this buys: for every job with a non-zero threshold a
    negative reading compares the same as zero against both bounds, so the
    guard changes nothing. It bites only for `alert_after_s: 0`, where
    `ok_dwell_s` is 0 too: there a negative reading fails `held < 0` differently
    from a zero one, and the episode would sit open — holding its recovery —
    until the clock caught up. Covered directly rather than through a contrived
    episode, and it is the contract the callers are written against."""
    core = core_with(settings, notifier, {})
    assert core._ok_held_s("FAIL", {"since": db.to_iso(NOW - 100)}, NOW) == 0.0
    assert core._ok_held_s("OK", {"since": None}, NOW) == 0.0
    assert core._ok_held_s("OK", {"since": "not-a-timestamp"}, NOW) == 0.0
    assert core._ok_held_s("OK", {"since": db.to_iso(NOW - 100)}, NOW) == 100.0
    assert core._ok_held_s("OK", {"since": db.to_iso(NOW + 100)}, NOW) == 0.0


def test_a_clock_step_backwards_does_not_strand_an_unsent_page(settings, notifier):
    """The retry backoff is `now - last_attempt >= ALERT_RETRY_MIN_S`, measured
    on a clock that can walk backwards (NTP, a VM resuming, the box RTC). After
    a step back, `waited` is negative — and "negative is not yet 5 minutes"
    would park an undelivered page until the clock caught up, which for a
    week-long step is a week of a job that is broken, knows it, and says
    nothing. `waited < 0` therefore reads as DUE: late-but-noisy over silent."""
    notifier.fail = True                                 # ntfy unreachable
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.recompute_all(now=NOW + DAY + 100)              # threshold crossed: POST fails
    assert len(notifier.attempts) == 1 and notifier.sent == []
    assert row(core, "tree")["alerted_at"] is None       # the page was handed back
    notifier.fail = False
    core.recompute_all(now=NOW + DAY + 50)               # the clock steps back 50 s
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]
    assert row(core, "tree")["alerted_at"] == db.to_iso(NOW + DAY + 50)


def test_a_trip_through_unknown_clears_the_episode_without_a_recovery(settings, notifier):
    """UNKNOWN is not alertable, and it used to be skipped entirely — so a
    paged job that passed through it kept `bad_since`/`alerted_at` and was
    un-pageable for good (400 h of continuous FAIL afterwards, zero pushes).
    The bookkeeping now clears like an OK; the recovery does not fire, because
    nothing was verified to be fixed."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.recompute_all(now=NOW + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]
    # The runs table loses this job's history (a DB restored from an older
    # snapshot, a manual cleanup): no run, no metrics → "never heard from".
    conn = core.connect()
    with conn:
        conn.execute("DELETE FROM runs WHERE job_id='tree'")
    conn.close()
    core.recompute_all(now=NOW + DAY + 60)
    assert row(core, "tree")["state"] == "UNKNOWN"
    assert row(core, "tree")["bad_since"] is None and row(core, "tree")["alerted_at"] is None
    assert len(notifier.sent) == 1                       # no "recovered" for a shrug
    # ...and the job can page again, on a fresh clock.
    core.record_ping(job, {"status": "fail"}, now=NOW + DAY + 120)
    core.recompute_all(now=NOW + DAY + 120 + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"] * 2


# --------------------------------------------------------------------------- #
# alert: never, mid-episode
# --------------------------------------------------------------------------- #

def test_no_recovery_for_a_job_switched_to_never_mid_episode(settings, notifier):
    """Graham silences a noisy job in jobs.yml while it is broken. The episode
    it was already paged for must not send a "→ OK" after the restart — noise
    for a job that is explicitly opted out."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    core.recompute_all(now=NOW + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]
    # Same DB, new registry: `alert: never` (a jobs.yml edit + restart).
    quiet = core_with(settings, notifier, {"tree": {"alert": "never"}})
    quiet.record_ping(quiet.registry.get("tree"), {"status": "ok"}, now=NOW + DAY + 60)
    quiet.recompute_all(now=NOW + DAY + 60 + dwell(quiet, "tree"))
    assert len(notifier.sent) == 1                       # no recovery
    assert row(quiet, "tree")["alerted_at"] is None      # ...and the episode is closed out


# --------------------------------------------------------------------------- #
# Disk gauges (DECISION 4)
# --------------------------------------------------------------------------- #

GIB = 1024 ** 3


def _capacity(free_gib, total_gib=400):
    return {"status": "metric", "metrics": {"disk_free_bytes": free_gib * GIB,
                                            "disk_total_bytes": total_gib * GIB}}


def test_a_disk_gauge_hovering_at_its_threshold_does_not_page(settings, notifier):
    """DECISION 4's only real flap: a disk sitting at the boundary (89.9 / 90.1
    % used) as a big file is written and removed. One hour rides it out."""
    core = core_with(settings, notifier, {"disk": {"alert_after_s": 3600}})
    job = core.registry.get("disk")                      # min_free 25 GiB, max_used 90%
    t = NOW
    for _ in range(10):                                  # ~30 min of hovering
        core.record_ping(job, _capacity(39), now=t)      # 90.25% used → BEHIND
        core.record_ping(job, _capacity(41), now=t + 90)  # 89.75% used → OK
        t += 180
    assert notifier.sent == []
    assert [c["to_state"] for c in changes(core, "disk")].count("BEHIND") == 10


def test_a_disk_that_stays_full_pages_after_an_hour_not_a_day(settings, notifier):
    """The 24 h default would be wrong here: a `disk` gauge already has a 48 h
    fuse of its own (state.DISK_METRIC_MAX_AGE_S) for a feeder that dies, and a
    capacity threshold is a LEVEL — a disk does not un-fill itself."""
    core = core_with(settings, notifier, {"disk": {"alert_after_s": 3600}})
    job = core.registry.get("disk")
    t = NOW
    while t < NOW + 3600:
        core.record_ping(job, _capacity(10), now=t)      # 97.5% used
        t += 300
    assert notifier.sent == []
    core.record_ping(job, _capacity(10), now=NOW + 3600)
    assert titles(notifier) == ["[dashboard] Mac disk → BEHIND"]
    assert notifier.sent[0][1] == "disk: BEHIND for over 1h"
    assert notifier.sent[0][2] == "default"              # actionable, not urgent


def test_a_stale_disk_gauge_is_muted_while_its_machine_is_offline(settings, notifier):
    """PR #9 deliberately made a stale `disk` reading LATE rather than FAIL so
    that the machine-offline rule in services.py mutes it while `mac-probe` is
    itself LATE. The threshold layer must not undo that: a Mac switched off for
    three days is ONE fact, not two."""
    core = core_with(settings, notifier, {"disk": {"alert_after_s": 3600},
                                          "macprobe": {"alert_after_s": 3600}})
    mac, disk = core.registry.get("macprobe"), core.registry.get("disk")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(disk, _capacity(200), now=NOW)
    t = NOW + 3600
    while t < NOW + 3 * DAY:                             # past the 48 h metric ceiling
        core.recompute_all(now=t)
        t += 1800
    assert row(core, "disk")["state"] == "LATE" and row(core, "macprobe")["state"] == "LATE"
    assert titles(notifier) == ["[dashboard] Mac probe → LATE"]    # one fact, one page
    assert row(core, "disk")["bad_since"] is not None    # the gauge's episode IS running
    assert row(core, "disk")["alerted_at"] is None       # ...with its page unspent


# --------------------------------------------------------------------------- #
# The per-job page cooldown
#
# One page per EPISODE caps an outage at one push. It says nothing about how
# often an episode may RESTART, and a container on `restart: unless-stopped`
# backoff restarts one every twenty minutes for ever: measured at 132 pushes a
# day on 19-min-down / 20-min-up, and an independently measured >=30-min
# crash-loop cycle at 48/day. That is the alert storm this branch exists to
# remove, relocated from "every transition" to "every episode".
#
# A rate limit on a pager is a silence mechanism, so it is built the only way a
# silence mechanism may be built here: it can DELAY a page and it can never
# CANCEL one. Everything below tests that boundary.
# --------------------------------------------------------------------------- #

DOWN = {"status": "ok", "metrics": {"running": "app-1"}}          # tunnel-1 missing
UP = {"status": "ok", "metrics": {"running": "app-1,tunnel-1"}}
BOX_A = 1200          # box-containers' real threshold: 20 minutes


def _duty(core, job, down_s, up_s, until, start=NOW, step=60):
    """Ping DOWN for ``down_s`` then UP for ``up_s``, repeating until ``until``."""
    t = start
    while t < until:
        end = min(t + down_s, until)
        while t < end:
            core.record_ping(job, DOWN, now=t)
            t += step
        end = min(t + up_s, until)
        while t < end:
            core.record_ping(job, UP, now=t)
            t += step
    return t


def _alerts(notifier):
    return [t for t in titles(notifier) if not t.endswith("→ OK")]


def _recoveries(notifier):
    return [t for t in titles(notifier) if t.endswith("→ OK")]


def test_a_flapping_container_pages_per_cooldown_not_per_episode(settings, notifier):
    """The storm, at the shipped threshold. 19 min down / 20 min up: the up
    phase outlasts the 600 s dwell, so every cycle is a genuinely NEW episode
    with its own unspent page — dozens a day, each of which used to page AND
    recover.

    Note what is NOT used to achieve the fix: the job's threshold is untouched.
    Making `box-containers` wait longer before paging would have re-opened the
    silence window the hard ceiling just closed — a container down for an hour
    would go quiet again."""
    core = core_with(settings, notifier, {"containers": {"alert_after_s": BOX_A}})
    job = core.registry.get("containers")
    _duty(core, job, 1140, 1200, until=NOW + DAY)
    # 36 episodes in the day; 4 six-hour cooldown windows.
    assert len(_alerts(notifier)) == 4
    # The board still saw every one of them — this filters the phone, not history.
    assert len(changes(core, "containers")) > 40


def test_the_cooldown_expires_into_a_page_and_never_into_silence(settings, notifier):
    """**THE INVARIANT that makes the cooldown safe**, and the only reason a rate
    limit is allowed anywhere near this file: a job that is still — or again —
    not-OK past its threshold when the cooldown expires PAGES.

    The mechanism is that a held-back page does not STAMP anything. `alerted_at`
    stays NULL, so the episode keeps its unspent page and the hard ceiling
    re-offers it on every single pass; the moment the cooldown is over, one of
    those passes goes through. Stamping in the cooldown branch would look
    identical for six hours and then be permanent silence."""
    core = core_with(settings, notifier, {"containers": {"alert_after_s": BOX_A}})
    job = core.registry.get("containers")
    t = NOW
    while t < NOW + 1300:                                # episode 1 crosses 20 min
        core.record_ping(job, DOWN, now=t)
        t += 60
    assert _alerts(notifier) == ["[dashboard] Containers → FAIL"]
    paged = db.from_iso(row(core, "containers")["last_paged_at"])
    assert paged is not None
    # It recovers properly — past the dwell, so the episode really closes.
    while t < NOW + 1300 + dwell(core, "containers") + 120:
        core.record_ping(job, UP, now=t)
        t += 60
    assert row(core, "containers")["bad_since"] is None
    # ...and then breaks again and STAYS broken, for hours, inside the cooldown.
    while t < paged + COOLDOWN_FLOOR_S - 120:
        core.record_ping(job, DOWN, now=t)
        t += 60
    assert len(_alerts(notifier)) == 1                   # silent, for hours
    assert row(core, "containers")["bad_since"] is not None
    assert row(core, "containers")["alerted_at"] is None  # the page is UNSPENT
    # The cooldown runs out. The very next pass pages — no new trigger, no new
    # transition, nothing changed except the clock.
    core.recompute_all(now=paged + COOLDOWN_FLOOR_S + 1)
    assert _alerts(notifier) == ["[dashboard] Containers → FAIL"] * 2


def test_a_cooldown_held_page_yields_no_recovery_either(settings, notifier):
    """Recoveries need no rule of their own: "recovery only for an episode that
    was actually paged" already means a held-back page produces no "-> OK". That
    is what halves the traffic rather than merely shifting it, and it is the
    reason the cooldown branch must not stamp `alerted_at` — stamping would make
    a *silent* episode announce its own recovery."""
    core = core_with(settings, notifier, {"containers": {"alert_after_s": BOX_A}})
    job = core.registry.get("containers")
    t = NOW
    while t < NOW + 1300:                                # episode 1: pages
        core.record_ping(job, DOWN, now=t)
        t += 60
    while t < NOW + 1300 + dwell(core, "containers") + 120:
        core.record_ping(job, UP, now=t)                 # ...and recovers
        t += 60
    assert _alerts(notifier) == ["[dashboard] Containers → FAIL"]
    assert _recoveries(notifier) == ["[dashboard] Containers → OK"]
    # Episode 2, entirely inside the cooldown: crosses its threshold, held back,
    # then recovers. Neither end of it reaches the phone.
    while t < NOW + 1300 + dwell(core, "containers") + 120 + 1400:
        core.record_ping(job, DOWN, now=t)
        t += 60
    assert row(core, "containers")["alerted_at"] is None
    while t < NOW + 1300 + dwell(core, "containers") + 120 + 1400 \
            + dwell(core, "containers") + 120:
        core.record_ping(job, UP, now=t)
        t += 60
    assert row(core, "containers")["bad_since"] is None   # episode 2 is over
    assert len(notifier.sent) == 2                        # ...and it said nothing


def test_the_cooldown_survives_a_restart(settings, notifier):
    """In memory this would reset on every deploy — and `docker compose up -d
    --build` is precisely the event that makes containers flap, so an in-memory
    cooldown would forget itself exactly when it is needed. Same DB, new `Core`
    (a container restart): the cooldown is still running."""
    core = core_with(settings, notifier, {"containers": {"alert_after_s": BOX_A}})
    job = core.registry.get("containers")
    t = NOW
    while t < NOW + 1300:
        core.record_ping(job, DOWN, now=t)
        t += 60
    assert len(notifier.sent) == 1
    while t < NOW + 1300 + dwell(core, "containers") + 120:
        core.record_ping(job, UP, now=t)
        t += 60
    # The container is redeployed: a brand-new Core over the same SQLite file.
    fresh = core_with(settings, notifier, {"containers": {"alert_after_s": BOX_A}})
    job = fresh.registry.get("containers")
    while t < NOW + 1300 + dwell(fresh, "containers") + 120 + 1400:
        fresh.record_ping(job, DOWN, now=t)
        t += 60
    assert row(fresh, "containers")["bad_since"] is not None   # past its threshold
    assert _alerts(notifier) == ["[dashboard] Containers → FAIL"]   # still held back


def test_a_failed_push_leaves_no_cooldown_behind(settings, notifier):
    """The rollback has TWO halves. Returning `alerted_at` without returning
    `last_paged_at` is a half-rollback: the episode gets its page back and then
    cannot spend it for six hours, because a POST that never reached ntfy still
    looks like a page to the rate limiter. One unlucky 429 would buy a whole
    cooldown of silence — the exact failure the rollback exists to prevent,
    moved one column to the left."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    core.record_ping(job, {"status": "fail"}, now=NOW)
    notifier.fail = True
    core.recompute_all(now=NOW + DAY)
    assert notifier.attempts and notifier.sent == []
    assert row(core, "tree")["alerted_at"] is None       # page handed back...
    assert row(core, "tree")["last_paged_at"] is None    # ...and so is the cooldown
    notifier.fail = False
    core.recompute_all(now=NOW + DAY + RETRY_MIN)        # backoff served: retry lands
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]
    assert row(core, "tree")["last_paged_at"] == db.to_iso(NOW + DAY + RETRY_MIN)


def test_a_last_paged_at_in_the_future_cannot_mute_a_job_for_ever(settings, notifier):
    """The same clock-step trap as `bad_since`, on the column added by this
    change. A stamp in the future makes `now - last_paged_at` permanently
    negative, i.e. a cooldown that never expires — a job that can never page
    again. Refuse to honour it, heal the row, and say so in the log."""
    core = core_with(settings, notifier, {"containers": {"alert_after_s": BOX_A}})
    job = core.registry.get("containers")
    conn = core.connect()
    with conn:
        db.set_last_paged_at(conn, "containers", db.to_iso(NOW + 50 * DAY))
    conn.close()
    t = NOW
    while t < NOW + 1300:
        core.record_ping(job, DOWN, now=t)
        t += 60
    assert _alerts(notifier) == ["[dashboard] Containers → FAIL"]
    # ...and the poisoned value is gone, replaced by this page's own stamp.
    stamped = db.from_iso(row(core, "containers")["last_paged_at"])
    assert stamped is not None and stamped < NOW + DAY


def test_an_unparseable_last_paged_at_is_healed_rather_than_trusted(settings, notifier):
    """Same rule for a value that is not a timestamp at all (a hand-edited row,
    a botched migration). Unusable means NO cooldown, never an eternal one."""
    core = core_with(settings, notifier, {"containers": {"alert_after_s": BOX_A}})
    job = core.registry.get("containers")
    conn = core.connect()
    with conn:
        db.set_last_paged_at(conn, "containers", "not-a-timestamp")
    conn.close()
    t = NOW
    while t < NOW + 1300:
        core.record_ping(job, DOWN, now=t)
        t += 60
    assert _alerts(notifier) == ["[dashboard] Containers → FAIL"]


def test_a_machine_probe_inside_its_own_cooldown_cannot_mute_its_siblings(settings, notifier):
    """**Suppression may only borrow an alert that EXISTS.** That was already
    enforced statically (`alert: never` on the probe voids the rule); the
    cooldown makes the same thing true temporarily, so it has to be enforced
    the same way.

    Without this, a Mac that dies a couple of hours after its probe last paged
    is: the probe's page held by its cooldown, and every sibling's page muted
    behind a probe that is not speaking. A real multi-day outage, zero pushes,
    for the length of the cooldown — the precise failure the static guard was
    written to prevent, reintroduced by a new mechanism.

    (Unreachable on the shipped file, where `mac-probe` is 72 h and a cooldown
    can never bind above the floor — see the arithmetic test below. It is
    reachable the moment anyone shortens that threshold, which is exactly the
    kind of edit nobody would expect to silence a machine.)"""
    core = core_with(settings, notifier, {"macprobe": {"alert_after_s": 3600},
                                          "mirror": {"alert_after_s": 3600}})
    mac, mirror = core.registry.get("macprobe"), core.registry.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW)
    # The probe paged for something of its own a few hours ago, so its cooldown
    # is still running when the Mac goes away. (Set after the pings: a stamp
    # ahead of `now` is a poisoned clock and would be healed away, as it should
    # be.)
    conn = core.connect()
    with conn:
        db.set_last_paged_at(conn, "macprobe", db.to_iso(NOW + 10_000))
    conn.close()
    core.recompute_all(now=NOW + 20_000)                 # Mac asleep: both LATE
    core.recompute_all(now=NOW + 23_700)                 # both past their threshold
    # Not vacuous: the probe really is gagged — its own page is held back and
    # its episode still carries an unspent one.
    assert "[dashboard] Mac probe → LATE" not in titles(notifier)
    assert row(core, "macprobe")["alerted_at"] is None
    # ...so the sibling must NOT hide behind it. Several alerts for one fact is
    # a nuisance; none at all is an outage nobody hears about.
    assert "[dashboard] Drive mirror → LATE" in titles(notifier)
    # And when the probe's cooldown finally expires, the machine speaks for
    # itself — delayed, not cancelled.
    core.recompute_all(now=NOW + 10_000 + COOLDOWN_FLOOR_S + 60)
    assert "[dashboard] Mac probe → LATE" in titles(notifier)


def test_a_cooldown_can_never_bind_on_a_job_whose_threshold_clears_the_floor():
    """The arithmetic that decides the blast radius, asserted rather than
    reasoned about in a comment.

    A new episode cannot page before `bad_since + alert_after_s`; a new episode
    cannot open before the previous one closed; and the previous one closed at
    or after the page that belonged to it. So consecutive pages are already at
    least one threshold apart, and for every job with `alert_after_s >=
    COOLDOWN_FLOOR_S` the cooldown equals that threshold and changes nothing at
    all. On the shipped file it binds on exactly three jobs — the three that can
    flap fast."""
    from dashboard.registry import load_registry
    from tests.conftest import EXAMPLE_JOBS
    reg = load_registry(EXAMPLE_JOBS)
    binds = {j.id for j in reg
             if not j.alert_never and cooldown_s(j) > j.alert_after_s}
    assert binds == {"box-containers", "box-disk", "mac-disk"}
    for j in reg:
        if not j.alert_never:
            assert cooldown_s(j) == max(j.alert_after_s, COOLDOWN_FLOOR_S)


# --------------------------------------------------------------------------- #
# The OK dwell has to span at least two OBSERVATIONS
#
# A dwell derived from the threshold alone is blind to how often the job is
# actually looked at, and a dwell shorter than the sampling interval is not a
# dwell at all: ONE sample satisfies it and closes the episode.
# --------------------------------------------------------------------------- #

def _crash_loop(core, job, down_of, period, hours, start=NOW):
    """Sample the job at its REAL cadence — the host cron posts `docker ps`
    every 300 s — with `down_of` samples in every `period` failing. That is what
    a crash-looping container looks like from the box: not a smooth outage, an
    alternating sequence.

    **The 60 s ticker runs between the samples**, and it is load-bearing. The
    dwell is measured against `jobs.since`, so it is the TICKS that notice a job
    has now been OK for 120 s — the pings alone would each land while `held` is
    still 0 and the bug would not reproduce at all. This is the difference
    between simulating the deployment and simulating a convenient fiction."""
    t = start
    nxt = start
    i = 0
    while t < start + hours * 3600:
        if t >= nxt:
            core.record_ping(job, DOWN if (i % period) < down_of else UP, now=t)
            nxt += 300
            i += 1
        else:
            core.recompute_all(now=t)
        t += 60
    return t


@pytest.mark.parametrize("down_of,period", [(1, 2), (2, 3), (3, 4), (4, 5)])
def test_a_container_down_most_of_the_time_pages_at_every_duty_cycle(
        settings, notifier, down_of, period):
    """`box-containers` is sampled every 300 s by the host cron and its old dwell
    was `min(300, 1200/10)` = 120 s, so ANY single OK sample satisfied it and
    closed the episode. Measured over a 6 h crash-loop at 50/67/75/80% down:
    ZERO pushes. It took four consecutive failing samples to page at all — i.e.
    the job had to be broken 20 minutes with no blip, which is exactly what a
    crash loop never is.

    Two samples of dwell is the whole fix: one OK sample can no longer end an
    episode, so the failing samples accumulate into an episode that crosses the
    threshold."""
    core = core_with(settings, notifier, {"containers": {"alert_after_s": BOX_A}})
    _crash_loop(core, core.registry.get("containers"), down_of, period, hours=6)
    pct = 100 * down_of // period
    assert _alerts(notifier), f"{pct}% down for 6 h paged NOTHING"


def test_every_scheduled_job_dwells_for_two_of_its_own_samples():
    """The property, over the shipped file rather than a fixture: a job that
    reports every `cadence_s` is only OBSERVED that often, so its episode may
    not be closed by fewer than two observations. Capped, because the other end
    is just as wrong — `pa-backup` has a 24 h cadence and an uncapped rule would
    give it a two-DAY dwell, i.e. an episode that cannot close for two days and
    therefore a page held hostage for two days."""
    from dashboard.registry import load_registry
    from dashboard.services import CADENCE_DWELL_SAMPLES, MAX_CADENCE_DWELL_S
    from tests.conftest import EXAMPLE_JOBS
    for j in load_registry(EXAMPLE_JOBS):
        if not j.cadence_s:
            continue                                     # a gauge has nothing to sample
        assert ok_dwell_s(j) >= min(CADENCE_DWELL_SAMPLES * j.cadence_s,
                                    MAX_CADENCE_DWELL_S)
        assert ok_dwell_s(j) <= MAX_CADENCE_DWELL_S
        # ...and a dwell must never eat the threshold it is protecting.
        assert j.alert_never or ok_dwell_s(j) < j.alert_after_s


def test_the_machine_probes_dwell_is_never_shorter_than_its_siblings():
    """`_returning_probes` mutes a sibling's `LATE -> OK` page for as long as the
    PROBE's episode is open. The siblings recover FIRST (mac_probe.py posts them
    before its own heartbeat), so their episodes close first — but only while the
    probe's dwell is at least as long as theirs. The old code could assert this
    in a comment ("no dwell exceeds MAX_OK_DWELL_S"); with a cadence-aware dwell
    that sentence is no longer true, so assert the thing itself.

    It holds because the cap makes 900 s the longest dwell there is and
    `mac-probe`'s 3600 s cadence reaches it. Remove the cap and `pa-backup`'s
    24 h cadence gives it a dwell 24x the probe's."""
    from dashboard.registry import load_registry
    from tests.conftest import EXAMPLE_JOBS
    reg = load_registry(EXAMPLE_JOBS)
    probe = reg.get("mac-probe")
    siblings = [j for j in reg if j.machine == "mac" and j.id != probe.id]
    assert siblings
    for j in siblings:
        assert ok_dwell_s(j) <= ok_dwell_s(probe), j.id


def test_an_unverifiable_ok_never_closes_an_episode_sooner_than_a_verified_one():
    """`ok_hold_s` and `ok_dwell_s` are checked in that order, so a hold SHORTER
    than the dwell would close an episode sooner for an OK we cannot verify than
    for one we can — and the hold path closes SILENTLY, with no recovery. The
    cadence-aware dwell is what made that reachable (it can now exceed the 300 s
    the hold used to floor at)."""
    from dashboard.services import ok_hold_s
    from dashboard.registry import load_registry
    from tests.conftest import EXAMPLE_JOBS
    for j in load_registry(EXAMPLE_JOBS):
        assert ok_hold_s(j) >= ok_dwell_s(j), j.id
    # And on a shape the shipped file does not have: a fast-paging job with a
    # cadence, where the threshold-derived hold would be only 300 s.
    doc = copy.deepcopy(JOBS_DOC)
    for raw in doc["jobs"]:
        raw.pop("alert", None)
        raw["alert_after_s"] = 0
    snap = parse_registry(doc).get("snap")               # cadence 300 -> dwell 600
    assert ok_dwell_s(snap) == 600 and ok_hold_s(snap) >= 600


# --------------------------------------------------------------------------- #
# Attacking `_returning_probes` — the mute added by THIS branch
#
# It is a brand-new way to be silent, in the area that has already produced two
# structural silence bugs. Four questions, asked adversarially: can it mute a
# page that is not the machine's fault? can it outlast its own justification?
# does it depend on registry order? can it be made permanent?
# --------------------------------------------------------------------------- #

def test_a_returning_probe_cannot_mute_a_siblings_non_late_page(settings, notifier):
    """The mute exists for ONE fact — "the Mac was asleep" — so it may only ever
    swallow a plain `LATE -> OK`. A sibling whose episode is about a FAILED RUN
    or a STALE DESTINATION is news of its own; the Mac having been asleep says
    nothing about it, and the wake-up is exactly when such a verdict lands."""
    core = core_with(settings, notifier, {"macprobe": {"alert_after_s": 3600},
                                          "tree": {"alert_after_s": 3600}})
    mac, tree = core.registry.get("macprobe"), core.registry.get("tree")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(tree, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 200_000)                # Mac gone: both LATE
    notifier.sent.clear()
    # The Mac wakes and the tree copy reports a REAL failure, then the probe's
    # own heartbeat lands — the probe is inside the LATE episode it is returning
    # from, which is precisely when `returning` is populated.
    core.record_ping(tree, {"status": "fail", "reason": "rclone exit 1"},
                     now=NOW + 200_100)
    core.record_ping(mac, {"status": "ok"}, now=NOW + 200_102)
    core.recompute_all(now=NOW + 200_100 + 3600)         # tree crosses its threshold
    assert "[dashboard] Tree copy → FAIL" in titles(notifier)


def test_a_returning_probes_mute_cannot_outlast_its_own_episode(settings, notifier):
    """The mute is bounded by the PROBE's OPEN EPISODE, and by nothing longer.

    Keyed on anything that survives the episode — "the last non-OK state this
    probe was in", say — it would never end: `mac-probe` goes LATE once, in its
    first week, and from then on EVERY sibling page decided while the sibling
    reads OK is swallowed, for ever, on the strength of a sleep that finished
    months ago. A silence with no expiry, set off by an ordinary night.

    So: the Mac sleeps once and everything closes cleanly. Long after, with the
    probe healthy and pinging throughout, `drive-mirror` alone has an outage
    that outlives its threshold and then recovers inside its own dwell — which
    is exactly the ceiling page that runs through the branch `returning` guards.
    It must go out."""
    core = core_with(settings, notifier, {"macprobe": {"alert_after_s": 3 * DAY},
                                          "mirror": {"alert_after_s": 3600}})
    mac, mirror = core.registry.get("macprobe"), core.registry.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 20_000)                 # one night's sleep: both LATE
    core.record_ping(mirror, {"status": "ok"}, now=NOW + 30_000)
    core.record_ping(mac, {"status": "ok"}, now=NOW + 30_002)
    core.recompute_all(now=NOW + 30_002 + dwell(core, "macprobe") + 60)
    assert row(core, "macprobe")["bad_since"] is None    # the sleep is fully over
    assert notifier.sent == []                           # ...and woke nobody
    # Weeks of normal operation, as far as the probe is concerned: it keeps
    # reporting hourly and never leaves OK again. Only the mirror goes quiet.
    t = NOW + 31_000
    stop = NOW + 34_200 + 3400                           # just under its 1 h threshold
    while t < stop:
        if (t - NOW) % 3600 < 60:
            core.record_ping(mac, {"status": "ok"}, now=t)
        core.recompute_all(now=t)
        t += 60
    assert row(core, "mirror")["state"] == "LATE"
    assert row(core, "macprobe")["state"] == "OK"
    assert notifier.sent == []                           # still under the threshold
    # It comes back — and its episode, held open by the dwell, crosses the
    # threshold while the job itself reads OK. That is the ceiling's page.
    core.record_ping(mirror, {"status": "ok"}, now=stop)
    core.recompute_all(now=NOW + 34_200 + 3700)
    assert "[dashboard] Drive mirror → LATE" in titles(notifier)


def test_a_sibling_that_stays_late_pages_once_the_probe_is_back(settings, notifier):
    """The version that would hurt most: the machine comes back, the probe
    recovers and closes, but one sibling never does — its launchd job was
    unloaded, so it is LATE for a reason that has nothing to do with sleep. The
    mute must not survive the probe's return."""
    core = core_with(settings, notifier, {"macprobe": {"alert_after_s": 3600},
                                          "mirror": {"alert_after_s": 3600}})
    mac, mirror = core.registry.get("macprobe"), core.registry.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 20_000)
    notifier.sent.clear()
    t = NOW + 20_100
    core.record_ping(mac, {"status": "ok"}, now=t)       # only the probe returns
    while t < NOW + 20_100 + 4 * 3600:                   # the mirror stays silent
        core.recompute_all(now=t)
        t += 300
    assert row(core, "mirror")["state"] == "LATE"
    assert "[dashboard] Drive mirror → LATE" in titles(notifier)


@pytest.mark.parametrize("reverse", [False, True])
def test_the_returning_probe_mute_does_not_depend_on_registry_order(
        settings, notifier, reverse):
    """`_returning_probes` is computed ONCE per batch, from the PRE-recompute
    rows, so it cannot matter whether the probe job sits before or after its
    siblings in jobs.yml. Read from post-recompute state instead and a sibling
    listed first would see a different answer from one listed last — a silence
    bug you could introduce by reordering a YAML file."""
    def mutate(doc):
        if reverse:
            doc["jobs"].reverse()

    core = core_with(settings, notifier,
                     {"macprobe": {"alert_after_s": 3600},
                      "mirror": {"alert_after_s": 3600}}, mutate=mutate)
    mac, mirror = core.registry.get("macprobe"), core.registry.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 20_000)
    core.recompute_all(now=NOW + 20_000 + 3600)          # both past their thresholds
    # The Mac wakes in the real order: siblings first, the probe's own heartbeat
    # last, each its own request and its own recompute.
    core.record_ping(mirror, {"status": "ok"}, now=NOW + 30_000)
    core.record_ping(mac, {"status": "ok"}, now=NOW + 30_002)
    core.recompute_all(now=NOW + 30_002 + dwell(core, "macprobe") + 60)
    # One page and one recovery, for the MACHINE — whichever end of jobs.yml the
    # probe was declared at.
    assert titles(notifier) == ["[dashboard] Mac probe → LATE",
                                "[dashboard] Mac probe → OK"]


# --------------------------------------------------------------------------- #
# THE EPISODE ACCUMULATOR (issue #15)
#
# PR #8 gave the DAMPING a cumulative backstop — `PROBE_NO_SUCCESS_S`, measured
# from the last successful probe — so a persistent failure reaches FAIL however
# the streak arithmetic falls. PR #11 gave the EPISODE nothing of the kind: an
# episode is destroyed by one verified OK that outlasts the dwell, so a
# destination failing for an hour and listing successfully once every quarter of
# an hour restarted its 6 h clock for ever. Measured over 24 h against the real
# `run_probe_cycle` with production values: 60/15 failed 73% of its probes and
# paged ZERO times; 300/30 failed 90% and paged ZERO times.
#
# Google Drive's `rateLimitExceeded` is intermittent by nature, so this was the
# likeliest shape to matter — and it is the one that took out the real nightly
# backup on 2026-09-10.
#
# Issue #13 item 4 had explicitly DECLINED an accumulator here, because two
# attempts at one during review each produced a silence bug. Both replaced the
# episode clock with a not-OK sum, which can only ever page later. This one is an
# OR beside the clock, so it cannot page later than before, and the first test
# below is the guard that it cannot page for noise either.
# --------------------------------------------------------------------------- #

STORM_CYCLES = 1152           # 4 days at the 300 s probe interval (real: 1159)
STORM_FAILURES = 27           # FAIL->OK flaps in the real incident (21 + 6)
PROBE_A = 21600               # `dashboard-probes`' real threshold: 6 hours


@pytest.fixture
def no_prune(monkeypatch):
    """Switch off `db.prune` for the long simulations below.

    Not a behaviour patch: `prune` has nothing to do with alerting. It is a
    speed one, and the factor is not small — its `id NOT IN (SELECT … LIMIT n)`
    is correlated, so SQLite re-runs the subquery per candidate row and the cost
    grows with the rows retained. The 1152-cycle replay takes **113 s** with it
    and **2 s** without. (Worth knowing for production too: `prune` runs inside
    every probe cycle, against tables it keeps at 2000 rows per job.)
    """
    monkeypatch.setattr(db, "prune", lambda *a, **kw: None)


def _isolated_failures(count, total):
    """`count` cycle indexes spread over `total`, never two adjacent — the shape
    of the real incident (21 of 1159 `gdrive:Backups` probes failed, 6 of 1159
    `gdrive:Gremlins` probes, and NO two failures were consecutive)."""
    picked = {int(i * total / count) for i in range(count)}
    assert len(picked) == count
    assert not any(i + 1 in picked for i in picked), "adjacent failures"
    return picked


def _failing_probe_rows(core, job_id="snap"):
    conn = core.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM probes WHERE job_id=? AND ok=0",
                            (job_id,)).fetchone()[0]
    finally:
        conn.close()


def _fail_runs(core, job_id="dashboard-probes"):
    conn = core.connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM runs WHERE job_id=? AND "
                            "status='fail'", (job_id,)).fetchone()[0]
    finally:
        conn.close()


@pytest.mark.parametrize("fail_threshold,expect_fail_states", [(2, 0), (1, 27)])
def test_the_historical_flap_storm_still_pages_nothing(
        settings, notifier, monkeypatch, no_prune, fail_threshold,
        expect_fail_states):
    """**THE REGRESSION GUARD FOR THE ACCUMULATOR.** Replay the incident this
    whole branch exists because of — 27 isolated transient probe failures in
    1152 cycles over four days, none of them adjacent, nothing actually broken —
    and assert the phone stays silent. The pre-0.2 dispatcher sent ~55 pushes for
    exactly this.

    Parametrised over the damping, because the two parameters ask different
    questions:

    - **2 (shipped)** — the faithful replay. Isolated transient failures never
      reach an alertable STATE at all, so no episode ever opens and the
      accumulator has nothing to add up. This is the "damping filters upstream"
      argument, asserted rather than assumed.
    - **1 (damping off)** — the adversarial version, and the one that actually
      tests the accumulator. Every failure now trips FAIL, so the state flaps 27
      times and the transition log carries 27 real not-OK spans for the
      accumulator to find. It must still not page: 27 spans of ~300 s spread over
      four days is ~1000 s inside any 12 h window, against a 6 h bar.

    A cumulative rule that pages on this pattern would be worse than the bug it
    fixes, so this test comes first."""
    settings.probe_fail_threshold = fail_threshold
    core = core_with(settings, notifier, {"dashboard-probes": {"alert_after_s": PROBE_A}})
    bad = _isolated_failures(STORM_FAILURES, STORM_CYCLES)
    t = NOW
    for i in range(STORM_CYCLES):
        _cycle(core, monkeypatch, t, ok=i not in bad)
        t += 300
    # Not vacuous: every failure really was recorded, and the damping really did
    # (or did not) keep it off the board.
    assert _failing_probe_rows(core) == STORM_FAILURES
    states = [c["to_state"] for c in changes(core, "dashboard-probes", limit=500)]
    assert states.count("FAIL") == expect_fail_states
    assert notifier.sent == []
    # The MARGIN, as a number rather than as "no pushes": over every 12 h window
    # of those four days, the most not-OK time the accumulator can find is
    # **1200 s against a 21600 s bar — 18x under it**. Measured and pinned here
    # because "zero pushes" on its own would read the same for a rule that was
    # one loosening away from paging; this says how much room there actually is.
    window = bad_window_s(core.registry.get("dashboard-probes"))
    conn = core.connect()
    try:
        worst = max(db.not_ok_seconds(conn, "dashboard-probes", w, w + window,
                                      ALERTABLE_STATES)
                    for w in range(int(NOW - window), int(t), 3600))
    finally:
        conn.close()
    assert worst <= (1500 if fail_threshold == 1 else 0)
    # The ticker is deliberately not interleaved here: every probe cycle already
    # ends in the identical `recompute_all`, the dwell arithmetic lands on the
    # same 300 s boundaries either way, and it was measured both ways at zero
    # pushes. Running 4 days of 60 s ticks costs 8 s of suite time to assert the
    # same zero.


@pytest.mark.parametrize("down_min,up_min,fail_pct", [(60, 15, 73),
                                                     (180, 15, 90),
                                                     (300, 30, 90)])
def test_an_intermittently_failing_destination_now_pages(
        settings, notifier, monkeypatch, no_prune, down_min, up_min, fail_pct):
    """The three patterns issue #15 measured at **zero pushes a day**.

    `fail_pct` is the share of probe cycles the self-job recorded as `fail`,
    i.e. how much of the day the board read FAIL — reproduced here to prove this
    is the same scenario the issue measured and not a friendlier one.

    Bounded at both ends on purpose. A silence test that only asserts "> 0"
    passes just as well for a fix that pages sixty times, which is the failure
    mode on the other side; the upper bound is the per-job cooldown's, and it is
    the reason an accumulator is safe to add at all."""
    core = core_with(settings, notifier, {"dashboard-probes": {"alert_after_s": PROBE_A}})
    period = (down_min + up_min) * 60
    t = NOW
    while t < NOW + DAY:
        _cycle(core, monkeypatch, t, ok=((t - NOW) % period) >= down_min * 60)
        t += 300
    cycles = int(DAY / 300)
    assert abs(100 * _fail_runs(core) // cycles - fail_pct) <= 2   # same scenario
    alerts, recoveries = _alerts(notifier), _recoveries(notifier)
    assert alerts, f"{fail_pct}% of the day FAIL, and zero pushes"
    # The cooldown is the only rate guarantee in the file, and it still holds:
    # at most one page per 6 h, each of which may cost one recovery as well.
    cap = DAY // COOLDOWN_FLOOR_S
    assert len(alerts) <= cap and len(recoveries) <= cap
    assert len(notifier.sent) <= 2 * cap
    assert all(t.endswith("→ FAIL") for t in alerts)
    # And the body is honest about WHICH rule spoke: this destination was not
    # broken for six unbroken hours, it was broken for six of the last twelve.
    assert notifier.sent[0][1] == "dashboard-probes: FAIL for over 6h in the last 12h"
    assert notifier.sent[0][2] == "high"


def test_a_muted_late_cannot_accumulate_into_a_page_for_something_else(settings, notifier):
    """The accumulator counts time in the state being paged about, NOT "any
    badness" — and this is the test that forced that.

    A flat accumulator laundered deliberately MUTED time into an unrelated page:
    every night the Mac sleeps, each of its jobs sits LATE for hours with its
    alert suppressed by the machine-offline rule ("one alert for the machine, not
    five"), and the next morning the first genuinely new `drive-mirror: BEHIND`
    episode paged the INSTANT it opened on the strength of that sleep. Per-state,
    muted LATE time can only ever count toward a LATE page — which the same mute
    still gags."""
    core = core_with(settings, notifier, {"mirror": {"alert_after_s": 3600},
                                          "macprobe": {"alert_after_s": 3 * DAY}})
    mac, mirror = core.registry.get("macprobe"), core.registry.get("mirror")
    core.record_ping(mac, {"status": "ok"}, now=NOW)
    core.record_ping(mirror, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 20_000)                 # asleep: both LATE
    core.recompute_all(now=NOW + 24_000)                 # mirror past its threshold
    assert row(core, "mirror")["state"] == "LATE"
    assert notifier.sent == []                           # muted: the Mac is asleep
    # The Mac wakes cleanly (siblings first, then the probe's own heartbeat) and
    # the muted episode closes with nothing said — 5.5 h of LATE, unpaged.
    core.record_ping(mirror, {"status": "ok", "metrics": {"pending": 0, "mismatch": 0}},
                     now=NOW + 30_000)
    core.record_ping(mac, {"status": "ok"}, now=NOW + 30_002)
    core.recompute_all(now=NOW + 30_000 + dwell(core, "mirror"))
    assert row(core, "mirror")["bad_since"] is None
    assert notifier.sent == []
    # Now a genuinely new problem of its own: pending uploads, a fresh episode,
    # seconds old. This must NOT page yet — the hours of LATE in the window were
    # declared not-news by the machine-offline rule, and a flat accumulator
    # turned them straight into a push here.
    core.record_ping(mirror, {"status": "ok", "metrics": {"pending": 4}},
                     now=NOW + 30_400)
    assert row(core, "mirror")["state"] == "BEHIND"
    assert notifier.sent == []
    # ...and it pages on its OWN hour, not on the Mac's night.
    core.record_ping(mirror, {"status": "ok", "metrics": {"pending": 4}},
                     now=NOW + 30_400 + 3600)
    assert titles(notifier) == ["[dashboard] Drive mirror → BEHIND"]
    assert notifier.sent[0][1] == "mirror: BEHIND for over 1h"   # continuous, not cumulative


def test_badness_older_than_the_window_does_not_count(settings, notifier):
    """The window is what keeps the accumulator from being a permanent black
    mark. A day of FAIL last week must not make a fresh five-minute episode
    page — otherwise a job's first bad week silences its threshold for ever."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": 3600}})
    job = core.registry.get("tree")
    t = NOW
    while t < NOW + 3600:                                # an hour of FAIL: pages once
        core.record_ping(job, {"status": "fail"}, now=t)
        t += 300
    core.record_ping(job, {"status": "fail"}, now=NOW + 3600)
    assert len(_alerts(notifier)) == 1
    core.record_ping(job, {"status": "ok"}, now=NOW + 3700)
    core.recompute_all(now=NOW + 3700 + dwell(core, "tree"))
    assert row(core, "tree")["bad_since"] is None
    # Well past the 2 h window (and past the 6 h cooldown, so the cooldown is not
    # what is being tested here), it breaks again briefly.
    late = NOW + 8 * 3600
    core.record_ping(job, {"status": "fail"}, now=late)
    core.recompute_all(now=late + 600)
    assert len(_alerts(notifier)) == 1                    # the old hour is forgotten
    core.recompute_all(now=late + 3600)                   # its own hour, its own page
    assert len(_alerts(notifier)) == 2


def test_the_accumulator_window_is_derived_from_the_job_not_hard_coded():
    """`bad_window_s` is a multiple of the job's OWN threshold, so a job that
    pages after a day is judged over two days and one that pages after an hour
    over two hours. A constant here would mean "broken half the time" for one job
    and "broken 1% of the time" for another."""
    from dashboard.registry import load_registry
    from tests.conftest import EXAMPLE_JOBS
    for j in load_registry(EXAMPLE_JOBS):
        assert bad_window_s(j) == BAD_WINDOW_MULTIPLE * j.alert_after_s
        # It can never fire EARLIER than the continuous rule: accruing a whole
        # threshold of not-OK time takes at least that long.
        assert bad_window_s(j) >= j.alert_after_s


# --------------------------------------------------------------------------- #
# `db.not_ok_seconds` — the accumulator's only input
#
# Derived from `state_changes` rather than stored, for the reasons
# `probe_fail_streak` is: nothing to migrate, nothing to keep in step, survives a
# restart, and the ingest route cannot write that table, so accumulated badness
# is not forgeable by a holder of INGEST_TOKEN.
# --------------------------------------------------------------------------- #

def _change(core, job_id, at, to_state, from_state="OK"):
    conn = core.connect()
    try:
        with conn:
            conn.execute("INSERT INTO state_changes (job_id, changed_at, "
                         "from_state, to_state) VALUES (?,?,?,?)",
                         (job_id, db.to_iso(at), from_state, to_state))
    finally:
        conn.close()


def _bad_s(core, job_id, start, end, states=("FAIL",)):
    conn = core.connect()
    try:
        return db.not_ok_seconds(conn, job_id, start, end, states)
    finally:
        conn.close()


def test_not_ok_seconds_sums_only_the_named_states_inside_the_window(settings, notifier):
    core = core_with(settings, notifier, {})
    _change(core, "tree", NOW, "FAIL")                   # FAIL from NOW
    _change(core, "tree", NOW + 600, "OK", "FAIL")       # ...to NOW+600
    _change(core, "tree", NOW + 1200, "LATE")            # LATE from NOW+1200
    _change(core, "tree", NOW + 1500, "OK", "LATE")      # ...to NOW+1500
    _change(core, "tree", NOW + 1800, "FAIL")            # FAIL from NOW+1800, open
    assert _bad_s(core, "tree", NOW, NOW + 2400) == 1200        # 600 + 600 open
    assert _bad_s(core, "tree", NOW, NOW + 2400, ("LATE",)) == 300
    assert _bad_s(core, "tree", NOW, NOW + 2400, ("FAIL", "LATE")) == 1500
    # The window clips both ends rather than counting whole spans that overlap it.
    assert _bad_s(core, "tree", NOW + 300, NOW + 2400) == 900
    assert _bad_s(core, "tree", NOW + 1900, NOW + 2400) == 500
    assert _bad_s(core, "tree", NOW + 700, NOW + 1000) == 0
    # A degenerate window is zero, not a negative or a whole span.
    assert _bad_s(core, "tree", NOW + 2400, NOW) == 0.0
    # A job with no transition log at all accrues nothing — unaccounted time
    # reads as OK, so the sum can only ever UNDER-estimate.
    assert _bad_s(core, "snap", NOW, NOW + 2400) == 0.0


def test_not_ok_seconds_cannot_be_inflated_by_a_clock_that_stepped_back(settings, notifier):
    """`state_changes.changed_at` is the writer's wall clock, so an NTP step
    backwards (or a box RTC ahead at boot — the step this code already heals for
    `bad_since`) leaves rows out of order and one dated in the FUTURE. The walk
    is by rowid, and a row whose stamp is not strictly older than the span being
    closed is skipped: one poisoned row must not be able to invent hours of
    badness and page for an outage that never happened."""
    core = core_with(settings, notifier, {})
    _change(core, "tree", NOW, "FAIL")
    _change(core, "tree", NOW + 300, "OK", "FAIL")
    _change(core, "tree", NOW + 50 * DAY, "FAIL", "OK")  # written while the clock was ahead
    _change(core, "tree", NOW + 600, "OK", "FAIL")       # the clock is back
    assert _bad_s(core, "tree", NOW, NOW + 1200) == 300  # the poisoned row adds nothing
    # An unparseable stamp is skipped for the same reason (it cannot be a
    # boundary), and cannot make the walk raise inside a recompute transaction.
    conn = core.connect()
    with conn:
        conn.execute("INSERT INTO state_changes (job_id, changed_at, from_state, "
                     "to_state) VALUES ('tree','not-a-timestamp','OK','FAIL')")
    conn.close()
    assert _bad_s(core, "tree", NOW, NOW + 1200) == 300


# --------------------------------------------------------------------------- #
# ESCALATION: an episode that gets WORSE (issue #16)
#
# `_page` short-circuited on `alerted_at`, so an episode had exactly one page —
# and for a capacity gauge that is inverted. Confirmed on `box-disk`: it paged
# "BEHIND for over 1h" at priority=default, then free space fell to 1 GiB, then
# `statvfs` failed outright (FAIL), and NOTHING further was sent — no push, and
# no high-priority push ever, because the priority is read off the state and the
# state that was paged was the mild one. BEHIND is the early warning; FAIL is the
# event you actually wanted to hear about.
#
# So severity is an ORDER (not a change detector), read off
# `notify.HIGH_PRIORITY_STATES` so it cannot drift from the priority the push
# carries, and an episode may page once more when it crosses from the
# default-priority rank to the high-priority one. Two ranks is also the bound:
# there is nowhere above rank 2 to go, so a worsening cannot become a storm.
# --------------------------------------------------------------------------- #

DISK_A = 3600             # `box-disk` / `mac-disk`' real threshold: 1 hour
UNREADABLE = {"status": "fail", "reason": "statvfs failed"}


def test_severity_is_the_ntfy_priority_and_nothing_invented(settings, notifier):
    """The ordering has exactly one source of truth. If someone adds a state to
    `HIGH_PRIORITY_STATES` and not to some second table here, the escalation and
    the `Priority` header would disagree — which IS the defect in #16, where the
    push that mattered was never sent at any priority."""
    from dashboard.notify import HIGH_PRIORITY_STATES
    for state in ("FAIL", "STALE_DEST"):
        assert state in HIGH_PRIORITY_STATES
        assert alert_severity(state) == 2
        assert notifier.priority_for(state) == "high"
    for state in ("BEHIND", "LATE"):
        assert alert_severity(state) == 1
        assert notifier.priority_for(state) == "default"
    for state in ("OK", "UNKNOWN", "", "nonsense"):
        assert alert_severity(state) == 0    # never a page, and below every rank


def test_a_gauge_that_worsens_after_paging_pages_again_at_high_priority(settings, notifier):
    """The confirmed #16 sequence, end to end."""
    core = core_with(settings, notifier, {"disk": {"alert_after_s": DISK_A}})
    job = core.registry.get("disk")                      # min_free 25 GiB
    t = NOW
    while t <= NOW + DISK_A:
        core.record_ping(job, _capacity(10), now=t)      # 97.5% used → BEHIND
        t += 300
    assert notifier.sent == [("[dashboard] Mac disk → BEHIND",
                              "disk: BEHIND for over 1h", "default")]
    assert row(core, "disk")["alerted_state"] == "BEHIND"
    # It fills further. Still BEHIND: the same fact, and no new push — the
    # escalation is an ORDER on states, not "something changed".
    core.record_ping(job, _capacity(1), now=t)
    assert len(notifier.sent) == 1
    # ...and then the reading fails outright. FAIL outranks BEHIND, so this goes
    # out, and at the priority FAIL has always carried.
    core.record_ping(job, UNREADABLE, now=t + 60)
    assert row(core, "disk")["state"] == "FAIL"
    assert notifier.sent[-1] == ("[dashboard] Mac disk → FAIL",
                                 "disk: BEHIND → FAIL", "high")
    assert row(core, "disk")["alerted_state"] == "FAIL"
    # ONCE. The board keeps saying FAIL for hours; the phone does not.
    t += 120
    while t < NOW + 6 * 3600:
        core.recompute_all(now=t)
        t += 60
    assert len(notifier.sent) == 2
    # And the recovery names what was last PAGED, which is now FAIL.
    core.record_ping(job, _capacity(200), now=t)
    core.recompute_all(now=t + dwell(core, "disk") + 60)
    assert notifier.sent[-1][1] == "disk: FAIL → OK"


def test_a_milder_state_after_a_severe_page_never_re_pages(settings, notifier):
    """The order runs one way only. A gauge whose reading comes BACK (FAIL →
    BEHIND: the disk is readable again and merely low) has got BETTER, and a
    "better, but still bad" push is the noise this whole branch removes."""
    core = core_with(settings, notifier, {"disk": {"alert_after_s": DISK_A}})
    job = core.registry.get("disk")
    core.record_ping(job, _capacity(200), now=NOW)       # a reading, then failure
    core.record_ping(job, UNREADABLE, now=NOW + 60)
    core.recompute_all(now=NOW + 60 + DISK_A)
    assert notifier.sent == [("[dashboard] Mac disk → FAIL",
                              "disk: FAIL for over 1h", "high")]
    core.record_ping(job, _capacity(10), now=NOW + 60 + DISK_A + 300)
    assert row(core, "disk")["state"] == "BEHIND"        # readable again, still low
    core.recompute_all(now=NOW + 60 + DISK_A + 1200)
    assert len(notifier.sent) == 1
    assert row(core, "disk")["alerted_state"] == "FAIL"  # unchanged by a milder state


def test_the_escalation_is_not_held_by_the_cooldown_but_does_stamp_it(settings, notifier):
    """`cooldown_s(disk)` is 6 h against a 1 h threshold, so a cooldown-gated
    escalation would mean "the disk is now unreadable" arriving up to six hours
    late — which is the residual PR #11 wrote down and left. The cooldown exists
    to stop the SAME fact repeating; a strictly worse state is a different fact.

    It is bounded without a timer (one per paged episode, and a paged episode is
    itself one per cooldown), and it still STAMPS `last_paged_at`, so the next
    episode's first page is pushed out by a full cooldown from the escalation.
    The rate limit moves; it is not lifted."""
    core = core_with(settings, notifier, {"disk": {"alert_after_s": DISK_A}})
    job = core.registry.get("disk")
    core.record_ping(job, _capacity(10), now=NOW)
    core.recompute_all(now=NOW + DISK_A)                 # BEHIND page
    first = row(core, "disk")["last_paged_at"]
    assert first == db.to_iso(NOW + DISK_A)
    # Deep inside the cooldown, the gauge fails outright.
    core.record_ping(job, UNREADABLE, now=NOW + DISK_A + 600)
    assert len(notifier.sent) == 2                       # not held for six hours
    assert row(core, "disk")["last_paged_at"] == db.to_iso(NOW + DISK_A + 600)
    # ...and the NEXT episode is rate-limited from the escalation, not from the
    # first page: it recovers, breaks again, and stays quiet inside the window.
    t = NOW + DISK_A + 660
    core.record_ping(job, _capacity(200), now=t)
    core.recompute_all(now=t + dwell(core, "disk") + 60)
    assert row(core, "disk")["bad_since"] is None
    assert len(notifier.sent) == 3                       # + the recovery
    t = NOW + DISK_A + 600 + COOLDOWN_FLOOR_S - 2 * 3600
    while t < NOW + DISK_A + 600 + COOLDOWN_FLOOR_S - 300:
        core.record_ping(job, _capacity(10), now=t)      # low again, for hours
        t += 300
    assert len(notifier.sent) == 3                       # still inside the cooldown
    assert row(core, "disk")["alerted_at"] is None        # the page is UNSPENT
    core.recompute_all(now=NOW + DISK_A + 600 + COOLDOWN_FLOOR_S + 60)
    assert len(notifier.sent) == 4                        # delayed, never cancelled


def test_an_escalation_that_does_not_land_is_retried_without_resending_the_first_page(
        settings, notifier):
    """The rollback has to put back the PAIR the escalation replaced, not NULL:
    NULL means "this episode has never paged", so the next pass would send the
    BEHIND page a second time as well. Restoring the pair leaves the escalation
    still due — the worse state is still worse — so it is delayed, never
    cancelled, like every other hold in this file."""
    core = core_with(settings, notifier, {"disk": {"alert_after_s": DISK_A}})
    job = core.registry.get("disk")
    core.record_ping(job, _capacity(10), now=NOW)
    core.recompute_all(now=NOW + DISK_A)
    assert len(notifier.sent) == 1
    paged_at = row(core, "disk")["alerted_at"]
    notifier.fail = True                                 # ntfy unreachable
    core.record_ping(job, UNREADABLE, now=NOW + DISK_A + 600)
    assert len(notifier.attempts) == 2 and len(notifier.sent) == 1
    # Handed back to the pair it replaced, not to NULL.
    assert row(core, "disk")["alerted_at"] == paged_at
    assert row(core, "disk")["alerted_state"] == "BEHIND"
    assert row(core, "disk")["last_paged_at"] == paged_at
    notifier.fail = False
    core.recompute_all(now=NOW + DISK_A + 600 + RETRY_MIN)
    assert titles(notifier) == ["[dashboard] Mac disk → BEHIND",
                                "[dashboard] Mac disk → FAIL"]
    assert notifier.sent[-1][2] == "high"
    assert row(core, "disk")["alerted_state"] == "FAIL"


def test_the_escalation_backoff_does_not_delay_it_behind_the_first_page(settings, notifier):
    """The retry backoff is keyed on the episode AND the rank being paged. Keyed
    on the episode alone, an escalation seconds after a successful first page
    reads as a retry of it and waits out `ALERT_RETRY_MIN_S` — a five-minute
    delay imposed by a mechanism that exists only to avoid hammering a dead
    ntfy."""
    core = core_with(settings, notifier, {"disk": {"alert_after_s": DISK_A}})
    job = core.registry.get("disk")
    core.record_ping(job, _capacity(10), now=NOW)
    core.recompute_all(now=NOW + DISK_A)                 # page, POST attempted now
    core.record_ping(job, UNREADABLE, now=NOW + DISK_A + 1)   # one second later
    assert len(notifier.sent) == 2
    assert notifier.sent[-1] == ("[dashboard] Mac disk → FAIL",
                                 "disk: BEHIND → FAIL", "high")


def test_the_worst_case_push_rate_per_cooldown_window(settings, notifier):
    """The arithmetic `cooldown_s`'s docstring quotes, asserted instead of
    argued. The pathological job for the escalation is one that crosses its
    threshold, gets strictly worse, then genuinely recovers — over and over, for
    ever. Per cooldown window that is page + escalation + recovery = THREE
    pushes, 12/day at the 6 h floor.

    That is the honest cost of both halves of this change, and it is still a
    third of the storm this branch removed (~55 pushes in 5 days from one job,
    for nothing at all) — and unlike that storm it takes a job that is genuinely
    broken, genuinely getting worse, every six hours."""
    core = core_with(settings, notifier, {"disk": {"alert_after_s": DISK_A}})
    job = core.registry.get("disk")
    t = NOW
    while t < NOW + DAY:
        stop = t + DISK_A + 600                          # low: crosses its hour
        while t < stop:
            core.record_ping(job, _capacity(10), now=t)
            t += 300
        core.record_ping(job, UNREADABLE, now=t)         # ...then unreadable
        t += 300
        stop = t + dwell(core, "disk") + 600             # ...then genuinely fine
        while t < stop:
            core.record_ping(job, _capacity(200), now=t)
            t += 300
    windows = DAY // COOLDOWN_FLOOR_S
    assert len(_alerts(notifier)) <= 2 * windows          # page + escalation
    assert len(_recoveries(notifier)) <= windows
    assert len(notifier.sent) <= 3 * windows
    # ...and not vacuous: the escalation really is reaching the phone, at the
    # priority that was missing entirely before this change.
    assert any(s[1] == "disk: BEHIND → FAIL" and s[2] == "high"
               for s in notifier.sent)


def test_the_recovery_names_the_state_that_was_paged_not_the_latest_one(settings, notifier):
    """From the same audit. `about` reads the LATEST non-OK state, so an episode
    paged as "BEHIND for over 1h" that later touched a different state recovered
    as "<that state> → OK" — a resolution for an alert that was never sent,
    which on a phone reads as a page you missed. `alerted_state` is the half that
    matches what was actually sent.

    Uses two states of the SAME rank, so the escalation is not involved: the bug
    is about naming, and it has to be fixed for the no-escalation case too."""
    core = core_with(settings, notifier, {"mirror": {"alert_after_s": 3600}})
    job = core.registry.get("mirror")                    # cadence 3600 + grace 600
    core.record_ping(job, {"status": "ok"}, now=NOW)
    core.recompute_all(now=NOW + 4300)                   # silence → LATE
    core.recompute_all(now=NOW + 4300 + 3600)            # ...pages about LATE
    assert titles(notifier) == ["[dashboard] Drive mirror → LATE"]
    assert row(core, "mirror")["alerted_state"] == "LATE"
    # It comes back, but with pending uploads: BEHIND, same rank, no new page.
    core.record_ping(job, {"status": "ok", "metrics": {"pending": 4}},
                     now=NOW + 8000)
    assert row(core, "mirror")["state"] == "BEHIND"
    assert len(notifier.sent) == 1
    # ...then really recovers. The recovery is about the LATE we paged for.
    core.record_ping(job, {"status": "ok", "metrics": {"pending": 0, "mismatch": 0}},
                     now=NOW + 8300)
    core.recompute_all(now=NOW + 8300 + dwell(core, "mirror"))
    assert notifier.sent[-1] == ("[dashboard] Drive mirror → OK",
                                 "mirror: LATE → OK", "default")


def test_an_episode_paged_before_alerted_state_existed_is_recorded_not_re_paged(
        settings, notifier):
    """The upgrade shape: an episode that is open AND paged when the column is
    added, so `alerted_at` is set and `alerted_state` is NULL. Ranked as 0 it
    would read as "worse than nothing" and duplicate the page that already went
    out; ranked at the top it would swallow a real escalation. It is recorded
    instead — from what the episode is about on the next pass — and then behaves
    like any other episode."""
    core = core_with(settings, notifier, {"disk": {"alert_after_s": DISK_A}})
    job = core.registry.get("disk")
    core.record_ping(job, _capacity(10), now=NOW)
    core.recompute_all(now=NOW + DISK_A)
    assert len(notifier.sent) == 1
    conn = core.connect()
    with conn:                                           # the pre-upgrade row
        conn.execute("UPDATE jobs SET alerted_state=NULL WHERE id='disk'")
    conn.close()
    core.recompute_all(now=NOW + DISK_A + 60)
    assert len(notifier.sent) == 1                        # no duplicate page
    assert row(core, "disk")["alerted_state"] == "BEHIND"  # recorded from `about`
    # ...and a genuine escalation still works afterwards.
    core.record_ping(job, UNREADABLE, now=NOW + DISK_A + 300)
    assert notifier.sent[-1] == ("[dashboard] Mac disk → FAIL",
                                 "disk: BEHIND → FAIL", "high")


def test_a_job_that_never_pages_never_escalates_either(settings, notifier):
    """`alert: never` is checked before everything, escalation included — it
    cannot page, so it can have nothing to escalate from."""
    core = core_with(settings, notifier, {})             # every job opted out
    job = core.registry.get("disk")
    core.record_ping(job, _capacity(10), now=NOW)
    core.recompute_all(now=NOW + DISK_A)
    core.record_ping(job, UNREADABLE, now=NOW + DISK_A + 300)
    core.recompute_all(now=NOW + 2 * DISK_A)
    assert notifier.sent == []
    assert row(core, "disk")["alerted_state"] is None


# --------------------------------------------------------------------------- #
# `Episode.damped_failure` — direct coverage
#
# It had NONE (zero occurrences under tests/): every damping test reached it
# through a 1-cycle good run, which the hold survives, so they passed either way.
# It is the flag that makes `dashboard-probes`' own `ok` heartbeat untrustworthy
# while a probed destination is failing, and it is the one hold that applies to
# EVERY state rather than only the destination-driven ones.
# --------------------------------------------------------------------------- #

def _episodes(core, now):
    """The `Episode` list `_recompute_pass` builds, without dispatching."""
    conn = core.connect()
    captured = {}
    original = Core._resolve_alerts

    def spy(self, conn, episodes, states, transitions, now):
        captured["episodes"] = episodes
        return original(self, conn, episodes, states, transitions, now)

    try:
        Core._resolve_alerts = spy
        with conn:
            core._recompute_pass(conn, now)
    finally:
        Core._resolve_alerts = original
        conn.close()
    return {ep.job.id: ep for ep in captured["episodes"]}


def test_damped_failure_is_set_only_for_the_self_job_and_only_while_a_probe_fails(
        settings, notifier):
    """Three things at once, because the flag is a conjunction: it is about the
    SELF job (nothing else writes its own heartbeat), it needs the newest probe
    row of some probed job to be a FAILURE, and it only means anything while the
    self-job reads OK (a FAIL needs no help to hold its episode open)."""
    core = core_with(settings, notifier, {"dashboard-probes": {"alert_after_s": PROBE_A}})
    probe_job = core.registry.get("dashboard-probes")
    core.record_ping(probe_job, {"status": "ok"}, now=NOW)
    core.record_ping(core.registry.get("snap"), {"status": "ok"}, now=NOW)
    _probe(core, "snap", NOW, ok=True, newest_iso=db.to_iso(NOW), count=1)
    eps = _episodes(core, NOW + 60)
    assert eps["dashboard-probes"].state == "OK"
    assert eps["dashboard-probes"].damped_failure is False
    # The destination starts failing. The damping keeps the self-job's own run
    # `ok` (streak 1 of 2), so its STATE is OK — and that OK is not evidence.
    _probe(core, "snap", NOW + 300, ok=False, error="rclone exit 7: rateLimitExceeded")
    core.record_ping(probe_job, {"status": "ok"}, now=NOW + 300)
    eps = _episodes(core, NOW + 360)
    assert eps["dashboard-probes"].state == "OK"
    assert eps["dashboard-probes"].damped_failure is True
    # ...and it is the SELF job's flag alone. `snap` is probed and failing, and
    # its own OK is judged by `dest_unverified`, which is a different question.
    assert eps["snap"].damped_failure is False
    # One good listing clears it: recovery is immediate, by design (PR #8).
    _probe(core, "snap", NOW + 600, ok=True, newest_iso=db.to_iso(NOW + 600), count=1)
    core.record_ping(probe_job, {"status": "ok"}, now=NOW + 600)
    assert _episodes(core, NOW + 660)["dashboard-probes"].damped_failure is False


def test_a_damped_failure_holds_an_episode_about_any_state(settings, notifier):
    """The two holds differ, and this is the difference. `dest_unverified` holds
    only a `STALE_DEST`/`BEHIND` episode — those were statements about the
    destination, and a returning heartbeat settles LATE/FAIL on its own.
    `damped_failure` holds whatever the episode was about, because the heartbeat
    itself is what is suppressing the fact: it cannot settle it."""
    from dashboard.services import Episode
    job = parse_registry(JOBS_DOC).get("dashboard-probes")
    for about in ("LATE", "FAIL", "STALE_DEST", "BEHIND"):
        damped = Episode(job, about, "OK", {}, damped_failure=True)
        assert Core._holding(damped, about) is True
        unverified = Episode(job, about, "OK", {}, dest_unverified=True)
        assert Core._holding(unverified, about) is (about in ("STALE_DEST", "BEHIND"))
    plain = Episode(job, "FAIL", "OK", {})
    assert Core._holding(plain, "FAIL") is False
