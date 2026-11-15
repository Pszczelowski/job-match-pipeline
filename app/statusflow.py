"""The status rules of the web buttons: which action a job can take from its status, why it is refused, and the undo of a status.
Moved out of app/web.py (v0.96.0); app/web.py re-exports the names. Since v0.96.0 it also holds the stages after the application."""
from __future__ import annotations

import sqlite3

from . import rejection
from .documents import KINDS

# Which buttons a job gets, by status (executor decisions, goal.md 5.7): Shortlist from scored or not_selected (awaiting_llm only
# when it already has a score, i.e. it waits to be scored again); Applied from shortlisted or approved; Reject from any status
# before the application, and (decision of 10 Nov 2026) from applied; a job that was applied for and is further along is not
# archived by a button. From v0.96.0 the stages after the application are separate actions: Interview from applied, Offer from
# interview, and "rejected by the employer" and "withdrawn" from applied, interview or offer (an employer rejection is not an archive).
FORWARD = {                                                # action -> (the status it leads to, the statuses it may start from)
    "shortlist": ("shortlisted", ("scored", "not_selected")),
    "applied": ("applied", ("shortlisted", "approved")),
    "interview": ("interview", ("applied",)),
    "offer": ("offer", ("interview",)),
    "accept": ("accepted", ("offer",)),
    "employer_rejected": ("rejected", ("applied", "interview", "offer")),
    "withdrawn": ("withdrawn", ("applied", "interview", "offer")),
}
ACTION_FOR_STATUS = {to: action for action, (to, _) in FORWARD.items()}      # the status a button leads to -> its action (one each, K3)
SHORTLIST_FROM = FORWARD["shortlist"][1]
APPLIED_FROM = FORWARD["applied"][1]
REJECT_FROM = rejection.REJECT_FROM
ACTION_ORDER = tuple(FORWARD) + ("reject",)                 # the order buttons and links are shown in
BUTTON_ACTIONS = ("shortlist", "applied", "reject")         # the actions that have their own per-job POST route and button (allowed_actions)
LABELS = {"shortlist": "Shortlist", "applied": "Applied", "interview": "Interview", "offer": "Offer", "accept": "Accepted",
          "employer_rejected": "Rejected by employer", "withdrawn": "Withdrawn"}
PAGE_LABELS = {"offers": {"withdrawn": "Declined", "employer_rejected": "Offer withdrawn by employer"}}   # per page, over LABELS

# The actions each list page offers (the bulk bar and the bulk route): `undo` and `reject` are not status actions of FORWARD.
PAGE_ACTIONS = {
    "top": ("shortlist", "applied", "undo", "reject"),
    "list": ("shortlist", "applied", "undo", "reject"),
    "applied": ("interview", "employer_rejected", "withdrawn", "undo", "reject"),
    "interviews": ("offer", "employer_rejected", "withdrawn", "undo"),
    "offers": ("accept", "withdrawn", "employer_rejected", "undo"),
    "accepted": ("undo",),
    "rejected": ("undo",),
    "withdrawn": ("undo",),
    "missing": ("reject",),
}
STAGE_PAGES = {"applied": "applied", "interview": "interviews", "offer": "offers", "accepted": "accepted", "rejected": "rejected", "withdrawn": "withdrawn"}   # status -> page
NEXT_INTERVIEW_PAGES = ("interviews",)      # the stage pages with the "Next interview" column (v0.99.0, decided 15 Nov 2026); one place to change
# The swipe of a row on a phone (v0.97.0, N6): per page (lead, trail), the actions that can sit behind the row. Lead is the swipe to the right (one
# forward action), trail the swipe to the left (the destructive or last one outermost); the first one the row allows is shown, the other actions of the
# row go under "More". select.js reads this from the markup (`data-swipe-lead`, `data-swipe-trail`) and holds no action name itself.
CAREFUL = ("reject", "employer_rejected", "withdrawn", "undo")      # the actions whose sheet puts the focus on Cancel (select.js reads `data-careful`)
SWIPE = {
    "top": (("shortlist", "applied"), ("reject",)),
    "list": (("shortlist", "applied"), ("reject",)),
    "applied": (("interview",), ("employer_rejected",)),
    "interviews": (("offer",), ("employer_rejected",)),
    "offers": (("accept",), ("withdrawn",)),
    "accepted": ((), ("undo",)),
    "rejected": ((), ("undo",)),
    "withdrawn": ((), ("undo",)),
    "missing": ((), ("reject",)),
}
APPLICATION_PAGES = tuple(STAGE_PAGES.values())          # the pages of the stages, in the order of the menu
STAGE_PAGE_TEXT = {                                       # page -> (title and menu text, text of the empty page); one table drives the routes, the menu and the pages
    "applied": ("Applied", "You have not marked any job as applied yet."),
    "interviews": ("Interviews", "No job is in the interview stage."),
    "offers": ("Offers", "No job has an offer."),
    "accepted": ("Accepted", "You have not accepted any offer yet."),
    "rejected": ("Rejected by employer", "No employer has rejected an application yet."),
    "withdrawn": ("Withdrawn", "You have not withdrawn any application."),
}

# Undo of a status (v0.49.0, executor decisions, goal.md 5.7): one step back, and only to a status from which the web app
# itself allows reaching the current one (`can`). Since v0.96.0 this holds for the stages after the application as well.
UNDO_STATUSES = {"shortlisted": "shortlist", "applied": "applied", "interview": "interview", "offer": "offer", "accepted": "accept",
                 "rejected": "employer_rejected", "withdrawn": "withdrawn"}      # status -> the action that leads to it
UNDO_NOTE = "web undo"
UNDO_REFUSAL_WHO = "only a job that is " + ", ".join(list(UNDO_STATUSES)[:-1]) + " or " + list(UNDO_STATUSES)[-1]


def label(action: str, page: str | None = None) -> str:
    """The button text of an action on a page (the Offers page words Withdrawn and Rejected by employer differently)."""
    return PAGE_LABELS.get(page, {}).get(action) or LABELS[action]


def can(action: str, status: str, scored: bool) -> bool:
    """True if the action may be done on a job with this status."""
    if action == "reject":
        return status in REJECT_FROM
    if action == "shortlist":
        return status in SHORTLIST_FROM or (status == "awaiting_llm" and scored)
    return action in FORWARD and status in FORWARD[action][1]


def available_actions(status: str, scored: bool) -> tuple[str, ...]:
    """Every action a job with this status may take, in `ACTION_ORDER` (the confirm page and the bulk route use this)."""
    return tuple(a for a in ACTION_ORDER if can(a, status, scored))


def allowed_actions(status: str, scored: bool) -> tuple[str, ...]:
    """The buttons a job with this status shows: any of `shortlist`, `applied`, `reject` (the actions with a per-job POST route)."""
    return tuple(a for a in BUTTON_ACTIONS if can(a, status, scored))


def refusal(action: str, job_id: int, status: str, scored: bool) -> str | None:
    """Why the action cannot be done on a job with this status, or None when it can."""
    if can(action, status, scored):
        return None
    if action == "shortlist":
        if status == "shortlisted":
            return f"Job {job_id} is already shortlisted. Nothing was changed."
        if status == "awaiting_llm":
            return f"Job {job_id} is waiting for its first score and cannot be shortlisted yet. Nothing was changed."
        return f"Job {job_id} has the status '{status}'; only a scored job can be shortlisted. Nothing was changed."
    if action == "applied":
        if status == "applied":
            return f"Job {job_id} is already marked as applied. Nothing was changed."
        return f"Job {job_id} has the status '{status}'; only a shortlisted or approved job can be marked as applied. Nothing was changed."
    if action == "interview":
        if status == "interview":
            return f"Job {job_id} is already in the interview stage. Nothing was changed."
        return f"Job {job_id} has the status '{status}'; only an applied job can be moved to interview. Nothing was changed."
    if action == "offer":
        if status == "offer":
            return f"Job {job_id} already has an offer. Nothing was changed."
        return f"Job {job_id} has the status '{status}'; only a job in the interview stage can be moved to offer. Nothing was changed."
    if action == "accept":
        if status == "accepted":
            return f"Job {job_id} is already marked as accepted. Nothing was changed."
        return f"Job {job_id} has the status '{status}'; only a job with an offer can be marked as accepted. Nothing was changed."
    if action == "employer_rejected":
        if status == "rejected":
            return f"Job {job_id} is already marked as rejected by the employer. Nothing was changed."
        return (f"Job {job_id} has the status '{status}'; only an applied job, or one in interview or offer, "
                "can be marked as rejected by the employer. Nothing was changed.")
    if action == "withdrawn":
        if status == "withdrawn":
            return f"Job {job_id} is already marked as withdrawn. Nothing was changed."
        return (f"Job {job_id} has the status '{status}'; only an applied job, or one in interview or offer, "
                "can be marked as withdrawn. Nothing was changed.")
    return rejection.refusal(job_id, status)


def undo_target(conn: sqlite3.Connection, job_id: int, status: str, scored: bool) -> tuple[str | None, str | None]:
    """(the status an undo would restore, None) or (None, why the status cannot be undone). One step only: the last row of
    `status_history` must be the change to the current status (not itself an undo), must say where the job came from, and that
    status must be one the web app allows the current status to be reached from."""
    if status not in UNDO_STATUSES:
        return None, (f"Job {job_id} has the status '{status}'; {UNDO_REFUSAL_WHO} can be set back here. "
                      "Nothing was changed.")
    last = conn.execute("SELECT from_status, to_status, note FROM status_history WHERE job_id = ? ORDER BY id DESC LIMIT 1",
                        (job_id,)).fetchone()
    if last is None or last["to_status"] != status:
        return None, (f"Job {job_id} is '{status}', but its last recorded status change was not the change to '{status}', "
                      "so the app does not know what to go back to. Nothing was changed.")
    if last["note"] == UNDO_NOTE:
        return None, f"The last change on job {job_id} was already an undo; the app goes back one step only. Nothing was changed."
    previous = last["from_status"]
    if not previous:
        return None, f"Job {job_id} has no earlier status recorded, so there is nothing to go back to. Nothing was changed."
    if not can(UNDO_STATUSES[status], previous, scored):
        return None, (f"Job {job_id} was '{previous}' before, and the web app does not allow '{status}' from there, "
                      "so it will not go back to it. Nothing was changed.")
    if previous == "approved":                                  # 'approved' means both documents are approved (goal.md 5.6)
        states = {r["kind"]: r["status"] for r in conn.execute("SELECT kind, status FROM documents WHERE job_id = ?", (job_id,))}
        if not all(states.get(k) == "approved" for k in KINDS):
            return None, (f"Job {job_id} was 'approved' before, but its documents are no longer both approved, so it cannot go "
                          "back to that status. Approve them again first. Nothing was changed.")
    return previous, None


def undo_info(conn: sqlite3.Connection, job_id: int, status: str, scored: bool) -> dict | None:
    """What the Undo button of a job shows, or None when there is nothing to undo (the button is shown only when it would work)."""
    if status not in UNDO_STATUSES:
        return None
    target, _ = undo_target(conn, job_id, status, scored)
    if target is None:
        return None
    return {"from": status, "to": target, "confirm": status != "shortlisted"}       # every stage but Shortlisted asks first
