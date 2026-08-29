<template>
  <div class="qa-trace" v-if="trace && trace.spans && trace.spans.length">
    <div class="trace-head">
      <span class="t-total">总耗时 <b>{{ fmt(trace.totalMs) }}</b></span>
      <span v-if="trace.bottleneckLabel" class="t-neck">⏱ 瓶颈：{{ trace.bottleneckLabel }}</span>
      <span v-for="(v, k) in trace.marks || {}" :key="k" class="t-mark" v-if="v !== null && v !== undefined && v !== ''">
        {{ markLabel(k) }}{{ v }}
      </span>
    </div>
    <div class="trace-bars">
      <template v-for="(s, i) in trace.spans" :key="i">
        <div
          class="bar-row"
          :class="{ err: s.status === 'error', clickable: hasDetail(s), open: openIdx === i }"
          :title="s.err ? ('异常：' + s.err) : s.label"
          @click="hasDetail(s) && toggle(i)"
        >
          <span class="bar-label" :class="'g-' + s.group">{{ s.label }}</span>
          <div class="bar-track">
            <div
              class="bar-fill"
              :class="['g-' + s.group, { neck: s.name === trace.bottleneck }]"
              :style="{ width: Math.max(1.5, s.pct) + '%' }"
            ></div>
            <span v-if="s.name === trace.bottleneck" class="neck-mark">◀</span>
          </div>
          <span class="bar-dur">{{ fmt(s.dur) }}</span>
          <span class="bar-pct">{{ s.pct }}%</span>
          <span class="bar-st" :class="{ ok: s.status !== 'error' }">{{ s.status === 'error' ? '✗' : '✓' }}</span>
        </div>
        <div v-if="openIdx === i" class="detail-panel" :class="'g-' + s.group">
          <div class="d-sec">
            <div class="d-sec-title">指标 / 参数</div>
            <div v-for="k in metaKeys(s)" :key="k" class="d-row">
              <span class="d-key">{{ DETAIL_META[k] || k }}</span>
              <span class="d-val" :title="fullText(s.attrs[k])">{{ clipText(s.attrs[k]) }}</span>
            </div>
            <div v-if="!metaKeys(s).length" class="d-empty">无</div>
          </div>
          <div v-if="ioKeys(s).length || s.attrs?.promptOmitted" class="d-sec">
            <div class="d-sec-title">Prompt / IO</div>
            <div v-if="s.attrs?.promptOmitted" class="d-note">内容超预算未采集</div>
            <div v-for="k in ioKeys(s)" :key="k" class="d-io">
              <div class="d-io-head">
                <span class="d-key">{{ IO_LABELS[k] || k }}</span>
                <span v-if="s.attrs?.[k + 'Truncated']" class="d-note">已截断，全文见 Langfuse</span>
                <span class="d-flex1"></span>
                <button class="d-btn" @click="copyText(k, s.attrs[k])">{{ copied[k] ? '已复制' : '复制' }}</button>
              </div>
              <pre class="d-io-pre" :class="{ expanded: ioExpanded[k] }" title="点击展开/收起全文" @click="toggleIo(k)">{{ s.attrs[k] }}</pre>
            </div>
          </div>
        </div>
      </template>
    </div>
  </div>
  <div v-else class="qa-trace empty">暂无链路耗时数据</div>
</template>

<script setup>
import { ref } from 'vue'

defineProps({ trace: { type: Object, default: () => ({}) } })

// —— 节点点击展开详情（行带 attrs 才可点；无 attrs 的老 trace 行保持原样零破坏）——
const openIdx = ref(-1)
const ioExpanded = ref({})
const copied = ref({})

function toggle(i) {
  openIdx.value = openIdx.value === i ? -1 : i
  ioExpanded.value = {}
  copied.value = {}
}
function hasDetail(s) {
  return !!(s.attrs && Object.keys(s.attrs).length)
}
function toggleIo(k) {
  ioExpanded.value = { ...ioExpanded.value, [k]: !ioExpanded.value[k] }
}
async function copyText(k, text) {
  try {
    await navigator.clipboard.writeText(String(text ?? ''))
    copied.value = { ...copied.value, [k]: true }
    setTimeout(() => { copied.value = { ...copied.value, [k]: false } }, 1500)
  } catch (e) { /* 剪贴板不可用（非安全上下文等）静默降级 */ }
}

// 对象值 JSON.stringify；null/undefined → 空串
function fullText(v) {
  if (v === null || v === undefined) return ''
  return typeof v === 'object' ? JSON.stringify(v) : String(v)
}
// 长文本 >80 字符截断展示（title 带全文）
function clipText(v, n = 80) {
  const t = fullText(v)
  return t.length > n ? t.slice(0, n) + '…' : t
}

// 节点 → 详情分区字段映射（key 对齐后端 attrs 命名）
const DETAIL_META = {
  grade: 'CRAG 分级', action: 'CRAG 动作', confidence: '置信', extras: 'CRAG 明细',
  route: '路由', queryType: '问题类型', reason: '理由', hits: '命中数',
  top1: 'top 分数', ef: 'HNSW ef', cand: '候选数', cloud: '云端向量命中',
  bge: 'bge 命中', rewritten: '改写后查询', changed: '是否改写',
  temperature: 'temperature', maxTokens: 'max_tokens', model: '模型',
  tokenUsage: 'tokens(in/out)', nMessages: '消息数', lines: '图谱链条数',
  annotated: '补标引用数', refs: '证据数', hit: 'hotqa 命中',
  topN: '重排输出数', degraded: '降级', k: 'RRF k', dw: 'dense 权重', sw: 'sparse 权重',
  lamda: 'MMR λ', candidates: '候选数', before: '过滤前', after: '过滤后',
}
const PROMPT_KEYS = ['promptSystem', 'promptUser', 'output']
const IO_LABELS = { promptSystem: 'System Prompt', promptUser: 'User Prompt', output: '输出' }

// 指标/参数分区：剔除 prompt 三键与截断标记（在 Prompt/IO 分区呈现）
function metaKeys(s) {
  const attrs = s.attrs || {}
  return Object.keys(attrs).filter(
    (k) => !PROMPT_KEYS.includes(k) && k !== 'promptOmitted' && !k.endsWith('Truncated')
  )
}
// Prompt/IO 分区：三键中实际有值的
function ioKeys(s) {
  const attrs = s.attrs || {}
  return PROMPT_KEYS.filter((k) => attrs[k] !== undefined && attrs[k] !== null && attrs[k] !== '')
}

function fmt(ms) {
  if (ms == null) return '-'
  return Math.round(ms) + 'ms'
}
function markLabel(k) {
  return ({ cacheLayer: '缓存·', route: '路由·', confidence: '置信·', provider_used: '模型·', llm_tier: '档位·', llm_route_reason: '路由·' })[k] || (k + '·')
}
</script>

<style scoped>
.qa-trace {
  margin-top: 10px;
  padding: 10px 12px;
  background: var(--surface);
  border: 1px solid var(--border-soft);
  border-radius: 8px;
  font-size: 12px;
}
.qa-trace.empty { color: var(--text-muted); }
.trace-head {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 8px;
  color: var(--text-muted);
}
.trace-head .t-total b { color: var(--text); font-size: 13px; }
.t-neck { color: var(--danger); font-weight: 600; }
.t-mark { color: var(--text-soft); }

.trace-bars { display: flex; flex-direction: column; gap: 3px; }
.bar-row {
  display: grid;
  grid-template-columns: 104px 1fr 52px 38px 14px;
  align-items: center;
  gap: 6px;
}
.bar-row.err .bar-label,
.bar-row.err .bar-dur { color: var(--danger); }
.bar-label {
  color: var(--text-soft);
  text-align: right;
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.bar-track {
  position: relative;
  height: 15px;
  background: var(--surface-3);
  border-radius: 3px;
}
.bar-fill { height: 100%; border-radius: 3px; transition: width .3s; }
.bar-fill.g-main { background: var(--primary, #3b82f6); }
.bar-fill.g-retrieval { background: var(--success, #22c55e); }
.bar-fill.neck { background: var(--danger, #ef4444); }
.neck-mark {
  position: absolute;
  right: 2px;
  top: 0;
  color: var(--danger);
  font-size: 11px;
  line-height: 15px;
}
.bar-dur { color: var(--text); font-variant-numeric: tabular-nums; font-size: 11px; }
.bar-pct { color: var(--text-muted); font-size: 11px; text-align: right; }
.bar-st { font-size: 11px; color: var(--danger); text-align: center; }
.bar-st.ok { color: var(--success); }
html.dark .bar-track { background: rgba(255, 255, 255, .08); }

/* —— 节点点击展开详情 —— */
.bar-row.clickable { cursor: pointer; border-radius: 3px; transition: background .15s; }
.bar-row.clickable:hover { background: var(--surface-2); }
.bar-row.open { background: var(--surface-2); }
.bar-row.open .bar-label { color: var(--text); font-weight: 600; }
.detail-panel {
  background: var(--surface-2);
  border-left: 2px solid var(--primary, #3b82f6);
  border-radius: 0 4px 4px 0;
  padding: 8px 10px;
  margin: 1px 0 2px;
  font-size: 11px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  min-width: 0;
}
.detail-panel.g-retrieval { border-left-color: var(--success, #22c55e); }
.detail-panel.g-main { border-left-color: var(--primary, #3b82f6); }
.d-sec { min-width: 0; }
.d-sec-title { font-weight: 600; color: var(--text-soft); margin-bottom: 4px; }
.d-row { display: flex; gap: 8px; line-height: 1.7; min-width: 0; }
.d-key { color: var(--text-muted); flex: 0 0 auto; min-width: 96px; }
.d-val { color: var(--text); word-break: break-all; min-width: 0; }
.d-empty { color: var(--text-muted); }
.d-note { color: var(--warning); }
.d-io { margin-bottom: 6px; }
.d-io:last-child { margin-bottom: 0; }
.d-io-head { display: flex; align-items: center; gap: 8px; margin-bottom: 3px; }
.d-io-head .d-key { min-width: 0; }
.d-flex1 { flex: 1; }
.d-btn {
  border: 1px solid var(--border-soft);
  background: transparent;
  color: var(--text-muted);
  font-size: 11px;
  line-height: 1.6;
  padding: 0 8px;
  border-radius: 4px;
  cursor: pointer;
  font-family: inherit;
}
.d-btn:hover { color: var(--text); border-color: var(--text-muted); }
.d-io-pre {
  margin: 0;
  padding: 6px 8px;
  background: var(--surface-3);
  border-radius: 4px;
  font-family: inherit;
  font-size: 11px;
  line-height: 1.5;
  max-height: 66px;   /* 折叠态约 4 行 */
  overflow: hidden;
  cursor: pointer;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-soft);
}
.d-io-pre.expanded { max-height: none; }
</style>
