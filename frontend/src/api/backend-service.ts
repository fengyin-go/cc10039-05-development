import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  ActionResult,
  EntryRow,
  OverviewResult,
  PageResult,
  RollbackCandidatesView,
} from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

/** Schema 回滚候选视图：列出本表可用版本和待恢复列差异。 */
export async function listRollbackCandidates(entryId: number): Promise<RollbackCandidatesView> {
  const response = await request(`/api/schema_evolution/${entryId}/rollback-candidates`)
  if (!response.ok) throw new Error('回滚候选视图读取失败')
  return await response.json()
}

/** 交付恢复材料：按范围圈定本次允许恢复的列。 */
export async function prepareRollbackMaterial(
  entryId: number,
  snapshotId: number,
  scope: string,
  depsConfirmed: boolean,
): Promise<ActionResult> {
  const response = await request(`/api/schema_evolution/${entryId}/rollback-material`, {
    method: 'POST',
    body: JSON.stringify({ snapshot_id: snapshotId, scope, deps_confirmed: depsConfirmed }),
  })
  return await response.json()
}

/** 用视图交付的恢复材料发起目标字段回滚。 */
export async function runSchemaRollback(
  entryId: number,
  material: Record<string, unknown>,
  columns: string[],
): Promise<ActionResult> {
  const response = await request(`/api/schema_evolution/${entryId}/rollback`, {
    method: 'POST',
    body: JSON.stringify({ material, columns }),
  })
  return await response.json()
}
