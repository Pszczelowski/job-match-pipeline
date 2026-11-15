"""app/statusflow.py (v0.96.0): the transition rules, their refusal texts and the undo of every status the web app can set.
Every test builds its own temporary database; none uses the real config, the real database or the network."""
import pytest

from app import db, statusflow
from app.statusflow import FORWARD, PAGE_ACTIONS, STAGE_PAGES, UNDO_NOTE, UNDO_STATUSES

AFTER_APPLICATION = ("interview", "offer", "employer_rejected", "withdrawn")


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "flow.db")
    yield c
    c.close()


def add_job(conn, status="scored", scored=True):
    with conn:
        jid = conn.execute("INSERT INTO jobs (dedup_key, title, company, status, first_seen_at, last_seen_at) "
                           "VALUES (?, 'Analyst', 'Acme', ?, '2026-11-01T08:00:00Z', '2026-11-01T08:00:00Z')",
                           (f"k{conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]}", status)).lastrowid
        if scored:
            conn.execute("INSERT INTO scores (job_id, prompt_version, model, overall, subscores, created_at) "
                         "VALUES (?, 'v1', 'm', 70, '{}', '2026-11-01T08:00:00Z')", (jid,))
    return jid


def move(conn, jid, to_status, note=None):
    db.set_status(conn, jid, to_status, note=note)


def test_forward_has_every_action_and_only_known_statuses():
    assert set(FORWARD) == {"shortlist", "applied", "interview", "offer", "accept", "employer_rejected", "withdrawn"}
    for action, (to_status, from_statuses) in FORWARD.items():
        assert to_status in db.STATUSES and set(from_statuses) <= set(db.STATUSES), action
        assert to_status not in from_statuses, action


def test_every_from_to_pair_belongs_to_exactly_one_action():
    """K3: a history row (previous status -> new status) identifies the action that wrote it (undo rows are excluded)."""
    seen = {}
    for action, (to_status, from_statuses) in FORWARD.items():
        for start in from_statuses:
            assert (start, to_status) not in seen, f"{(start, to_status)} is reached by {seen[(start, to_status)]} and {action}"
            seen[(start, to_status)] = action


@pytest.mark.parametrize("status", db.STATUSES)
@pytest.mark.parametrize("scored", [True, False])
def test_every_status_and_action_is_allowed_or_refused_with_a_reason(status, scored):
    for action in statusflow.ACTION_ORDER:
        why = statusflow.refusal(action, 7, status, scored)
        assert (why is None) == statusflow.can(action, status, scored) == (action in statusflow.available_actions(status, scored)), (status, action)
        if why is not None:
            assert why.startswith("Job 7") and why.endswith("Nothing was changed."), (status, action, why)


def test_allowed_actions_keeps_the_three_original_buttons_only():
    for status in db.STATUSES:
        for scored in (True, False):
            assert set(statusflow.allowed_actions(status, scored)) <= {"shortlist", "applied", "reject"}
    assert statusflow.allowed_actions("applied", True) == ("reject",)
    assert statusflow.available_actions("applied", True) == ("interview", "employer_rejected", "withdrawn", "reject")
    assert statusflow.available_actions("interview", True) == ("offer", "employer_rejected", "withdrawn")
    assert statusflow.available_actions("offer", True) == ("accept", "employer_rejected", "withdrawn")
    assert statusflow.available_actions("rejected", True) == statusflow.available_actions("withdrawn", True) == ()
    assert statusflow.available_actions("awaiting_llm", True) == ("shortlist", "reject")
    assert statusflow.available_actions("awaiting_llm", False) == ("reject",)


@pytest.mark.parametrize("action,status,text", [
    ("interview", "interview", "is already in the interview stage"),
    ("interview", "scored", "only an applied job can be moved to interview"),
    ("offer", "offer", "already has an offer"),
    ("offer", "applied", "only a job in the interview stage can be moved to offer"),
    ("employer_rejected", "rejected", "is already marked as rejected by the employer"),
    ("employer_rejected", "shortlisted", "can be marked as rejected by the employer"),
    ("withdrawn", "withdrawn", "is already marked as withdrawn"),
    ("withdrawn", "new", "can be marked as withdrawn"),
])
def test_the_refusal_texts_of_the_new_actions(action, status, text):
    why = statusflow.refusal(action, 12, status, True)
    assert text in why and why.startswith("Job 12") and why.endswith("Nothing was changed.")


def test_the_old_refusal_texts_are_unchanged():
    assert statusflow.refusal("shortlist", 3, "shortlisted", True) == "Job 3 is already shortlisted. Nothing was changed."
    assert statusflow.refusal("applied", 3, "scored", True) == ("Job 3 has the status 'scored'; only a shortlisted or approved job "
                                                                "can be marked as applied. Nothing was changed.")


def test_labels_per_page():
    assert [statusflow.label(a) for a in AFTER_APPLICATION] == ["Interview", "Offer", "Rejected by employer", "Withdrawn"]
    assert statusflow.label("withdrawn", "offers") == "Declined"
    assert statusflow.label("employer_rejected", "offers") == "Offer withdrawn by employer"
    assert statusflow.label("withdrawn", "applied") == "Withdrawn" and statusflow.label("offer", "offers") == "Offer"


def test_page_actions_and_stage_pages():
    assert PAGE_ACTIONS == {
        "top": ("shortlist", "applied", "undo", "reject"), "list": ("shortlist", "applied", "undo", "reject"),
        "applied": ("interview", "employer_rejected", "withdrawn", "undo", "reject"),
        "interviews": ("offer", "employer_rejected", "withdrawn", "undo"), "offers": ("accept", "withdrawn", "employer_rejected", "undo"),
        "accepted": ("undo",), "rejected": ("undo",), "withdrawn": ("undo",), "missing": ("reject",)}
    for actions in PAGE_ACTIONS.values():
        assert set(actions) <= set(FORWARD) | {"undo", "reject"}
    assert STAGE_PAGES == {"applied": "applied", "interview": "interviews", "offer": "offers", "accepted": "accepted", "rejected": "rejected", "withdrawn": "withdrawn"}
    assert set(STAGE_PAGES) <= set(db.STATUSES) and set(STAGE_PAGES.values()) <= set(PAGE_ACTIONS)


# --- Undo -------------------------------------------------------------------------------------------------------

def path(conn, *steps, scored=True):
    """A job that walked through the statuses `steps` (the first is where it started); returns its id."""
    jid = add_job(conn, steps[0], scored=scored)
    for step in steps[1:]:
        move(conn, jid, step)
    return jid


@pytest.mark.parametrize("steps,expected", [
    (("scored", "shortlisted"), "scored"),
    (("shortlisted", "applied"), "shortlisted"),
    (("applied", "interview"), "applied"),
    (("interview", "offer"), "interview"),
    (("applied", "rejected"), "applied"),
    (("interview", "rejected"), "interview"),
    (("offer", "rejected"), "offer"),
    (("applied", "withdrawn"), "applied"),
    (("offer", "withdrawn"), "offer"),
])
def test_undo_goes_one_step_back(conn, steps, expected):
    jid = path(conn, *steps)
    status = steps[-1]
    assert status in UNDO_STATUSES
    assert statusflow.undo_target(conn, jid, status, True) == (expected, None)
    info = statusflow.undo_info(conn, jid, status, True)
    assert info == {"from": status, "to": expected, "confirm": status != "shortlisted"}


def test_interview_to_rejected_then_undo_then_interview_is_a_clean_round_trip(conn):
    jid = path(conn, "applied", "interview", "rejected")
    target, why = statusflow.undo_target(conn, jid, "rejected", True)
    assert (target, why) == ("interview", None)
    move(conn, jid, target, note=UNDO_NOTE)
    assert statusflow.undo_target(conn, jid, "interview", True)[0] is None                    # the last row was an undo
    assert "already an undo" in statusflow.undo_target(conn, jid, "interview", True)[1]
    assert statusflow.undo_info(conn, jid, "interview", True) is None


def test_undo_refusals(conn):
    plain = path(conn, "scored", "new")                                                       # not an undoable status
    assert "can be set back here" in statusflow.undo_target(conn, plain, "new", True)[1]
    assert statusflow.undo_info(conn, plain, "new", True) is None
    no_history = add_job(conn, "interview")                                                   # no history row at all
    assert "last recorded status change was not" in statusflow.undo_target(conn, no_history, "interview", True)[1]
    wrong_row = path(conn, "applied", "interview", "offer")
    assert "last recorded status change was not" in statusflow.undo_target(conn, wrong_row, "interview", True)[1]
    with conn:
        no_previous = add_job(conn, "applied")
        conn.execute("INSERT INTO status_history (job_id, from_status, to_status, note, changed_at) VALUES (?, NULL, 'applied', NULL, 'x')", (no_previous,))
    assert "no earlier status" in statusflow.undo_target(conn, no_previous, "applied", True)[1]
    not_allowed = path(conn, "scored", "offer")                                               # scored -> offer: the web app does not allow it
    assert "does not allow 'offer' from there" in statusflow.undo_target(conn, not_allowed, "offer", True)[1]
    assert statusflow.undo_info(conn, not_allowed, "offer", True) is None


def test_undo_of_applied_back_to_approved_needs_both_documents_approved(conn):
    jid = path(conn, "approved", "applied")
    assert "no longer both approved" in statusflow.undo_target(conn, jid, "applied", True)[1]


def test_undo_never_leaves_the_allowed_pairs(conn):
    """Whatever status an undo restores is a status the action may start from."""
    for status, action in UNDO_STATUSES.items():
        for previous in FORWARD[action][1]:
            jid = path(conn, previous, status)
            if previous == "approved":
                continue
            assert statusflow.undo_target(conn, jid, status, True)[0] == previous, (previous, status)


# --- Accepted (v0.98.0) -------------------------------------------------------------------------------------------

def test_accept_is_allowed_only_from_offer():
    for status in db.STATUSES:
        assert statusflow.can("accept", status, True) is (status == "offer"), status
        assert (statusflow.refusal("accept", 7, status, True) is None) is (status == "offer"), status
    assert statusflow.refusal("accept", 7, "accepted", True) == "Job 7 is already marked as accepted. Nothing was changed."
    assert statusflow.refusal("accept", 7, "applied", True) == ("Job 7 has the status 'applied'; only a job with an offer can be marked as accepted. "
                                                              "Nothing was changed.")
    assert statusflow.ACTION_FOR_STATUS["accepted"] == "accept" and statusflow.label("accept") == "Accepted"


def test_an_accepted_job_goes_back_to_offer_with_undo_only(conn):
    jid = add_job(conn, "offer")
    move(conn, jid, "accepted")
    assert statusflow.undo_target(conn, jid, "accepted", True) == ("offer", None)
    assert statusflow.available_actions("accepted", True) == ()                      # no way on, and not 'reject' either (K3 holds)
    move(conn, jid, "offer", note=UNDO_NOTE)
    assert statusflow.undo_target(conn, jid, "offer", True)[0] is None               # one step back only
