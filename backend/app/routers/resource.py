"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for

router = APIRouter(prefix="/api", tags=["业务资源"])

PREVIEW_ACTIONS = {
    "预览恢复", "预览超限", "预览所选", "预览所选文档", "逐份预览",
    "preview_recovery", "preview_documents", "preview_selection",
}
SUBMIT_ACTIONS = {
    "逐份提交转型", "提交文档转型", "提交恢复请求", "提交转型请求",
    "submit_document", "submit_flatten_request", "submit_recovery",
}
CORRECT_ACTIONS = {
    "提交更正", "提交完整更正", "更正文档", "重试更正",
    "correct_document", "submit_correction", "retry_document",
}
CLAIM_ACTIONS = {
    "领取已确认结果", "领取展平结果", "领取确认结果", "领取契约输入",
    "claim_confirmed", "claim_confirmed_results", "claim_for_cast",
}


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


def _run_recovery_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult | None:
    """把携带文档正文的恢复类动作分派给对应领域服务。"""
    action = payload.action.strip()
    if action not in (PREVIEW_ACTIONS | SUBMIT_ACTIONS | CORRECT_ACTIONS | CLAIM_ACTIONS):
        return None
    service = service_for(module)
    if service is None:
        return None

    kwargs: dict[str, Any] = {}
    kwargs.update(payload.params or {})
    kwargs.update(payload.values or {})
    row = service.get_entry(entry_id) if hasattr(service, "get_entry") else None
    if row and row.get("展平编号"):
        kwargs.setdefault("flatten_no", row.get("展平编号"))
        kwargs.setdefault("path_id", row.get("展平编号"))
    if payload.document is not None:
        kwargs["document"] = payload.document
    if payload.documents is not None:
        kwargs["documents"] = payload.documents

    if action in PREVIEW_ACTIONS and hasattr(service, "preview_documents"):
        result = service.preview_documents(**kwargs)
        return ActionResult(ok=True, message=result.get("message", "预览完成"), entry=result)
    if action in SUBMIT_ACTIONS and hasattr(service, "submit_document"):
        result = service.submit_document(**kwargs)
        return ActionResult(ok=bool(result.ok), message=result.message, entry=result.to_dict())
    if action in CORRECT_ACTIONS and hasattr(service, "correct_document"):
        doc_id = kwargs.pop("doc_id", None) or kwargs.pop("document_id", None)
        result = service.correct_document(doc_id, **kwargs)
        return ActionResult(ok=bool(result.ok), message=result.message, entry=result.to_dict())
    if action in CLAIM_ACTIONS and hasattr(service, "claim_confirmed_results"):
        result = service.claim_confirmed_results(**kwargs)
        return ActionResult(ok=bool(result.ok), message=result.message, entry=result.to_dict())
    return ActionResult(ok=False, message=f"模块 {module} 不支持恢复动作「{action}」")


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    recovery_result = _run_recovery_action(module, entry_id, payload)
    if recovery_result is not None:
        return recovery_result
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
