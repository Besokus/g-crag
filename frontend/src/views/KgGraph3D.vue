<template>
  <div>
    <div class="card" style="margin-bottom:12px">
      <div class="row" style="gap:8px">
        <input class="input" v-model="entity" placeholder="搜索设备/故障（空=全量图谱）" @keyup.enter="loadGraph" style="flex:1" />
        <button class="btn btn-primary" @click="loadGraph" :disabled="loading">{{ loading ? '加载中…' : '🔍 搜索' }}</button>
        <button class="btn btn-ghost" @click="toggle3D">{{ is3D ? '切换2D' : '切换3D' }}</button>
      </div>
    </div>

    <div class="graph-container" ref="containerRef">
      <canvas ref="canvasRef"></canvas>
      <div class="graph-tools">
        <button class="fs-btn" @click="export3DPng" title="导出 PNG">📷</button>
        <button class="fs-btn" @click="toggleFullscreen" :title="isFs ? '退出全屏(Esc)' : '全屏'">{{ isFs ? '⤫' : '⛶' }}</button>
      </div>
      <div v-if="!graph" class="graph-hint">搜索设备或点击「搜索」加载知识图谱</div>
      <div class="graph-info" v-if="graph">
        <span class="badge badge-neutral">{{ graph.nodes?.length || 0 }} 节点</span>
        <span class="badge badge-neutral">{{ graph.links?.length || 0 }} 关系</span>
      </div>
      <div class="graph-legend" v-if="graph && is3D">
        <span><i class="dot" style="background:#3498db"></i>设备</span>
        <span><i class="dot" style="background:#e74c3c"></i>故障</span>
        <span><i class="dot" style="background:#2ecc71"></i>操作</span>
        <span><i class="dot" style="background:#9b59b6"></i>规程</span>
        <span><i class="dot" style="background:#f39c12"></i>参数</span>
        <span><i class="dot" style="background:#1abc9c"></i>部件</span>
      </div>
    </div>

    <div class="card" v-if="selected">
      <div class="src-head">节点详情 <a class="ev-btn" style="float:right" @click="clearSel()">✕</a></div>
      <div class="cause"><b>{{ selected.label || selected.name }}</b>
        <div class="cause-line">类型：{{ typeName(selected.type) }} · 出度 {{ selected.outDegree || 0 }} · 邻接 {{ selected.neighborCount || 0 }}</div>
        <div class="cause-line" v-if="selected.dimension">知识维度：{{ selected.dimension }}</div>
        <div v-if="selected.rels && selected.rels.length" class="rel-line">
          <span class="rel-chip" v-for="(r, i) in selected.rels.slice(0, 6)" :key="i">{{ r }}</span>
        </div>
        <div class="cause-line" v-if="selected.neighborNames && selected.neighborNames.length" style="color:var(--text-muted)">关联：{{ selected.neighborNames.slice(0, 8).join(' · ') }}</div>
      </div>
    </div>
    <div v-if="tooltip" class="graph-tip" :style="{ left: tooltip.x + 'px', top: tooltip.y + 'px' }">{{ tooltip.text }}</div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { getKgGraph } from '../api'
import * as THREE from 'three'

const entity = ref('')
const graph = ref(null)
const loading = ref(false)
const selected = ref(null)
const is3D = ref(true)

const containerRef = ref(null)
const canvasRef = ref(null)
let animId = null
let dragCleanup = null
let currentRenderer = null
let currentCamera = null
const isFs = ref(false)
const tooltip = ref(null)

// 3D 节点交互（点击选中/邻接高亮）—— render3D 闭包内注册内部实现，此处暴露模板可调入口
let _clearSel = null
function clearSel() { _clearSel && _clearSel() }
const _DIM_LABEL = { equipment: '设备', fault: '故障', action: '操作', rule: '规程', metric: '参数', component: '部件', default: '其他' }
function typeName(t) { return ({ Equipment: '设备', Fault: '故障', Action: '操作' })[t] || t || '设备' }
function dimName(k) { return _DIM_LABEL[k] || _DIM_LABEL.default }
function toggleFullscreen() {
  const el = containerRef.value
  if (!el) return
  if (document.fullscreenElement) document.exitFullscreen()
  else el.requestFullscreen()
}
function onFsChange() {
  isFs.value = !!document.fullscreenElement
  const w = containerRef.value?.clientWidth || window.innerWidth
  const h = containerRef.value?.clientHeight || window.innerHeight
  if (currentRenderer) { currentRenderer.setSize(w, h); currentCamera.aspect = w / h; currentCamera.updateProjectionMatrix() }
}
// 3D 图谱导出 PNG
function export3DPng() {
  if (!currentRenderer) return
  // 强制渲染一帧确保画布内容最新
  currentRenderer.render(currentRenderer.domElement.__scene || new THREE.Scene(), currentCamera)
  const url = canvasRef.value.toDataURL('image/png')
  const a = document.createElement('a'); a.href = url; a.download = '3D知识图谱.png'; a.click()
}

async function loadGraph() {
  loading.value = true
  try {
    const r = await getKgGraph(entity.value, 200)
    graph.value = r.data || null
    await nextTick()
    if (graph.value && is3D.value) render3D()
  } catch { /* silent */ } finally { loading.value = false }
}

// 简易 3D 力导向图（基于 Three.js）
function render3D() {
  if (!canvasRef.value || !graph.value) return
  const container = containerRef.value
  const W = container.clientWidth || 600
  const H = container.clientHeight || 500

  try {
      const scene = new THREE.Scene()
      scene.background = new THREE.Color(0x1a1a2e)
      const camera = new THREE.PerspectiveCamera(60, W / H, 0.1, 1000)
      currentCamera = camera
      camera.position.set(25, 22, 25)
      const renderer = new THREE.WebGLRenderer({ canvas: canvasRef.value, antialias: true })
      currentRenderer = renderer
      renderer.setSize(W, H)
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))

      // 光源
      const ambient = new THREE.AmbientLight(0x404060)
      scene.add(ambient)
      const dir = new THREE.DirectionalLight(0xffffff, 0.8)
      dir.position.set(10, 20, 10)
      scene.add(dir)

      // 布局
      const nodes = graph.value.nodes || []
      const links = graph.value.links || []
      const N = nodes.length
      const positions = new Array(N).fill(0).map(() => ({
        x: (Math.random() - 0.5) * 12,
        y: (Math.random() - 0.5) * 12,
        z: (Math.random() - 0.5) * 12,
        vx: 0, vy: 0, vz: 0,
      }))
      // 拖动交互状态 + 渲染同步（simulate 与拖动共用，避免位置/连线不一致）
      const raycaster = new THREE.Raycaster()
      const ndcMouse = new THREE.Vector2()
      const dragPlane = new THREE.Plane()
      const dragHit = new THREE.Vector3()
      let dragIdx = -1
      const syncRender = () => {
        for (let i = 0; i < N; i++) {
          spheres[i]?.position.set(positions[i].x, positions[i].y, positions[i].z)
          labels[i]?.position.set(positions[i].x, positions[i].y + 1.0, positions[i].z)
        }
        while (lineGroup.children.length) lineGroup.remove(lineGroup.children[0])
        lineMat.opacity = selectedIdx >= 0 ? 0.14 : 0.4
        for (const [si, ti] of linkPairs) {
          const hot = selectedIdx >= 0 && (si === selectedIdx || ti === selectedIdx)
          const pts = [new THREE.Vector3(positions[si].x, positions[si].y, positions[si].z),
                       new THREE.Vector3(positions[ti].x, positions[ti].y, positions[ti].z)]
          lineGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts), hot ? highlightLineMat : lineMat))
        }
      }

      // 按「知识维度」分 6 类着色。后端 category 仅按三元组主/宾(SPO)二分，不体现语义类别；
      // 故基于节点 name 的电网运维领域关键词做语义归类，落到 6 个知识维度。
      const DIM_COLORS = {
        equipment: 0x3498db, // 设备/装置 - 蓝
        fault:     0xe74c3c, // 故障/异常 - 红
        action:    0x2ecc71, // 操作/处置 - 绿
        rule:      0x9b59b6, // 规程/制度 - 紫
        metric:    0xf39c12, // 参数/指标 - 橙
        component: 0x1abc9c, // 部件/材料 - 青
        default:   0x95a5a6,
      }
      // 分类优先级：数值参数 > 故障 > 规程 > 操作 > 设备 > 部件（含数字/阈值的先判，避免被关键词误判）
      const classify = (raw) => {
        const s = (raw || '').toString()
        if (/[0-9.]+\s*(℃|°C|Pa|MPa|kV|kA|kV|V|A|Ω|MΩ|mL|μL|%|Hz|rpm|kW|MW)/.test(s) ||
            /(不低于|不高于|不超过|至少|以上|以下|约为|左右|≤|≥)/.test(s)) return 'metric'
        if (/(泄漏|卡涩|喷油|超标|接地|拒动|误动|放电|击穿|故障|异响|老化|缺陷|烧穿|受潮|过热|异常|跳闸|闭锁|磨损|发热)/.test(s)) return 'fault'
        if (/(票|制度|规程|监护|许可|措施|巡回|交接班|两票|三制)/.test(s)) return 'rule'
        if (/^(用|检查|测量|验电|停电|装设|拆除|更换|清洗|回收|抽离|巡视|试验|操作|解体|倒闸|悬挂|穿戴|执行|关注|倾听|申请|判断|选用|进行|做好|持续|严禁|不得|禁止|由|每)/.test(s)) return 'action'
        if (/(变压器|断路器|隔离开关|互感器|开关柜|继电器|线路|电缆|母线|避雷器|兆欧表|GIS|气室|配电|输电|发电机|电抗器|电容器)/.test(s)) return 'equipment'
        return 'component'
      }
      const spheres = []
      for (let i = 0; i < N; i++) {
        const color = DIM_COLORS[classify(nodes[i]?.name)] ?? DIM_COLORS.default
        // 节点大小按 symbolSize 映射（数据 28~36 → 0.7~1.3 可视区间）
        const sym = nodes[i]?.symbolSize || 28
        const size = Math.min(1.3, Math.max(0.7, sym / 28))
        const geo = new THREE.SphereGeometry(size, 18, 18)
        const mat = new THREE.MeshPhongMaterial({ color, emissive: color, emissiveIntensity: 0.45 })
        const mesh = new THREE.Mesh(geo, mat)
        mesh.position.set(positions[i].x, positions[i].y, positions[i].z)
        scene.add(mesh)
        spheres.push(mesh)
      }

      // 连线
      const lineMat = new THREE.LineBasicMaterial({ color: 0x555577, transparent: true, opacity: 0.4 })
      const highlightLineMat = new THREE.LineBasicMaterial({ color: 0xffd76a, transparent: true, opacity: 0.95 })
      const lineGroup = new THREE.Group()
      const linkPairs = []
      for (const l of links || []) {
        const si = nodes.findIndex(n => n.id === l.source || n.id === l.source?.id)
        const ti = nodes.findIndex(n => n.id === l.target || n.id === l.target?.id)
        if (si >= 0 && ti >= 0) {
          linkPairs.push([si, ti, l.value])
          const pts = [new THREE.Vector3(positions[si].x, positions[si].y, positions[si].z),
                       new THREE.Vector3(positions[ti].x, positions[ti].y, positions[ti].z)]
          const geo = new THREE.BufferGeometry().setFromPoints(pts)
          const line = new THREE.Line(geo, lineMat)
          lineGroup.add(line)
        }
      }
      scene.add(lineGroup)

      // 标签 Sprite（每个标签独立 canvas，避免共用 ctx 导致 CanvasTexture 串台成同一文字）
      const labels = []
      for (let i = 0; i < Math.min(N, 50); i++) {
        const label = (nodes[i]?.name || nodes[i]?.label || '')?.slice(0, 8)
        if (!label) continue
        const lc = document.createElement('canvas')
        lc.width = 128; lc.height = 64
        const ctx = lc.getContext('2d')
        ctx.fillStyle = '#ffffff'; ctx.font = '14px sans-serif'; ctx.textAlign = 'center'
        ctx.fillText(label, 64, 36)
        const tex = new THREE.CanvasTexture(lc)
        const mat = new THREE.SpriteMaterial({ map: tex, transparent: true, opacity: 0.8 })
        const sprite = new THREE.Sprite(mat)
        sprite.position.set(positions[i].x, positions[i].y + 1.0, positions[i].z)
        sprite.scale.set(3, 1.5, 1)
        scene.add(sprite)
        labels.push(sprite)
      }

      // 简单力导向模拟
      let iter = 0
      const L0 = 3, REPULSE = 18, SPRING_K = 0.06, GRAVITY = 0.012, BOUND = N > 120 ? 24 : 14
      function simulate() {
        if (iter++ > 120) return
        const damp = Math.max(0.35, 0.88 - iter * 0.004)   // 降温收敛
        for (let i = 0; i < N; i++) {
          if (i === dragIdx) continue
          let fx = 0, fy = 0, fz = 0
          // 电荷斥力（带 cutoff：距离>12 忽略，省算力 + 防节点四散）
          for (let j = 0; j < N; j++) {
            if (i === j) continue
            const dx = positions[i].x - positions[j].x
            const dy = positions[i].y - positions[j].y
            const dz = positions[i].z - positions[j].z
            const d2 = dx * dx + dy * dy + dz * dz
            if (d2 > 144) continue
            const d = Math.sqrt(d2) + 0.1
            const f = REPULSE / (d * d)
            fx += f * dx / d; fy += f * dy / d; fz += f * dz / d
          }
          // 弹簧力（带平衡长度 L0：边长趋近 L0=4，而非越拉越长）
          for (const [si, ti] of linkPairs) {
            if (si === i || ti === i) {
              const j = si === i ? ti : si
              const dx = positions[j].x - positions[i].x
              const dy = positions[j].y - positions[i].y
              const dz = positions[j].z - positions[i].z
              const d = Math.sqrt(dx * dx + dy * dy + dz * dz) + 0.1
              const f = (d - L0) * SPRING_K
              fx += f * dx / d; fy += f * dy / d; fz += f * dz / d
            }
          }
          // 向心力（拉回原点，防飞出视野）
          fx -= positions[i].x * GRAVITY
          fy -= positions[i].y * GRAVITY
          fz -= positions[i].z * GRAVITY
          positions[i].vx = (positions[i].vx + fx) * damp
          positions[i].vy = (positions[i].vy + fy) * damp
          positions[i].vz = (positions[i].vz + fz) * damp
          // 位置边界约束（防飞出相机视野）
          positions[i].x = Math.max(-BOUND, Math.min(BOUND, positions[i].x + positions[i].vx))
          positions[i].y = Math.max(-BOUND, Math.min(BOUND, positions[i].y + positions[i].vy))
          positions[i].z = Math.max(-BOUND, Math.min(BOUND, positions[i].z + positions[i].vz))
        }
        syncRender()
        if (iter < 100) setTimeout(simulate, 30)
      }
      simulate()

      // 节点拖动（raycaster 命中 → 投影平面跟随鼠标 → syncRender 实时刷新）
      const dom = renderer.domElement
      const setMouse = (e) => {
        const r = dom.getBoundingClientRect()
        ndcMouse.x = ((e.clientX - r.left) / r.width) * 2 - 1
        ndcMouse.y = -((e.clientY - r.top) / r.height) * 2 + 1
      }
      // 点击 vs 拖动：按下记录位置，抬起时位移 < 5px 视为点击选中
      let downAt = null
      const onDown = (e) => {
        downAt = { x: e.clientX, y: e.clientY }
        setMouse(e)
        raycaster.setFromCamera(ndcMouse, camera)
        const hits = raycaster.intersectObjects(spheres)
        if (hits.length) {
          dragIdx = spheres.indexOf(hits[0].object)
          const camDir = new THREE.Vector3()
          camera.getWorldDirection(camDir)
          dragPlane.setFromNormalAndCoplanarPoint(camDir, hits[0].point)
          dom.style.cursor = 'grabbing'
          e.preventDefault()
        }
      }
      const onMove = (e) => {
        setMouse(e)
        if (dragIdx >= 0) {
          raycaster.setFromCamera(ndcMouse, camera)
          if (raycaster.ray.intersectPlane(dragPlane, dragHit)) {
            positions[dragIdx].x = Math.max(-BOUND, Math.min(BOUND, dragHit.x))
            positions[dragIdx].y = Math.max(-BOUND, Math.min(BOUND, dragHit.y))
            positions[dragIdx].z = Math.max(-BOUND, Math.min(BOUND, dragHit.z))
            positions[dragIdx].vx = 0; positions[dragIdx].vy = 0; positions[dragIdx].vz = 0
            syncRender()
          }
        } else {
          // hover：命中节点 → 显示名称 tooltip + grab 光标
          raycaster.setFromCamera(ndcMouse, camera)
          const hovered = raycaster.intersectObjects(spheres)
          dom.style.cursor = hovered.length ? 'grab' : 'default'
          const rc = containerRef.value?.getBoundingClientRect()
          if (hovered.length && rc) {
            const hi = spheres.indexOf(hovered[0].object)
            tooltip.value = { x: e.clientX - rc.left + 12, y: e.clientY - rc.top + 12, text: nodes[hi]?.name || '' }
          } else {
            tooltip.value = null
          }
        }
      }
      const onUp = (e) => {
        if (downAt && Math.abs(e.clientX - downAt.x) + Math.abs(e.clientY - downAt.y) < 5) {
          setMouse(e)
          raycaster.setFromCamera(ndcMouse, camera)
          const hits = raycaster.intersectObjects(spheres)
          if (hits.length) selectNode(spheres.indexOf(hits[0].object))
          else { selected.value = null; clearHighlight() }
        }
        downAt = null
        dragIdx = -1
        dom.style.cursor = 'default'
      }
      dom.addEventListener('pointerdown', onDown)
      dom.addEventListener('pointermove', onMove)
      dom.addEventListener('pointerup', onUp)
      dragCleanup = () => { dom.removeEventListener('pointerdown', onDown); dom.removeEventListener('pointermove', onMove); dom.removeEventListener('pointerup', onUp) }

      // ===== 节点选中 / 邻接高亮 =====
      let selectedIdx = -1
      function selectNode(i) {
        selectedIdx = i
        const n = nodes[i] || {}
        const nbrSet = new Set()
        const rels = []
        for (const [si, ti, rel] of linkPairs) {
          if (si === i) { nbrSet.add(ti); if (rel) rels.push(`→ ${nodes[ti]?.name || ''} (${rel})`) }
          else if (ti === i) { nbrSet.add(si); if (rel) rels.push(`← ${nodes[si]?.name || ''} (${rel})`) }
        }
        selected.value = {
          name: n.name, label: n.name, type: n.type,
          outDegree: n.outDegree || 0,
          dimension: dimName(classify(n.name)),
          neighborCount: nbrSet.size,
          neighborNames: [...nbrSet].map(j => nodes[j]?.name).filter(Boolean),
          rels,
        }
        applyHighlight(i, nbrSet)
      }
      function clearHighlight() { selectedIdx = -1; applyHighlight(-1, new Set()) }
      function applyHighlight(sel, nbrSet) {
        for (let i = 0; i < N; i++) {
          const mat = spheres[i]?.material
          if (!mat) continue
          mat.transparent = true
          if (sel >= 0 && i !== sel && !nbrSet.has(i)) {
            mat.emissiveIntensity = 0.1; mat.opacity = 0.28
          } else {
            mat.emissiveIntensity = 0.45; mat.opacity = 1
          }
        }
        syncRender()
      }
      _clearSel = () => { clearHighlight(); selected.value = null }

      // 旋转动画：无选中→绕原点转；有选中→绕该节点转（平滑过渡）
      let phase = 1, phaseAngle = 0
      const RADIUS = 30, Y0 = 22, SPIN_SPEED = 0.0025
      const TEN_LOOPS = Math.PI * 20
      let focusTarget = { x: 0, y: 0, z: 0 }   // 当前相机注视点（平滑过渡用）
      function animate() {
        if (dragIdx < 0) {
          phaseAngle += SPIN_SPEED
          if (phaseAngle >= TEN_LOOPS) { phaseAngle = 0; phase = phase === 1 ? 2 : 1 }
          // 选中节点时，相机绕该节点旋转（半径按节点大小缩放）
          const tx = selectedIdx >= 0 ? positions[selectedIdx].x : 0
          const ty = selectedIdx >= 0 ? positions[selectedIdx].y : 0
          const tz = selectedIdx >= 0 ? positions[selectedIdx].z : 0
          // 平滑过渡注视点（lerp，避免跳变）
          focusTarget.x += (tx - focusTarget.x) * 0.06
          focusTarget.y += (ty - focusTarget.y) * 0.06
          focusTarget.z += (tz - focusTarget.z) * 0.06
          const r = selectedIdx >= 0 ? 10 : RADIUS
          const y0 = selectedIdx >= 0 ? ty + 8 : Y0
          if (phase === 1) {
            camera.position.set(focusTarget.x + r * Math.cos(phaseAngle), y0, focusTarget.z + r * Math.sin(phaseAngle))
          } else {
            camera.position.set(focusTarget.x, focusTarget.y + r * Math.sin(phaseAngle), focusTarget.z + r * Math.cos(phaseAngle))
          }
        }
        camera.lookAt(focusTarget.x, focusTarget.y, focusTarget.z)
        renderer.render(scene, camera)
        animId = requestAnimationFrame(animate)
      }
      animate()
  } catch (e) { console.error('3D render error:', e) }
}

function toggle3D() { is3D.value = !is3D.value; if (is3D.value) { nextTick(() => loadGraph()) } }

function cleanup() {
  if (animId) { cancelAnimationFrame(animId); animId = null }
  if (dragCleanup) { dragCleanup(); dragCleanup = null }
}

onMounted(() => { loadGraph(); document.addEventListener('fullscreenchange', onFsChange) })
onUnmounted(() => { cleanup(); document.removeEventListener('fullscreenchange', onFsChange) })
</script>

<style scoped>
.graph-container { position: relative; background: var(--surface-2); border-radius: var(--radius); min-height: 500px; overflow: hidden; }
.graph-container canvas { display: block; width: 100%; height: 100%; }
.graph-hint { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--text-muted); font-size: 14px; }
.graph-info { position: absolute; top: 50px; right: 10px; display: flex; gap: 6px; }
.graph-tools { position: absolute; top: 10px; right: 10px; z-index: 10; display: flex; gap: 6px; }
.graph-tools .fs-btn { width: 34px; height: 34px; border-radius: var(--radius-sm); background: rgba(0,0,0,.55); color: #fff; border: 1px solid rgba(255,255,255,.25); cursor: pointer; font-size: 14px; display: flex; align-items: center; justify-content: center; }
.graph-tools .fs-btn:hover { background: rgba(0,0,0,.75); }
.graph-legend { position: absolute; left: 10px; bottom: 10px; display: flex; flex-wrap: wrap; gap: 6px 12px; padding: 8px 10px; background: rgba(0,0,0,.5); border: 1px solid rgba(255,255,255,.15); border-radius: var(--radius-sm); color: #e6e6e6; font-size: 12px; max-width: 60%; }
.graph-legend span { display: inline-flex; align-items: center; gap: 4px; }
.graph-legend .dot { display: inline-block; width: 10px; height: 10px; border-radius: 50%; }
/* 悬浮 tooltip（跟鼠标走） */
.graph-tip { position: absolute; z-index: 20; background: rgba(0,0,0,.82); color: #fff; padding: 4px 10px; border-radius: 6px; font-size: 13px; pointer-events: none; white-space: nowrap; backdrop-filter: blur(4px); }
/* 关系标签行 */
.rel-line { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.rel-chip { display: inline-block; background: var(--surface-2); border: 1px solid var(--border); border-radius: 10px; padding: 2px 10px; font-size: 12px; color: var(--text-soft); max-width: 220px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>