"""Drafts nobody comes back to."""
import json
import logging
import threading

import pytest

from src.services.assistant.bl import drafts_bl, sweeps
from src.services.assistant.core import metrics
from src.services.assistant.kinds import alert_set
from src.services.assistant.models.api.draft import ChangeAlertStatusPayload
from src.services.assistant.models.db import Conversation, Draft
from tests.assistant.conftest import OTHER_TENANT, TENANT
from tests.assistant.logs import rendered
from tests.assistant.rows import (
    add_conversation,
    add_draft,
    ago,
    change,
    draft_payload,
    messages,
    read,
)

CANARY = "canary-70a1be"
KIND = "change_alert_status"


@pytest.fixture()
def db(clean_db):
    return clean_db


@pytest.fixture()
def conversation(db):
    return add_conversation(db)


def run() -> None:
    sweeps.run(threading.Event())


def state(db, draft) -> str:
    return read(db, Draft, draft)["state"]


def events(event: str) -> float:
    return metrics.drafts_total.labels(kind=KIND, event=event)._value.get()


def closed() -> float:
    labels = metrics.draft_outcomes_total.labels(kind=KIND, outcome="outcome_unknown")
    return labels._value.get()


def stale() -> float:
    return metrics.drafts_stale_applying._value.get()


def applying(db, conversation, *, since, **values):
    return add_draft(db, conversation, state="applying", updated_at=since, **values)


# --- expiry ---------------------------------------------------------------------------


def test_a_pending_draft_expires(db, conversation):
    before = events("expired")
    old = ago(days=2)
    draft = add_draft(db, conversation, expires_at=ago(seconds=2), updated_at=old)
    run()
    row = read(db, Draft, draft)
    assert row["state"] == "expired"
    assert row["resolved_at"] is not None and row["updated_at"] > old
    assert events("expired") == before + 1


def test_one_that_can_still_be_confirmed_stays(db, conversation):
    draft = add_draft(db, conversation, expires_at=ago(seconds=-30))
    before = read(db, Draft, draft)
    run()
    assert read(db, Draft, draft) == before


@pytest.mark.parametrize(
    "other",
    ["applying", "applied", "partially_applied", "failed", "discarded", "expired"],
)
def test_only_pending_drafts_expire(db, conversation, other):
    draft = add_draft(db, conversation, state=other, expires_at=ago(hours=1))
    before = read(db, Draft, draft)
    run()
    assert read(db, Draft, draft) == before


def test_expiry_is_no_news_to_the_conversation(db):
    used = ago(days=3)
    conversation = add_conversation(db, updated_at=used)
    add_draft(db, conversation, expires_at=ago(hours=1))
    run()
    assert messages(db, conversation) == []
    assert read(db, Conversation, conversation)["updated_at"] == used


def test_drafts_of_every_tenant_expire(db, conversation):
    """The sweep serves all of them, like the period itself."""
    other = add_conversation(db, tenant_id=OTHER_TENANT)
    drafts = [
        add_draft(db, conversation, expires_at=ago(hours=1)),
        add_draft(db, other, tenant_id=OTHER_TENANT, expires_at=ago(hours=1)),
    ]
    run()
    assert [state(db, d) for d in drafts] == ["expired", "expired"]


def test_more_than_one_batch(db, conversation, monkeypatch):
    monkeypatch.setattr(sweeps, "_BATCH", 2)
    before = events("expired")
    drafts = [add_draft(db, conversation, expires_at=ago(hours=1)) for _ in range(5)]
    run()
    assert {state(db, d) for d in drafts} == {"expired"}
    assert events("expired") == before + 5


def test_a_draft_somebody_is_changing_is_left_to_the_next_run(db, conversation):
    busy = add_draft(db, conversation, expires_at=ago(hours=1))
    other = add_draft(db, conversation, expires_at=ago(hours=1))
    with db.connect() as conn, conn.begin():
        conn.execute(Draft.__table__.select().where(Draft.id == busy).with_for_update())
        run()  # would wait, and then fail
        assert (state(db, busy), state(db, other)) == ("pending", "expired")
    run()
    assert state(db, busy) == "expired"


# --- the gauge ------------------------------------------------------------------------


def test_drafts_stuck_in_applying_are_counted(db, conversation, caplog):
    applying(db, conversation, since=ago(minutes=4))
    stuck = [
        applying(db, conversation, since=ago(minutes=6)),
        applying(db, conversation, since=ago(hours=23)),
    ]
    add_draft(db, conversation, updated_at=ago(hours=1))
    add_draft(db, conversation, state="failed", updated_at=ago(hours=1))
    run()
    assert stale() == 2
    assert [state(db, d) for d in stuck] == ["applying", "applying"]
    [line] = [r for r in caplog.records if "stuck in applying" in r.message]
    assert (line.levelname, line.drafts) == ("WARNING", 2)


def test_the_gauge_goes_back(db, conversation, caplog):
    draft = applying(db, conversation, since=ago(hours=1))
    run()
    assert stale() == 1
    change(db, Draft, draft, state="failed")
    caplog.clear()
    run()
    assert stale() == 0
    assert "stuck" not in caplog.text


# --- closed after a day -----------------------------------------------------------------


def test_an_abandoned_draft_is_closed(db, caplog):
    used = ago(days=3)
    conversation = add_conversation(db, updated_at=used)
    draft = applying(db, conversation, since=ago(hours=25))
    before = closed()
    run()
    row = read(db, Draft, draft)
    assert row["state"] == "failed"
    assert row["resolved_at"] is not None
    assert row["outcome"]["code"] == "outcome_unknown"
    assert row["outcome"]["gateway_result"] == "unknown"
    assert row["outcome"]["unconfirmed_count"] == 2
    assert row["outcome"]["finished_at"] is not None
    assert closed() == before + 1
    # It tells the model, and is no activity of the user's.
    [event] = messages(db, conversation)
    assert (event["role"], event["draft_id"]) == ("event", draft)
    assert "outcome_unknown. 0 applied, 0 failed, 2 unconfirmed" in event["content"]
    assert read(db, Conversation, conversation)["updated_at"] == used
    # Closed is not stuck.
    assert stale() == 0
    [line] = [r for r in caplog.records if "closed without a result" in r.message]
    assert (line.tenant_id, line.draft_id) == (TENANT, str(draft))


@pytest.mark.parametrize(("hours", "expected"), [(23.9, "applying"), (24.1, "failed")])
def test_the_boundary(db, conversation, hours, expected):
    draft = applying(db, conversation, since=ago(hours=hours))
    run()
    assert state(db, draft) == expected


@pytest.mark.parametrize("other", ["pending", "applied", "discarded", "expired"])
def test_only_applying_drafts_are_closed(db, conversation, other):
    draft = add_draft(db, conversation, state=other, updated_at=ago(days=3))
    run()
    assert state(db, draft) == other
    assert messages(db, conversation) == []


def test_what_was_known_is_kept(db, conversation):
    """Which alerts needed no change is not forgotten."""
    payload = ChangeAlertStatusPayload.parse_obj(draft_payload("fp-1", "fp-2"))
    settled = {
        "fp-1": alert_set.AlertOutcome(
            fingerprint="fp-1",
            name="name of fp-1",
            result="skipped",
            status_after="acknowledged",
        )
    }
    _, outcome = alert_set.pending(payload, settled, "unknown")
    draft = applying(db, conversation, since=ago(hours=1))
    drafts_bl.keep_applying(
        drafts_bl.DraftKey(draft, TENANT, "ops@example.com", conversation), outcome
    )
    change(db, Draft, draft, updated_at=ago(hours=25))
    run()
    stored = read(db, Draft, draft)["outcome"]
    assert [r["result"] for r in stored["results"]] == ["skipped", "unconfirmed"]
    assert (stored["code"], stored["skipped_count"]) == ("outcome_unknown", 1)


def test_one_that_somebody_settles_meanwhile_keeps_its_result(
    db, conversation, monkeypatch
):
    draft = applying(db, conversation, since=ago(hours=25))
    before = closed()
    load = drafts_bl.load

    def settled_after_the_look(key):
        view = load(key)
        change(db, Draft, draft, state="applied")
        return view

    monkeypatch.setattr(sweeps.drafts_bl, "load", settled_after_the_look)
    run()
    assert state(db, draft) == "applied"
    assert messages(db, conversation) == []
    assert closed() == before


def test_one_that_cannot_be_closed_does_not_keep_the_others(
    db, conversation, monkeypatch, caplog
):
    broken = applying(db, conversation, since=ago(hours=26))
    other = applying(db, conversation, since=ago(hours=25))
    errors = metrics.internal_errors_total.labels(site="sweep_close")
    before = errors._value.get()
    resolve = drafts_bl.resolve

    def failing_for_one(key, **values):
        if key.id == broken:
            raise RuntimeError(CANARY)
        return resolve(key, **values)

    monkeypatch.setattr(sweeps.drafts_bl, "resolve", failing_for_one)
    with caplog.at_level(logging.DEBUG):
        run()
    assert (state(db, broken), state(db, other)) == ("applying", "failed")
    assert errors._value.get() == before + 1
    assert not [r for r in caplog.records if CANARY in rendered(r)]


def stored_by_a_newer_version(**values) -> dict:
    return {
        "code": "name_taken",
        "message": "A preset with this name exists.",
        "gateway_result": "rejected",
        "applied_count": 0,
        "failed_count": 0,
        "unconfirmed_count": 0,
        "skipped_count": 0,
        "not_found_count": 0,
        "results": [],
        "finished_at": None,
        "object": {"type": "preset", "id": "a-preset-id"},
        "confirm_state": {"sent_at": "2026-11-26T09:59:59+00:00", "step": 1},
        **values,
    }


UNREADABLE = {
    "kind": {"kind": "from_the_future", "outcome": stored_by_a_newer_version()},
    "payload": {"payload": {"kind": CANARY}},
    "outcome": {
        "outcome": stored_by_a_newer_version(
            code="outcome_pending", gateway_result="conflict"
        )
    },
    # Known otherwise: of an object this version cannot read, it could claim that
    # nothing was made.
    "object_type": {
        "outcome": stored_by_a_newer_version(
            code="outcome_pending",
            gateway_result="unknown",
            object={"type": "silence", "id": "a-silence-id"},
        )
    },
    "object": {
        "outcome": stored_by_a_newer_version(
            code="outcome_pending", gateway_result="unknown", object={"type": "preset"}
        )
    },
}


@pytest.mark.parametrize("unreadable", list(UNREADABLE.values()), ids=list(UNREADABLE))
def test_what_this_version_cannot_read_is_closed_generically(
    db, conversation, caplog, unreadable
):
    """An unknown kind, a payload or an outcome this version cannot read: closed all
    the same, keeping what was stored."""
    draft = applying(db, conversation, since=ago(hours=25), **unreadable)
    kind = read(db, Draft, draft)["kind"]
    errors = metrics.internal_errors_total.labels(site="sweep_close")
    before = errors._value.get()
    with caplog.at_level(logging.DEBUG):
        run()
    row = read(db, Draft, draft)
    assert row["state"] == "failed" and row["resolved_at"] is not None
    stored = row["outcome"]
    assert (stored["code"], stored["message"], stored["gateway_result"]) == (
        "outcome_unknown",
        "The result of this change could not be determined.",
        "unknown",
    )
    assert stored["results"] == [] and stored["finished_at"] is not None
    for count in ("applied", "failed", "unconfirmed", "skipped", "not_found"):
        assert stored[f"{count}_count"] == 0
    if "outcome" in unreadable:
        assert stored["object"] == unreadable["outcome"]["object"]
        assert stored["confirm_state"]["step"] == 1
    [event] = messages(db, conversation)
    assert event["content"].startswith(f"draft {draft} ({kind}")
    assert event["content"].endswith(
        "): outcome_unknown. 0 applied, 0 failed, 0 unconfirmed, 0 skipped, 0 not found."
    )
    assert errors._value.get() == before
    assert not [r for r in caplog.records if CANARY in rendered(r)]


def test_bad_rows_never_keep_a_good_one(db, conversation):
    """More unreadable rows than a run takes, all older than the one good row."""
    bad = [
        applying(
            db,
            conversation,
            since=ago(hours=26, seconds=n),
            **list(UNREADABLE.values())[n % len(UNREADABLE)],
        )
        for n in range(201)
    ]
    good = applying(db, conversation, since=ago(hours=25))
    run()
    run()
    assert {state(db, d) for d in bad} == {"failed"}
    assert state(db, good) == "failed"
    assert read(db, Draft, good)["outcome"]["unconfirmed_count"] == 2
    # What was stored stays.
    for draft in bad:
        stored = read(db, Draft, draft)["outcome"]
        assert stored["code"] == "outcome_unknown"
        if "object" in stored:
            assert stored["confirm_state"]["step"] == 1


def test_the_kinds_own_keys_stay(db, conversation):
    """A close by the kind merges too: `confirm_state`, and keys of a newer version."""
    payload = ChangeAlertStatusPayload.parse_obj(draft_payload("fp-1", "fp-2"))
    _, outcome = alert_set.pending(payload, {}, "unknown")
    kept = {
        "confirm_state": {"sent_at": "2026-11-26T09:59:59+00:00", "step": 0},
        "key_of_a_newer_version": [1, 2],
    }
    draft = applying(
        db,
        conversation,
        since=ago(hours=25),
        outcome={**json.loads(outcome.json()), **kept},
    )
    run()
    stored = read(db, Draft, draft)["outcome"]
    assert (stored["code"], stored["unconfirmed_count"]) == ("outcome_unknown", 2)
    assert {key: stored[key] for key in kept} == kept


def test_several_tenants_at_once(db, conversation):
    other = add_conversation(db, tenant_id=OTHER_TENANT, created_by="other@example.com")
    mine = applying(db, conversation, since=ago(hours=25))
    theirs = applying(
        db,
        other,
        since=ago(hours=25),
        tenant_id=OTHER_TENANT,
        created_by="other@example.com",
    )
    run()
    assert (state(db, mine), state(db, theirs)) == ("failed", "failed")
    assert [m["tenant_id"] for m in messages(db, conversation)] == [TENANT]
    assert [m["tenant_id"] for m in messages(db, other)] == [OTHER_TENANT]


# --- a run ------------------------------------------------------------------------------


@pytest.mark.parametrize("failing", ["_expire", "_report_stale", "_close_abandoned"])
def test_a_failing_step_does_not_keep_the_others(
    db, conversation, monkeypatch, caplog, failing
):
    pending = add_draft(db, conversation, expires_at=ago(hours=1))
    stuck = applying(db, conversation, since=ago(hours=1))
    abandoned = applying(db, conversation, since=ago(hours=25))
    metrics.drafts_stale_applying.set(0)

    def fail(stop):
        raise RuntimeError(CANARY)

    monkeypatch.setattr(sweeps, failing, fail)
    with caplog.at_level(logging.DEBUG):
        run()
    assert (state(db, pending) == "expired") is (failing != "_expire")
    assert (stale() == 1) is (failing != "_report_stale")
    assert (state(db, abandoned) == "failed") is (failing != "_close_abandoned")
    assert state(db, stuck) == "applying"
    [line] = [r for r in caplog.records if "draft sweep failed" in r.message]
    assert line.levelname == "ERROR"
    assert not [r for r in caplog.records if CANARY in rendered(r)]


def test_a_stopped_run_does_nothing_more(db, conversation):
    drafts = [
        add_draft(db, conversation, expires_at=ago(hours=1)),
        applying(db, conversation, since=ago(hours=25)),
    ]
    stop = threading.Event()
    stop.set()
    sweeps.run(stop)
    assert [state(db, d) for d in drafts] == ["pending", "applying"]


def test_a_run_ends_between_batches(db, conversation, monkeypatch):
    monkeypatch.setattr(sweeps, "_BATCH", 1)
    drafts = [add_draft(db, conversation, expires_at=ago(hours=1)) for _ in range(3)]
    stop = threading.Event()
    counted = metrics.drafts_total

    class Stopping:
        def labels(self, **labels):
            stop.set()
            return counted.labels(**labels)

    monkeypatch.setattr(sweeps.metrics, "drafts_total", Stopping())
    sweeps.run(stop)
    assert sorted(state(db, d) for d in drafts) == ["expired", "pending", "pending"]


def test_a_run_ends_between_abandoned_drafts(db, conversation, monkeypatch):
    drafts = [applying(db, conversation, since=ago(hours=h)) for h in (27, 26, 25)]
    stop = threading.Event()
    close = sweeps._close

    def stopping(row):
        close(row)
        stop.set()

    monkeypatch.setattr(sweeps, "_close", stopping)
    sweeps.run(stop)
    assert [state(db, d) for d in drafts] == ["failed", "applying", "applying"]


def test_two_workers_at_once(db, conversation):
    """Without a lock: each draft is expired, and closed, once."""
    for _ in range(4):
        add_draft(db, conversation, expires_at=ago(hours=1))
        applying(db, conversation, since=ago(hours=25))
    before = events("expired"), closed()
    threads = [threading.Thread(target=run) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert (events("expired"), closed()) == (before[0] + 4, before[1] + 4)
    assert len(messages(db, conversation)) == 4
