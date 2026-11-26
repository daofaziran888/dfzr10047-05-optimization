"""超限源文档逐份受理、版本留痕与取值转型领取的恢复数据集。

这里的所有状态都放在 ``store.volatile`` 中：测试调用 ``store.reload_seed()``
时会一并清空，避免跨用例污染。恢复中心只认完整且已确认的文档尝试，绝不会把
一份超限文档截取前几行冒充完整结果。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from app.store import store

COMPLETE_STATUSES = {"已受理", "已确认", "已领取", "claimed", "accepted", "confirmed"}
CLAIMED_STATUSES = {"已领取", "claimed"}


class RecoveryResult(tuple):
    """同时兼容三元组解包和字典读取的动作结果。

    既有服务层多使用 ``ok, message, record = service.method(...)``，而新恢复
    接口的调用方也可能直接读取 ``result["receipt_id"]``，因此这里做一层适配。
    """

    # tuple 子类型不能再声明非空 __slots__；普通实例属性即可满足适配层需要。

    def __new__(
        cls,
        ok: bool,
        message: str,
        record: dict[str, Any] | None = None,
        **extra: Any,
    ) -> "RecoveryResult":
        record = record if record is not None else {}
        obj = tuple.__new__(cls, (bool(ok), message, record))
        obj.ok = bool(ok)
        obj.message = message
        obj.record = record
        obj._extra = extra
        return obj

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return tuple.__getitem__(self, key)
        if key == "ok":
            return self.ok
        if key == "message":
            return self.message
        if key == "record":
            return self.record
        if key in self._extra:
            return self._extra[key]
        if isinstance(self.record, dict) and key in self.record:
            return self.record[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        return key in {"ok", "message", "record"} or key in self._extra or key in self.record

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": self.ok,
            "message": self.message,
            "record": self.record,
        }
        if isinstance(self.record, dict):
            data.update(self.record)
        data.update(self._extra)
        return data


class DocumentRecoveryCenter:
    """源文档预览、逐份受理、更正版本和转型领取的内存领域中心。"""

    DEFAULT_LIMIT = 10

    def _state(self) -> dict[str, Any]:
        return store.volatile.setdefault("document_recovery", {
            "counter": 0,
            "groups": {},
            "source_documents": {},
            "attempts": [],
            "receipts": [],
            "claims": [],
            "contract_batches": {},
        })

    def _next_id(self, prefix: str, supplied: str | None = None) -> str:
        if supplied:
            return str(supplied)
        state = self._state()
        state["counter"] += 1
        return f"{prefix}-{state['counter']:04d}"

    @staticmethod
    def _now(value: Any = None) -> str:
        return str(value) if value else datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _truthy(value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().casefold()
        if text in {"", "none", "null", "unknown", "未知"}:
            return None
        if text in {"1", "true", "yes", "y", "是", "完整", "已完整", "ok"}:
            return True
        if text in {"0", "false", "no", "n", "否", "不完整", "截断", "truncated"}:
            return False
        return None

    @staticmethod
    def _first(raw: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
        for key in keys:
            if key in raw and raw[key] not in (None, ""):
                return raw[key]
        return default

    def _quota_limit(self, group_id: str, supplied: Any = None) -> int:
        state = self._state()
        group = state["groups"].setdefault(group_id, {"quota_limit": self.DEFAULT_LIMIT})
        for candidate in (supplied, group.get("quota_limit"), self.DEFAULT_LIMIT):
            try:
                if candidate is not None:
                    value = int(candidate)
                    group["quota_limit"] = value
                    return value
            except (TypeError, ValueError):
                continue
        group["quota_limit"] = self.DEFAULT_LIMIT
        return self.DEFAULT_LIMIT

    # ------------------------------------------------------------------
    # 文档规范化与实际展开
    # ------------------------------------------------------------------
    def normalize_document(
        self,
        raw: Any,
        index: int = 0,
        *,
        group_id: str = "DG-DEFAULT",
        path_id: str | None = None,
        source_path: str | None = None,
    ) -> dict[str, Any]:
        """把多种请求写法归一为带实际展开行和完整性结论的文档描述。"""
        if isinstance(raw, str):
            text = raw.strip()
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                raw = {"doc_id": text}
        if not isinstance(raw, dict):
            raw = {"value": raw}

        doc_id = str(self._first(
            raw, ("doc_id", "document_id", "文档编号", "源文档编号", "id"),
            f"DOC-{index + 1}",
        ))
        doc_name = str(self._first(
            raw, ("doc_name", "document_name", "文档名称", "源文档", "name", "label", "名称"),
            doc_id,
        ))
        version = self._first(raw, ("version", "revision", "版本", "更正版本", "attempt_version"))

        registry = self._state().get("source_documents", {})
        registered = registry.get(doc_id) or registry.get(doc_name)
        if isinstance(registered, dict):
            merged = dict(registered)
            merged.update(raw)
            raw = merged

        payload = raw
        for key in ("document", "source_document", "源文档", "source", "payload", "content", "raw_document"):
            candidate = raw.get(key)
            if isinstance(candidate, dict):
                payload = candidate
                break

        path = source_path or raw.get("source_path") or raw.get("path") or raw.get("原始路径")
        columns = raw.get("target_columns") or raw.get("columns") or raw.get("展平后列名")

        path_row: dict[str, Any] | None = None
        if path_id or raw.get("path_id") or raw.get("flatten_no") or raw.get("展平编号"):
            lookup_id = str(path_id or raw.get("path_id") or raw.get("flatten_no") or raw.get("展平编号"))
            from app.services.nested_flatten import NestedFlattenService
            path_row = NestedFlattenService()._find(lookup_id)
            if path_row:
                path = path or path_row.get("原始路径")
                columns = columns or path_row.get("展平后列名")
        path = str(path or "$.items[*]")

        target_columns: list[str] = []
        if isinstance(columns, str):
            target_columns = [c.strip() for c in columns.split(",") if c.strip()]
        elif isinstance(columns, (list, tuple)):
            target_columns = [str(c).strip() for c in columns if str(c).strip()]
        elif path_row:
            target_columns = [
                c.strip() for c in str(path_row.get("展平后列名") or "").split(",") if c.strip()
            ]

        segments = [s for s in path.replace("$.", "").split(".") if s]
        indexed_items = list(self._resolve_indexed(payload, segments))
        derived_rows = [
            self._project(item, target_columns)
            for _index_chain, item in indexed_items
        ]

        provided_rows = self._first(raw, ("rows", "flat_rows", "flattened_rows", "output_rows", "展平行明细"))
        actual_rows_data: list[dict[str, Any]] = []
        if isinstance(provided_rows, list):
            for row in provided_rows:
                actual_rows_data.append(dict(row) if isinstance(row, dict) else {"value": row})

        explicit_count = self._first(raw, (
            "actual_rows", "expanded_rows", "expand_rows", "row_count", "rows_count",
            "实际展开行数", "展开行数", "输出行数",
        ))
        explicit_count_int: int | None = None
        if explicit_count is not None:
            try:
                explicit_count_int = int(explicit_count)
            except (TypeError, ValueError):
                pass

        # 没有提供可展开内容时，允许调用方只给行数阈值描述；生成带数组位置的
        # 最小行，保证预览/受理/领取链路仍可测试。
        if not derived_rows and explicit_count_int and not actual_rows_data:
            derived_rows = [
                {target_columns[0] if target_columns else "line": f"{doc_name}-R{i + 1}"}
                for i in range(explicit_count_int)
            ]
            indexed_items = [((i,), derived_rows[i]) for i in range(explicit_count_int)]
            synthetic = True
        else:
            synthetic = False

        if actual_rows_data:
            actual_count = len(actual_rows_data)
        else:
            actual_count = len(derived_rows)
            actual_rows_data = derived_rows

        source_array_length = self._source_array_length(payload, segments)
        expected_count = self._first(raw, (
            "expected_count", "expected_rows", "element_count", "数组元素数", "应有行数", "完整行数",
        ))
        expected_count_int: int | None = None
        if expected_count is not None:
            try:
                expected_count_int = int(expected_count)
            except (TypeError, ValueError):
                pass

        complete_flag = self._truthy(self._first(raw, ("complete", "is_complete", "completed", "full", "完整", "是否完整")))
        truncated_flag = self._truthy(self._first(raw, ("truncated", "is_truncated", "截断", "是否截断")))
        issues: list[str] = []
        if complete_flag is False:
            issues.append("源文档被标记为不完整")
        if truncated_flag is True:
            issues.append("源文档存在截断，不能把部分结果当成完整展平")
        if expected_count_int is not None and expected_count_int != source_array_length:
            issues.append(
                f"原文档数组元素数 {source_array_length} 与声明的完整元素数 {expected_count_int} 不符"
            )
        if explicit_count_int is not None and explicit_count_int != actual_count:
            issues.append(f"实际展开行数 {actual_count} 与声明行数 {explicit_count_int} 不符")
        if provided_rows and len(provided_rows) != len(derived_rows) and explicit_count_int is None:
            # 调用方直接给的展平行必须与源文档推导结果一致，防止拿截取结果冒充。
            issues.append(
                f"提交的展平行数 {len(provided_rows)} 与源文档实际展开 {len(derived_rows)} 不符"
            )
        if len(indexed_items) != actual_count and not provided_rows:
            issues.append("数组元素序号与实际展开行未能一一对应")

        checksum = "sha256:" + hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]

        return {
            "doc_id": doc_id,
            "doc_name": doc_name,
            "version": version,
            "group_id": str(raw.get("group_id") or group_id),
            "path_id": path_id or raw.get("path_id") or raw.get("flatten_no"),
            "source_path": path,
            "complete": not issues and complete_flag is not False and truncated_flag is not True,
            "issues": issues,
            "valid": not issues,
            "actual_rows": actual_count,
            "expanded_rows": actual_count,
            "row_count": actual_count,
            "source_array_length": source_array_length,
            "array_length": source_array_length,
            "array_indexes": list(range(actual_count)),
            "数组元素序号": list(range(actual_count)),
            "checksum": checksum,
            "synthetic_rows": synthetic,
            "rows_data": actual_rows_data,
            "indexed_items": indexed_items,
            "source_document": payload,
            "target_columns": target_columns,
            "raw_descriptor": {k: v for k, v in raw.items() if k not in {"document", "source_document"}},
        }

    def register_document(self, document: Any = None, **kwargs: Any) -> dict[str, Any]:
        """登记源文档，之后预览/受理可只传 doc_id，模拟列表中勾选文档。"""
        raw = document if document is not None else kwargs.get("source_document") or kwargs
        group_id = str(kwargs.get("group_id") or (raw.get("group_id") if isinstance(raw, dict) else None) or "DG-DEFAULT")
        normalized = self.normalize_document(
            raw, group_id=group_id,
            path_id=kwargs.get("path_id") or kwargs.get("flatten_no"),
            source_path=kwargs.get("source_path"),
        )
        record = {
            "doc_id": normalized["doc_id"],
            "doc_name": normalized["doc_name"],
            "group_id": group_id,
            "source_document": normalized["source_document"],
            "actual_rows": normalized["actual_rows"],
            "complete": normalized["complete"],
            "checksum": normalized["checksum"],
        }
        self._state()["source_documents"][normalized["doc_id"]] = normalized["source_document"]
        return record

    def preview_selected(self, doc_ids: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        if doc_ids is None:
            doc_ids = kwargs.get("selected_ids") or kwargs.get("document_ids") or []
        return self.preview(list(doc_ids), **kwargs)

    def _resolve_indexed(self, node: Any, segments: list[str], index_chain: tuple[int, ...] = ()):
        if not segments:
            yield index_chain, node
            return
        head, *rest = segments
        array_marker = head.endswith("[*]")
        key = head[:-3] if array_marker else head
        if not isinstance(node, dict):
            return
        child = node.get(key)
        if child is None:
            return
        if array_marker or isinstance(child, list):
            items = child if isinstance(child, list) else [child]
            for index, item in enumerate(items):
                yield from self._resolve_indexed(item, rest, (*index_chain, index))
        else:
            yield from self._resolve_indexed(child, rest, index_chain)

    def _source_array_length(self, payload: Any, segments: list[str]) -> int:
        node = payload
        for pos, segment in enumerate(segments):
            array_marker = segment.endswith("[*]")
            key = segment[:-3] if array_marker else segment
            if not isinstance(node, dict):
                return 0
            node = node.get(key)
            if node is None:
                return 0
            if array_marker:
                return len(node) if isinstance(node, list) else 1
            if isinstance(node, list) and pos == len(segments) - 1:
                return len(node)
        return len(node) if isinstance(node, list) else (1 if node is not None else 0)

    @staticmethod
    def _project(item: Any, target_columns: list[str]) -> dict[str, Any]:
        if not target_columns:
            if isinstance(item, dict):
                return dict(item)
            return {"value": item}
        if not isinstance(item, dict):
            return {target_columns[0]: item, **{c: "" for c in target_columns[1:]}}
        if len(target_columns) == 1 and target_columns[0] not in item:
            return {target_columns[0]: ";".join(f"{k}={v}" for k, v in item.items())}
        return {col: item.get(col, "") for col in target_columns}

    # ------------------------------------------------------------------
    # 预览与额度
    # ------------------------------------------------------------------
    def _coerce_documents(self, documents: Any) -> list[Any]:
        if documents is None:
            return []
        if isinstance(documents, dict) and (
            "documents" in documents or "selected_documents" in documents
        ):
            documents = documents.get("documents") or documents.get("selected_documents")
        if isinstance(documents, (str, bytes)):
            try:
                documents = json.loads(documents)
            except json.JSONDecodeError:
                return [{"doc_id": str(documents)}]
        if isinstance(documents, (list, tuple)):
            return list(documents)
        return [documents]

    def _accepted_attempts(self, group_id: str, *, include_released: bool = False) -> list[dict[str, Any]]:
        result = []
        for attempt in self._state()["attempts"]:
            if attempt.get("group_id") != group_id or not attempt.get("accepted"):
                continue
            if not include_released and attempt.get("quota_released"):
                continue
            result.append(attempt)
        return result

    def accepted_rows(self, group_id: str, *, exclude_doc_id: str | None = None) -> int:
        return sum(
            int(a.get("actual_rows") or 0)
            for a in self._accepted_attempts(group_id)
            if exclude_doc_id is None or a.get("doc_id") != exclude_doc_id
        )

    def reconcile_group(self, group_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        """在整组额度内做逐份受理调度。

        已领取结果占用的额度不能撤回；未领取的有效尝试按“小而完整的文档先
        受理”调度，从而保证乙的 2 行不会被甲的 9 行连坐。调度只改变受理状态，
        不删除任何超限历史版本。
        """
        state = self._state()
        limit = self._quota_limit(group_id, limit)
        attempts = [a for a in state["attempts"] if a.get("group_id") == group_id]
        locked = [a for a in attempts if a.get("accepted") and (a.get("claimed") or a.get("admission_locked"))]
        locked_ids = {a["attempt_id"] for a in locked}
        remaining = limit - sum(int(a.get("actual_rows") or 0) for a in locked)

        candidates = [
            a for a in attempts
            if a.get("valid")
            and not a.get("superseded_by")
            and not a.get("claimed")
            and a["attempt_id"] not in locked_ids
        ]
        # 优先显式高优先级，其次较小行数；相同行数保持提交顺序。
        candidates.sort(key=lambda a: (
            -int(a.get("priority") or 0),
            int(a.get("actual_rows") or 0),
            str(a.get("submitted_at") or ""),
            a["attempt_id"],
        ))
        selected = set(locked_ids)
        for attempt in candidates:
            row_count = int(attempt.get("actual_rows") or 0)
            if row_count <= max(0, remaining):
                selected.add(attempt["attempt_id"])
                remaining -= row_count

        for attempt in attempts:
            receipt = self.get_receipt_by_attempt(attempt["attempt_id"])
            if not attempt.get("valid") or attempt.get("superseded_by"):
                continue
            if attempt.get("claimed"):
                continue
            if attempt["attempt_id"] in selected:
                attempt["accepted"] = True
                attempt["是否受理"] = True
                attempt["confirmed"] = True
                attempt["是否确认"] = True
                attempt["can_enter_cast"] = True
                attempt["能否进入取值转型"] = True
                attempt["admission_status"] = "已受理"
                attempt["受理状态"] = "已受理"
                attempt["approval_status"] = "已通过"
                attempt["审批状态"] = "已通过"
                attempt["status"] = "已确认"
                attempt["状态"] = "已确认"
                attempt["decided_at"] = self._now()
                if receipt:
                    receipt.update({
                        "accepted": True, "是否受理": True,
                        "confirmed": True, "是否确认": True,
                        "can_enter_cast": True, "能否进入取值转型": True,
                        "can_continue_on_value_cast": True, "可在取值转型页续做": True,
                        "status": "可在取值转型页续做", "状态": "可在取值转型页续做",
                    })
            else:
                baseline = sum(int(a.get("actual_rows") or 0) for a in attempts if a["attempt_id"] in selected)
                available = max(0, limit - baseline)
                attempt["accepted"] = False
                attempt["是否受理"] = False
                attempt["confirmed"] = False
                attempt["是否确认"] = False
                attempt["can_enter_cast"] = False
                attempt["能否进入取值转型"] = False
                attempt["admission_status"] = "未受理"
                attempt["受理状态"] = "未受理"
                attempt["approval_status"] = "待审批"
                attempt["审批状态"] = "待审批"
                attempt["status"] = "超限待更正"
                attempt["状态"] = "超限待更正"
                attempt["available_quota"] = available
                attempt["剩余可受理行数"] = available
                attempt["overflow_rows"] = max(0, int(attempt.get("actual_rows") or 0) - available)
                attempt["超限行数"] = attempt["overflow_rows"]
                reason = (
                    f"整组额度 {limit} 行已优先受理其他完整文档；"
                    f"该文档需 {attempt.get('actual_rows')} 行，当前调度后剩余 {available} 行。"
                    "可提交完整更正版，历史尝试仍保留。"
                )
                attempt["reason"] = reason
                attempt["受理说明"] = reason
                if receipt:
                    receipt.update({
                        "accepted": False, "是否受理": False,
                        "confirmed": False, "是否确认": False,
                        "can_enter_cast": False, "能否进入取值转型": False,
                        "can_continue_on_value_cast": False, "可在取值转型页续做": False,
                        "status": "未受理·可恢复", "状态": "未受理·可恢复",
                        "reason": reason, "受理说明": reason,
                    })
        return attempts

    def preview(self, documents: Any = None, **kwargs: Any) -> dict[str, Any]:
        """按与受理完全相同的额度规则预览所选文档，不写入任何状态。"""
        if documents is None and "selected_documents" in kwargs:
            documents = kwargs.pop("selected_documents")
        group_id = str(kwargs.get("group_id") or kwargs.get("quota_id") or kwargs.get("batch_id") or "DG-DEFAULT")
        limit = self._quota_limit(group_id, kwargs.get("quota_limit", kwargs.get("row_limit", kwargs.get("limit"))))
        raw_docs = self._coerce_documents(documents if documents is not None else kwargs.get("documents"))
        selected_ids = kwargs.get("selected_ids") or kwargs.get("doc_ids")
        if selected_ids and raw_docs and all(isinstance(d, str) for d in raw_docs):
            raw_docs = [{"doc_id": d} for d in raw_docs]

        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_docs):
            doc = self.normalize_document(
                raw, index, group_id=group_id,
                path_id=kwargs.get("path_id") or kwargs.get("flatten_no"),
                source_path=kwargs.get("source_path"),
            )
            normalized.append(doc)

        if selected_ids:
            wanted = {str(x) for x in selected_ids}
            normalized = [d for d in normalized if d["doc_id"] in wanted or d["doc_name"] in wanted]

        explicit_baseline = kwargs.get("already_accepted_rows", kwargs.get("accepted_rows"))
        if explicit_baseline is None:
            selected_doc_ids = {d["doc_id"] for d in normalized}
            baseline = sum(
                int(a.get("actual_rows") or 0)
                for a in self._accepted_attempts(group_id)
                if a.get("doc_id") not in selected_doc_ids
            )
        else:
            baseline = int(explicit_baseline or 0)

        total = sum(int(d["actual_rows"]) for d in normalized)
        cumulative = baseline
        for doc in normalized:
            before = cumulative
            cumulative += int(doc["actual_rows"])
            doc["accepted_rows_before"] = before
            doc["available_quota_before"] = max(0, limit - before)
            doc["fits_remaining"] = doc["valid"] and cumulative <= limit
            doc["cumulative_rows"] = cumulative
            doc["quota_after_doc"] = cumulative
            doc["within_group_limit"] = cumulative <= limit
            doc["would_accept"] = doc["fits_remaining"]
            doc["preview_verdict"] = (
                "可受理" if doc["fits_remaining"]
                else "完整性不符" if not doc["valid"]
                else f"超出当前剩余额度 {max(0, cumulative - limit)} 行"
            )

        group_total = baseline + total
        result = {
            "ok": True,
            "preview": True,
            "group_id": group_id,
            "quota_limit": limit,
            "row_limit": limit,
            "baseline_accepted_rows": baseline,
            "selected_count": len(normalized),
            "document_count": len(normalized),
            "actual_rows": total,
            "expanded_rows": total,
            "total_rows": group_total,
            "selected_rows": total,
            "remaining_quota": max(0, limit - group_total),
            "over_limit_rows": max(0, group_total - limit),
            "within_limit": group_total <= limit,
            "all_complete": all(d["complete"] for d in normalized),
            "all_valid": all(d["valid"] for d in normalized),
            "acceptable": all(d["would_accept"] for d in normalized),
            "documents": normalized,
            "items": [self._public_doc(d) for d in normalized],
        }
        names = "、".join(f"{d['doc_name']} {d['actual_rows']} 行" for d in normalized) or "未选择文档"
        if group_total <= limit:
            result["message"] = f"预览：{names}；合计 {group_total}/{limit} 行，额度内可逐份受理。"
        else:
            result["message"] = (
                f"预览：{names}；合计 {group_total}/{limit} 行，"
                f"超出 {group_total - limit} 行，不能作为同一完整批次受理。"
            )
        return result

    @staticmethod
    def _public_doc(doc: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in doc.items() if k not in {"rows_data", "indexed_items", "source_document"}}

    # ------------------------------------------------------------------
    # 逐份提交、更正、审批竞态
    # ------------------------------------------------------------------
    def submit_document(self, document: Any = None, **kwargs: Any) -> RecoveryResult:
        raw = document if document is not None else kwargs.get("document") or kwargs.get("values") or kwargs
        if isinstance(raw, dict) and "document" not in raw and any(k in raw for k in ("documents", "selected_documents")):
            raw = self._coerce_documents(raw)[int(kwargs.get("document_index", 0))]
        group_id = str(kwargs.get("group_id") or (raw.get("group_id") if isinstance(raw, dict) else None) or "DG-DEFAULT")
        limit = self._quota_limit(group_id, kwargs.get("quota_limit", kwargs.get("row_limit", kwargs.get("limit"))))
        doc = self.normalize_document(
            raw, kwargs.get("document_index", 0), group_id=group_id,
            path_id=kwargs.get("path_id") or kwargs.get("flatten_no"),
            source_path=kwargs.get("source_path"),
        )
        now = self._now(kwargs.get("at") or kwargs.get("submit_time"))
        request_key = kwargs.get("idempotency_key") or kwargs.get("request_key")
        if request_key:
            for receipt in self._state()["receipts"]:
                if receipt.get("idempotency_key") == request_key:
                    return RecoveryResult(receipt.get("accepted", False), "相同请求已提交，返回原回执且不重复占用额度。", receipt, duplicate=True)

        duplicate = self._find_duplicate_submission(group_id, doc)
        if duplicate is not None and kwargs.get("dedupe", True):
            receipt = self.get_receipt_by_attempt(duplicate["attempt_id"])
            if receipt:
                return RecoveryResult(True, "相同文档版本已受理，返回原回执且不重复执行。", receipt, duplicate=True)

        attempt_id = self._next_id(kwargs.get("attempt_prefix", "FA"), kwargs.get("attempt_id"))
        receipt_id = self._next_id(kwargs.get("receipt_prefix", "FR"), kwargs.get("receipt_id"))
        baseline = self.accepted_rows(group_id, exclude_doc_id=doc["doc_id"])
        available = max(0, limit - baseline)
        fits = doc["valid"] and baseline + doc["actual_rows"] <= limit
        overflow = max(0, baseline + doc["actual_rows"] - limit)

        rows = []
        for row_index, row_data in enumerate(doc["rows_data"]):
            index_chain = doc["indexed_items"][row_index][0] if row_index < len(doc["indexed_items"]) else (row_index,)
            array_index = index_chain[0] if index_chain else row_index
            rows.append({
                "row_id": f"{attempt_id}-ROW-{row_index + 1}",
                "展平行编号": f"{attempt_id}-ROW-{row_index + 1}",
                "row_index": row_index,
                "flat_row_number": row_index + 1,
                "展平实际行号": row_index + 1,
                "array_index": array_index,
                "source_index": array_index,
                "数组元素序号": array_index,
                "array_element_no": array_index + 1,
                "数组元素第几个": array_index + 1,
                "index_chain": list(index_chain),
                "索引链": list(index_chain),
                "locator": f"{doc['doc_id']}:{doc['source_path']}[{array_index}]",
                "错误定位": f"{doc['doc_id']}:{doc['source_path']}[{array_index}]",
                "data": row_data,
                "展平值": row_data,
            })

        if not doc["valid"]:
            status = "完整性校验失败"
            accepted = False
            reason = "；".join(doc["issues"])
        elif not fits:
            status = "超限待更正"
            accepted = False
            reason = (
                f"完整展开需 {doc['actual_rows']} 行；整组额度 {limit} 行，"
                f"其他已受理文档占用 {baseline} 行，当前可确认 {available} 行，"
                f"缺口/溢出 {overflow} 行。不能截取前 {available} 行冒充完整结果。"
            )
        else:
            status = "已确认"
            accepted = True
            reason = f"完整 {doc['actual_rows']} 行通过完整性与额度校验，已确认可领取到取值转型。"

        attempt = {
            "attempt_id": attempt_id,
            "尝试编号": attempt_id,
            "doc_id": doc["doc_id"],
            "文档编号": doc["doc_id"],
            "doc_name": doc["doc_name"],
            "文档名称": doc["doc_name"],
            "group_id": group_id,
            "整组编号": group_id,
            "path_id": doc.get("path_id"),
            "source_path": doc["source_path"],
            "原始路径": doc["source_path"],
            "version": kwargs.get("version") or doc.get("version") or self._next_version(doc["doc_id"]),
            "版本": kwargs.get("version") or doc.get("version") or self._next_version(doc["doc_id"]),
            "status": status,
            "状态": status,
            "admission_status": "已受理" if accepted else "未受理",
            "受理状态": "已受理" if accepted else "未受理",
            "approval_status": "已通过" if accepted else "待审批",
            "审批状态": "已通过" if accepted else "待审批",
            "accepted": accepted,
            "是否受理": accepted,
            "confirmed": accepted,
            "是否确认": accepted,
            "can_enter_cast": accepted,
            "能否进入取值转型": accepted,
            "complete": doc["complete"],
            "是否完整": doc["complete"],
            "valid": doc["valid"],
            "issues": doc["issues"],
            "问题清单": doc["issues"],
            "actual_rows": doc["actual_rows"],
            "expanded_rows": doc["actual_rows"],
            "row_count": doc["actual_rows"],
            "实际展开行数": doc["actual_rows"],
            "source_array_length": doc["source_array_length"],
            "原数组长度": doc["source_array_length"],
            "quota_limit": limit,
            "整组额度": limit,
            "accepted_rows_before": baseline,
            "变更前已受理行数": baseline,
            "available_quota": available,
            "剩余可受理行数": available,
            "overflow_rows": overflow,
            "超限行数": overflow,
            "confirmed_indexes": list(range(doc["actual_rows"])) if accepted else [],
            "已确认数组序号": list(range(doc["actual_rows"])) if accepted else [],
            "unconfirmed_indexes": list(range(doc["actual_rows"])) if not accepted else [],
            "未确认数组序号": list(range(doc["actual_rows"])) if not accepted else [],
            "overflow_indexes": list(range(available, doc["actual_rows"])) if not accepted else [],
            "超限数组序号": list(range(available, doc["actual_rows"])) if not accepted else [],
            "array_indexes": [i for i in range(doc["actual_rows"])],
            "数组元素序号": [i for i in range(doc["actual_rows"])],
            "recoverable": not accepted,
            "可恢复": not accepted,
            "recovery_action": "提交该文档的新完整更正版，或调整整组额度后重试；历史版本和数组位置继续保留。",
            "恢复操作": "提交该文档的新完整更正版，或调整整组额度后重试；历史版本和数组位置继续保留。",
            "checksum": doc["checksum"],
            "校验指纹": doc["checksum"],
            "rows": rows,
            "展平行": rows,
            "source_document": doc["source_document"],
            "原文档": doc["source_document"],
            "submitted_at": now,
            "提交时间": now,
            "decided_at": now if accepted else None,
            "受理时间": now if accepted else None,
            "superseded_by": None,
            "被后续尝试替代": None,
            "correction_of": kwargs.get("correction_of"),
            "更正自": kwargs.get("correction_of"),
            "idempotency_key": request_key,
        }
        self._state()["attempts"].append(attempt)

        receipt = {
            "receipt_id": receipt_id,
            "回执编号": receipt_id,
            "kind": "文档处理回执",
            "回执类型": "文档处理回执",
            "attempt_id": attempt_id,
            "尝试编号": attempt_id,
            "doc_id": doc["doc_id"],
            "文档编号": doc["doc_id"],
            "doc_name": doc["doc_name"],
            "文档名称": doc["doc_name"],
            "group_id": group_id,
            "整组编号": group_id,
            "version": attempt["version"],
            "版本": attempt["version"],
            "status": "可在取值转型页续做" if accepted else "未受理·可恢复",
            "状态": "可在取值转型页续做" if accepted else "未受理·可恢复",
            "accepted": accepted,
            "是否受理": accepted,
            "confirmed": accepted,
            "是否确认": accepted,
            "can_enter_cast": accepted,
            "能否进入取值转型": accepted,
            "can_continue_on_value_cast": accepted,
            "可在取值转型页续做": accepted,
            "actual_rows": doc["actual_rows"],
            "实际展开行数": doc["actual_rows"],
            "quota_limit": limit,
            "整组额度": limit,
            "quota_used_after": baseline + doc["actual_rows"] if accepted else baseline,
            "受理后已用额度": baseline + doc["actual_rows"] if accepted else baseline,
            "available_quota": max(0, limit - (baseline + doc["actual_rows"] if accepted else baseline)),
            "剩余额度": max(0, limit - (baseline + doc["actual_rows"] if accepted else baseline)),
            "reason": reason,
            "受理说明": reason,
            "next_page": "/value_cast" if accepted else "/nested_flatten",
            "下一页面": "/value_cast" if accepted else "/nested_flatten",
            "created_at": now,
            "创建时间": now,
            "idempotency_key": request_key,
            "claim_id": None,
            "领取编号": None,
            "claimed": False,
            "是否已领取": False,
        }
        self._state()["receipts"].append(receipt)
        attempt["receipt_id"] = receipt_id

        # 两份请求可能以任意顺序到达；按同一整组额度规则重新调度，确保小而完整
        # 的乙文档不会被甲的超限尝试连坐。
        self.reconcile_group(group_id, limit)
        accepted = bool(receipt.get("accepted"))
        if accepted:
            message = (
                f"{doc['doc_name']} 的 {doc['actual_rows']} 行已受理，回执 {receipt_id}，可到取值转型页领取。"
            )
        else:
            reason = receipt.get("reason") or reason
            message = (
                f"{doc['doc_name']} 的 {doc['actual_rows']} 行暂未受理：{reason} "
                f"已保留 {attempt_id} 供恢复。"
            )
        return RecoveryResult(accepted, message, receipt, attempt=attempt, preview=self._attempt_preview(attempt))

    def _find_duplicate_submission(self, group_id: str, doc: dict[str, Any]) -> dict[str, Any] | None:
        for attempt in reversed(self._state()["attempts"]):
            if (
                attempt.get("group_id") == group_id
                and attempt.get("doc_id") == doc["doc_id"]
                and attempt.get("checksum") == doc["checksum"]
                and attempt.get("actual_rows") == doc["actual_rows"]
                and attempt.get("accepted")
                and not attempt.get("quota_released")
            ):
                return attempt
        return None

    def _next_version(self, doc_id: str) -> int:
        versions = [int(a.get("version") or 0) for a in self._state()["attempts"] if a.get("doc_id") == doc_id and str(a.get("version")).isdigit()]
        return (max(versions) + 1) if versions else 1

    def _attempt_preview(self, attempt: dict[str, Any]) -> dict[str, Any]:
        return {
            "attempt_id": attempt["attempt_id"],
            "doc_id": attempt["doc_id"],
            "doc_name": attempt["doc_name"],
            "actual_rows": attempt["actual_rows"],
            "status": attempt["status"],
            "accepted": attempt["accepted"],
            "array_positions": [r["array_index"] for r in attempt.get("rows", [])],
        }

    def correct_document(self, doc_id: str | None = None, document: Any = None, **kwargs: Any) -> RecoveryResult:
        """提交迟到更正；始终生成新版本，绝不覆盖或混入旧尝试。"""
        if document is None and isinstance(doc_id, (dict, list)):
            document = doc_id
            doc_id = kwargs.get("doc_id")
        raw = document if document is not None else kwargs.get("document") or kwargs.get("correction") or {}
        if doc_id is None and isinstance(raw, dict):
            doc_id = raw.get("doc_id") or raw.get("document_id") or raw.get("文档编号")
        old = self.get_latest_attempt(str(doc_id), group_id=kwargs.get("group_id")) if doc_id else None
        if old is None and kwargs.get("attempt_id"):
            old = self.get_attempt(str(kwargs["attempt_id"]))
        if old is None:
            return RecoveryResult(False, f"找不到文档 {doc_id} 的历史尝试，无法更正。", {})

        now = self._now(kwargs.get("at") or kwargs.get("correct_time"))
        if not isinstance(raw, dict):
            raw = {"doc_id": doc_id, "document": raw}
        raw.setdefault("doc_id", old["doc_id"])
        raw.setdefault("doc_name", old["doc_name"])
        raw.setdefault("group_id", old["group_id"])

        # 若旧受理尚未领取，允许新版本替代它并释放预占额度；已领取的历史批次
        # 不能改写，旧额度保留，新尝试按新的完整文档重新受理。
        if old.get("accepted") and not old.get("claimed") and kwargs.get("replace_unclaimed", True):
            old["quota_released"] = True
            old["status"] = "已由更正版替代（原受理未领取）"
            old["approval_status"] = "已撤销"
        old["superseded_by"] = "PENDING"

        result = self.submit_document(
            raw,
            group_id=old["group_id"],
            quota_limit=kwargs.get("quota_limit", old.get("quota_limit")),
            correction_of=old["attempt_id"],
            version=kwargs.get("version") or (int(old.get("version") or 1) + 1),
            at=now,
            path_id=old.get("path_id"),
            source_path=old.get("source_path"),
            idempotency_key=kwargs.get("idempotency_key"),
        )
        new_attempt = result._extra.get("attempt")
        if new_attempt is not None:
            old["superseded_by"] = new_attempt["attempt_id"]
            new_attempt["correction_of"] = old["attempt_id"]
            result.record["correction_of"] = old["attempt_id"]
            result.record["original_attempt_id"] = old["attempt_id"]
            result._extra["original_attempt"] = old
        return result

    def approve_attempt(self, attempt_id: str, **kwargs: Any) -> RecoveryResult:
        """审批回执；已被更正版替代的旧尝试禁止迟到审批复活。"""
        attempt = self.get_attempt(attempt_id)
        if attempt is None:
            return RecoveryResult(False, f"展平尝试 {attempt_id} 不存在。", {})
        if attempt.get("superseded_by"):
            receipt = self.get_receipt_by_attempt(attempt_id) or {}
            receipt = dict(receipt)
            receipt.update({
                "accepted": False,
                "can_enter_cast": False,
                "stale": True,
                "reason": "该尝试已被完整更正版替代；迟到审批不能恢复旧索引或与新版本混批。",
            })
            return RecoveryResult(False, "审批到达时旧尝试已被更正，按失效版本保留，不允许进入转型。", receipt, stale=True)
        if not attempt.get("valid"):
            return RecoveryResult(False, "完整性校验未通过，不能审批受理。", self.get_receipt_by_attempt(attempt_id) or {})
        limit = int(attempt.get("quota_limit") or self.DEFAULT_LIMIT)
        baseline = self.accepted_rows(attempt["group_id"], exclude_doc_id=attempt["doc_id"])
        if baseline + int(attempt["actual_rows"]) > limit and not kwargs.get("override_quota"):
            return RecoveryResult(False, "审批时额度仍不足；请等待更正版或调整整组额度。", self.get_receipt_by_attempt(attempt_id) or {})
        attempt["accepted"] = True
        attempt["confirmed"] = True
        attempt["can_enter_cast"] = True
        attempt["status"] = "已确认"
        attempt["admission_status"] = "已受理"
        attempt["approval_status"] = kwargs.get("approval_status") or "审批通过"
        attempt["decided_at"] = self._now(kwargs.get("at"))
        receipt = self.get_receipt_by_attempt(attempt_id)
        if receipt:
            receipt.update({"accepted": True, "confirmed": True, "can_enter_cast": True, "status": "可在取值转型页续做"})
        return RecoveryResult(True, f"尝试 {attempt_id} 已审批确认。", receipt or {})

    # ------------------------------------------------------------------
    # 查询历史
    # ------------------------------------------------------------------
    def get_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        return next((a for a in self._state()["attempts"] if a.get("attempt_id") == attempt_id), None)

    def get_latest_attempt(self, doc_id: str, group_id: str | None = None) -> dict[str, Any] | None:
        candidates = [
            a for a in self._state()["attempts"]
            if a.get("doc_id") == doc_id and (group_id is None or a.get("group_id") == group_id)
        ]
        return candidates[-1] if candidates else None

    def get_original_failed_attempt(self, doc_id: str, group_id: str | None = None) -> dict[str, Any] | None:
        candidates = [
            a for a in self._state()["attempts"]
            if a.get("doc_id") == doc_id and (group_id is None or a.get("group_id") == group_id)
        ]
        for attempt in candidates:
            if not attempt.get("accepted"):
                return attempt
        return candidates[0] if candidates else None

    def list_attempts(self, doc_id: str | None = None, group_id: str | None = None) -> list[dict[str, Any]]:
        return [
            a for a in self._state()["attempts"]
            if (doc_id is None or a.get("doc_id") == doc_id)
            and (group_id is None or a.get("group_id") == group_id)
        ]

    def get_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        return next((r for r in self._state()["receipts"] if r.get("receipt_id") == receipt_id), None)

    def get_receipt_by_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        return next((r for r in self._state()["receipts"] if r.get("attempt_id") == attempt_id), None)

    def list_receipts(self, group_id: str | None = None, *, confirmed_only: bool = False) -> list[dict[str, Any]]:
        return [
            r for r in self._state()["receipts"]
            if (group_id is None or r.get("group_id") == group_id)
            and (not confirmed_only or r.get("confirmed") or r.get("accepted"))
        ]

    # ------------------------------------------------------------------
    # 取值转型领取
    # ------------------------------------------------------------------
    def claim_confirmed(self, **kwargs: Any) -> RecoveryResult:
        state = self._state()
        group_id = str(kwargs.get("group_id") or kwargs.get("quota_id") or kwargs.get("batch_group") or "DG-DEFAULT")
        claim_key = kwargs.get("claim_id") or kwargs.get("idempotency_key") or kwargs.get("claim_key")
        if claim_key:
            existing = next((c for c in state["claims"] if c.get("claim_id") == claim_key or c.get("idempotency_key") == claim_key), None)
            if existing:
                replay = dict(existing)
                original_rows = int(existing.get("new_rows") or existing.get("total_rows") or 0)
                replay.update({
                    "idempotent_replay": True,
                    "duplicate": True,
                    "new_rows": 0,
                    "executed_rows": 0,
                    "duplicate_rows": original_rows,
                    "message": "相同领取请求已处理，返回原领取回执；本次调用不重复执行。",
                })
                return RecoveryResult(True, replay["message"], replay, idempotent_replay=True)

        receipt_ids = kwargs.get("receipt_ids") or kwargs.get("receipt_id")
        if isinstance(receipt_ids, str):
            receipt_ids = [receipt_ids]
        doc_ids = kwargs.get("doc_ids")
        if isinstance(doc_ids, str):
            doc_ids = [doc_ids]

        include_unconfirmed = bool(kwargs.get("include_unconfirmed", False))
        if receipt_ids:
            wanted = {str(x) for x in receipt_ids}
            receipts = [r for r in state["receipts"] if r.get("receipt_id") in wanted or r.get("attempt_id") in wanted]
        else:
            receipts = self.list_receipts(
                group_id,
                confirmed_only=not include_unconfirmed,
            )
        if doc_ids:
            wanted_docs = {str(x) for x in doc_ids}
            receipts = [r for r in receipts if r.get("doc_id") in wanted_docs]

        # 同一文档若已有更新的确认回执，领取时以最新完整版本为准；旧失败回执
        # 仍可查询，但不会被误装进批次。
        decisions: list[dict[str, Any]] = []
        included_rows: list[dict[str, Any]] = []
        executed_rows = 0
        cast_ok = 0
        cast_failed = 0
        duplicate_rows = 0
        batch_id = str(kwargs.get("batch_id") or kwargs.get("contract_batch_id") or f"PB-CONTRACT-{group_id}")
        cast_no = kwargs.get("cast_no") or kwargs.get("rule_id") or kwargs.get("转型编号")
        execute = kwargs.get("execute", True)
        at = self._now(kwargs.get("at") or kwargs.get("claim_time"))
        claim_id = self._next_id(kwargs.get("claim_prefix", "VC-CLAIM"), claim_key)

        for receipt in receipts:
            attempt = self.get_attempt(str(receipt["attempt_id"]))
            base_decision = {
                "receipt_id": receipt["receipt_id"],
                "回执编号": receipt["receipt_id"],
                "attempt_id": receipt["attempt_id"],
                "尝试编号": receipt["attempt_id"],
                "doc_id": receipt["doc_id"],
                "文档编号": receipt["doc_id"],
                "doc_name": receipt.get("doc_name"),
                "文档名称": receipt.get("doc_name"),
                "version": receipt.get("version"),
                "版本": receipt.get("version"),
                "actual_rows": receipt.get("actual_rows"),
                "row_count": receipt.get("actual_rows"),
                "实际展开行数": receipt.get("actual_rows"),
                "entered_cast": False,
                "能否进入转型": False,
                "included_in_claim": False,
                "本次是否领取": False,
                "executed": False,
                "是否执行": False,
                "duplicate": False,
                "是否重复": False,
                "can_enter_cast": False,
                "本次能否进入转型": False,
            }
            if attempt is None:
                base_decision["reason"] = "展平尝试不存在"
                decisions.append(base_decision)
                continue
            original_failed = self.get_original_failed_attempt(attempt["doc_id"], group_id)
            base_decision["original_failed_attempt_id"] = (
                original_failed["attempt_id"] if original_failed and original_failed["attempt_id"] != attempt["attempt_id"] else None
            )
            if receipt.get("claimed") or attempt.get("claimed"):
                base_decision.update({
                    "duplicate": True,
                    "是否重复": True,
                    "already_claimed": True,
                    "original_claim_id": receipt.get("claim_id") or attempt.get("claim_id"),
                    "原领取编号": receipt.get("claim_id") or attempt.get("claim_id"),
                    "reason": "该文档已经领取并执行，本次跳过，不重复执行。",
                    "结论": "该文档已经领取并执行，本次跳过，不重复执行。",
                })
                duplicate_rows += int(receipt.get("actual_rows") or 0)
                decisions.append(base_decision)
                continue
            if not receipt.get("confirmed") or not attempt.get("confirmed") or not attempt.get("complete"):
                base_decision["reason"] = receipt.get("reason") or "只有完整且被确认的文档行才能进入取值转型。"
                decisions.append(base_decision)
                continue
            if attempt.get("superseded_by"):
                base_decision["reason"] = "该尝试已被更正版替代，不能进入批次。"
                decisions.append(base_decision)
                continue

            base_decision.update({
                "entered_cast": True,
                "能否进入转型": True,
                "included_in_claim": True,
                "本次是否领取": True,
                "can_enter_cast": True,
                "本次能否进入转型": True,
                "reason": "完整且已确认，本次领取进入取值转型。",
                "结论": "完整且已确认，本次领取进入取值转型。",
            })
            batch_rows, ok_count, fail_count = self._execute_attempt_rows(
                attempt, batch_id, cast_no, at, execute=execute,
            )
            included_rows.extend(batch_rows)
            executed_rows += len(batch_rows) if execute else 0
            cast_ok += ok_count
            cast_failed += fail_count
            base_decision.update({
                "executed": bool(execute),
                "是否执行": bool(execute),
                "executed_rows": len(batch_rows),
                "执行行数": len(batch_rows),
                "cast_ok": ok_count,
                "转型成功数": ok_count,
                "cast_failed": fail_count,
                "转型失败数": fail_count,
                "batch_id": batch_id,
                "契约输入批次": batch_id,
            })
            decisions.append(base_decision)

            receipt["claimed"] = True
            receipt["是否已领取"] = True
            receipt["claim_id"] = claim_id
            receipt["领取编号"] = claim_id
            receipt["claimed_at"] = at
            receipt["batch_id"] = batch_id
            attempt["claimed"] = True
            attempt["是否已领取"] = True
            attempt["claim_id"] = claim_id
            attempt["领取编号"] = claim_id
            attempt["claimed_at"] = at
            attempt["领取时间"] = at
            attempt["batch_id"] = batch_id
            attempt["契约输入批次"] = batch_id
            attempt["status"] = "已领取"
            attempt["状态"] = "已领取"

        # 持久化契约输入行（按 receipt/row 幂等）。
        batch = state["contract_batches"].setdefault(batch_id, {
            "batch_id": batch_id,
            "group_id": group_id,
            "cast_no": cast_no,
            "rows": [],
            "created_at": at,
        })
        existing_keys = {row["row_key"] for row in batch["rows"]}
        for row in included_rows:
            if row["row_key"] not in existing_keys:
                batch["rows"].append(row)
                existing_keys.add(row["row_key"])

        parse_batch = self._sync_parse_batch(batch_id, group_id, batch["rows"], at)
        result = {
            "claim_id": claim_id,
            "领取编号": claim_id,
            "idempotency_key": claim_key,
            "group_id": group_id,
            "整组编号": group_id,
            "batch_id": batch_id,
            "contract_batch_id": batch_id,
            "契约输入批次": batch_id,
            "cast_no": cast_no,
            "转型编号": cast_no,
            "decisions": decisions,
            "documents": decisions,
            "文档回执": decisions,
            "included": [d for d in decisions if d["included_in_claim"]],
            "已领取文档": [d for d in decisions if d["included_in_claim"]],
            "skipped": [d for d in decisions if not d["included_in_claim"]],
            "跳过文档": [d for d in decisions if not d["included_in_claim"]],
            "total_receipts": len(decisions),
            "回执总数": len(decisions),
            "included_count": sum(1 for d in decisions if d["included_in_claim"]),
            "领取文档数": sum(1 for d in decisions if d["included_in_claim"]),
            "skipped_count": sum(1 for d in decisions if not d["included_in_claim"]),
            "跳过文档数": sum(1 for d in decisions if not d["included_in_claim"]),
            "total_rows": len(batch["rows"]),
            "批次总行数": len(batch["rows"]),
            "new_rows": len(included_rows),
            "本次新增行数": len(included_rows),
            "executed_rows": executed_rows,
            "执行行数": executed_rows,
            "duplicate_rows": duplicate_rows,
            "重复跳过行数": duplicate_rows,
            "cast_ok": cast_ok,
            "转型成功行数": cast_ok,
            "cast_failed": cast_failed,
            "转型失败行数": cast_failed,
            "parse_batch": parse_batch,
            "批次统计": parse_batch,
            "rows": batch["rows"],
            "契约输入行": batch["rows"],
            "claimed_at": at,
            "领取时间": at,
        }
        doc_summary = "；".join(
            f"{d['doc_name']} {'进入转型' if d['entered_cast'] else '不进入（' + d.get('reason', '') + '）'}"
            for d in decisions
        ) or "没有可领取的已确认回执"
        result["message"] = f"领取回执 {claim_id}：{doc_summary}。"
        state["claims"].append(result)
        return RecoveryResult(True, result["message"], result, claim=result)

    def _execute_attempt_rows(
        self,
        attempt: dict[str, Any],
        batch_id: str,
        cast_no: str | None,
        at: str,
        *,
        execute: bool,
    ) -> tuple[list[dict[str, Any]], int, int]:
        batch_rows: list[dict[str, Any]] = []
        ok_count = 0
        fail_count = 0
        cast_service = None
        if cast_no and execute:
            from app.services.value_cast import ValueCastService
            cast_service = ValueCastService()

        for source_row in attempt.get("rows", []):
            raw_data = dict(source_row.get("data") or {})
            cast_values = dict(raw_data)
            failures: list[dict[str, Any]] = []
            if cast_service and cast_no:
                cast_values, failures = cast_service.cast_row(cast_no, raw_data, at)
                if failures:
                    self._quarantine_recovery_failure(
                        attempt, source_row, batch_id, cast_no, failures, at,
                    )
            status = "转型失败" if failures else "转型成功"
            if failures:
                fail_count += 1
            else:
                ok_count += 1
            row_key = f"{attempt['attempt_id']}:{source_row['row_id']}"
            batch_rows.append({
                "row_key": row_key,
                "batch_id": batch_id,
                "契约输入批次": batch_id,
                "doc_id": attempt["doc_id"],
                "文档编号": attempt["doc_id"],
                "doc_name": attempt.get("doc_name"),
                "文档名称": attempt.get("doc_name"),
                "attempt_id": attempt["attempt_id"],
                "尝试编号": attempt["attempt_id"],
                "version": attempt.get("version"),
                "版本": attempt.get("version"),
                "receipt_id": attempt.get("receipt_id"),
                "回执编号": attempt.get("receipt_id"),
                "row_id": source_row["row_id"],
                "row_index": source_row["row_index"],
                "flat_row_number": source_row["flat_row_number"],
                "array_index": source_row["array_index"],
                "数组元素序号": source_row["array_index"],
                "source_index": source_row["source_index"],
                "array_element_no": source_row["array_element_no"],
                "index_chain": source_row.get("index_chain", []),
                "source_path": attempt.get("source_path"),
                "locator": source_row.get("locator"),
                "错误定位": source_row.get("locator"),
                "raw": raw_data,
                "原始值": raw_data,
                "cast_values": cast_values,
                "转型后值": cast_values,
                "failures": failures,
                "失败明细": failures,
                "status": status,
                "状态": status,
                "executed": execute,
            })
        return batch_rows, ok_count, fail_count

    def _quarantine_recovery_failure(
        self,
        attempt: dict[str, Any],
        source_row: dict[str, Any],
        batch_id: str,
        cast_no: str,
        failures: list[dict[str, Any]],
        at: str,
    ) -> None:
        rows = store.rows("row_quarantine")
        for failure in failures:
            field = str(failure.get("field") or "*")
            quarantine_no = (
                f"RQ-REC-{attempt['doc_id']}-v{attempt.get('version')}-"
                f"idx{source_row['array_index']}-{field}"
            )
            if any(r.get("隔离编号") == quarantine_no for r in rows):
                continue
            entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            rows.append({
                "id": entry_id,
                "status": "已隔离",
                "pending": True,
                "abnormal": True,
                "隔离编号": quarantine_no,
                "关联批次": batch_id,
                "原始行号": str(source_row["array_index"] + 1),
                "原始文档编号": attempt["doc_id"],
                "文档尝试编号": attempt["attempt_id"],
                "原始数组序号": str(source_row["array_index"]),
                "展平实际行号": str(source_row["flat_row_number"]),
                "错误定位": source_row.get("locator"),
                "失败字段": field,
                "失败原因": f"取值转型失败: {failure.get('reason')}",
                "原始行文本": json.dumps(source_row.get("data") or {}, ensure_ascii=False),
                "复核结论": "",
                "隔离时间": at,
            })

    def _sync_parse_batch(self, batch_id: str, group_id: str, rows: list[dict[str, Any]], at: str) -> dict[str, Any]:
        batches = store.rows("parse_batch")
        batch = next((r for r in batches if r.get("批次记录编号") == batch_id or r.get("关联投递批次") == batch_id), None)
        if batch is None:
            entry_id = max((int(r.get("id", 0)) for r in batches), default=0) + 1
            batch = {
                "id": entry_id,
                "status": "解析中",
                "pending": True,
                "abnormal": False,
                "批次记录编号": batch_id,
                "关联投递批次": group_id,
                "解析器标识": "nested_flatten-recovery",
                "总行数": "0",
                "成功行数": "0",
                "隔离行数": "0",
                "批次状态": "进行中",
                "完成时间": "",
                "契约输入批次": "是",
            }
            batches.append(batch)
        total = len(rows)
        failed = sum(1 for r in rows if r.get("status") == "转型失败")
        success = total - failed
        batch.update({
            "总行数": str(total),
            "成功行数": str(success),
            "隔离行数": str(failed),
            "批次状态": "存在隔离" if failed else "已闭合",
            "status": "存在隔离" if failed else "已闭合",
            "pending": failed > 0,
            "abnormal": failed > 0,
            "完成时间": at if not failed else "",
        })
        return dict(batch)

    def get_contract_batch(self, batch_id: str) -> dict[str, Any] | None:
        return self._state()["contract_batches"].get(batch_id)

    def list_claims(self, group_id: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self._state()["claims"] if group_id is None or c.get("group_id") == group_id]


recovery_center = DocumentRecoveryCenter()

# 便于不同测试/调用习惯按类名导入。
DocumentRecoveryService = DocumentRecoveryCenter
FlattenRecoveryService = DocumentRecoveryCenter
OverLimitRecoveryService = DocumentRecoveryCenter
OverLimitRecovery = DocumentRecoveryCenter
FlattenRecovery = DocumentRecoveryCenter
