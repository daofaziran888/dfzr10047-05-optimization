"""超限文档恢复与取值转型领取的专用 HTTP 契约。

通用资源路由仍负责表格 CRUD；这些路径承载需要提交文档正文和幂等领取的动作。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, Query

from app.services.document_recovery import recovery_center

router = APIRouter(prefix="/api", tags=["超限文档恢复"])


def _payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    return dict(payload or {})


@router.post("/nested_flatten/recovery/preview")
@router.post("/nested_flatten/preview-recovery")
@router.post("/nested_flatten/preview")
@router.post("/recovery/preview")
@router.post("/flatten-recovery/preview")
def preview(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = _payload(payload)
    documents = data.pop("documents", None)
    if documents is None:
        documents = data.pop("selected_documents", None)
    return recovery_center.preview(documents, **data)


@router.post("/nested_flatten/recovery/register")
@router.post("/recovery/register")
def register_document(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = _payload(payload)
    document = data.pop("document", None)
    return recovery_center.register_document(document, **data)


@router.post("/nested_flatten/recovery/preview-selected")
@router.post("/recovery/preview-selected")
def preview_selected(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = _payload(payload)
    return recovery_center.preview_selected(**data)


@router.post("/nested_flatten/recovery/submit")
@router.post("/nested_flatten/submit-document")
@router.post("/nested_flatten/submit")
@router.post("/recovery/submit")
@router.post("/flatten-recovery/submit")
def submit(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = _payload(payload)
    document = data.pop("document", None)
    if document is None and "documents" in data:
        documents = data.pop("documents")
        index = int(data.pop("document_index", 0) or 0)
        document = documents[index] if isinstance(documents, list) and index < len(documents) else documents
    result = recovery_center.submit_document(document, **data)
    return result.to_dict()


@router.post("/nested_flatten/recovery/submit-each")
@router.post("/recovery/submit-each")
def submit_each(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = _payload(payload)
    documents = data.pop("documents", [])
    results = [recovery_center.submit_document(doc, **data).to_dict() for doc in documents]
    return {
        "ok": all(item.get("accepted") for item in results),
        "message": f"已逐份提交 {len(results)} 份文档，每份独立受理、互不连坐。",
        "results": results,
    }


@router.post("/nested_flatten/recovery/correct")
@router.post("/nested_flatten/correct")
@router.post("/recovery/correct")
@router.post("/flatten-recovery/correct")
def correct(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = _payload(payload)
    doc_id = data.pop("doc_id", None) or data.pop("document_id", None)
    document = data.pop("document", None) or data.pop("correction", None)
    result = recovery_center.correct_document(doc_id, document, **data)
    return result.to_dict()


@router.post("/nested_flatten/recovery/approve")
@router.post("/recovery/approve")
def approve(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    data = _payload(payload)
    attempt_id = str(data.pop("attempt_id", ""))
    result = recovery_center.approve_attempt(attempt_id, **data)
    return result.to_dict()


@router.get("/nested_flatten/recovery/attempts")
@router.get("/nested_flatten/attempts")
@router.get("/recovery/attempts")
def attempts(
    doc_id: str | None = Query(default=None),
    group_id: str | None = Query(default=None),
) -> dict[str, Any]:
    items = recovery_center.list_attempts(doc_id, group_id)
    return {"items": items, "total": len(items)}


@router.get("/nested_flatten/recovery/attempts/{attempt_id}")
@router.get("/recovery/attempts/{attempt_id}")
def attempt_detail(attempt_id: str) -> dict[str, Any]:
    attempt = recovery_center.get_attempt(attempt_id)
    return attempt or {"ok": False, "message": f"尝试 {attempt_id} 不存在"}


@router.get("/nested_flatten/recovery/receipts")
@router.get("/recovery/receipts")
def receipts(
    group_id: str | None = Query(default=None),
    confirmed_only: bool = Query(default=False),
) -> dict[str, Any]:
    items = recovery_center.list_receipts(group_id, confirmed_only=confirmed_only)
    return {"items": items, "total": len(items)}


@router.get("/nested_flatten/recovery/receipts/{receipt_id}")
@router.get("/recovery/receipts/{receipt_id}")
def receipt_detail(receipt_id: str) -> dict[str, Any]:
    receipt = recovery_center.get_receipt(receipt_id)
    return receipt or {"ok": False, "message": f"回执 {receipt_id} 不存在"}


@router.post("/value_cast/recovery/claim")
@router.post("/value_cast/claim-confirmed")
@router.post("/value_cast/claim")
@router.post("/recovery/claim")
@router.post("/flatten-recovery/claim")
def claim(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    result = recovery_center.claim_confirmed(**_payload(payload))
    return result.to_dict()


@router.get("/value_cast/recovery/claims")
@router.get("/recovery/claims")
def claims(group_id: str | None = Query(default=None)) -> dict[str, Any]:
    items = recovery_center.list_claims(group_id)
    return {"items": items, "total": len(items)}


@router.get("/value_cast/recovery/batches/{batch_id}")
def contract_batch(batch_id: str) -> dict[str, Any]:
    batch = recovery_center.get_contract_batch(batch_id)
    return batch or {"ok": False, "message": f"契约输入批次 {batch_id} 不存在"}
