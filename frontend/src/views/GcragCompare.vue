<template>
  <div class="cmp-page">
    <div class="card ask-card">
      <div class="ask-row">
        <input class="input" v-model="query" placeholder="输入一个问题，三种纠错模式依次作答" @keyup.enter="run" :disabled="running" />
        <label class="chk" title="跳过问答缓存、实时生成（故障注入开启时自动跳过）">
          <input type="checkbox" v-model="noCache" :disabled="!!fault" /> 跳过缓存
        </label>
        <button class="btn btn-primary" @click="run" :disabled="running || !query.trim()">
          {{ running ? `作答中 ${doneCount}/${cols.length}` : '对比作答' }}</button>
      </div>
      <div class="presets">
        <button v-for="p in PRESETS" :key="p.q" class="preset" :disabled="running" @click="pick(p)">
          <span class="badge" :class="p.badge">{{ p.tag }}</span>{{ p.q }}
        </button>
      </div>
    </div>

    <div class="status-bar" :class="{ warn: !!fault }">
      <span class="sb-item">生成：<b>Claude</b></span>
      <span class="sb-item">打分器：<b>gte-rerank-v2</b>
        <span class="badge" :class="fault ? 'badge-danger' : 'badge-success'">{{ fault ? `故障注入：${FAULT_LABEL[fault]}` : '正常' }}</span>
      </span>
      <span class="sb-hint" v-if="fault">打分失败时任何模式都不纠错、不拒答，分级记为 unscored、置信度降为 medium</span>
      <span class="sb-spacer"></span>
      <template v-if="isAdmin">
        <span class="sb-item muted">故障注入</span>
        <button v-for="k in [null, 'timeout', 'error']" :key="String(k)" class="btn btn-sm"
                :class="fault === k ? 'btn-primary' : 'btn-ghost'" :disabled="faultBusy" @click="setFault(k)">
          {{ k ? FAULT_LABEL[k] : '关闭' }}</button>
      </template>
    </div>

    <div class="cols">
      <div v-for="(c, ci) in cols" :key="ci" class="card col" :class="{ selected: graphCol === ci }">
        <div class="col-head">
          <select class="select mode-sel" v-model="c.mode" :disabled="running">
            <option v-for="m in MODE_OPTIONS" :key="m" :value="m">{{ m }}</option>
          </select>
          <span class="col-desc">{{ MODE_DESC[c.mode] }}</span>
        </div>

        <div v-if="c.loading" class="col-empty">⏳ 作答中…</div>
        <div v-else-if="c.error" class="col-empty err">✗ {{ c.error }}</div>
        <div v-else-if="!c.data" class="col-empty">{{ running ? '排队中' : '等待提问' }}</div>
        <template v-else>
          <div class="grade-flow">
            <span class="badge" :class="gradeBadge(c.data.initialGrade)">{{ gradeLabel(c.data.initialGrade) }}</span>
            <span class="arrow">→</span>
            <span class="badge" :class="gradeBadge(c.data.cragGrade)">{{ gradeLabel(c.data.cragGrade) }}</span>
            <span class="badge badge-primary">{{ ACTION_LABEL[c.data.cragAction] || c.data.cragAction }}</span>
            <span class="badge" :class="confBadge(c.data.confidence)">{{ CONF_LABEL[c.data.confidence] || c.data.confidence }}</span>
          </div>
          <div class="meta">
            <span>top1 {{ top1(c.data) }}</span>
            <span>⏱ {{ c.data.responseTime }}s</span>
            <span v-if="c.data.cached" class="badge badge-neutral">缓存</span>
            <span v-if="c.data.scoringOk === false" class="badge badge-danger" :title="c.data.scoringReason">打分失败</span>
            <span v-if="c.data.llmDegraded" class="badge badge-danger">LLM 降级</span>
          </div>

          <div class="sec" v-if="(c.data.correctionRounds || []).length">
            <div class="sec-title">纠错轮次</div>
            <div v-for="r in c.data.correctionRounds" :key="r.round" class="round">
              <div class="round-head">
                <b>第 {{ r.round }} 轮 · {{ ROUND_LABEL[r.action] || r.action }}</b>
                <span class="badge" :class="r.status === 'done' ? 'badge-success' : 'badge-neutral'">{{ STATUS_LABEL[r.status] || r.status }}</span>
              </div>
              <div class="round-body">
                {{ gradeLabel(r.gradeBefore) }} → {{ gradeLabel(r.gradeAfter) }}
                · top1 {{ fmt(r.top1Before) }} → {{ fmt(r.top1After) }}
                <template v-if="r.admitted !== undefined"> · 采纳 <b>{{ r.admitted }}</b> 条</template>
              </div>
              <div class="round-body muted" v-if="r.detail && r.detail.entities && r.detail.entities.length">
                链接实体：{{ r.detail.entities.join('、') }}
                <template v-if="r.detail.pathCount !== undefined"> · 路径 {{ r.detail.pathCount }} 条</template>
              </div>
            </div>
          </div>

          <div class="sec">
            <div class="sec-title">回答</div>
            <div class="ans md" v-html="renderMd(c.data.answer)"></div>
          </div>

          <div class="sec">
            <div class="sec-title">上下文来源（{{ (c.data.retrievalSource || []).length }}）</div>
            <div v-for="(s, si) in c.data.retrievalSource || []" :key="si" class="src" :class="{ graph: s.origin === 'graph' }">
              <span class="src-idx">[{{ si + 1 }}]</span>
              <span class="badge" :class="s.origin === 'graph' ? 'badge-primary' : 'badge-neutral'">{{ s.origin === 'graph' ? '图谱' : '检索' }}</span>
              <span class="src-score">{{ fmt(s.score) }}</span>
              <span class="src-text" :title="s.chunk">{{ (s.chunk || '').slice(0, 60) }}</span>
              <span v-if="(s.pathIds || []).length" class="src-path">经路径 {{ s.pathIds.map(i => '#' + i).join(' ') }}</span>
            </div>
          </div>

          <div class="col-actions">
            <button class="btn btn-sm btn-ghost" @click="c.traceOpen = !c.traceOpen">📊 链路耗时</button>
            <button v-if="(c.data.evidencePaths || []).length" class="btn btn-sm"
                    :class="graphCol === ci ? 'btn-primary' : 'btn-ghost'" @click="graphCol = ci">🧭 证据路径（{{ c.data.evidencePaths.length }}）</button>
          </div>
          <QaTraceChart v-if="c.traceOpen && c.data.trace" :trace="c.data.trace" />
        </template>
      </div>
    </div>

    <div class="card" v-if="graphData">
      <div class="card-header">
        <h3 class="card-title">证据路径 · {{ cols[graphCol].mode }}
          <span class="card-desc">高亮的路径带出了被采纳进上下文的出处 chunk；灰色为找到但未采纳的路径</span></h3>
      </div>
      <div class="graph-wrap">
        <div ref="graphEl" class="graph-el"></div>
        <div class="path-list">
          <div v-for="p in graphData.paths" :key="p.id" class="path" :class="{ hit: graphData.hit.has(p.id) }">
            <span class="path-id">#{{ p.id }}</span>{{ p.text }}
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import MarkdownIt from 'markdown-it'
import * as echarts from 'echarts/core'
import { GraphChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import QaTraceChart from '../components/QaTraceChart.vue'
import { compareAnswer, getFaultInjection, setFaultInjection } from '../api'
import { useAuthStore } from '../stores/auth'

echarts.use([GraphChart, TooltipComponent, CanvasRenderer])

const PRESETS = [
  { tag: '图谱挽回', badge: 'badge-success', q: '导致11人死亡的那起事故，最早接到报告的消防救援机构是哪个？' },
  { tag: '分级盲区', badge: 'badge-warning', q: "与高德地图合作开启'沿街取'服务的那家企业，最早是在哪一年进入中国市场的？" },
  { tag: '正常题', badge: 'badge-info', q: '三星堆博物馆新馆在四川开放后，其建筑面积是原展馆的几倍，以及新馆共展出了多少件(套)文物？' },
  { tag: '诚实拒答', badge: 'badge-danger', q: '2026年上海理工大学学生手册是什么' },
]
const MODE_OPTIONS = ['none', 'legacy', 'gcrag', 'gcrag_v2', 'gcrag_path', 'gcrag_path_all']
const MODE_DESC = {
  none: '不纠错：检索结果直接交给 LLM',
  legacy: '原有纠错：LLM 改写查询后重检索',
  gcrag: '图谱纠错：分级为模糊/不相关时，由图谱定位证据',
  gcrag_v2: '图谱纠错（图谱证据只追加、不挤掉检索结果）',
  gcrag_path: '路径感知重打分：用"问题 + 路径三元组"给图谱证据打分',
  gcrag_path_all: '路径感知重打分，且分级为可信时也补充图谱证据',
}
const GRADE_LABEL = { correct: '可信', ambiguous: '模糊', incorrect: '不相关', unscored: '未打分' }
const ACTION_LABEL = {
  normal: '未纠错', graph_recovered: '图谱挽回', graph_partial: '图谱部分改善', graph_supplemented: '图谱补充证据',
  rewrite_recovered: '改写挽回', rewrite_partial: '改写部分改善', ambiguous_kept: '保持模糊', refused: '拒答',
  unscored: '打分失败·不纠错',
}
const ROUND_LABEL = { graph_evidence: '图谱定位证据', graph_supplement: '图谱补充证据', rewrite: 'LLM 改写重检索' }
const STATUS_LABEL = { done: '已执行', no_improvement: '无改善', skipped_no_entity: '未链接到实体', skipped_no_path: '无图谱路径',
  unavailable: '图谱不可用', failed: '失败' }
const CONF_LABEL = { high: '高置信', medium: '证据有限', refused: '证据不足' }
const FAULT_LABEL = { timeout: '超时', error: '报错' }

const auth = useAuthStore()
const isAdmin = computed(() => auth.role === 'admin')
const query = ref(PRESETS[0].q)
const noCache = ref(false)
const running = ref(false)
const cols = reactive(['none', 'gcrag', 'gcrag_path_all'].map(mode => ({ mode, data: null, loading: false, error: '', traceOpen: false })))
const doneCount = computed(() => cols.filter(c => c.data || c.error).length)
const graphCol = ref(-1)
const fault = ref(null)
const faultBusy = ref(false)

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
function renderMd(t) { return t ? md.render(String(t)).replace(/\[(\d+)\]/g, '<sup class="cite-ref">[$1]</sup>') : '' }
function fmt(v) { return v === null || v === undefined ? '—' : Number(v).toFixed(3) }
function top1(d) { const s = (d.retrievalSource || []).filter(x => x.origin !== 'graph').map(x => +x.score || 0); return s.length ? fmt(Math.max(...s)) : '—' }
function gradeLabel(g) { return GRADE_LABEL[g] || g || '—' }
function gradeBadge(g) { return ({ correct: 'badge-success', ambiguous: 'badge-warning', incorrect: 'badge-danger' })[g] || 'badge-neutral' }
function confBadge(c) { return ({ high: 'badge-success', medium: 'badge-warning', refused: 'badge-danger' })[c] || 'badge-neutral' }

function pick(p) { query.value = p.q; run() }

async function run() {
  const q = query.value.trim()
  if (!q || running.value) return
  running.value = true
  graphCol.value = -1
  cols.forEach(c => { c.data = null; c.error = ''; c.traceOpen = false })
  for (const c of cols) {            // 顺序调用：Claude 代理突发并发会 403
    c.loading = true
    try {
      const r = await compareAnswer(q, c.mode, noCache.value || !!fault.value)
      if (r && r.code === 200) c.data = r.data
      else c.error = (r && r.message) || '请求失败'
    } catch (e) {
      c.error = e.message || '请求失败'
    }
    c.loading = false
  }
  running.value = false
  // 默认展示最右侧带证据路径的一列
  for (let i = cols.length - 1; i >= 0; i--) if ((cols[i].data?.evidencePaths || []).length) { graphCol.value = i; break }
}

async function loadFault() {
  if (!isAdmin.value) return
  try { const r = await getFaultInjection(); fault.value = r?.data?.scorer || null } catch (e) { /* 非管理员或后端未就绪 */ }
}
async function setFault(k) {
  faultBusy.value = true
  try { const r = await setFaultInjection(k); if (r?.code === 200) fault.value = r.data.scorer || null } finally { faultBusy.value = false }
}

// ---- 证据路径图 ----
const graphEl = ref(null)
let chart = null
const graphData = computed(() => {
  const c = cols[graphCol.value]
  const paths = c?.data?.evidencePaths || []
  if (!paths.length) return null
  const hit = new Set((c.data.retrievalSource || []).filter(s => s.origin === 'graph').flatMap(s => s.pathIds || []))
  const linked = new Set((c.data.linking?.entities || []).map(e => e.name))
  const nodes = new Map(), edges = new Map()
  for (const p of paths) {
    for (const n of p.nodes) {
      const cur = nodes.get(n) || { name: n, hit: false, start: linked.has(n) }
      cur.hit = cur.hit || hit.has(p.id)
      nodes.set(n, cur)
    }
    for (const e of p.edges) {
      const k = `${e.source}|${e.target}|${e.type}`
      const cur = edges.get(k) || { source: e.source, target: e.target, type: e.type, hit: false }
      cur.hit = cur.hit || hit.has(p.id)
      edges.set(k, cur)
    }
  }
  return { paths, hit, nodes: [...nodes.values()], edges: [...edges.values()] }
})

function drawGraph() {
  const g = graphData.value
  if (!g || !graphEl.value) return
  if (!chart) chart = echarts.init(graphEl.value)
  const css = getComputedStyle(document.documentElement)
  const primary = css.getPropertyValue('--primary').trim() || '#4f46e5'
  const accent = css.getPropertyValue('--accent').trim() || '#0ea5e9'
  const muted = css.getPropertyValue('--text-soft').trim() || '#94a3b8'
  chart.setOption({
    tooltip: { formatter: p => p.dataType === 'edge' ? `${p.data.source} -[${p.data.type}]-> ${p.data.target}` : p.data.name },
    series: [{
      type: 'graph', layout: 'force', roam: true, draggable: true,
      force: { repulsion: 260, edgeLength: [70, 130] },
      edgeSymbol: ['none', 'arrow'], edgeSymbolSize: 7,
      label: { show: true, fontSize: 11, color: 'inherit' },
      edgeLabel: { show: true, fontSize: 10, formatter: p => p.data.type },
      data: g.nodes.map(n => ({
        name: n.name, symbolSize: n.start ? 30 : n.hit ? 24 : 16,
        itemStyle: { color: n.start ? accent : n.hit ? primary : muted },
        label: { color: n.hit || n.start ? undefined : muted },
      })),
      links: g.edges.map(e => ({
        source: e.source, target: e.target, type: e.type,
        lineStyle: { color: e.hit ? primary : muted, width: e.hit ? 3 : 1, opacity: e.hit ? 0.95 : 0.45 },
      })),
    }],
  }, true)
}
watch(graphData, () => nextTick(drawGraph))
function onResize() { chart && chart.resize() }
onMounted(() => { loadFault(); window.addEventListener('resize', onResize) })
onBeforeUnmount(() => { window.removeEventListener('resize', onResize); chart && chart.dispose(); chart = null })
</script>

<style scoped>
.cmp-page { display: flex; flex-direction: column; gap: 12px; }
.ask-card { margin-bottom: 0; }
.ask-row { display: flex; gap: 10px; align-items: center; }
.ask-row .input { flex: 1; }
.chk { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-muted); white-space: nowrap; }
.presets { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.preset { display: inline-flex; align-items: center; gap: 6px; padding: 6px 10px; border: 1px solid var(--border); border-radius: 999px;
  background: var(--surface); color: var(--text); font-size: 12px; cursor: pointer; max-width: 100%; text-align: left; }
.preset:hover:not(:disabled) { border-color: var(--primary); background: var(--primary-soft); }
.preset:disabled { opacity: .5; cursor: not-allowed; }

.status-bar { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 8px 14px; border: 1px solid var(--border);
  border-radius: var(--radius-lg); background: var(--surface); font-size: 12px; color: var(--text-muted); }
.status-bar.warn { border-color: var(--danger); background: var(--danger-soft); }
.sb-item { display: inline-flex; align-items: center; gap: 6px; }
.sb-hint { color: var(--danger); }
.sb-spacer { flex: 1; }
.muted { color: var(--text-soft); }

.cols { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; align-items: start; }
@media (max-width: 1100px) { .cols { grid-template-columns: 1fr; } }
.col { margin-bottom: 0; padding: 14px; }
.col.selected { border-color: var(--primary); }
.col-head { display: flex; flex-direction: column; gap: 4px; margin-bottom: 10px; }
.mode-sel { width: auto; font-weight: 700; padding: 5px 8px; }
.col-desc { font-size: 12px; color: var(--text-soft); }
.col-empty { padding: 30px 0; text-align: center; color: var(--text-soft); font-size: 13px; }
.col-empty.err { color: var(--danger); }
.grade-flow { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.arrow { color: var(--text-soft); }
.meta { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; font-size: 12px; color: var(--text-muted); margin-top: 8px; }
.sec { margin-top: 12px; }
.sec-title { font-size: 12px; font-weight: 700; color: var(--text-muted); margin-bottom: 6px; }
.round { border-left: 3px solid var(--primary-soft-2); padding: 4px 8px; margin-bottom: 6px; font-size: 12px; }
.round-head { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
.round-body { margin-top: 2px; color: var(--text); }
.ans { font-size: 13px; line-height: 1.65; max-height: 260px; overflow: auto; padding: 8px 10px; background: var(--surface-2); border-radius: var(--radius-sm); }
.ans :deep(p) { margin: 0 0 6px; }
.ans :deep(.cite-ref) { color: var(--primary); font-weight: 600; }
.src { display: flex; align-items: center; gap: 6px; font-size: 12px; padding: 3px 0; border-bottom: 1px dashed var(--border-soft); }
.src.graph { background: var(--primary-soft); border-radius: 4px; padding: 3px 4px; }
.src-idx { color: var(--text-soft); }
.src-score { font-variant-numeric: tabular-nums; color: var(--text-muted); }
.src-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.src-path { color: var(--primary); white-space: nowrap; }
.col-actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }

.graph-wrap { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 12px; }
@media (max-width: 1100px) { .graph-wrap { grid-template-columns: 1fr; } }
.graph-el { height: 420px; border: 1px solid var(--border-soft); border-radius: var(--radius-sm); }
.path-list { max-height: 420px; overflow: auto; font-size: 12px; }
.path { padding: 5px 8px; border-radius: 4px; color: var(--text-soft); }
.path.hit { color: var(--text); background: var(--primary-soft); font-weight: 600; }
.path-id { display: inline-block; min-width: 28px; color: var(--primary); }
</style>
