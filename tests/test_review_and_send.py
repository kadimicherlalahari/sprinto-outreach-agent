from email.message import EmailMessage

import pytest

from sprinto_agent import review
from sprinto_agent.config import Settings
from sprinto_agent.util import content_hash, now_iso

INBOX = "operator@inbox.example"


def make_draft(store):
    ts = now_iso()
    run = store.insert("research_runs", {"is_mock": True, "status": "completed", "started_at": ts, "input_json": "{}", "icp_version": "t"})
    co = store.insert("companies", {"run_id": run, "name": "Acme", "domain": "acme.example", "found_via": "test",
                                    "stage1_status": "shortlisted", "stage1_reason": "", "pages_fetched": 0,
                                    "created_at": ts, "updated_at": ts})
    poc = store.insert("pocs", {"company_id": co, "full_name": "Priya Test", "title": "CTO", "email": "priya@acme.example",
                                "role_label": "CTO / VP Engineering", "source": "hunter", "source_detail_json": "{}",
                                "verification_status": "verified", "verification_json": "{}", "created_at": ts})
    s, b = "Acme and compliance", "Hi Priya,\n\nBody."
    return store.insert("drafts", {"company_id": co, "poc_id": poc, "run_id": run, "original_subject": s, "original_body": b,
                                   "current_subject": s, "current_body": b, "current_hash": content_hash(s, b), "version": 1,
                                   "status": "pending_review", "approved_hash": None, "hook_evidence_id": None,
                                   "pain_points_json": "[]", "guidance_json": "[]", "composer": "template",
                                   "created_at": ts, "updated_at": ts})


class FakeSMTP:
    sent = []

    def __init__(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg, to_addrs=None):
        FakeSMTP.sent.append((msg, to_addrs))


def live(**k):
    base = dict(mock=False, send_mode="live", send_transport="smtp", smtp_user="me@inbox.example", smtp_password="x", operator_inbox=INBOX)
    base.update(k)
    return Settings(**base)


def test_unreviewed_and_rejected_drafts_cannot_send(store):
    did = make_draft(store)
    with pytest.raises(review.SendBlocked, match="only approved"):
        review.send(store, Settings(mock=True, operator_inbox=INBOX), did)
    review.reject(store, did, "op", "not a fit")
    with pytest.raises(review.SendBlocked, match="rejected"):
        review.send(store, Settings(mock=True, operator_inbox=INBOX), did)


def test_edit_after_approval_requires_reapproval(store):
    did = make_draft(store)
    d = review.approve(store, did, "op", recipient=INBOX)
    assert d["status"] == "approved"
    d = review.edit(store, did, "Acme and compliance", "Hi Priya,\n\nEdited body.", "op")
    assert d["status"] == "pending_review" and d["approved_hash"] is None and d["version"] == 2
    with pytest.raises(review.SendBlocked):
        review.send(store, Settings(mock=True, operator_inbox=INBOX), did)
    review.approve(store, did, "op", expected_hash=d["current_hash"], recipient=INBOX)
    out = review.send(store, Settings(mock=True, operator_inbox=INBOX), did)
    assert out["status"] == "dry_run" and "Edited body." in out["preview"]


def test_stale_approval_rejected(store):
    did = make_draft(store)
    old = store.draft(did)["current_hash"]
    review.edit(store, did, "New subject", "Hi Priya,\n\nNew.", "op")
    with pytest.raises(review.ReviewError, match="changed"):
        review.approve(store, did, "op", expected_hash=old)


def test_tampered_content_after_approval_blocked(store):
    did = make_draft(store)
    review.approve(store, did, "op", recipient=INBOX)
    store.update("drafts", did, {"current_body": "sneaky change"})  # bypassing edit()
    with pytest.raises(review.SendBlocked, match="differs"):
        review.send(store, Settings(mock=True, operator_inbox=INBOX), did)


def test_history_keeps_original_final_decision_timestamp_run(store):
    did = make_draft(store)
    review.edit(store, did, "S2", "Hi Priya,\n\nB2", "alice", "tone")
    review.approve(store, did, "alice", recipient=INBOX)
    hist = store.reviews_for(did)
    assert [h["decision"] for h in hist] == ["edit", "approve"]
    assert all(h["decided_at"] and h["run_id"] and h["content_hash"] for h in hist)
    d = store.draft(did)
    assert d["original_body"] == "Hi Priya,\n\nBody." and d["current_body"] == "Hi Priya,\n\nB2"


def test_dry_run_is_default_and_sends_nothing(store):
    did = make_draft(store)
    review.approve(store, did, "op", recipient=INBOX)
    s = Settings(mock=False, operator_inbox=INBOX)          # default send_mode
    assert s.send_mode == "dry-run"
    out = review.send(store, s, did, smtp_factory=lambda: (_ for _ in ()).throw(AssertionError("SMTP used")))
    assert out["status"] == "dry_run"
    assert store.sends_for(did)[0]["status"] == "dry_run"


def test_live_send_only_to_operator_inbox(store):
    did = make_draft(store)
    review.approve(store, did, "op", recipient=INBOX)
    FakeSMTP.sent = []
    out = review.send(store, live(), did, smtp_factory=FakeSMTP)
    assert out["status"] == "sent"
    msg, to = FakeSMTP.sent[0]
    assert to == [INBOX] and msg["To"] == INBOX and msg["Cc"] is None and msg["Bcc"] is None
    assert "priya@acme.example" not in msg.as_string()
    with pytest.raises(review.SendBlocked, match="already"):
        review.send(store, live(), did, smtp_factory=FakeSMTP)


def test_recipient_guard_blocks_extra_recipients():
    m = EmailMessage()
    m["To"] = INBOX
    m["Cc"] = "prospect@acme.example"
    with pytest.raises(review.SendBlocked, match="recipient guard"):
        review.assert_only_operator(m, INBOX)
    m2 = EmailMessage()
    m2["To"] = "prospect@acme.example"
    with pytest.raises(review.SendBlocked):
        review.assert_only_operator(m2, INBOX)


def test_send_blocked_without_inbox_or_credentials(store):
    did = make_draft(store)
    review.approve(store, did, "op", recipient=INBOX)
    with pytest.raises(review.SendBlocked, match="OPERATOR_INBOX"):
        review.send(store, Settings(mock=True, operator_inbox=None), did)
    with pytest.raises(review.SendBlocked, match="SMTP"):
        review.send(store, live(smtp_password=None), did, smtp_factory=FakeSMTP)
    with pytest.raises(review.SendBlocked, match="mock"):
        review.send(store, live(mock=True), did, smtp_factory=FakeSMTP)


def test_superseded_draft_cannot_send(store):
    did = make_draft(store)
    review.approve(store, did, "op", recipient=INBOX)
    store.update("drafts", did, {"status": "superseded", "approved_hash": None})
    with pytest.raises(review.SendBlocked):
        review.send(store, Settings(mock=True, operator_inbox=INBOX), did)
