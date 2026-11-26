"""取值转型领域服务：按目标类型将字符串取值转为强类型字段并记录失败数，产出转型规则数据集。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.base import BaseModuleService
from app.services.document_recovery import recovery_center
from app.store import store


class ValueCastService(BaseModuleService):
    MODULE = "value_cast"
    ENTITY = "转型规则"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["转型编号", "目标类型", "来源类型", "日期格式约定", "数值精度", "空值填充", "转型失败数", "最近执行"]
    REQUIRED_FIELDS = ["转型编号", "目标类型", "来源类型"]
    STATUS_ORDER = ["待执行", "执行中", "已完成", "存在失败"]
    ACTION_RULES = {"启动转型": "执行中", "确认完成": "已完成", "登记失败": "存在失败"}
    NEGATIVE_ACTIONS = ["登记失败"]

    # 业务侧用 yyyy-MM-dd 约定日期格式，转型前需翻译成 strptime 的格式串
    PATTERN_TOKENS = (("yyyy", "%Y"), ("yy", "%y"), ("MM", "%m"), ("dd", "%d"),
                      ("HH", "%H"), ("mm", "%M"), ("ss", "%S"))

    @classmethod
    def to_strptime_pattern(cls, pattern: str) -> str:
        """把 yyyy-MM-dd HH:mm:ss 这类业务约定翻译成 strptime 格式串。"""
        if not pattern:
            return "%Y-%m-%d %H:%M:%S"
        if "%" in pattern:
            return pattern
        translated = pattern
        for token, replacement in cls.PATTERN_TOKENS:
            translated = translated.replace(token, replacement)
        return translated

    def cast_value(self, raw: Any, target_type: str, date_pattern: str = "",
                   precision: int = 0, null_fill: Any = None) -> tuple[bool, Any, str]:
        """单值转型：失败时返回原因，不用空值填充伪装成功。"""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if null_fill is None:
                return False, None, f"取值为空且未配置空值填充，无法转型为 {target_type}"
            return True, null_fill, ""

        text = str(raw).strip()
        try:
            if target_type in ("int", "int64", "integer"):
                return True, int(text), ""
            if target_type in ("float", "float64", "double", "decimal"):
                value = float(text)
                return True, (round(value, precision) if precision else value), ""
            if target_type in ("date", "datetime", "datetime64"):
                pattern = self.to_strptime_pattern(date_pattern)
                return True, datetime.strptime(text.replace("T", " "), pattern).isoformat(), ""
            if target_type == "bool":
                lowered = text.lower()
                if lowered in ("true", "1", "yes", "y"):
                    return True, True, ""
                if lowered in ("false", "0", "no", "n"):
                    return True, False, ""
                return False, None, f"取值 {text!r} 无法转型为 bool"
            if target_type in ("str", "string", "text", "category"):
                return True, text, ""
            return False, None, f"未知目标类型 {target_type}"
        except (ValueError, TypeError) as exc:
            return False, None, f"取值 {text!r} 转型 {target_type} 失败: {exc}"

    def cast_row(self, cast_no: str, row: dict[str, Any],
                 execute_time: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """按转型规则对一行执行转型，返回 (转型后取值, 失败明细)。

        失败数按行回写坏行隔离，因此失败明细必须带字段名与失败原因。
        """
        rule = next(
            (r for r in store.rows(self.MODULE) if r.get("转型编号") == cast_no), None)
        if rule is None:
            return {}, [{"field": "*", "reason": f"转型规则 {cast_no} 不存在"}]

        target_type = str(rule.get("目标类型") or "")
        date_pattern = str(rule.get("日期格式约定") or "")
        precision_text = str(rule.get("数值精度") or "").strip()
        precision = int(precision_text) if precision_text.isdigit() else 0
        null_fill_text = str(rule.get("空值填充") or "").strip()
        null_fill: Any = null_fill_text or None
        if null_fill_text.upper() in ("NAN", "NAT"):
            null_fill = None

        cast_values: dict[str, Any] = {}
        failures: list[dict[str, Any]] = []
        for field, raw in row.items():
            ok, value, reason = self.cast_value(
                raw, target_type, date_pattern, precision, null_fill)
            if ok:
                cast_values[field] = value
            else:
                failures.append({"field": field, "reason": reason, "raw": str(raw)})

        self._record_execution(cast_no, len(failures), execute_time)
        return cast_values, failures

    def _record_execution(self, cast_no: str, failure_count: int,
                          execute_time: str) -> None:
        """回写规则执行结果：失败数累加并置为存在失败状态。"""
        for rule in store.rows(self.MODULE):
            if rule.get("转型编号") != cast_no:
                continue
            existing_text = str(rule.get("转型失败数") or "0").strip()
            existing = int(existing_text) if existing_text.isdigit() else 0
            rule["转型失败数"] = str(existing + failure_count)
            rule["最近执行"] = execute_time
            if failure_count > 0:
                rule["status"] = "存在失败"
                rule["pending"] = True
                rule["abnormal"] = True
            else:
                rule["status"] = "已完成"
                rule["pending"] = False
                rule["abnormal"] = False
            return

    def quarantine_failures(self, cast_no: str, intake_batch: str, row_num: int,
                            failures: list[dict[str, Any]], raw_text: str,
                            quarantine_time: str, *,
                            locator: str | None = None,
                            array_index: int | None = None,
                            doc_id: str | None = None,
                            attempt_id: str | None = None) -> list[str]:
        """把转型失败按行写入坏行隔离，返回生成的隔离编号。"""
        created: list[str] = []
        for failure in failures:
            quarantine_no = f"RQ-{intake_batch}-{row_num}-{failure['field']}"
            rows = store.rows("row_quarantine")
            if any(r.get("隔离编号") == quarantine_no for r in rows):
                continue
            entry_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            rows.append({
                "id": entry_id, "status": "已隔离", "pending": True, "abnormal": False,
                "隔离编号": quarantine_no, "关联批次": intake_batch,
                "原始行号": str(row_num), "失败字段": str(failure["field"]),
                "失败原因": f"取值转型失败: {failure['reason']}",
                "原始行文本": raw_text, "复核结论": "", "隔离时间": quarantine_time,
                "原始文档编号": doc_id or "",
                "文档尝试编号": attempt_id or "",
                "原始数组序号": "" if array_index is None else str(array_index),
                "错误定位": locator or "",
            })
            created.append(quarantine_no)
        return created

    def failure_summary(self, cast_no: str) -> dict[str, Any]:
        """输出规则的失败计数，供解析指标与坏行隔离对账。"""
        for rule in store.rows(self.MODULE):
            if rule.get("转型编号") == cast_no:
                text = str(rule.get("转型失败数") or "0").strip()
                return {
                    "转型编号": cast_no,
                    "目标类型": rule.get("目标类型"),
                    "转型失败数": int(text) if text.isdigit() else 0,
                    "最近执行": rule.get("最近执行"),
                    "status": rule.get("status"),
                }
        return {"转型编号": cast_no, "转型失败数": 0, "status": "规则不存在"}

    # ------------------------------------------------------------------
    # 从嵌套展平回执领取完整且已确认的结果
    # ------------------------------------------------------------------
    def claim_confirmed_results(self, **kwargs: Any):
        """领取已确认展平文档并写入契约输入批次；按回执/领取键幂等。"""
        return recovery_center.claim_confirmed(**kwargs)

    def claim_flatten_receipts(self, **kwargs: Any):
        return recovery_center.claim_confirmed(**kwargs)

    def claim_document_results(self, **kwargs: Any):
        return recovery_center.claim_confirmed(**kwargs)

    def consume_processing_receipt(self, receipt_id: str, **kwargs: Any):
        """领取并执行单份文档回执；重复调用不会重复执行。"""
        return recovery_center.claim_confirmed(receipt_ids=[receipt_id], **kwargs)

    def get_contract_input_batch(self, batch_id: str) -> dict[str, Any] | None:
        return recovery_center.get_contract_batch(batch_id)

    def list_recovery_claims(self, group_id: str | None = None) -> list[dict[str, Any]]:
        return recovery_center.list_claims(group_id)

    claim_confirmed = claim_confirmed_results
    claim_for_cast = claim_confirmed_results
    claim_documents = claim_confirmed_results
    claim_processing_receipts = claim_flatten_receipts
    consume_receipt = consume_processing_receipt
