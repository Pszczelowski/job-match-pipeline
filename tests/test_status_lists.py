"""K4 (v0.96.0): every status of `db.STATUSES` is decided in every hand-written status list of the code. When a status is added
(for example `accepted` in a later migration), the test of the list it was left out of fails until the list is decided.
Each list is a pair here: the statuses the list holds and, with a comment, the ones it deliberately does not."""
from app import analytics, db, descriptions, merge, rejection, report, statusflow

# Each entry: (the list as the code has it, the statuses it deliberately leaves out and why)
AFTER_APPLICATION = {"applied", "interview", "offer", "accepted", "rejected", "withdrawn"}


def decided(name, held, left_out):
    held, left_out = set(held), set(left_out)
    assert not held & left_out, f"{name}: a status is both in the list and in the not-list"
    assert set(db.STATUSES) <= held | left_out, f"{name}: undecided {sorted(set(db.STATUSES) - held - left_out)}"
    assert held <= set(db.STATUSES), f"{name}: unknown {sorted(held - set(db.STATUSES))}"


def test_report_acted():
    """The Top 10 hides what the user acted on: every status from the application on."""
    decided("report.ACTED", report.ACTED, {
        "new", "awaiting_llm", "scored", "not_selected", "location_failed",        # the pipeline's own states, still open
        "shortlisted", "approved"})                                              # prepared but not yet applied for: stay on the Top 10
    assert set(report.ACTED) == AFTER_APPLICATION


def test_descriptions_user_statuses():
    """Jobs the user moved are protected from the language and Poland rules."""
    decided("descriptions.USER_STATUSES", descriptions.USER_STATUSES, {
        "new", "awaiting_llm", "scored", "not_selected", "location_failed"})     # the rules may still change these
    assert set(descriptions.USER_STATUSES) == {"shortlisted", "approved"} | AFTER_APPLICATION


def test_merge_ranks():
    decided("merge.STATUS_RANK", merge.STATUS_RANK, set())                       # every status has a rank, so the furthest job is kept
    assert merge.STATUS_RANK["interview"] > merge.STATUS_RANK["applied"] > merge.STATUS_RANK["shortlisted"]


def test_analytics_groups():
    decided("analytics.BY_STATUS", analytics.BY_STATUS, set())                   # every status belongs to a group of the funnel
    assert set(analytics.APPLIED_EVER) == AFTER_APPLICATION


def test_analytics_responded():
    decided("analytics.RESPONDED", analytics.RESPONDED, {
        "new", "awaiting_llm", "scored", "not_selected", "location_failed", "shortlisted", "approved",   # not applied for yet
        "applied",                                                                                      # applied, no answer yet
        "withdrawn"})                                                                                    # the user's own decision, not an answer


def test_rejectable_statuses():
    """`reject` (archive) is offered from these; the others are not archived by a button (a rejected job keeps its history, N2)."""
    decided("rejection.REJECT_FROM", rejection.REJECT_FROM, {
        "interview", "offer", "accepted", "rejected", "withdrawn"})              # further along than applied: not archived by a button
    assert statusflow.REJECT_FROM == rejection.REJECT_FROM


def test_restorable_statuses():
    """`restore-job` brings an archived job back to its status if it is here, else as `new`."""
    decided("db.RESTORED_STATUSES", db.RESTORED_STATUSES, {
        "new", "awaiting_llm", "scored", "not_selected", "location_failed",      # come back as new and are scored again
        "shortlisted", "approved",                                               # come back as new: the documents are not restored
        "rejected", "withdrawn"})                                                # not archived by a button (N3), so nothing to restore


def test_statusflow_knows_every_status():
    """Every status is either a target of an action (it can be set in the web app), or one the pipeline sets."""
    targets = {to for to, _ in statusflow.FORWARD.values()}
    pipeline_only = {"new", "awaiting_llm", "scored", "not_selected", "location_failed",   # set by the pipeline and the scorer
                     "approved"}                                                           # set by approving both documents
    decided("statusflow targets", targets, pipeline_only)
    assert targets == set(statusflow.UNDO_STATUSES)
    assert set(statusflow.STAGE_PAGES) == AFTER_APPLICATION

