<template>
  <div class="quality-events-page">
    <section class="stat-grid quality-stats" aria-label="质量事件统计">
      <button
        v-for="item in statusCards"
        :key="item.status || 'all'"
        type="button"
        class="stat stat-filter"
        :class="[{ active: filters.status === item.status }, item.tone]"
        :aria-pressed="filters.status === item.status"
        @click="filterByStatus(item.status)"
      >
        <span class="stat-val">{{ item.value }}</span>
        <span class="stat-lbl">{{ item.label }}</span>
      </button>
    </section>

    <section class="card event-workspace">
      <div class="card-header">
        <div>
          <h2 class="card-title">
            质量事件列表
            <span class="badge badge-neutral">{{ events.total }}</span>
          </h2>
        </div>
        <button type="button" class="btn btn-ghost btn-sm" :disabled="refreshing" @click="refresh">
          {{ refreshing ? '刷新中...' : '刷新' }}
        </button>
      </div>

      <form class="filter-grid" @submit.prevent="applyFilters">
        <label class="filter-field">
          <span>处理状态</span>
          <select v-model="filters.status" class="select">
            <option value="">全部状态</option>
            <option v-for="status in statuses" :key="status" :value="status">
              {{ statusLabel(status) }}
            </option>
          </select>
        </label>
        <label class="filter-field">
          <span>事件来源</span>
          <select v-model="filters.source" class="select">
            <option value="">全部来源</option>
            <option v-for="item in stats.sources" :key="item.value" :value="item.value">
              {{ sourceLabel(item.value) }}（{{ item.count }}）
            </option>
          </select>
        </label>
        <label class="filter-field">
          <span>事件类型</span>
          <select v-model="filters.eventType" class="select">
            <option value="">全部类型</option>
            <option v-for="item in stats.eventTypes" :key="item.value" :value="item.value">
              {{ eventTypeLabel(item.value) }}（{{ item.count }}）
            </option>
          </select>
        </label>
        <label class="filter-field">
          <span>Trace ID</span>
          <input v-model.trim="filters.traceId" class="input mono" placeholder="输入完整 Trace ID" />
        </label>
        <label class="filter-field">
          <span>会话 ID</span>
          <input v-model.trim="filters.conversationId" class="input mono" placeholder="输入完整会话 ID" />
        </label>
        <label class="filter-field">
          <span>开始时间</span>
          <input v-model="filters.startAt" type="datetime-local" class="input" />
        </label>
        <label class="filter-field">
          <span>结束时间</span>
          <input v-model="filters.endAt" type="datetime-local" class="input" />
        </label>
        <div class="filter-actions">
          <button type="submit" class="btn btn-primary" :disabled="listLoading">查询</button>
          <button type="button" class="btn btn-ghost" :disabled="listLoading" @click="resetFilters">重置</button>
        </div>
      </form>

      <div v-if="loadError" class="load-error" role="alert">{{ loadError }}</div>

      <div class="table-wrap" :aria-busy="listLoading">
        <table class="tbl event-table">
          <thead>
            <tr>
              <th>状态</th>
              <th>来源 / 类型</th>
              <th>问题与原因</th>
              <th>链路标识</th>
              <th>证据</th>
              <th>发生时间</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="event in events.list" :key="event.id">
              <td>
                <span class="badge" :class="statusBadge(event.status)">
                  {{ statusLabel(event.status) }}
                </span>
              </td>
              <td class="source-cell">
                <b>{{ sourceLabel(event.source) }}</b>
                <small>{{ eventTypeLabel(event.eventType) }}</small>
              </td>
              <td class="summary-cell">
                <b>{{ event.query || '未记录问题' }}</b>
                <small>{{ event.reason || answerExcerpt(event.answer) || '未记录原因' }}</small>
              </td>
              <td class="trace-cell">
                <span class="mono" :title="event.traceId">{{ shortId(event.traceId) }}</span>
                <small class="mono" :title="event.conversationId">{{ shortId(event.conversationId) }}</small>
              </td>
              <td>
                <span v-if="event.sources?.length" class="badge badge-info">
                  {{ event.sources.length }} 条
                </span>
                <span v-else class="muted">无</span>
              </td>
              <td class="time-cell">{{ formatTime(event.createdAt) }}</td>
              <td>
                <button type="button" class="btn btn-link btn-sm" @click="openDetail(event)">
                  {{ canManage ? '查看 / 处理' : '查看详情' }}
                </button>
              </td>
            </tr>
            <tr v-if="listLoading">
              <td colspan="7" class="loading-row">正在加载质量事件...</td>
            </tr>
            <tr v-else-if="!events.list.length">
              <td colspan="7" class="empty">暂无符合条件的质量事件</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div class="pagination">
        <div class="page-size">
          <span>每页</span>
          <select v-model.number="pageSize" class="select compact-select" @change="changePageSize">
            <option :value="10">10</option>
            <option :value="20">20</option>
            <option :value="50">50</option>
          </select>
          <span>条</span>
        </div>
        <span class="page-summary">第 {{ page }} / {{ totalPages }} 页</span>
        <button type="button" class="btn btn-ghost btn-sm" :disabled="page <= 1 || listLoading" @click="goPage(page - 1)">
          上一页
        </button>
        <button type="button" class="btn btn-ghost btn-sm" :disabled="page >= totalPages || listLoading" @click="goPage(page + 1)">
          下一页
        </button>
      </div>
    </section>

    <div v-if="detailOpen" class="modal-bg" role="presentation" @click.self="closeDetail">
      <article class="modal quality-modal" role="dialog" aria-modal="true" aria-labelledby="quality-event-title">
        <header class="modal-head">
          <div>
            <div id="quality-event-title">质量事件详情</div>
            <div v-if="selectedEvent" class="modal-sub mono">{{ selectedEvent.id }}</div>
          </div>
          <button type="button" class="icon-btn close-button" title="关闭" aria-label="关闭" @click="closeDetail">×</button>
        </header>

        <div v-if="detailLoading" class="detail-loading">正在加载事件详情...</div>
        <div v-else-if="selectedEvent" class="modal-body">
          <dl class="detail-grid">
            <div>
              <dt>状态</dt>
              <dd><span class="badge" :class="statusBadge(selectedEvent.status)">{{ statusLabel(selectedEvent.status) }}</span></dd>
            </div>
            <div><dt>来源</dt><dd>{{ sourceLabel(selectedEvent.source) }}</dd></div>
            <div><dt>事件类型</dt><dd>{{ eventTypeLabel(selectedEvent.eventType) }}</dd></div>
            <div><dt>发生时间</dt><dd>{{ formatTime(selectedEvent.createdAt) }}</dd></div>
            <div><dt>Trace ID</dt><dd class="mono break-text">{{ selectedEvent.traceId || '—' }}</dd></div>
            <div><dt>会话 ID</dt><dd class="mono break-text">{{ selectedEvent.conversationId || '—' }}</dd></div>
            <div><dt>用户</dt><dd>{{ selectedEvent.user?.username || selectedEvent.user?.id || '—' }}</dd></div>
            <div><dt>处理时间</dt><dd>{{ formatTime(selectedEvent.handledAt) }}</dd></div>
          </dl>

          <section class="detail-section">
            <h3>问题</h3>
            <div class="content-text">{{ selectedEvent.query || '—' }}</div>
          </section>
          <section class="detail-section">
            <h3>回答</h3>
            <div class="content-text answer-text">{{ selectedEvent.answer || '—' }}</div>
          </section>
          <section class="detail-section">
            <h3>触发原因</h3>
            <div class="content-text">{{ selectedEvent.reason || '—' }}</div>
          </section>

          <section class="detail-section">
            <h3>检索证据 <span class="badge badge-neutral">{{ selectedEvent.sources?.length || 0 }}</span></h3>
            <div v-if="selectedEvent.sources?.length" class="source-list">
              <div v-for="(source, index) in selectedEvent.sources" :key="`${source.doc}-${index}`" class="source-row">
                <div class="source-index">{{ index + 1 }}</div>
                <div class="source-content">
                  <div class="source-title">
                    <b>{{ source.doc || source.docName || '未命名来源' }}</b>
                    <span v-if="source.score !== null && source.score !== undefined" class="badge badge-info">
                      {{ formatScore(source.score) }}
                    </span>
                  </div>
                  <div class="source-excerpt">{{ source.chunk || source.content || '未记录片段内容' }}</div>
                </div>
              </div>
            </div>
            <div v-else class="empty compact-empty">未记录检索证据</div>
          </section>

          <section v-if="selectedEvent.management?.history?.length" class="detail-section">
            <h3>处理记录</h3>
            <ol class="history-list">
              <li v-for="(item, index) in selectedEvent.management.history" :key="`${item.at}-${index}`">
                <span>{{ statusLabel(item.from) }} → {{ statusLabel(item.to) }}</span>
                <small>{{ item.operator || '未知操作人' }} · {{ formatTime(item.at) }}</small>
                <p v-if="item.note">{{ item.note }}</p>
              </li>
            </ol>
          </section>

          <details class="raw-detail">
            <summary>原始载荷</summary>
            <pre>{{ payloadJson }}</pre>
          </details>
        </div>

        <footer v-if="selectedEvent && canManage" class="modal-foot">
          <label class="status-field">
            <span>处理状态</span>
            <select v-model="statusForm.status" class="select">
              <option v-for="status in availableStatuses" :key="status" :value="status">
                {{ statusLabel(status) }}
              </option>
            </select>
          </label>
          <label class="note-field">
            <span>处理备注</span>
            <textarea v-model.trim="statusForm.note" class="input" rows="2" maxlength="1000" placeholder="填写判断依据或后续动作"></textarea>
          </label>
          <button type="button" class="btn btn-primary save-status" :disabled="statusSaving" @click="saveStatus">
            {{ statusSaving ? '保存中...' : '保存状态' }}
          </button>
        </footer>
      </article>
    </div>

    <div v-if="toastMessage" class="toast" role="status">{{ toastMessage }}</div>
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import {
  getQualityEvent,
  getQualityEvents,
  getQualityEventStats,
  updateQualityEventStatus,
} from '../api'
import { useAuthStore } from '../stores/auth'
import { hasPerm } from '../utils/perm'

const statuses = ['open', 'processing', 'resolved', 'ignored']
const transitionMap = {
  open: statuses,
  processing: statuses,
  resolved: ['open', 'resolved'],
  ignored: ['open', 'ignored'],
}

const auth = useAuthStore()
const canManage = computed(
  () => hasPerm(auth.role, 'feedback:manage') || hasPerm(auth.role, 'system:config'),
)
const stats = ref({ counts: {}, sources: [], eventTypes: [] })
const events = ref({ total: 0, list: [] })
const filters = reactive({
  status: '',
  source: '',
  eventType: '',
  traceId: '',
  conversationId: '',
  startAt: '',
  endAt: '',
})
const page = ref(1)
const pageSize = ref(20)
const listLoading = ref(false)
const refreshing = ref(false)
const loadError = ref('')
const detailOpen = ref(false)
const detailLoading = ref(false)
const selectedEvent = ref(null)
const statusSaving = ref(false)
const statusForm = reactive({ status: 'open', note: '' })
const toastMessage = ref('')
let toastTimer

const totalPages = computed(() => Math.max(1, Math.ceil((events.value.total || 0) / pageSize.value)))
const statusCards = computed(() => {
  const counts = stats.value.counts || {}
  return [
    { status: '', label: '全部事件', value: counts.total || 0, tone: 'all-tone' },
    { status: 'open', label: '待处理', value: counts.open || 0, tone: 'open-tone' },
    { status: 'processing', label: '处理中', value: counts.processing || 0, tone: 'processing-tone' },
    { status: 'resolved', label: '已解决', value: counts.resolved || 0, tone: 'resolved-tone' },
    { status: 'ignored', label: '已忽略', value: counts.ignored || 0, tone: 'ignored-tone' },
  ]
})
const availableStatuses = computed(
  () => transitionMap[selectedEvent.value?.status] || statuses,
)
const payloadJson = computed(() => JSON.stringify(selectedEvent.value?.payload || {}, null, 2))

function unwrapBiz(response) {
  if (!response || response.code !== 200) {
    throw new Error(response?.message || '请求失败')
  }
  return response.data
}

function errorMessage(error, fallback) {
  return error?.response?.data?.message || error?.message || fallback
}

function notify(message) {
  toastMessage.value = message
  clearTimeout(toastTimer)
  toastTimer = setTimeout(() => {
    toastMessage.value = ''
  }, 2400)
}

function statusLabel(status) {
  return {
    open: '待处理',
    pending: '待处理',
    failed: '待处理',
    processing: '处理中',
    resolved: '已解决',
    handled: '已解决',
    ignored: '已忽略',
  }[status] || status || '未知'
}

function statusBadge(status) {
  return {
    open: 'badge-warning',
    processing: 'badge-info',
    resolved: 'badge-success',
    ignored: 'badge-neutral',
  }[status] || 'badge-neutral'
}

function sourceLabel(source) {
  return {
    feedback: '用户反馈',
    online_eval: '在线评测',
    qa: '问答链路',
    qa_service: '问答服务',
    retrieval: '检索链路',
    retrieval_eval: '检索评测',
    governance: '知识治理',
  }[source] || source || '未知来源'
}

function eventTypeLabel(eventType) {
  return {
    dislike: '负向反馈',
    low_faith: '低忠实度',
    low_faithfulness: '低忠实度',
    low_retrieval_quality: '低检索质量',
    answer_quality: '回答质量',
    refused: '证据不足拒答',
    eval_low: '评测低分',
    doc_blocked: '文档治理阻断',
    over_confident: '过度自信冲突',
  }[eventType] || eventType || '未知类型'
}

function answerExcerpt(value) {
  const text = String(value || '').replace(/\s+/g, ' ').trim()
  return text.length > 90 ? `${text.slice(0, 90)}...` : text
}

function shortId(value) {
  const text = String(value || '')
  if (!text) return '—'
  return text.length > 18 ? `${text.slice(0, 8)}...${text.slice(-6)}` : text
}

function parseUtcDate(value) {
  if (!value) return null
  const text = String(value)
  const normalized = /(Z|[+-]\d{2}:\d{2})$/i.test(text) ? text : `${text}Z`
  const date = new Date(normalized)
  return Number.isNaN(date.getTime()) ? null : date
}

function formatTime(value) {
  const date = parseUtcDate(value)
  return date ? date.toLocaleString('zh-CN', { hour12: false }) : '—'
}

function formatScore(value) {
  const score = Number(value)
  return Number.isFinite(score) ? score.toFixed(3) : String(value)
}

function toUtcIso(value) {
  if (!value) return undefined
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString()
}

function listParams() {
  return {
    page: page.value,
    size: pageSize.value,
    status: filters.status || undefined,
    source: filters.source || undefined,
    eventType: filters.eventType || undefined,
    traceId: filters.traceId || undefined,
    conversationId: filters.conversationId || undefined,
    startAt: toUtcIso(filters.startAt),
    endAt: toUtcIso(filters.endAt),
  }
}

async function loadStats() {
  try {
    stats.value = unwrapBiz(await getQualityEventStats()) || stats.value
  } catch (error) {
    notify(errorMessage(error, '统计加载失败'))
  }
}

async function loadEvents() {
  listLoading.value = true
  loadError.value = ''
  try {
    const data = unwrapBiz(await getQualityEvents(listParams()))
    events.value = {
      total: Number(data?.total || 0),
      list: Array.isArray(data?.list) ? data.list : [],
    }
    if (page.value > totalPages.value) {
      page.value = totalPages.value
      await loadEvents()
    }
  } catch (error) {
    loadError.value = errorMessage(error, '质量事件加载失败')
  } finally {
    listLoading.value = false
  }
}

async function refresh() {
  refreshing.value = true
  await Promise.all([loadStats(), loadEvents()])
  refreshing.value = false
}

function applyFilters() {
  page.value = 1
  loadEvents()
}

function resetFilters() {
  Object.assign(filters, {
    status: '',
    source: '',
    eventType: '',
    traceId: '',
    conversationId: '',
    startAt: '',
    endAt: '',
  })
  page.value = 1
  loadEvents()
}

function filterByStatus(status) {
  filters.status = status
  page.value = 1
  loadEvents()
}

function goPage(target) {
  page.value = Math.min(totalPages.value, Math.max(1, target))
  loadEvents()
}

function changePageSize() {
  page.value = 1
  loadEvents()
}

async function openDetail(event) {
  detailOpen.value = true
  detailLoading.value = true
  selectedEvent.value = null
  try {
    selectedEvent.value = unwrapBiz(await getQualityEvent(event.id))
    statusForm.status = selectedEvent.value?.status || 'open'
    statusForm.note = selectedEvent.value?.note || ''
  } catch (error) {
    notify(errorMessage(error, '事件详情加载失败'))
    detailOpen.value = false
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  if (statusSaving.value) return
  detailOpen.value = false
  selectedEvent.value = null
}

async function saveStatus() {
  if (!selectedEvent.value || !canManage.value) return
  statusSaving.value = true
  try {
    selectedEvent.value = unwrapBiz(
      await updateQualityEventStatus(
        selectedEvent.value.id,
        statusForm.status,
        statusForm.note,
      ),
    )
    statusForm.status = selectedEvent.value.status
    statusForm.note = selectedEvent.value.note || ''
    notify('质量事件状态已更新')
    await Promise.all([loadStats(), loadEvents()])
  } catch (error) {
    notify(errorMessage(error, '状态更新失败'))
  } finally {
    statusSaving.value = false
  }
}

function handleKeydown(event) {
  if (event.key === 'Escape' && detailOpen.value) closeDetail()
}

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
  refresh()
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown)
  clearTimeout(toastTimer)
})
</script>

<style scoped>
.quality-events-page {
  min-width: 0;
}

.quality-stats {
  grid-template-columns: repeat(5, minmax(0, 1fr));
}

.stat-filter {
  width: 100%;
  min-height: 86px;
  border: 1px solid var(--border);
  color: inherit;
  font: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color .15s, box-shadow .15s, transform .15s;
}

.stat-filter:hover {
  border-color: var(--text-soft);
  box-shadow: var(--shadow);
  transform: translateY(-1px);
}

.stat-filter.active {
  border-color: var(--primary);
  box-shadow: 0 0 0 2px var(--primary-soft);
}

.all-tone { border-left: 3px solid var(--primary); }
.open-tone { border-left: 3px solid var(--warning); }
.processing-tone { border-left: 3px solid var(--info); }
.resolved-tone { border-left: 3px solid var(--success); }
.ignored-tone { border-left: 3px solid var(--text-soft); }

.filter-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(150px, 1fr));
  gap: 12px;
  align-items: end;
  margin-bottom: 18px;
  padding: 14px;
  background: var(--surface-2);
  border: 1px solid var(--border-soft);
  border-radius: var(--radius);
}

.filter-field,
.status-field,
.note-field {
  display: grid;
  gap: 5px;
  min-width: 0;
}

.filter-field > span,
.status-field > span,
.note-field > span {
  color: var(--text-muted);
  font-size: 12px;
  font-weight: 600;
}

.filter-actions {
  display: flex;
  gap: 8px;
  align-items: center;
  min-height: 38px;
}

.load-error {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--danger);
  border-radius: var(--radius-sm);
  background: var(--danger-soft);
  color: var(--danger);
  font-size: 13px;
}

.table-wrap {
  overflow-x: auto;
}

.event-table {
  min-width: 1040px;
}

.source-cell {
  min-width: 150px;
}

.source-cell small,
.summary-cell small,
.trace-cell small {
  display: block;
  margin-top: 4px;
  color: var(--text-soft);
}

.summary-cell {
  min-width: 280px;
  max-width: 440px;
}

.summary-cell b,
.summary-cell small {
  display: -webkit-box;
  overflow: hidden;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-height: 1.45;
}

.trace-cell {
  min-width: 170px;
  color: var(--text-muted);
}

.time-cell {
  min-width: 150px;
  color: var(--text-muted);
  white-space: nowrap;
}

.mono {
  font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace;
}

.loading-row {
  padding: 44px 16px !important;
  color: var(--text-soft) !important;
  text-align: center;
}

.pagination {
  display: flex;
  justify-content: flex-end;
  align-items: center;
  gap: 8px;
  margin-top: 16px;
}

.page-size {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-right: 8px;
  color: var(--text-muted);
  font-size: 12px;
}

.compact-select {
  width: 68px;
  padding: 5px 8px;
}

.page-summary {
  min-width: 82px;
  color: var(--text-muted);
  font-size: 12px;
  text-align: center;
}

.quality-modal {
  max-width: 980px;
  max-height: 90vh;
  height: auto;
}

.modal-sub {
  margin-top: 2px;
  color: var(--text-soft);
  font-size: 11px;
  font-weight: 400;
}

.close-button {
  flex: 0 0 34px;
  font-size: 22px;
  line-height: 1;
}

.detail-loading {
  padding: 80px 20px;
  color: var(--text-soft);
  text-align: center;
}

.modal-body {
  min-height: 0;
  padding: 18px;
  overflow-y: auto;
}

.detail-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px 18px;
  margin: 0;
}

.detail-grid div {
  min-width: 0;
}

.detail-grid dt {
  margin-bottom: 4px;
  color: var(--text-soft);
  font-size: 11px;
}

.detail-grid dd {
  margin: 0;
  color: var(--text);
  font-size: 13px;
}

.break-text {
  overflow-wrap: anywhere;
}

.detail-section {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}

.detail-section h3 {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 0 0 8px;
  font-size: 13px;
}

.content-text {
  color: var(--text);
  line-height: 1.7;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.answer-text {
  color: var(--text-muted);
}

.source-list {
  display: grid;
  gap: 10px;
}

.source-row {
  display: grid;
  grid-template-columns: 28px minmax(0, 1fr);
  gap: 10px;
  padding: 10px 0;
  border-bottom: 1px solid var(--border-soft);
}

.source-index {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--surface-2);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 700;
}

.source-content {
  min-width: 0;
}

.source-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.source-excerpt {
  margin-top: 5px;
  color: var(--text-muted);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.compact-empty {
  padding: 22px 0;
}

.history-list {
  display: grid;
  gap: 10px;
  margin: 0;
  padding-left: 20px;
}

.history-list li {
  padding-left: 4px;
}

.history-list small {
  display: block;
  margin-top: 2px;
  color: var(--text-soft);
}

.history-list p {
  margin: 4px 0 0;
  color: var(--text-muted);
}

.raw-detail {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px solid var(--border);
}

.raw-detail summary {
  color: var(--text-muted);
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
}

.raw-detail pre {
  max-height: 320px;
  margin: 10px 0 0;
  padding: 12px;
  overflow: auto;
  border-radius: var(--radius-sm);
  background: var(--surface-2);
  color: var(--text-muted);
  font: 11px/1.6 ui-monospace, SFMono-Regular, Consolas, monospace;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.modal-foot {
  display: grid;
  grid-template-columns: 150px minmax(0, 1fr) auto;
  gap: 12px;
  align-items: end;
  padding: 14px 18px;
  border-top: 1px solid var(--border);
  background: var(--surface-2);
}

.note-field textarea {
  min-height: 64px;
  resize: vertical;
}

.save-status {
  min-height: 38px;
  margin-bottom: 1px;
}

@media (max-width: 1100px) {
  .quality-stats {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .filter-grid {
    grid-template-columns: repeat(2, minmax(150px, 1fr));
  }

  .detail-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 680px) {
  .quality-stats,
  .filter-grid,
  .detail-grid,
  .modal-foot {
    grid-template-columns: 1fr;
  }

  .quality-stats {
    gap: 8px;
  }

  .stat-filter {
    min-height: 72px;
  }

  .filter-actions {
    width: 100%;
  }

  .filter-actions .btn {
    flex: 1;
  }

  .pagination {
    justify-content: space-between;
    flex-wrap: wrap;
  }

  .page-size {
    width: 100%;
  }

  .quality-modal {
    max-height: 94vh;
  }
}
</style>
