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

from dashboard import db, probes
from dashboard.probes import ProbeResult
from dashboard.registry import parse_registry
from dashboard.services import Core
from tests.conftest import JOBS_DOC, pin_created_at

NOW = 1_800_000_000.0
DAY = 86400
# Seconds of unbroken OK an episode needs before it counts as over, for every
# threshold long enough to hit the cap (see services.ok_dwell_s).
MIN_DWELL = 300
# Shortest gap between two ntfy attempts for the same episode (services).
RETRY_MIN = 300


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
    core.recompute_all(now=NOW + 300 + MIN_DWELL)        # OK held → episode over
    assert row(core, "tree")["bad_since"] is None and row(core, "tree")["alerted_at"] is None
    # Episode 2: past the threshold → one page, then one recovery.
    core.record_ping(job, {"status": "fail"}, now=NOW + 900)
    core.recompute_all(now=NOW + 900 + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]
    core.record_ping(job, {"status": "ok"}, now=NOW + 1000 + DAY)
    assert len(notifier.sent) == 1                       # the OK has to be held first
    core.recompute_all(now=NOW + 1000 + DAY + MIN_DWELL)  # episode closes → one recovery
    assert titles(notifier)[-1] == "[dashboard] Tree copy → OK"
    assert notifier.sent[-1][1] == "tree: FAIL → OK" and notifier.sent[-1][2] == "default"
    assert row(core, "tree")["bad_since"] is None and row(core, "tree")["alerted_at"] is None
    for i in range(1, 10):                               # ...and only one
        core.recompute_all(now=NOW + 1000 + DAY + MIN_DWELL + 60 * i)
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
    """Muting the flaps must not mute the real thing that follows them."""
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
    while t < broke + 21600:
        core.record_ping(job, {"status": "fail"}, now=t)
        t += 300
    assert notifier.sent == []                           # 21300 s in: still holding fire
    core.record_ping(job, {"status": "fail"}, now=broke + 21600)
    assert titles(notifier) == ["[dashboard] Probe cycle → FAIL"]
    assert notifier.sent[0][1] == "dashboard-probes: FAIL for over 6h"


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
    core.recompute_all(now=NOW + 30_000 + MIN_DWELL)     # OK held: the episode ends
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
    core.recompute_all(now=NOW + DAY + 3600 + MIN_DWELL)
    assert titles(notifier)[-1] == "[dashboard] Tree copy → OK"


def test_the_retry_backoff_never_delays_a_new_episodes_first_page(settings, notifier):
    """The backoff is keyed on the EPISODE, not the job. A failed push must not
    hold up the first page of whatever breaks next — that would be the
    threshold quietly getting longer after every ntfy hiccup."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": 0}})
    job = core.registry.get("tree")
    notifier.fail = True
    core.record_ping(job, {"status": "fail"}, now=NOW)   # episode 1: POST fails
    notifier.fail = False
    assert notifier.attempts and notifier.sent == []
    assert row(core, "tree")["alerted_at"] is None
    core.record_ping(job, {"status": "ok"}, now=NOW + 30)     # ...and it recovers
    assert row(core, "tree")["bad_since"] is None        # dwell is 0 at a 0 threshold
    # A brand-new failure well inside the 5-minute retry window pages at once.
    core.record_ping(job, {"status": "fail"}, now=NOW + 60)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]


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
        core.recompute_all(now=NOW + DAY + 1 + MIN_DWELL)   # OK held: episode closed
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
    _cycle(core, monkeypatch, NOW + 600, ok=True)        # one good listing is enough
    assert row(core, "dashboard-probes")["state"] == "OK"
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
    while t < NOW + 1140 + 180:                          # 3 min up: longer than the dwell
        core.record_ping(job, up, now=t)
        t += 60
    assert titles(notifier) == ["[dashboard] Containers → FAIL",
                                "[dashboard] Containers → OK"]
    assert notifier.sent[0][1] == "containers: FAIL for over 20m"
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
    core.recompute_all(now=NOW + 300 + MIN_DWELL)
    assert row(core, "dashboard-probes")["bad_since"] is None
    assert notifier.sent == []
    # Hours later it breaks for good: the clock starts NOW, not back at the blip.
    t = NOW + 4 * 3600
    while t < NOW + 4 * 3600 + 21600:
        core.record_ping(job, {"status": "fail"}, now=t)
        t += 300
    assert notifier.sent == []
    core.record_ping(job, {"status": "fail"}, now=NOW + 4 * 3600 + 21600)
    assert titles(notifier) == ["[dashboard] Probe cycle → FAIL"]


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
    nothing, and that is exactly the shape that makes a job un-pageable."""
    core = core_with(settings, notifier, {"tree": {"alert_after_s": DAY}})
    job = core.registry.get("tree")
    conn = core.connect()
    with conn:
        conn.execute("UPDATE jobs SET bad_since=NULL, alerted_at=? WHERE id='tree'",
                     (db.to_iso(NOW),))
    conn.close()
    core.recompute_all(now=NOW + 10)
    assert row(core, "tree")["alerted_at"] is None       # healed, with no recovery sent
    assert notifier.sent == []
    # ...and the job pages normally on the next real episode.
    core.record_ping(job, {"status": "fail"}, now=NOW + 20)
    core.recompute_all(now=NOW + 20 + DAY)
    assert titles(notifier) == ["[dashboard] Tree copy → FAIL"]


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
    quiet.recompute_all(now=NOW + DAY + 60 + MIN_DWELL)
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
