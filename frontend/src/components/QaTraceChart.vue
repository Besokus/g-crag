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
      <div
        v-for="(s, i) in trace.spans"
        :key="i"
        class="bar-row"
        :class="{ err: s.status === 'error' }"
        :title="s.err ? ('异常：' + s.err) : s.label"
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
    </div>
  </div>
  <div v-else class="qa-trace empty">暂无链路耗时数据</div>
</template>

<script setup>
defineProps({ trace: { type: Object, default: () => ({}) } })

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
  right: -13px;
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
</style>
