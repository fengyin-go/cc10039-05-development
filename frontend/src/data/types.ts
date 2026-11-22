/** 纯前端数据层的公共类型：与全栈版后端返回的结构保持一致，换回后端时页面不用改。 */

export type EntryRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  [field: string]: string | number | boolean
}

export type ModuleMeta = {
  key: string
  name: string
  entity: string
  desc: string
  fields: string[]
  statuses: string[]
  actions: string[]
  actionTargets: Record<string, string>
  metrics: string[]
}

export type PageResult = {
  items: EntryRow[]
  total: number
  page: number
  size: number
}

export type ActionResult = {
  ok: boolean
  message: string
  entry?: Record<string, unknown> | null
}

/** Schema 回滚候选视图：单个可用版本的列差异与依赖提示。 */
export type ColumnDiff = {
  列名: string
  快照类型: string | null
  当前类型: string | null
  差异: string
}

export type RollbackCandidate = {
  快照ID: number
  版本号: string
  快照时间戳: string
  快照类型: string
  含全部待恢复列: boolean
  列差异: ColumnDiff[]
  额外列: string[]
  下游依赖: { 列名: string; 下游消费: string[] }[]
  交付前需核对下游依赖: boolean
}

export type RollbackMaterial = {
  材料编号: string
  目标湖表: string
  来源快照ID: number
  来源版本号: string
  范围: string
  待恢复列: string[]
  允许恢复列: string[]
  字段定义: Record<string, string>
  下游依赖已核对: boolean
  交付时间: string
}

export type RollbackReceipt = {
  材料编号: string
  实际读取快照ID: number
  实际读取版本号: string
  恢复列: string[]
  字段定义: Record<string, string>
  恢复时间: string
}

export type RollbackCandidatesView = {
  变更ID: number
  湖表名: string
  变更状态: string
  待恢复列: string[]
  当前字段定义: Record<string, string>
  候选: RollbackCandidate[]
  恢复材料: RollbackMaterial | null
  回滚回执: RollbackReceipt | null
}

export type OverviewResult = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}
