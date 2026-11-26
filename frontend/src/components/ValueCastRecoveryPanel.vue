<template>
  <section class="cast-recovery-panel" data-testid="value-cast-recovery-panel">
    <div class="panel-head">
      <h3>从文档处理回执领取已确认展平结果</h3>
      <p>
        取值转型只读取完整且已确认的文档行；领取按文档回执幂等，重复领取会跳过已执行的乙文档，
        并把展平行写入契约输入批次、同步批次统计。
      </p>
    </div>

    <div class="controls">
      <label>
        <span>整组编号</span>
        <input v-model="groupId" data-testid="cast-group-id" />
      </label>
      <label>
        <span>契约输入批次</span>
        <input v-model="batchId" data-testid="contract-batch-id" />
      </label>
      <label>
        <span>转型规则编号（可空）</span>
        <input v-model="castNo" placeholder="例如 VC-003" data-testid="claim-cast-no" />
      </label>
      <button class="btn primary" type="button" data-testid="claim-confirmed-results" :disabled="loading" @click="claim">
        领取已确认结果并执行转型
      </button>
      <RouterLink class="btn" :to="{ path: '/nested_flatten', query: { group: groupId } }" data-testid="back-to-flatten">
        返回嵌套展平
      </RouterLink>
    </div>

    <div v-if="errorMessage" class="error" data-testid="claim-error">{{ errorMessage }}</div>

    <div v-if="result" class="claim-result" data-testid="claim-result" :data-claim-id="result.claim_id">
      <p class="message">{{ result.message }}</p>
      <div class="stat-row">
        <div class="stat-card"><span>批次总行数</span><b data-testid="batch-total-rows">{{ result.total_rows }}</b></div>
        <div class="stat-card"><span>本次新增/执行</span><b data-testid="new-rows">{{ result.new_rows }}</b></div>
        <div class="stat-card"><span>重复跳过行</span><b data-testid="duplicate-rows">{{ result.duplicate_rows }}</b></div>
        <div class="stat-card"><span>转型成功/失败</span><b>{{ result.cast_ok }} / {{ result.cast_failed }}</b></div>
      </div>

      <h4>文档级领取回执</h4>
      <table class="decision-table">
        <thead>
          <tr><th>文档</th><th>版本/尝试</th><th>行数</th><th>本次进入转型</th><th>执行/重复</th><th>说明</th><th>返回核对</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="decision in result.decisions"
            :key="decision.attempt_id"
            :data-testid="`claim-decision-${decision.doc_id}`"
            :data-entered="String(decision.entered_cast)"
            :data-duplicate="String(decision.duplicate)"
          >
            <td><b>{{ decision.doc_name }}</b><br /><span class="muted">{{ decision.doc_id }}</span></td>
            <td>v{{ decision.version }}<br /><span class="muted">{{ decision.attempt_id }}</span></td>
            <td>{{ decision.actual_rows }}</td>
            <td>
              <span :class="decision.entered_cast ? 'ok-text' : 'warn-text'" :data-testid="`enter-cast-${decision.doc_id}`">
                {{ decision.entered_cast ? '是，有明确回执' : '否' }}
              </span>
            </td>
            <td>{{ decision.executed ? '已执行' : '未执行' }}<template v-if="decision.duplicate"> / 重复跳过</template></td>
            <td>{{ decision.reason }}</td>
            <td>
              <RouterLink
                v-if="decision.original_failed_attempt_id"
                class="link"
                :to="{ path: '/nested_flatten', query: { group: groupId, attempt: decision.original_failed_attempt_id } }"
                :data-testid="`open-original-failure-${decision.doc_id}`"
              >
                打开原 {{ decision.doc_name }} 失败版本
              </RouterLink>
              <span v-else class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>

      <div v-if="result.parse_batch" class="batch-stats" data-testid="batch-stats">
        <h4>批次统计已同步</h4>
        <p>
          {{ result.parse_batch.批次记录编号 }}：总 {{ result.parse_batch.总行数 }} 行，
          成功 {{ result.parse_batch.成功行数 }} 行，隔离 {{ result.parse_batch.隔离行数 }} 行，
          状态 {{ result.parse_batch.status }}
        </p>
      </div>

      <h4>契约输入行与原数组定位</h4>
      <div class="table-scroll">
        <table class="row-table">
          <thead>
            <tr><th>文档/版本</th><th>展平行</th><th>原数组序号</th><th>定位</th><th>原始值</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in result.rows" :key="row.row_key" :data-testid="`contract-row-${row.doc_id}-${row.array_index}`">
              <td>{{ row.doc_name }} v{{ row.version }}<br /><span class="muted">{{ row.attempt_id }}</span></td>
              <td>{{ row.flat_row_number }}</td>
              <td data-testid="source-array-index">{{ row.array_index }}</td>
              <td>{{ row.locator }}</td>
              <td><code>{{ JSON.stringify(row.raw) }}</code></td>
              <td>{{ row.status }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { postRecovery } from '@/api/backend-service'

type AnyRecord = Record<string, any>

const route = useRoute()
const groupId = ref(String(route.query.group ?? 'DG-JIAYI-10'))
const batchId = ref(`PB-CONTRACT-${groupId.value}`)
const castNo = ref('')
const loading = ref(false)
const errorMessage = ref('')
const result = ref<AnyRecord | null>(null)

watch(groupId, (value) => {
  batchId.value = `PB-CONTRACT-${value}`
})

async function claim() {
  loading.value = true
  errorMessage.value = ''
  try {
    result.value = await postRecovery<AnyRecord>('/api/value_cast/recovery/claim', {
      group_id: groupId.value,
      batch_id: batchId.value,
      cast_no: castNo.value || undefined,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '领取失败'
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.cast-recovery-panel { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin: 12px 0 16px; }
.panel-head h3 { margin: 0 0 4px; font-size: 16px; }
.panel-head p { margin: 0; color: var(--muted); font-size: 13px; }
.controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 12px 0; }
.controls label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.controls input { padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; min-width: 180px; }
.error { color: #b42318; font-size: 13px; }
.message { background: #eff8ff; border: 1px solid #b2ddff; color: #1849a9; border-radius: 6px; padding: 9px 11px; font-size: 13px; }
.stat-row { display: grid; grid-template-columns: repeat(4, minmax(140px, 1fr)); gap: 10px; margin: 12px 0; }
.stat-card { border: 1px solid var(--border); border-radius: 8px; padding: 10px; background: #fbfcfe; display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.stat-card b { font-size: 20px; color: #1f2937; }
.decision-table, .row-table { width: 100%; border-collapse: collapse; font-size: 13px; margin: 8px 0 14px; }
.decision-table th, .decision-table td, .row-table th, .row-table td { border: 1px solid var(--border); padding: 7px 8px; text-align: left; vertical-align: top; }
.ok-text { color: #067647; font-weight: 600; }
.warn-text { color: #b54708; font-weight: 600; }
.muted { color: var(--muted); font-size: 12px; }
.batch-stats { background: #f6f8fb; border-radius: 8px; padding: 8px 12px; font-size: 13px; }
.table-scroll { overflow-x: auto; }
code { white-space: pre-wrap; word-break: break-all; font-size: 12px; }
@media (max-width: 900px) { .stat-row { grid-template-columns: repeat(2, 1fr); } }
</style>
