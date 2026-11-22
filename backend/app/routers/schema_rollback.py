"""Schema 回滚候选与恢复材料端点。

回滚候选视图读取本表可用快照的字段定义，列出可用版本和待恢复列差异；
值班员在视图中交付恢复材料后发起目标字段回滚，回执写回实际读取的快照
版本和字段定义。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import service_for
from app.services.schema_evolution import SchemaEvolutionService

router = APIRouter(prefix="/api/schema_evolution", tags=["Schema回滚"])


def _service() -> SchemaEvolutionService:
    service = service_for("schema_evolution")
    if not isinstance(service, SchemaEvolutionService):
        raise HTTPException(status_code=404, detail="Schema 演进服务未注册")
    return service


@router.get("/{entry_id}/rollback-candidates")
def rollback_candidates(entry_id: int) -> dict[str, Any]:
    """回滚候选视图：本表可用版本、待恢复列差异、已交付材料和回滚回执。"""
    payload, message = _service().rollback_candidates(entry_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=message)
    return payload


class MaterialPayload(BaseModel):
    snapshot_id: int
    scope: str = "仅待恢复列"
    deps_confirmed: bool = False


@router.post("/{entry_id}/rollback-material", response_model=ActionResult)
def prepare_rollback_material(entry_id: int, payload: MaterialPayload) -> ActionResult:
    """交付恢复材料：按范围圈定允许恢复的列，必要时先核对下游依赖。"""
    material, message = _service().prepare_rollback_material(
        entry_id,
        payload.snapshot_id,
        scope=payload.scope,
        deps_confirmed=payload.deps_confirmed,
    )
    return ActionResult(ok=material is not None, message=message, entry=material)


class RollbackPayload(BaseModel):
    material: dict[str, Any] = Field(default_factory=dict)
    columns: list[str] | None = None


@router.post("/{entry_id}/rollback", response_model=ActionResult)
def rollback_with_material(entry_id: int, payload: RollbackPayload) -> ActionResult:
    """目标字段回滚：读取交付的字段定义恢复对应列，写回实际读取的来源。"""
    entry, message = _service().rollback_with_material(
        entry_id, payload.material, payload.columns)
    return ActionResult(ok=entry is not None, message=message, entry=entry)
