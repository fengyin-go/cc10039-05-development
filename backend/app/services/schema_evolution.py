"""Schema 演进领域服务：管理湖表 Schema 的可控演进、兼容性检查和回滚。

上游依赖快照版本模块提供变更前快照，下游联动回填任务进行存量数据迁移。
兼容性检查是核心：变更类型与兼容级别必须匹配，不兼容变更需要回填。

回滚候选视图：读取本表可用快照的字段定义，列出可用版本和待恢复列的名称、
类型差异；值班员在视图中交付恢复材料后发起目标字段回滚，回执写回实际读取
的快照版本和字段定义，不允许把一个版本的编号和另一版的列定义拼到一起。
"""
from __future__ import annotations

import datetime
import re
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


# 合法兼容级别及其允许的变更类型
COMPATIBILITY_RULES: dict[str, set[str]] = {
    "向后兼容": {"添加字段", "修改默认值", "添加索引"},
    "向前兼容": {"删除字段", "放宽约束"},
    "不兼容": {"修改字段类型", "重命名字段", "删除字段"},
}

# 恢复材料可选范围：仅待恢复列 / 整版列（该版本的全部列）
RECOVERY_SCOPES = ("仅待恢复列", "整版列")

COLUMN_DEF_FIELD = "字段定义"
CURRENT_DEF_FIELD = "当前字段定义"
MATERIAL_FIELD = "恢复材料"
RECEIPT_FIELD = "回滚回执"


def _split_columns(raw: Any) -> list[str]:
    """把变更字段拆成列名列表，支持顿号、逗号、分号和空白分隔。"""
    return [part for part in re.split(r"[、,，;/；\s]+", str(raw or "")) if part]


def _defs_to_map(defs: Any) -> dict[str, str]:
    """把 [{列名, 类型}] 形式的字段定义规整成 {列名: 类型}。"""
    result: dict[str, str] = {}
    for item in defs or []:
        name = str(item.get("列名", "")).strip()
        if name:
            result[name] = str(item.get("类型", "")).strip()
    return result


class SchemaEvolutionService(BaseModuleService):
    MODULE = "schema_evolution"
    ENTITY = "Schema变更"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["湖表名", "变更类型", "变更字段", "兼容级别", "变更前快照"]
    REQUIRED_FIELDS = ["湖表名", "变更类型", "变更字段"]
    STATUS_ORDER = ["草稿", "待审批", "已执行", "已回滚"]
    ACTION_RULES = {"提交审批": "待审批", "执行变更": "已执行"}
    NEGATIVE_ACTIONS = []

    def submit_approval(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """提交审批：执行兼容性检查。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "草稿":
            return None, f"Schema 变更状态为{entry.get('status')}，不可提交审批"

        change_type = entry.get("变更类型", "")
        compat_level = entry.get("兼容级别", "")

        # 兼容性检查
        allowed = COMPATIBILITY_RULES.get(compat_level, set())
        if change_type not in allowed:
            return (
                None,
                f"兼容性检查失败：{compat_level} 不允许 {change_type}，"
                f"允许的操作：{'、'.join(allowed) if allowed else '无'}",
            )

        # 检查变更前快照是否可用
        snapshot_ref = entry.get("变更前快照", "")
        if snapshot_ref:
            snapshots = store.find_by_field("snapshot_version", "版本号", snapshot_ref)
            available = [s for s in snapshots if s.get("status") == "可用"]
            if not available:
                return None, f"变更前快照 {snapshot_ref} 不可用"

        entry["status"] = "待审批"
        entry["pending"] = True
        entry["abnormal"] = False

        store.log_event("schema_submitted", "schema_evolution", "snapshot_version", {
            "entry_id": entry_id,
            "table_name": entry.get("湖表名", ""),
            "change_type": change_type,
            "compat_level": compat_level,
        })

        return entry, f"Schema 变更 {entry.get('湖表名')} - {change_type} 已提交审批，兼容级别={compat_level}"

    def execute_change(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """执行 Schema 变更：联动回填任务进行存量数据迁移。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "待审批":
            return None, f"Schema 变更状态为{entry.get('status')}，不可执行"

        table_name = entry.get("湖表名", "")
        change_type = entry.get("变更类型", "")
        compat_level = entry.get("兼容级别", "")

        # 自身状态流转
        entry["status"] = "已执行"
        entry["pending"] = False
        entry["abnormal"] = False

        # 如果是不兼容变更，触发回填任务
        if compat_level == "不兼容":
            backfill_jobs = store.find_by_field("backfill_job", "目标湖表", table_name)
            if backfill_jobs:
                for bj in backfill_jobs:
                    if bj.get("status") == "待调度":
                        bj["pending"] = True

            store.log_event("schema_to_backfill", "schema_evolution", "backfill_job", {
                "entry_id": entry_id,
                "table_name": table_name,
                "change_type": change_type,
                "compat_level": compat_level,
                "affected_jobs": len(backfill_jobs),
            })

        store.log_event("schema_executed", "schema_evolution", "lake_table", {
            "entry_id": entry_id,
            "table_name": table_name,
            "change_type": change_type,
        })

        return entry, f"Schema 变更 {table_name} - {change_type} 已执行"

    def rollback_change(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """回滚 Schema 变更：恢复到变更前快照。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "已执行":
            return None, f"Schema 变更状态为{entry.get('status')}，不可回滚"

        snapshot_ref = entry.get("变更前快照", "")
        table_name = entry.get("湖表名", "")

        # 尝试恢复快照
        if snapshot_ref:
            # 查找该类快照
            for key in store.snapshot_keys():
                if snapshot_ref in key and table_name in key:
                    entry["status"] = "已回滚"
                    entry["pending"] = False
                    entry["abnormal"] = False
                    store.log_event("schema_rolled_back", "schema_evolution", "snapshot_version", {
                        "entry_id": entry_id,
                        "table_name": table_name,
                        "snapshot_ref": snapshot_ref,
                    })
                    return entry, f"Schema 变更回滚到快照 {snapshot_ref}"

        entry["status"] = "已回滚"
        entry["pending"] = False
        entry["abnormal"] = True
        return entry, f"Schema 变更已回滚（快照 {snapshot_ref} 无法精确恢复）"

    # ---- 回滚候选视图 ----

    def _available_snapshots(self, table_name: str) -> list[dict[str, Any]]:
        """本表可用快照：候选和材料来源都只允许从这里取。"""
        return [
            sv for sv in store.rows("snapshot_version")
            if sv.get("湖表名") == table_name and sv.get("status") == "可用"
        ]

    @staticmethod
    def _snapshot_def_map(snapshot: dict[str, Any]) -> dict[str, str]:
        return _defs_to_map(snapshot.get(COLUMN_DEF_FIELD))

    @staticmethod
    def _downstream_consumers(table_name: str, column: str) -> list[str]:
        """字段级下游依赖：血缘中以上游源 table.column 登记的消费方。"""
        prefix = f"{table_name}.{column}"
        consumers: list[str] = []
        for edge in store.rows("lineage_graph"):
            if edge.get("status") == "链路断裂":
                continue
            if str(edge.get("上游源", "")).strip() == prefix:
                target = str(edge.get("下游消费", "")).strip()
                if target:
                    consumers.append(target)
        return consumers

    def _refetch_snapshot(self, table_name: str, version_no: str) -> dict[str, Any] | None:
        """取不到列定义时重新取材料：仍限定本表可用快照，且必须带列定义。"""
        for snap in self._available_snapshots(table_name):
            if snap.get("版本号") == version_no and self._snapshot_def_map(snap):
                return snap
        return None

    def rollback_candidates(self, entry_id: int) -> tuple[dict[str, Any] | None, str]:
        """回滚候选视图：列出本表可用版本，展示待恢复列的名称和类型差异。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"

        table_name = entry.get("湖表名", "")
        targets = _split_columns(entry.get("变更字段", ""))
        current_defs = _defs_to_map(entry.get(CURRENT_DEF_FIELD))

        candidates: list[dict[str, Any]] = []
        for snap in self._available_snapshots(table_name):
            snap_defs = self._snapshot_def_map(snap)
            diffs: list[dict[str, Any]] = []
            for col in targets:
                snap_type = snap_defs.get(col)
                current_type = current_defs.get(col)
                if snap_type is None:
                    kind = "快照缺少该列"
                elif current_type is None:
                    kind = "当前缺少该列"
                elif snap_type != current_type:
                    kind = "类型不一致"
                else:
                    kind = "一致"
                diffs.append({
                    "列名": col,
                    "快照类型": snap_type,
                    "当前类型": current_type,
                    "差异": kind,
                })
            extra_columns = [c for c in snap_defs if c not in current_defs]
            dependencies = [
                {"列名": col, "下游消费": consumers}
                for col in extra_columns
                if (consumers := self._downstream_consumers(table_name, col))
            ]
            candidates.append({
                "快照ID": snap.get("id"),
                "版本号": snap.get("版本号"),
                "快照时间戳": snap.get("快照时间戳"),
                "快照类型": snap.get("快照类型"),
                "含全部待恢复列": all(col in snap_defs for col in targets),
                "列差异": diffs,
                "额外列": extra_columns,
                "下游依赖": dependencies,
                "交付前需核对下游依赖": bool(dependencies),
            })

        payload = {
            "变更ID": entry.get("id"),
            "湖表名": table_name,
            "变更状态": entry.get("status"),
            "待恢复列": targets,
            "当前字段定义": current_defs,
            "候选": candidates,
            "恢复材料": entry.get(MATERIAL_FIELD),
            "回滚回执": entry.get(RECEIPT_FIELD),
        }
        return payload, f"湖表 {table_name} 共有 {len(candidates)} 份可用快照候选"

    def prepare_rollback_material(
        self,
        entry_id: int,
        snapshot_id: int,
        scope: str = "仅待恢复列",
        deps_confirmed: bool = False,
    ) -> tuple[dict[str, Any] | None, str]:
        """交付恢复材料：从候选快照读取字段定义，按范围圈定本次允许恢复的列。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "已执行":
            return None, f"Schema 变更状态为{entry.get('status')}，仅已执行变更可准备恢复材料"
        if scope not in RECOVERY_SCOPES:
            return None, f"未知恢复范围：{scope}，可选：{'、'.join(RECOVERY_SCOPES)}"

        table_name = entry.get("湖表名", "")
        snapshot = store.find("snapshot_version", int(snapshot_id))
        if snapshot is None:
            return None, f"来源快照 {snapshot_id} 不存在，请核对目标湖表 {table_name} 与来源快照"
        # 候选只纳入本表可用快照，材料来源受同一约束
        if snapshot.get("湖表名") != table_name or snapshot.get("status") != "可用":
            return None, (
                f"来源快照 {snapshot.get('版本号')} 不属于湖表 {table_name} 的可用快照，"
                "请核对目标湖表和来源"
            )

        def_map = self._snapshot_def_map(snapshot)
        if not def_map:
            # 取不到列定义：重新取材料，并再次核对目标湖表和来源
            refetched = self._refetch_snapshot(table_name, str(snapshot.get("版本号", "")))
            if refetched is None:
                return None, (
                    f"取不到快照 {snapshot.get('版本号')} 的列定义，重新取材料失败："
                    f"请核对目标湖表 {table_name} 与来源快照"
                )
            snapshot = refetched
            def_map = self._snapshot_def_map(snapshot)

        targets = [c for c in _split_columns(entry.get("变更字段", "")) if c in def_map]
        if not targets:
            return None, f"快照 {snapshot.get('版本号')} 不含任何待恢复列，无法作为恢复材料"

        current_defs = _defs_to_map(entry.get(CURRENT_DEF_FIELD))
        extra_columns = [c for c in def_map if c not in current_defs]
        pending_deps: dict[str, list[str]] = {}
        if scope == "仅待恢复列":
            allowed = list(targets)
        else:
            # 整版列会带上后续加上的列，交付前需核对这些列的下游依赖
            for col in extra_columns:
                consumers = self._downstream_consumers(table_name, col)
                if consumers:
                    pending_deps[col] = consumers
            if pending_deps and not deps_confirmed:
                detail = "；".join(
                    f"{col}→{'、'.join(consumers)}" for col, consumers in pending_deps.items()
                )
                return None, f"整版列包含后续加上的列，交付前需核对下游依赖：{detail}"
            allowed = list(def_map)

        material = {
            "材料编号": f"RB-{entry_id}-{snapshot['id']}-{len(store._events) + 1}",
            "目标湖表": table_name,
            "来源快照ID": snapshot["id"],
            "来源版本号": snapshot.get("版本号"),
            "范围": scope,
            "待恢复列": targets,
            "允许恢复列": allowed,
            "字段定义": def_map,
            "下游依赖已核对": not pending_deps or deps_confirmed,
            "交付时间": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        entry[MATERIAL_FIELD] = material

        store.log_event("rollback_material_prepared", "schema_evolution", "snapshot_version", {
            "entry_id": entry_id,
            "table_name": table_name,
            "snapshot_id": snapshot["id"],
            "version_no": snapshot.get("版本号"),
            "scope": scope,
            "allowed_columns": allowed,
        })

        return material, (
            f"恢复材料已交付：来源 {table_name} {material['来源版本号']}，"
            f"本次允许恢复列：{'、'.join(allowed)}"
        )

    def rollback_with_material(
        self,
        entry_id: int,
        material: dict[str, Any] | None,
        columns: list[str] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """目标字段回滚：读取交付的字段定义恢复对应列，回执写回实际读取的来源。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, "Schema 变更不存在"
        if entry.get("status") != "已执行":
            return None, f"Schema 变更状态为{entry.get('status')}，不可回滚"

        delivered = entry.get(MATERIAL_FIELD)
        if not isinstance(material, dict) or not delivered:
            return None, "缺少恢复材料，请先在回滚候选视图准备材料"
        if material.get("材料编号") != delivered.get("材料编号"):
            return None, "恢复材料与视图交付不一致，请重新在回滚候选视图准备材料"

        table_name = entry.get("湖表名", "")
        # 以来源快照为准重新读取，回执不允许拼接不同版本的信息
        snapshot = store.find("snapshot_version", int(material.get("来源快照ID", 0) or 0))
        if (
            snapshot is None
            or snapshot.get("湖表名") != table_name
            or snapshot.get("status") != "可用"
        ):
            return None, f"来源快照不可用，请核对目标湖表 {table_name} 与来源快照后重新取材料"
        if snapshot.get("版本号") != material.get("来源版本号"):
            return None, "材料版本号与来源快照不一致，回滚回执不能拼接不同版本的信息，请重新取材料"

        def_map = self._snapshot_def_map(snapshot)
        if not def_map:
            refetched = self._refetch_snapshot(table_name, str(snapshot.get("版本号", "")))
            if refetched is None:
                return None, (
                    f"取不到快照 {snapshot.get('版本号')} 的列定义，重新取材料失败："
                    f"请核对目标湖表 {table_name} 与来源快照"
                )
            snapshot = refetched
            def_map = self._snapshot_def_map(snapshot)

        delivered_defs = material.get("字段定义") or {}
        mismatched = [c for c, t in delivered_defs.items() if def_map.get(c) != t]
        if mismatched:
            return None, (
                f"材料列定义与来源快照 {snapshot.get('版本号')} 不一致（{'、'.join(mismatched)}），"
                "回滚回执不能把一个版本的编号和另一版的列定义拼到一起"
            )

        allowed = list(delivered.get("允许恢复列") or [])
        targets = list(columns or allowed)
        if not targets:
            return None, "未指定要恢复的列"
        out_of_scope = [c for c in targets if c not in allowed]
        if out_of_scope:
            return None, f"本次材料范围不允许恢复列：{'、'.join(out_of_scope)}"
        missing = [c for c in targets if c not in def_map]
        if missing:
            return None, f"来源快照缺少列定义：{'、'.join(missing)}，无法恢复"

        # 按交付的字段定义恢复对应列
        current_defs = _defs_to_map(entry.get(CURRENT_DEF_FIELD))
        for col in targets:
            current_defs[col] = def_map[col]
        entry[CURRENT_DEF_FIELD] = [
            {"列名": name, "类型": ctype} for name, ctype in current_defs.items()
        ]

        receipt = {
            "材料编号": delivered["材料编号"],
            "实际读取快照ID": snapshot["id"],
            "实际读取版本号": snapshot.get("版本号"),
            "恢复列": targets,
            "字段定义": {col: def_map[col] for col in targets},
            "恢复时间": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        entry[RECEIPT_FIELD] = receipt
        entry["status"] = "已回滚"
        entry["pending"] = False
        entry["abnormal"] = False

        store.log_event("schema_rolled_back", "schema_evolution", "snapshot_version", {
            "entry_id": entry_id,
            "table_name": table_name,
            "snapshot_id": snapshot["id"],
            "version_no": snapshot.get("版本号"),
            "columns": targets,
        })

        return entry, (
            f"Schema 变更已回滚：{table_name} 读取快照 {snapshot.get('版本号')} "
            f"恢复列 {'、'.join(targets)}"
        )

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交审批":
            return self.submit_approval(entry_id)
        if action == "执行变更":
            return self.execute_change(entry_id)
        return super().run_action(entry_id, action)