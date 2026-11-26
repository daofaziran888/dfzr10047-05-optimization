"""超限嵌套展平文档的逐份恢复与幂等领取链路。"""
from __future__ import annotations

import pytest

from app.services import SERVICES
from app.store import store


@pytest.fixture(autouse=True)
def reset_store():
    store.reload_seed()
    yield
    store.reload_seed()


def doc(doc_id: str, name: str, rows: int, *, complete: bool = True):
    return {
        "doc_id": doc_id,
        "doc_name": name,
        "complete": complete,
        "items": [{"seq": index, "value": f"{doc_id}-{index}"} for index in range(rows)],
    }


def test_preview_lists_actual_rows_and_same_quota_rule():
    svc = SERVICES["nested_flatten"]
    result = svc.preview_documents(
        [doc("JIA", "甲", 9), doc("YI", "乙", 2)],
        group_id="G", row_limit=10,
    )
    assert result["actual_rows"] == 11
    assert result["within_limit"] is False
    by_id = {item["doc_id"]: item for item in result["documents"]}
    assert by_id["JIA"]["actual_rows"] == 9
    assert by_id["YI"]["actual_rows"] == 2
    assert "超出 1 行" in result["message"]


def test_small_document_accepted_and_large_failure_version_retained():
    svc = SERVICES["nested_flatten"]
    ok_y, _msg, receipt_y = svc.submit_document(doc("YI", "乙", 2), group_id="G", row_limit=10)
    ok_j, _msg, receipt_j = svc.submit_document(doc("JIA", "甲", 9), group_id="G", row_limit=10)

    assert ok_y is True
    assert receipt_y["can_enter_cast"] is True
    assert ok_j is False
    assert receipt_j["can_enter_cast"] is False

    failed = svc.get_failed_attempt("JIA", "G")
    assert failed["actual_rows"] == 9
    assert [row["array_index"] for row in failed["rows"]] == list(range(9))
    assert failed["unconfirmed_indexes"] == list(range(9))
    assert failed["recoverable"] is True


def test_correction_creates_new_version_and_does_not_mix_indexes():
    svc = SERVICES["nested_flatten"]
    svc.submit_document(doc("YI", "乙", 2), group_id="G", row_limit=10)
    _ok, _msg, old_receipt = svc.submit_document(doc("JIA", "甲", 9), group_id="G", row_limit=10)
    old_id = old_receipt["attempt_id"]

    ok, _msg, new_receipt = svc.correct_document("JIA", doc("JIA", "甲", 8), group_id="G")
    assert ok is True
    assert new_receipt["actual_rows"] == 8
    assert new_receipt["original_attempt_id"] == old_id

    old = svc.get_document_attempt(old_id)
    new = svc.get_document_attempt(new_receipt["attempt_id"])
    assert old["actual_rows"] == 9
    assert old["superseded_by"] == new["attempt_id"]
    assert new["correction_of"] == old_id
    assert [row["array_index"] for row in old["rows"]] == list(range(9))
    assert [row["array_index"] for row in new["rows"]] == list(range(8))
    # 新版本的第 7 个索引必须来自新文档，不能混入旧 9 行批次。
    assert new["rows"][7]["data"]["value"] == "JIA-7"


def test_late_approval_cannot_revive_superseded_attempt():
    svc = SERVICES["nested_flatten"]
    _ok, _m, old = svc.submit_document(doc("JIA", "甲", 9), group_id="G", row_limit=10)
    svc.correct_document("JIA", doc("JIA", "甲", 8), group_id="G")
    result = svc.approve_document_attempt(old["attempt_id"])
    assert result.ok is False
    assert result["stale"] is True
    assert result.record["can_enter_cast"] is False


def test_claim_includes_corrected_jia_and_skips_duplicate_yi():
    nf = SERVICES["nested_flatten"]
    vc = SERVICES["value_cast"]
    nf.submit_document(doc("YI", "乙", 2), group_id="G", row_limit=10)
    _ok, _m, old_jia = nf.submit_document(doc("JIA", "甲", 9), group_id="G", row_limit=10)
    nf.correct_document("JIA", doc("JIA", "甲", 8), group_id="G")

    first = vc.claim_confirmed_results(group_id="G", claim_id="C1", batch_id="PB-G")
    assert first.ok is True
    assert first[2]["total_rows"] == 10
    decisions = {item["doc_id"]: item for item in first[2]["decisions"]}
    assert decisions["JIA"]["entered_cast"] is True
    assert decisions["JIA"]["actual_rows"] == 8
    assert decisions["YI"]["entered_cast"] is True
    assert decisions["YI"]["actual_rows"] == 2

    second = vc.claim_confirmed_results(group_id="G", claim_id="C2", batch_id="PB-G")
    assert second[2]["new_rows"] == 0
    assert second[2]["duplicate_rows"] == 10
    repeat = {item["doc_id"]: item for item in second[2]["decisions"]}
    assert repeat["JIA"]["duplicate"] is True
    assert repeat["YI"]["duplicate"] is True

    batch = next(row for row in store.rows("parse_batch") if row["批次记录编号"] == "PB-G")
    assert batch["总行数"] == "10"
    assert batch["成功行数"] == "10"
    assert batch["隔离行数"] == "0"

    old = nf.get_document_attempt(old_jia["attempt_id"])
    assert old["actual_rows"] == 9


def test_explicit_receipt_replay_is_idempotent():
    nf = SERVICES["nested_flatten"]
    vc = SERVICES["value_cast"]
    _ok, _m, receipt = nf.submit_document(doc("YI", "乙", 2), group_id="G", row_limit=10)
    first = vc.consume_processing_receipt(receipt["receipt_id"], claim_id="SAME")
    second = vc.consume_processing_receipt(receipt["receipt_id"], claim_id="SAME")
    assert first[2]["new_rows"] == 2
    assert second[2].get("idempotent_replay") is True
    assert second[2]["new_rows"] == 0


def test_yi_executed_early_is_skipped_when_jia_later_claimed():
    nf = SERVICES["nested_flatten"]
    vc = SERVICES["value_cast"]
    nf.submit_document(doc("YI", "乙", 2), group_id="G", row_limit=10)
    nf.submit_document(doc("JIA", "甲", 9), group_id="G", row_limit=10)

    early = vc.claim_confirmed_results(group_id="G", claim_id="EARLY", batch_id="PB-G")
    assert [d["doc_id"] for d in early[2]["included"]] == ["YI"]
    assert early[2]["executed_rows"] == 2

    nf.correct_document("JIA", doc("JIA", "甲", 8), group_id="G")
    later = vc.claim_confirmed_results(group_id="G", claim_id="LATER", batch_id="PB-G")
    decisions = {d["doc_id"]: d for d in later[2]["decisions"]}
    assert decisions["JIA"]["entered_cast"] is True
    assert decisions["JIA"]["executed_rows"] == 8
    assert decisions["YI"]["duplicate"] is True
    assert decisions["YI"]["executed"] is False
    assert later[2]["new_rows"] == 8
    assert later[2]["duplicate_rows"] == 2
    assert later[2]["total_rows"] == 10


def test_incomplete_document_is_not_sliced_or_confirmed():
    svc = SERVICES["nested_flatten"]
    bad = doc("BAD", "残文档", 3, complete=False)
    bad["expected_count"] = 9
    result = svc.preview_documents([bad], group_id="G", row_limit=10)
    assert result["all_valid"] is False
    ok, _msg, receipt = svc.submit_document(bad, group_id="G", row_limit=10)
    assert ok is False
    assert receipt["confirmed"] is False
    attempt = svc.get_document_attempt(receipt["attempt_id"])
    assert attempt["can_enter_cast"] is False
    assert len(attempt["rows"]) == 3
