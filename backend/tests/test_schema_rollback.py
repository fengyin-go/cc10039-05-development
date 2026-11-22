"""Schema 回滚候选视图与恢复材料链路测试。

覆盖验收场景：两张表的同名快照 + 本表两份列结构不同的可用版本；
候选只纳入本表可用快照；材料范围决定本次允许恢复的列；取不到列定义时
重新取材料要核对目标湖表和来源；回执不拼接一个版本的编号和另一版的列定义。

注意：store 是全局单例，本文件的夹具一律使用唯一表名，避免与其他测试互相影响。
"""
from __future__ import annotations

import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.schema_evolution import SchemaEvolutionService
from app.store import store

_counter = itertools.count(1)


def _defs(*pairs: tuple[str, str]) -> list[dict[str, str]]:
    return [{"列名": name, "类型": ctype} for name, ctype in pairs]


def _next_id(module: str) -> int:
    return max((int(r.get("id", 0)) for r in store.rows(module)), default=0) + 1


def _fixture(*, extra_column: bool = True, downstream: bool = True):
    """构造回滚场景：本表两份列结构不同的可用版本 + 另一张表的同名快照。"""
    n = next(_counter)
    table = f"rb_orders_{n}"
    other_table = f"rb_orders_stage_{n}"
    svc = SchemaEvolutionService()

    old_defs = _defs(("order_id", "bigint"), ("discount_rate", "decimal(10,2)"), ("dt", "string"))
    new_defs = _defs(("order_id", "bigint"), ("discount_rate", "double"), ("dt", "string"))
    if extra_column:
        new_defs.append({"列名": "channel_tag", "类型": "string"})

    base_id = _next_id("snapshot_version")
    snap_old = {"id": base_id, "status": "可用", "pending": False, "abnormal": False,
                "湖表名": table, "快照时间戳": "2026-10-15 02:00", "版本号": "v9.1",
                "记录数": "100", "快照类型": "全量快照", "字段定义": old_defs}
    snap_new = {"id": base_id + 1, "status": "可用", "pending": False, "abnormal": False,
                "湖表名": table, "快照时间戳": "2026-11-01 02:00", "版本号": "v9.2",
                "记录数": "200", "快照类型": "全量快照", "字段定义": new_defs}
    # 另一张表的同名快照：版本号相同、列定义不同，用来验证不串表
    snap_same_name = {"id": base_id + 2, "status": "可用", "pending": False, "abnormal": False,
                      "湖表名": other_table, "快照时间戳": "2026-11-01 03:00", "版本号": "v9.2",
                      "记录数": "5", "快照类型": "全量快照",
                      "字段定义": _defs(("order_id", "bigint"), ("discount_rate", "string"))}
    # 本表非可用快照：不得进入候选
    snap_pending = {"id": base_id + 3, "status": "生成中", "pending": True, "abnormal": False,
                    "湖表名": table, "快照时间戳": "2026-11-20 02:00", "版本号": "v9.3",
                    "记录数": "0", "快照类型": "全量快照", "字段定义": list(old_defs)}
    store.rows("snapshot_version").extend([snap_old, snap_new, snap_same_name, snap_pending])

    entry_id = _next_id("schema_evolution")
    store.rows("schema_evolution").append({
        "id": entry_id, "status": "已执行", "pending": False, "abnormal": False,
        "湖表名": table, "变更类型": "修改字段类型", "变更字段": "discount_rate",
        "兼容级别": "不兼容", "变更前快照": "v9.1",
        "当前字段定义": _defs(("order_id", "bigint"), ("discount_rate", "string"), ("dt", "string")),
    })

    if downstream and extra_column:
        store.rows("lineage_graph").append({
            "id": _next_id("lineage_graph"), "status": "已建立", "pending": False,
            "abnormal": False, "上游源": f"{table}.channel_tag", "中间转换": "etl",
            "下游消费": f"ads_channel_{n}", "血缘类型": "字段级", "记录时间": "2026-11-10",
        })

    return svc, entry_id, snap_old, snap_new, snap_same_name


class TestRollbackCandidates:
    """回滚候选视图：只纳入本表可用快照，展示待恢复列名称和类型差异。"""

    def test_candidates_only_include_own_available_snapshots(self):
        svc, entry_id, snap_old, snap_new, snap_same_name = _fixture()
        payload, msg = svc.rollback_candidates(entry_id)
        assert payload is not None, msg
        ids = {c["快照ID"] for c in payload["候选"]}
        assert ids == {snap_old["id"], snap_new["id"]}
        # 同名快照（另一张表）和本表非可用快照都不得进入候选
        assert snap_same_name["id"] not in ids
        assert all(c["版本号"] != "v9.3" for c in payload["候选"])

    def test_candidates_show_column_diffs(self):
        svc, entry_id, snap_old, snap_new, _ = _fixture()
        payload, _ = svc.rollback_candidates(entry_id)
        assert payload is not None
        assert payload["待恢复列"] == ["discount_rate"]

        by_version = {c["版本号"]: c for c in payload["候选"]}
        old_diff = by_version["v9.1"]["列差异"][0]
        assert old_diff["列名"] == "discount_rate"
        assert old_diff["快照类型"] == "decimal(10,2)"
        assert old_diff["当前类型"] == "string"
        assert old_diff["差异"] == "类型不一致"

        # 较晚的版本带有后续加上的列，且交付前需核对这些列的下游依赖
        new_cand = by_version["v9.2"]
        assert new_cand["列差异"][0]["快照类型"] == "double"
        assert new_cand["额外列"] == ["channel_tag"]
        assert new_cand["交付前需核对下游依赖"] is True
        assert new_cand["下游依赖"][0]["列名"] == "channel_tag"
        assert by_version["v9.1"]["交付前需核对下游依赖"] is False

    def test_seed_demo_entry_candidates(self):
        """种子演示数据：orders_fact 的候选不含 orders_fact_stage 的同名 v2.4。"""
        svc = SchemaEvolutionService()
        payload, msg = svc.rollback_candidates(4)
        assert payload is not None, msg
        assert payload["湖表名"] == "orders_fact"
        versions = sorted(c["版本号"] for c in payload["候选"])
        assert versions == ["v2.3", "v2.4"]
        by_version = {c["版本号"]: c for c in payload["候选"]}
        assert by_version["v2.3"]["列差异"][0]["快照类型"] == "decimal(10,2)"
        assert by_version["v2.4"]["额外列"] == ["channel_tag"]


class TestRollbackMaterial:
    """恢复材料交付：范围决定允许恢复的列，来源必须是本表可用快照。"""

    def test_scope_changes_allowed_columns(self):
        svc, entry_id, _, snap_new, _ = _fixture()
        material, msg = svc.prepare_rollback_material(
            entry_id, snap_new["id"], scope="仅待恢复列")
        assert material is not None, msg
        assert material["允许恢复列"] == ["discount_rate"]

        # 整版列含后续加上的列，未核对下游依赖时不得交付
        material2, msg2 = svc.prepare_rollback_material(
            entry_id, snap_new["id"], scope="整版列")
        assert material2 is None
        assert "下游依赖" in msg2 and "channel_tag" in msg2

        # 核对后交付，允许恢复的列随范围扩大
        material3, msg3 = svc.prepare_rollback_material(
            entry_id, snap_new["id"], scope="整版列", deps_confirmed=True)
        assert material3 is not None, msg3
        assert "channel_tag" in material3["允许恢复列"]
        assert "discount_rate" in material3["允许恢复列"]

    def test_material_source_must_be_own_available_snapshot(self):
        svc, entry_id, _, _, snap_same_name = _fixture()
        # 另一张表的同名快照不能作为本表恢复材料来源
        material, msg = svc.prepare_rollback_material(entry_id, snap_same_name["id"])
        assert material is None
        assert "目标湖表" in msg and "来源" in msg

    def test_refetch_when_column_defs_missing(self):
        svc, entry_id, _, _, _ = _fixture()
        table = f"rb_orders_{1}"  # 占位，实际从候选里取
        payload, _ = svc.rollback_candidates(entry_id)
        assert payload is not None
        table = payload["湖表名"]

        # 同一版本号两份可用快照：一份取不到列定义，重新取材料应落到带定义的那份
        base_id = _next_id("snapshot_version")
        no_defs = {"id": base_id, "status": "可用", "pending": False, "abnormal": False,
                   "湖表名": table, "快照时间戳": "2026-11-05 02:00", "版本号": "v9.9",
                   "记录数": "1", "快照类型": "全量快照"}
        with_defs = {"id": base_id + 1, "status": "可用", "pending": False, "abnormal": False,
                     "湖表名": table, "快照时间戳": "2026-11-05 03:00", "版本号": "v9.9",
                     "记录数": "1", "快照类型": "全量快照",
                     "字段定义": _defs(("order_id", "bigint"), ("discount_rate", "decimal(10,2)"))}
        orphan = {"id": base_id + 2, "status": "可用", "pending": False, "abnormal": False,
                  "湖表名": table, "快照时间戳": "2026-11-06 02:00", "版本号": "v9.10",
                  "记录数": "1", "快照类型": "全量快照"}
        store.rows("snapshot_version").extend([no_defs, with_defs, orphan])

        material, msg = svc.prepare_rollback_material(entry_id, no_defs["id"])
        assert material is not None, msg
        assert material["来源快照ID"] == with_defs["id"]
        assert material["来源版本号"] == "v9.9"

        # 完全取不到列定义时，重新取材料失败并提示核对目标湖表和来源
        material2, msg2 = svc.prepare_rollback_material(entry_id, orphan["id"])
        assert material2 is None
        assert "重新取材料失败" in msg2 and table in msg2


class TestRollbackWithMaterial:
    """目标字段回滚：按交付的字段定义恢复，回执写回实际读取的来源。"""

    def test_rollback_writes_receipt_from_actually_read_snapshot(self):
        svc, entry_id, snap_old, _, _ = _fixture()
        material, msg = svc.prepare_rollback_material(entry_id, snap_old["id"])
        assert material is not None, msg

        entry, msg2 = svc.rollback_with_material(entry_id, material, ["discount_rate"])
        assert entry is not None, msg2
        assert entry["status"] == "已回滚"

        receipt = entry["回滚回执"]
        assert receipt["实际读取快照ID"] == snap_old["id"]
        assert receipt["实际读取版本号"] == "v9.1"
        assert receipt["恢复列"] == ["discount_rate"]
        assert receipt["字段定义"] == {"discount_rate": "decimal(10,2)"}
        current = {d["列名"]: d["类型"] for d in entry["当前字段定义"]}
        assert current["discount_rate"] == "decimal(10,2)"

        # 重新打开回滚候选视图：关联来源与回执一致，恢复的字段对应同一来源
        payload, _ = svc.rollback_candidates(entry_id)
        assert payload is not None
        assert payload["回滚回执"]["实际读取版本号"] == payload["恢复材料"]["来源版本号"]
        assert payload["回滚回执"]["实际读取快照ID"] == payload["恢复材料"]["来源快照ID"]
        assert payload["回滚回执"]["恢复列"] == ["discount_rate"]

    def test_rollback_rejects_mixed_version_number(self):
        svc, entry_id, snap_old, _, _ = _fixture()
        material, _ = svc.prepare_rollback_material(entry_id, snap_old["id"])
        assert material is not None
        # 把材料版本号改成另一版：回执不得拼接不同版本的信息
        tampered = dict(material, 来源版本号="v9.2")
        entry, msg = svc.rollback_with_material(entry_id, tampered, ["discount_rate"])
        assert entry is None
        assert "拼接" in msg or "不一致" in msg

    def test_rollback_rejects_foreign_column_defs(self):
        svc, entry_id, snap_old, _, snap_same_name = _fixture()
        material, _ = svc.prepare_rollback_material(entry_id, snap_old["id"])
        assert material is not None
        # 用另一张表同名快照的列定义替换材料字段定义：编号与列定义不能拼到一起
        foreign_defs = {d["列名"]: d["类型"] for d in snap_same_name["字段定义"]}
        tampered = dict(material, 字段定义=foreign_defs)
        entry, msg = svc.rollback_with_material(entry_id, tampered, ["discount_rate"])
        assert entry is None
        assert "不一致" in msg

    def test_rollback_rejects_columns_out_of_scope(self):
        svc, entry_id, _, snap_new, _ = _fixture()
        material, _ = svc.prepare_rollback_material(
            entry_id, snap_new["id"], scope="仅待恢复列")
        assert material is not None
        # 仅待恢复列范围不允许恢复后续加上的列
        entry, msg = svc.rollback_with_material(entry_id, material, ["channel_tag"])
        assert entry is None
        assert "不允许恢复列" in msg

    def test_rollback_requires_view_delivered_material(self):
        svc, entry_id, snap_old, _, _ = _fixture()
        entry, msg = svc.rollback_with_material(entry_id, None, ["discount_rate"])
        assert entry is None
        assert "材料" in msg
        # 材料编号与视图交付不一致时同样拒绝
        material, _ = svc.prepare_rollback_material(entry_id, snap_old["id"])
        assert material is not None
        forged = dict(material, 材料编号="RB-forged")
        entry2, msg2 = svc.rollback_with_material(entry_id, forged, ["discount_rate"])
        assert entry2 is None
        assert "不一致" in msg2
