<template>
  <section class="recovery-panel" data-testid="flatten-recovery-panel">
    <div class="recovery-head">
      <h3>超限源文档逐份恢复</h3>
      <p>
        先按同一整组额度预览实际展开行数，再逐份提交。单份超限只保留该文档的历史尝试和可恢复记录，
        不卡住同组其他完整文档。
      </p>
    </div>

    <div class="recovery-controls">
      <label>
        <span>整组编号</span>
        <input v-model="groupId" data-testid="recovery-group-id" />
      </label>
      <label>
        <span>整组额度（行）</span>
        <input
          v-model.number="quotaLimit"
          type="number"
          min="1"
          data-testid="recovery-quota-input"
        />
      </label>
      <label class="path-field">
        <span>展平路径</span>
        <input v-model="sourcePath" data-testid="recovery-source-path" />
      </label>
      <button class="btn" type="button" data-testid="load-jiayi-scenario" @click="loadScenario">
        载入甲 9 行、乙 2 行场景
      </button>
      <button class="btn primary" type="button" data-testid="preview-selected" :disabled="loading" @click="previewSelected">
        预览所选文档
      </button>
      <button class="btn primary" type="button" data-testid="submit-selected-each" :disabled="loading" @click="submitSelectedEach">
        逐份提交转型
      </button>
    </div>

    <details class="json-editor">
      <summary>文档 JSON（可粘贴其他源文档）</summary>
      <textarea v-model="jsonText" rows="10" data-testid="recovery-json" />
      <button class="btn" type="button" data-testid="apply-json" @click="applyJson">应用 JSON 到文档列表</button>
    </details>

    <div v-if="message" class="recovery-message" data-testid="recovery-message">{{ message }}</div>
    <div v-if="errorMessage" class="recovery-error" data-testid="recovery-error">{{ errorMessage }}</div>

    <div v-if="preview" class="preview-box" data-testid="preview-result" :data-within-limit="String(preview.within_limit)">
      <strong>{{ preview.message }}</strong>
      <div class="preview-docs">
        <article
          v-for="doc in preview.documents"
          :key="doc.doc_id"
          class="doc-card"
          :data-testid="`preview-doc-${doc.doc_id}`"
          :data-valid="String(doc.valid)"
        >
          <label class="doc-select">
            <input v-model="selectedIds" type="checkbox" :value="doc.doc_id" :data-testid="`select-${doc.doc_id}`" />
            <span>{{ doc.doc_name }}（{{ doc.doc_id }}）</span>
          </label>
          <p>实际展开行数：<b :data-testid="`actual-rows-${doc.doc_id}`">{{ doc.actual_rows }}</b> 行</p>
          <p>原数组长度：{{ doc.source_array_length }}；结论：{{ doc.preview_verdict }}</p>
          <p v-if="doc.issues.length" class="recovery-error">{{ doc.issues.join('；') }}</p>
          <button
            class="btn"
            type="button"
            :data-testid="`submit-${doc.doc_id}`"
            :disabled="loading"
            @click="submitOne(doc)"
          >
            {{ doc.doc_name }}提交转型
          </button>
        </article>
      </div>
    </div>

    <div class="recovery-grid">
      <div class="attempt-list">
        <div class="section-title">
          <h4>尝试版本与文档处理回执</h4>
          <button class="btn ghost" type="button" data-testid="refresh-attempts" @click="refreshAttempts">刷新</button>
        </div>
        <p v-if="!attempts.length" class="muted">尚未提交恢复文档。</p>
        <article
          v-for="attempt in attempts"
          :key="attempt.attempt_id"
          class="attempt-card"
          :data-testid="`attempt-card-${attempt.doc_id}-v${attempt.version}`"
          :data-accepted="String(attempt.accepted)"
        >
          <header>
            <b>{{ attempt.doc_name }} · v{{ attempt.version }}</b>
            <span>{{ attempt.status }}</span>
          </header>
          <p>
            <span :data-testid="`attempt-rows-${attempt.attempt_id}`">实际 {{ attempt.actual_rows }} 行</span>
            ｜数组位置：{{ Array.isArray(attempt.array_indexes) ? attempt.array_indexes.join(',') : '—' }}
          </p>
          <p class="muted">{{ attempt.attempt_id }}<template v-if="attempt.receipt_id"> ｜回执 {{ attempt.receipt_id }}</template></p>
          <div class="button-row">
            <button
              class="link"
              type="button"
              :data-testid="`view-original-${attempt.doc_id}`"
              @click="openAttempt(attempt.attempt_id)"
            >
              打开原失败版本/明细
            </button>
            <button
              v-if="attempt.doc_id === 'JIA' && Number(attempt.actual_rows) === 9"
              class="link"
              type="button"
              data-testid="correct-JIA-8"
              @click="submitJiaCorrection"
            >
              提交甲的完整 8 行更正
            </button>
            <RouterLink
              v-if="attempt.accepted && attempt.receipt_id"
              class="link"
              :to="{ path: '/value_cast', query: { group: groupId, receipt: attempt.receipt_id } }"
              data-testid="goto-value-cast"
            >
              持回执前往取值转型
            </RouterLink>
          </div>
        </article>
      </div>

      <div v-if="detail" class="detail-box" data-testid="attempt-detail" :data-attempt-id="detail.attempt_id">
        <div class="section-title">
          <h4>{{ detail.doc_name }} v{{ detail.version }} · {{ detail.actual_rows }} 行</h4>
          <button class="btn ghost" type="button" data-testid="close-detail" @click="closeDetail">关闭</button>
        </div>
        <p :class="detail.accepted ? 'ok-text' : 'error-text'">
          {{ detail.admission_status }} / {{ detail.status }}：{{ detail.reason || detail.受理说明 }}
        </p>
        <p>原文档、数组元素序号与实际展开行：</p>
        <table class="mini-table">
          <thead>
            <tr><th>展平行</th><th>数组序号</th><th>定位</th><th>值</th></tr>
          </thead>
          <tbody>
            <tr v-for="row in detail.rows" :key="row.row_id">
              <td>{{ row.flat_row_number }}</td>
              <td data-testid="detail-array-index">{{ row.array_index }}</td>
              <td>{{ row.locator || row.错误定位 }}</td>
              <td><code>{{ JSON.stringify(row.data) }}</code></td>
            </tr>
          </tbody>
        </table>
        <details>
          <summary>原始源文档 JSON</summary>
          <pre data-testid="source-document-json">{{ JSON.stringify(detail.source_document, null, 2) }}</pre>
        </details>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { getRecovery, postRecovery } from '@/api/backend-service'

type AnyRecord = Record<string, any>

const route = useRoute()
const router = useRouter()
const groupId = ref(String(route.query.group ?? 'DG-JIAYI-10'))
const quotaLimit = ref(10)
const sourcePath = ref('$.items[*]')
const loading = ref(false)
const message = ref('')
const errorMessage = ref('')
const preview = ref<AnyRecord | null>(null)
const attempts = ref<AnyRecord[]>([])
const detail = ref<AnyRecord | null>(null)
const selectedIds = ref<string[]>(['JIA', 'YI'])

function scenarioDocuments(): AnyRecord[] {
  return [
    {
      doc_id: 'JIA',
      doc_name: '甲',
      complete: true,
      source_path: '$.items[*]',
      items: Array.from({ length: 9 }, (_, index) => ({ seq: index, payload: `jia-original-${index}` })),
    },
    {
      doc_id: 'YI',
      doc_name: '乙',
      complete: true,
      source_path: '$.items[*]',
      items: Array.from({ length: 2 }, (_, index) => ({ seq: index, payload: `yi-${index}` })),
    },
  ]
}

const documents = ref<AnyRecord[]>(scenarioDocuments())
const jsonText = ref(JSON.stringify(documents.value, null, 2))
const selectedDocuments = computed(() =>
  documents.value.filter((doc) => selectedIds.value.includes(String(doc.doc_id))),
)

function loadScenario() {
  documents.value = scenarioDocuments()
  jsonText.value = JSON.stringify(documents.value, null, 2)
  selectedIds.value = ['JIA', 'YI']
  message.value = '已载入甲 9 行、乙 2 行；整组额度保持 10。请先预览。'
  errorMessage.value = ''
}

function applyJson() {
  try {
    const parsed = JSON.parse(jsonText.value)
    const list = Array.isArray(parsed) ? parsed : parsed.documents ?? [parsed]
    documents.value = list
    selectedIds.value = list.map((doc: AnyRecord, index: number) => String(doc.doc_id ?? doc.文档编号 ?? `DOC-${index + 1}`))
    message.value = `已应用 ${list.length} 份文档。`
  } catch (error) {
    errorMessage.value = error instanceof Error ? `JSON 解析失败：${error.message}` : 'JSON 解析失败'
  }
}

async function previewSelected() {
  loading.value = true
  errorMessage.value = ''
  try {
    const result = await postRecovery<AnyRecord>('/api/nested_flatten/recovery/preview', {
      documents: selectedDocuments.value,
      group_id: groupId.value,
      quota_limit: quotaLimit.value,
      source_path: sourcePath.value,
    })
    preview.value = result
    message.value = String(result.message ?? '预览完成')
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '预览失败'
  } finally {
    loading.value = false
  }
}

async function submitOne(doc: AnyRecord) {
  loading.value = true
  errorMessage.value = ''
  try {
    const result = await postRecovery<AnyRecord>('/api/nested_flatten/recovery/submit', {
      document: doc,
      group_id: groupId.value,
      quota_limit: quotaLimit.value,
      source_path: sourcePath.value,
    })
    message.value = String(result.message ?? (result.accepted ? '文档已受理' : '文档未受理，已保留恢复记录'))
    await refreshAttempts()
    if (result.attempt_id) await openAttempt(String(result.attempt_id))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '提交失败'
  } finally {
    loading.value = false
  }
}

async function submitSelectedEach() {
  loading.value = true
  errorMessage.value = ''
  try {
    // 按实际/预估行数从小到大逐份提交，确保乙 2 行先得到稳定回执；后端仍逐份独立受理。
    const ordered = [...selectedDocuments.value].sort((a, b) => (Number(a.items?.length ?? a.actual_rows ?? 0) - Number(b.items?.length ?? b.actual_rows ?? 0)))
    for (const doc of ordered) {
      await postRecovery('/api/nested_flatten/recovery/submit', {
        document: doc,
        group_id: groupId.value,
        quota_limit: quotaLimit.value,
        source_path: sourcePath.value,
      })
    }
    message.value = '已逐份提交；每份文档有独立回执，不会因另一份超限而整组失败。'
    await refreshAttempts()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '逐份提交失败'
  } finally {
    loading.value = false
  }
}

async function submitJiaCorrection() {
  loading.value = true
  errorMessage.value = ''
  try {
    const corrected = {
      doc_id: 'JIA',
      doc_name: '甲',
      complete: true,
      source_path: '$.items[*]',
      items: Array.from({ length: 8 }, (_, index) => ({ seq: index, payload: `jia-complete-v2-${index}` })),
    }
    jsonText.value = JSON.stringify([corrected, ...documents.value.filter((d) => d.doc_id !== 'JIA')], null, 2)
    documents.value = [corrected, ...documents.value.filter((d) => d.doc_id !== 'JIA')]
    const result = await postRecovery<AnyRecord>('/api/nested_flatten/recovery/correct', {
      doc_id: 'JIA',
      group_id: groupId.value,
      document: corrected,
      quota_limit: quotaLimit.value,
    })
    message.value = String(result.message ?? '更正版已提交')
    if (result.original_attempt_id) {
      await router.replace({ query: { ...route.query, attempt: result.original_attempt_id as string } })
      await openAttempt(String(result.original_attempt_id))
    }
    await refreshAttempts()
    await previewSelected()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '更正提交失败'
  } finally {
    loading.value = false
  }
}

async function refreshAttempts() {
  try {
    const payload = await getRecovery<{ items: AnyRecord[] }>(
      `/api/nested_flatten/recovery/attempts?group_id=${encodeURIComponent(groupId.value)}`,
    )
    attempts.value = payload.items ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史版本读取失败'
  }
}

async function openAttempt(attemptId: string) {
  try {
    detail.value = await getRecovery<AnyRecord>(`/api/nested_flatten/recovery/attempts/${encodeURIComponent(attemptId)}`)
    await router.replace({ query: { ...route.query, group: groupId.value, attempt: attemptId } })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '尝试明细读取失败'
  }
}

function closeDetail() {
  detail.value = null
  const query = { ...route.query }
  delete query.attempt
  void router.replace({ query })
}

onMounted(async () => {
  if (!route.query.group) {
    await router.replace({ query: { ...route.query, group: groupId.value } })
  }
  await refreshAttempts()
  const attemptId = route.query.attempt
  if (typeof attemptId === 'string') await openAttempt(attemptId)
  await previewSelected()
})
</script>

<style scoped>
.recovery-panel { background: #fff; border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; margin: 12px 0 16px; }
.recovery-head h3 { margin: 0 0 4px; font-size: 16px; }
.recovery-head p { margin: 0; color: var(--muted); font-size: 13px; }
.recovery-controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: flex-end; margin: 12px 0; }
.recovery-controls label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--muted); }
.recovery-controls input { padding: 7px 9px; border: 1px solid var(--border); border-radius: 6px; min-width: 120px; }
.recovery-controls .path-field input { min-width: 220px; }
.json-editor { margin: 8px 0; font-size: 13px; }
.json-editor textarea { width: 100%; margin: 8px 0; border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-family: ui-monospace, monospace; font-size: 12px; }
.recovery-message { background: #ecfdf3; border: 1px solid #abefc6; color: #067647; border-radius: 6px; padding: 8px 10px; font-size: 13px; margin: 8px 0; }
.recovery-error { color: #b42318; font-size: 13px; margin: 6px 0; }
.preview-box { border-top: 1px solid var(--border); padding-top: 10px; margin-top: 10px; }
.preview-docs { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; margin-top: 10px; }
.doc-card, .attempt-card, .detail-box { border: 1px solid var(--border); border-radius: 8px; padding: 10px; background: #fbfcfe; }
.doc-select { display: flex; gap: 6px; align-items: center; font-weight: 600; }
.doc-card p, .attempt-card p { margin: 6px 0; font-size: 13px; }
.recovery-grid { display: grid; grid-template-columns: minmax(280px, 0.9fr) minmax(360px, 1.1fr); gap: 14px; margin-top: 14px; }
.attempt-list { min-width: 0; }
.section-title { display: flex; justify-content: space-between; align-items: center; gap: 8px; }
.section-title h4 { margin: 0 0 8px; }
.attempt-card { margin-bottom: 8px; background: #fff; }
.attempt-card header { display: flex; justify-content: space-between; gap: 8px; font-size: 13px; }
.button-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.muted { color: var(--muted); font-size: 12px; }
.ok-text { color: #067647; }
.error-text { color: #b42318; }
.mini-table { width: 100%; border-collapse: collapse; font-size: 12px; margin: 8px 0; }
.mini-table th, .mini-table td { border: 1px solid var(--border); padding: 4px 6px; vertical-align: top; }
.mini-table code, pre { white-space: pre-wrap; word-break: break-all; }
pre { background: #0f172a; color: #e2e8f0; padding: 10px; border-radius: 6px; max-height: 260px; overflow: auto; font-size: 12px; }
@media (max-width: 1100px) { .recovery-grid { grid-template-columns: 1fr; } }
</style>
