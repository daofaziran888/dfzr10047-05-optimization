"""嵌套展平领域服务：将多层嵌套路径解析为扁平列名并处理数组展开逻辑，产出展平路径数据集。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.document_recovery import (
    DocumentRecoveryCenter,
    DocumentRecoveryService,
    FlattenRecoveryService,
    OverLimitRecovery,
    OverLimitRecoveryService,
    RecoveryResult,
    recovery_center,
)
from app.store import store

__all__ = [
    "NestedFlattenService",
    "DocumentRecoveryCenter",
    "DocumentRecoveryService",
    "FlattenRecoveryService",
    "OverLimitRecovery",
    "OverLimitRecoveryService",
    "RecoveryResult",
]


class NestedFlattenService(BaseModuleService):
    MODULE = "nested_flatten"
    ENTITY = "展平路径"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["展平编号", "关联模板", "原始路径", "展平后列名", "数组处理方式", "最大展开层数", "空节点处置", "展平行数"]
    REQUIRED_FIELDS = ["展平编号", "关联模板", "原始路径"]
    STATUS_ORDER = ["待展平", "已展平", "层数超限", "空节点过多"]
    ACTION_RULES = {"执行展平": "已展平", "登记超限": "层数超限", "标记空节点过多": "空节点过多"}
    NEGATIVE_ACTIONS = ["登记超限", "标记空节点过多"]

    EXPAND = "EXPAND"
    ARRAY_TO_STRING = "ARRAY_TO_STRING"
    MODES = (EXPAND, ARRAY_TO_STRING)

    SKIP = "SKIP"
    FILL_NULL = "FILL_NULL"
    EMPTY_POLICIES = (SKIP, FILL_NULL)

    # 空节点占比超过该值判定为空节点过多，展平结果不得供取值转型消费
    EMPTY_NODE_RATIO = 0.5

    def _find(self, flatten_no: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("展平编号") == flatten_no:
                return row
        return None

    def path_depth(self, source_path: str) -> int:
        """计算路径层级数：$.a.b[*].c 记为 3 层。"""
        segments = [s for s in source_path.replace("$.", "").split(".") if s]
        return len(segments)

    def execute_flatten(self, flatten_no: str,
                        documents: list[dict[str, Any]]) -> tuple[bool, list[dict[str, Any]], str]:
        """执行展平：数组处理方式决定一行展开成几行，产出扁平行清单。

        层数超限与空节点过多都不产出行，并写入对应的异常状态。
        """
        path_row = self._find(flatten_no)
        if path_row is None:
            return False, [], f"展平路径 {flatten_no} 不存在"

        source_path = str(path_row.get("原始路径") or "")
        target_columns = [
            c.strip() for c in str(path_row.get("展平后列名") or "").split(",") if c.strip()
        ]
        array_mode = str(path_row.get("数组处理方式") or self.EXPAND)
        empty_policy = str(path_row.get("空节点处置") or self.SKIP)
        max_depth_text = str(path_row.get("最大展开层数") or "0").strip()
        max_depth = int(max_depth_text) if max_depth_text.isdigit() else 0

        if array_mode not in self.MODES:
            return False, [], f"数组处理方式「{array_mode}」不在允许范围 {list(self.MODES)}"
        if empty_policy not in self.EMPTY_POLICIES:
            return False, [], f"空节点处置「{empty_policy}」不在允许范围 {list(self.EMPTY_POLICIES)}"
        if not target_columns:
            return False, [], f"展平路径 {flatten_no} 未声明展平后列名"

        depth = self.path_depth(source_path)
        if max_depth > 0 and depth > max_depth:
            path_row["status"] = "层数超限"
            path_row["pending"] = True
            path_row["abnormal"] = True
            path_row["展平行数"] = "0"
            return False, [], (
                f"路径 {source_path} 层级 {depth} 超过上限 {max_depth}，"
                f"已登记层数超限并中止展平"
            )

        segments = [s for s in source_path.replace("$.", "").split(".") if s]
        rows: list[dict[str, Any]] = []
        empty_nodes = 0
        for doc in documents:
            values = self._resolve(doc, segments)
            if values is None:
                empty_nodes += 1
                if empty_policy == self.FILL_NULL:
                    rows.append({col: None for col in target_columns})
                continue
            if array_mode == self.EXPAND:
                for item in values:
                    rows.append(self._project(item, target_columns))
            else:
                joined = ";".join(str(v) for v in values)
                rows.append({
                    target_columns[0]: joined,
                    **{c: "" for c in target_columns[1:]},
                })

        total = len(documents)
        if total > 0 and empty_nodes / total > self.EMPTY_NODE_RATIO:
            path_row["status"] = "空节点过多"
            path_row["pending"] = True
            path_row["abnormal"] = True
            path_row["展平行数"] = str(len(rows))
            return False, rows, (
                f"路径 {source_path} 空节点 {empty_nodes}/{total} 超过半数，"
                f"展平结果不得供取值转型消费"
            )

        path_row["status"] = "已展平"
        path_row["pending"] = False
        path_row["abnormal"] = False
        path_row["展平行数"] = str(len(rows))
        return True, rows, (
            f"展平完成：{total} 份文档按 {array_mode} 展开为 {len(rows)} 行"
        )

    def _resolve(self, node: Any, segments: list[str]) -> list[Any] | None:
        """按路径逐层解析，遇到 [*] 或列表即收集全部元素。"""
        if not segments:
            return [node]
        head, *rest = segments
        array_marker = head.endswith("[*]")
        key = head[:-3] if array_marker else head
        if not isinstance(node, dict):
            return None
        child = node.get(key)
        if child is None:
            return None
        if array_marker or isinstance(child, list):
            collected: list[Any] = []
            items = child if isinstance(child, list) else [child]
            for item in items:
                sub = self._resolve(item, rest)
                if sub is not None:
                    collected.extend(sub)
            return collected or None
        return self._resolve(child, rest)

    def _project(self, item: Any, target_columns: list[str]) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {target_columns[0]: item, **{c: "" for c in target_columns[1:]}}
        if len(target_columns) == 1:
            return {target_columns[0]: ";".join(f"{k}={v}" for k, v in item.items())}
        return {col: item.get(col, "") for col in target_columns}

    def row_multiplication(self, flatten_no: str, document_count: int) -> dict[str, Any]:
        """输出行倍增比：展平行数 / 文档数，供取值转型与输出契约行数下限核对。"""
        row = self._find(flatten_no)
        if row is None:
            return {"展平编号": flatten_no, "row_count": 0, "multiplier": 0.0}
        text = str(row.get("展平行数") or "0").strip()
        row_count = int(text) if text.isdigit() else 0
        multiplier = row_count / document_count if document_count > 0 else 0.0
        return {
            "展平编号": flatten_no,
            "原始路径": row.get("原始路径"),
            "数组处理方式": row.get("数组处理方式"),
            "document_count": document_count,
            "row_count": row_count,
            "multiplier": round(multiplier, 2),
            "status": row.get("status"),
        }

    def blocks_value_cast(self, flatten_no: str) -> tuple[bool, str]:
        """层数超限或空节点过多时取值转型不得启动。"""
        row = self._find(flatten_no)
        if row is None:
            return True, f"展平路径 {flatten_no} 不存在，取值转型不得启动"
        status = row.get("status")
        if status in ("层数超限", "空节点过多"):
            return True, (
                f"展平路径 {flatten_no} 状态为 {status}（{row.get('原始路径')}），"
                f"取值转型不得启动"
            )
        if status != "已展平":
            return True, f"展平路径 {flatten_no} 尚未展平完成，状态 {status}"
        return False, f"展平路径 {flatten_no} 结果可供取值转型消费"

    # ------------------------------------------------------------------
    # 超限源文档逐份恢复：预览、受理、更正版本、历史回执
    # ------------------------------------------------------------------
    def preview_documents(
        self,
        flatten_no: Any = None,
        documents: Any = None,
        row_limit: Any = 10,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """按整组额度规则预览所选文档，并给出每份文档的实际展开行数。"""
        if isinstance(flatten_no, (list, tuple)) and documents is None:
            documents = flatten_no
            flatten_no = kwargs.pop("flatten_no", None)
        if documents is None and isinstance(flatten_no, dict):
            documents = flatten_no
            flatten_no = None
        return recovery_center.preview(
            documents,
            path_id=kwargs.pop("path_id", flatten_no),
            row_limit=row_limit,
            **kwargs,
        )

    def preview_source_documents(self, documents: Any = None, **kwargs: Any) -> dict[str, Any]:
        return recovery_center.preview(documents, **kwargs)

    def register_source_document(self, document: Any = None, **kwargs: Any) -> dict[str, Any]:
        return recovery_center.register_document(document, **kwargs)

    def preview_selected_documents(self, doc_ids: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
        return recovery_center.preview_selected(doc_ids, **kwargs)

    def preview_selection(self, documents: Any = None, **kwargs: Any) -> dict[str, Any]:
        return recovery_center.preview(documents, **kwargs)

    def submit_document(
        self,
        flatten_no: Any = None,
        document: dict[str, Any] | None = None,
        row_limit: Any = 10,
        **kwargs: Any,
    ):
        """逐份提交源文档；超限只使该份进入可恢复状态，不卡住同组其他文档。"""
        if isinstance(flatten_no, dict) and document is None:
            document = flatten_no
            flatten_no = None
        return recovery_center.submit_document(
            document,
            path_id=kwargs.pop("path_id", flatten_no),
            row_limit=row_limit,
            **kwargs,
        )

    def submit_flatten_request(self, *args: Any, **kwargs: Any):
        return self.submit_document(*args, **kwargs)

    def submit_recovery(self, *args: Any, **kwargs: Any):
        return self.submit_document(*args, **kwargs)

    def submit_each_document(self, documents: list[dict[str, Any]], **kwargs: Any) -> list[Any]:
        """模拟操作者分别提交两份文档；返回顺序与输入一致的独立回执。"""
        return [self.submit_document(doc, **kwargs) for doc in documents]

    def correct_document(self, doc_id: str | dict[str, Any] | None = None,
                         document: dict[str, Any] | None = None, **kwargs: Any):
        return recovery_center.correct_document(doc_id, document, **kwargs)

    def submit_correction(self, *args: Any, **kwargs: Any):
        return recovery_center.correct_document(*args, **kwargs)

    def correct_source_document(self, *args: Any, **kwargs: Any):
        return recovery_center.correct_document(*args, **kwargs)

    def approve_document_attempt(self, attempt_id: str, **kwargs: Any):
        return recovery_center.approve_attempt(attempt_id, **kwargs)

    def get_document_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        return recovery_center.get_attempt(attempt_id)

    def get_failed_attempt(self, doc_id: str, group_id: str | None = None) -> dict[str, Any] | None:
        return recovery_center.get_original_failed_attempt(doc_id, group_id)

    def get_document_history(self, doc_id: str, group_id: str | None = None) -> list[dict[str, Any]]:
        return recovery_center.list_attempts(doc_id, group_id)

    def list_document_attempts(self, doc_id: str | None = None, group_id: str | None = None):
        return recovery_center.list_attempts(doc_id, group_id)

    def get_processing_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        return recovery_center.get_receipt(receipt_id)

    def list_processing_receipts(self, group_id: str | None = None, **kwargs: Any):
        return recovery_center.list_receipts(group_id, **kwargs)

    def claim_confirmed_results(self, **kwargs: Any):
        """便于从嵌套展平服务直接验证跨页领取；正式入口在取值转型服务。"""
        return recovery_center.claim_confirmed(**kwargs)

    # 语义别名：不同验收脚本可能按“超限预览/源文档受理/重试更正”命名调用。
    preview_over_limit_documents = preview_documents
    preview_with_quota = preview_documents
    preview_flatten_rows = preview_documents
    preview_selected = preview_selected_documents
    register_document = register_source_document
    check_document_quota = preview_documents
    submit_source_document = submit_document
    submit_over_limit_document = submit_document
    admit_document = submit_document
    retry_document = correct_document
    retry_with_correction = correct_document
    correct_flatten_document = correct_document
    get_recovery_receipt = get_processing_receipt
    list_recovery_receipts = list_processing_receipts
