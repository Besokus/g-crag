<template>
  <div class="page">
    <h2 class="page-title">链路诊断 · 问答耗时节点分析</h2>
    <div class="toolbar">
      <input class="input" v-model="bottleneck" placeholder="瓶颈节点（如 llm/rerank/embedding）" @keyup.enter="reset" />
      <label class="slow">
        <input type="checkbox" v-model="onlySlow" /> 只看慢查询 ≥
        <input class="input num" v-model.number="slowMs" /> ms
      </label>
      <button class="btn btn-primary" @click="reset">查询</button>
    </div>

    <table class="tbl">
      <thead>
        <tr><th>时间</th><th>问题</th><th>总耗时</th><th>瓶颈节点</th><th>缓存</th><th>置信</th><th>用户</th><th></th></tr>
      </thead>
      <tbody>
        <tr v-for="r in list" :key="r.traceId">
          <td class="ts">{{ r.ts }}</td>
          <td class="q" :title="r.query">{{ r.query }}</td>
          <td :class="{ slowrow: r.totalMs >= (onlySlow ? slowMs : 1500) }">{{ fmt(r.totalMs) }}</td>
          <td><span class="badge badge-danger" v-if="r.bottleneck">{{ r.bottleneck }}</span><span v-else>—</span></td>
          <td>{{ r.cacheLayer || '—' }}</td>
          <td>{{ r.confidence || '—' }}</td>
          <td>{{ r.username || '—' }}</td>
          <td><a class="link" @click="open(r.traceId)">详情</a></td>
        </tr>
        <tr v-if="!list.length"><td colspan="8" class="empty">暂无记录（问几个问题后这里会出现链路记录）</td></tr>
      </tbody>
    </table>

    <div class="pager" v-if="total > size">
      <button class="btn btn-ghost btn-sm" :disabled="page <= 1" @click="page--; load()">上一页</button>
      <span class="hint">{{ page }} / {{ Math.ceil(total / size) || 1 }}</span>
      <button class="btn btn-ghost btn-sm" :disabled="page * size >= total" @click="page++; load()">下一页</button>
    </div>

    <div class="modal-overlay" v-if="detail" @click.self="detail = null">
      <div class="modal">
        <div class="modal-head">
          链路详情 · <span class="muted">{{ detail.query }}</span>
          <a class="close" @click="detail = null">✕</a>
        </div>
        <div class="d-meta">
          总耗时 <b>{{ fmt(detail.totalMs) }}</b> · 瓶颈 {{ detail.bottleneckLabel || detail.bottleneck || '—' }}
          · {{ detail.ts }} · {{ detail.cacheLayer || '未缓存' }}
        </div>
        <QaTraceChart :trace="detail" />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getQaTraces, getQaTrace } from '../api'
import QaTraceChart from '../components/QaTraceChart.vue'

const list = ref([])
const total = ref(0)
const page = ref(1)
const size = ref(20)
const bottleneck = ref('')
const onlySlow = ref(false)
const slowMs = ref(1500)
const detail = ref(null)

function reset() { page.value = 1; load() }

async function load() {
  const params = { page: page.value, size: size.value }
  if (bottleneck.value.trim()) params.bottleneck = bottleneck.value.trim()
  if (onlySlow.value) params.slowMs = slowMs.value
  try {
    const r = await getQaTraces(params)
    list.value = (r.data && r.data.list) || []
    total.value = (r.data && r.data.total) || 0
  } catch (e) { list.value = []; total.value = 0 }
}

async function open(traceId) {
  try {
    const r = await getQaTrace(traceId)
    detail.value = r.data || null
  } catch (e) { detail.value = null }
}

function fmt(ms) {
  if (ms == null) return '—'
  if (ms >= 1000) return (ms / 1000).toFixed(2) + 's'
  return Math.round(ms) + 'ms'
}

onMounted(load)
</script>

<style scoped>
.page { padding: 16px 20px; }
.page-title { font-size: 18px; margin: 0 0 14px; color: var(--text); }
.toolbar { display: flex; gap: 12px; align-items: center; margin-bottom: 14px; flex-wrap: wrap; }
.toolbar .input { padding: 6px 10px; }
.toolbar .num { width: 80px; }
.slow { display: flex; align-items: center; gap: 5px; font-size: 12px; color: var(--text-muted); }
.tbl { width: 100%; border-collapse: collapse; font-size: 13px; background: var(--surface); }
.tbl th, .tbl td { border: 1px solid var(--border); padding: 7px 9px; text-align: left; }
.tbl th { background: var(--surface-2); font-weight: 600; color: var(--text-soft); }
.tbl .ts { white-space: nowrap; color: var(--text-muted); font-size: 12px; }
.tbl .q { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tbl td.slowrow { color: var(--danger); font-weight: 600; }
.tbl .link { color: var(--primary); cursor: pointer; }
.empty { text-align: center; color: var(--text-muted); padding: 24px; }
.pager { display: flex; align-items: center; gap: 12px; justify-content: center; margin-top: 14px; }
.pager .hint { font-size: 12px; color: var(--text-muted); }

.modal-overlay { position: fixed; inset: 0; background: rgba(0, 0, 0, .4); display: flex; align-items: center; justify-content: center; z-index: 50; }
.modal { background: var(--surface); border-radius: 10px; width: min(720px, 92vw); max-height: 86vh; overflow: auto; padding: 16px 18px; box-shadow: 0 8px 32px rgba(0, 0, 0, .2); }
.modal-head { font-weight: 600; margin-bottom: 10px; font-size: 15px; }
.modal-head .muted { color: var(--text-muted); font-weight: 400; font-size: 13px; }
.modal-head .close { float: right; cursor: pointer; color: var(--text-muted); }
.d-meta { color: var(--text-muted); font-size: 12px; margin-bottom: 8px; }
.d-meta b { color: var(--text); }
</style>
