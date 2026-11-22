<template>
  <div class="modal-backdrop" data-testid="rollback-panel">
    <div class="modal rollback-modal">
      <h3>回滚候选视图 · 变更 #{{ entryId }}</h3>

      <p v-if="view" class="page-desc">
        湖表 {{ view.湖表名 }} · 状态 {{ view.变更状态 }} · 待恢复列：{{ view.待恢复列.join('、') || '—' }}
      </p>

      <section v-if="view && view.回滚回执" class="receipt-box" data-testid="rollback-receipt">
        <h4>回滚回执（关联来源）</h4>
        <p>
          实际读取版本：{{ view.回滚回执.实际读取版本号 }}（快照 #{{ view.回滚回执.实际读取快照ID }}）
          · 材料 {{ view.回滚回执.材料编号 }} · 恢复时间 {{ view.回滚回执.恢复时间 }}
        </p>
        <table class="data-table">
          <thead>
            <tr><th>恢复列</th><th>恢复后类型</th></tr>
          </thead>
          <tbody>
            <tr v-for="col in view.回滚回执.恢复列" :key="col">
              <td>{{ col }}</td>
              <td>{{ view.回滚回执.字段定义[col] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
      </section>

      <template v-if="view && !view.回滚回执">
        <h4>本表可用版本（{{ view.候选.length }}）</h4>
        <table class="data-table">
          <thead>
            <tr>
              <th>版本号</th>
              <th>快照时间</th>
              <th>待恢复列差异</th>
              <th>额外列</th>
              <th>下游依赖</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="cand in view.候选" :key="cand.快照ID" :data-testid="`candidate-${cand.快照ID}`">
              <td>{{ cand.版本号 }}</td>
              <td>{{ cand.快照时间戳 }}</td>
              <td>
                <div v-for="diff in cand.列差异" :key="diff.列名">
                  {{ diff.列名 }}：{{ diff.快照类型 ?? '缺失' }} → 当前 {{ diff.当前类型 ?? '缺失' }}
                  <span v-if="diff.差异 !== '一致'" class="diff-tag">{{ diff.差异 }}</span>
                </div>
              </td>
              <td>{{ cand.额外列.join('、') || '—' }}</td>
              <td>
                <span v-if="cand.交付前需核对下游依赖" class="diff-tag">
                  需核对：{{ depText(cand) }}
                </span>
                <span v-else>—</span>
              </td>
              <td>
                <button
                  class="link"
                  type="button"
                  :data-testid="`prepare-${cand.快照ID}`"
                  :disabled="!cand.含全部待恢复列 || view.变更状态 !== '已执行'"
                  @click="prepare(cand)"
                >
                  交付材料
                </button>
              </td>
            </tr>
            <tr v-if="!view.候选.length">
              <td colspan="6" class="empty-state">本表暂无可用快照版本</td>
            </tr>
          </tbody>
        </table>

        <div class="scope-row">
          <span>恢复范围：</span>
          <label><input v-model="scope" type="radio" value="仅待恢复列" data-testid="scope-target" />仅待恢复列</label>
          <label><input v-model="scope" type="radio" value="整版列" data-testid="scope-full" />整版列</label>
          <label v-if="needDepsConfirm" class="deps-confirm">
            <input v-model="depsConfirmed" type="checkbox" data-testid="deps-confirmed" />已核对下游依赖
          </label>
        </div>
      </template>

      <section v-if="material && !view?.回滚回执" class="material-box" data-testid="rollback-material">
        <h4>恢复材料 {{ material.材料编号 }}</h4>
        <p>
          来源：{{ material.目标湖表 }} {{ material.来源版本号 }}（快照 #{{ material.来源快照ID }}）
          · 范围：{{ material.范围 }} · 本次允许恢复列：{{ material.允许恢复列.join('、') }}
        </p>
        <div class="scope-row">
          <span>选择恢复列：</span>
          <label v-for="col in material.允许恢复列" :key="col">
            <input v-model="selectedColumns" type="checkbox" :value="col" :data-testid="`col-${col}`" />
            {{ col }}（{{ material.字段定义[col] }}）
          </label>
        </div>
        <button
          class="btn primary"
          type="button"
          data-testid="run-rollback"
          :disabled="!selectedColumns.length || running"
          @click="rollback"
        >
          {{ running ? '回滚中…' : '发起目标字段回滚' }}
        </button>
      </section>

      <p v-if="errorMessage" class="error-text" data-testid="rollback-error">{{ errorMessage }}</p>

      <div class="page-actions">
        <button class="btn" type="button" data-testid="close-rollback" @click="emit('close')">关闭</button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  listRollbackCandidates,
  prepareRollbackMaterial,
  runSchemaRollback,
} from '@/api/backend-service'
import type { RollbackCandidate, RollbackCandidatesView, RollbackMaterial } from '@/data/types'

const props = defineProps<{ entryId: number }>()
const emit = defineEmits<{ close: [] }>()

const view = ref<RollbackCandidatesView | null>(null)
const material = ref<RollbackMaterial | null>(null)
const scope = ref('仅待恢复列')
const depsConfirmed = ref(false)
const needDepsConfirm = ref(false)
const selectedColumns = ref<string[]>([])
const running = ref(false)
const errorMessage = ref('')

function depText(cand: RollbackCandidate): string {
  return cand.下游依赖.map((d) => `${d.列名}→${d.下游消费.join('/')}`).join('；')
}

async function reload() {
  errorMessage.value = ''
  try {
    view.value = await listRollbackCandidates(props.entryId)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回滚候选视图读取失败'
  }
}

async function prepare(cand: RollbackCandidate) {
  errorMessage.value = ''
  try {
    const result = await prepareRollbackMaterial(
      props.entryId, cand.快照ID, scope.value, depsConfirmed.value)
    if (!result.ok) {
      if (result.message.includes('下游依赖')) needDepsConfirm.value = true
      throw new Error(result.message)
    }
    material.value = result.entry as unknown as RollbackMaterial
    selectedColumns.value = [...material.value.允许恢复列]
    needDepsConfirm.value = false
    depsConfirmed.value = false
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复材料交付失败'
  }
}

async function rollback() {
  if (!material.value) return
  running.value = true
  errorMessage.value = ''
  try {
    const result = await runSchemaRollback(
      props.entryId,
      material.value as unknown as Record<string, unknown>,
      selectedColumns.value,
    )
    if (!result.ok) throw new Error(result.message)
    material.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回滚失败'
  } finally {
    running.value = false
  }
}

onMounted(reload)
</script>
