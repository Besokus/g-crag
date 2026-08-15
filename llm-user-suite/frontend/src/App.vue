<template>
  <div v-if="!token" class="login">
    <form class="panel" @submit.prevent="login">
      <h1>LLM as a User</h1><p>Grid-QA 旁路评测控制台</p>
      <input v-model="username" placeholder="用户名" autocomplete="username">
      <input v-model="password" type="password" placeholder="密码" autocomplete="current-password">
      <button>登录</button><div class="error">{{ error }}</div>
    </form>
  </div>
  <main v-else>
    <header><div><h1>LLM as a User</h1><span>行为驱动评测闭环</span></div><button class="ghost" @click="logout">退出</button></header>
    <nav><button v-for="item in tabs" :key="item.id" :class="{active:tab===item.id}" @click="tab=item.id;load()">{{ item.name }}</button></nav>
    <section v-if="tab==='scenarios'">
      <div class="title"><h2>用户行为剧本</h2><button @click="load">刷新</button></div>
      <div class="cards"><article v-for="s in scenarios" :key="s.id" class="card">
        <div><b>{{ s.name }}</b><span class="badge" :class="s.status">{{ s.status }}</span></div>
        <p>版本 {{ s.currentVersion }} · {{ s.signature?.slice(0,12) }}</p>
        <div class="actions"><button @click="openScenario(s.id)">查看</button><button v-if="isAdmin&&s.status==='draft'" @click="review(s.id,'approve')">审核通过</button><button v-if="isAdmin&&s.status==='active'" @click="runScenario(s.id)">执行回放</button></div>
      </article></div>
    </section>
    <section v-if="tab==='runs'">
      <div class="title"><h2>回放运行</h2><button @click="load">刷新</button></div>
      <table><thead><tr><th>Run</th><th>状态</th><th>分数</th><th>结论</th><th>根因</th><th></th></tr></thead>
      <tbody><tr v-for="r in runs" :key="r.id" @click="openRun(r.id)"><td>{{ r.id.slice(0,12) }}</td><td>{{ r.status }}</td><td>{{ fmt(r.score) }}</td><td>{{ r.verdict }}</td><td>{{ r.rootCause }}</td><td><button v-if="isAdmin&&['queued','running','evaluating'].includes(r.status)" @click.stop="stopRun(r.id)">取消</button></td></tr></tbody></table>
    </section>
    <section v-if="tab==='reports'">
      <div class="title"><h2>优化报告</h2><button @click="load">刷新</button></div>
      <div class="cards"><article v-for="r in reports" :key="r.id" class="card" @click="openReport(r.id)"><b>{{ r.verdict }} · {{ r.runId.slice(0,12) }}</b><p>{{ r.summary }}</p><small>回调：{{ r.callbackStatus }}</small></article></div>
    </section>
    <section v-if="tab==='sessions'">
      <div class="title"><h2>脱敏用户会话</h2><button @click="load">刷新</button></div>
      <table><thead><tr><th>会话</th><th>租户</th><th>事件</th><th>差评</th><th>重试</th><th>降级</th></tr></thead>
      <tbody><tr v-for="s in sessions" :key="s.id"><td>{{ s.id.slice(0,12) }}</td><td>{{ s.tenantId }}</td><td>{{ s.eventCount }}</td><td>{{ s.hasDislike?'是':'否' }}</td><td>{{ s.retryCount }}</td><td>{{ s.hasDegradation?'是':'否' }}</td></tr></tbody></table>
    </section>
    <section v-if="tab==='retests'">
      <div class="title"><h2>自进化复测</h2><button @click="load">刷新</button></div>
      <table><thead><tr><th>草稿</th><th>状态</th><th>回流前</th><th>回流后</th><th>Lift</th></tr></thead>
      <tbody><tr v-for="r in retests" :key="r.id" @click="openRun(r.runId)"><td>{{ r.draftId.slice(0,12) }}</td><td>{{ r.status }}</td><td>{{ fmt(r.beforeScore) }}</td><td>{{ fmt(r.afterScore) }}</td><td>{{ fmt(r.lift) }}</td></tr></tbody></table>
    </section>
    <section v-if="tab==='metrics'">
      <div class="title"><h2>分钟聚合指标</h2><button @click="load">刷新</button></div>
      <table><thead><tr><th>指标</th><th>分钟</th><th>标签</th><th>Last</th><th>样本</th></tr></thead>
      <tbody><tr v-for="m in metricRows" :key="`${m.name}-${m.minute}-${JSON.stringify(m.labels)}`"><td>{{ m.name }}</td><td>{{ m.minute }}</td><td>{{ JSON.stringify(m.labels) }}</td><td>{{ fmt(m.last) }}</td><td>{{ m.samples }}</td></tr></tbody></table>
    </section>
    <aside v-if="detail" class="drawer"><button class="close" @click="detail=null">×</button><pre>{{ JSON.stringify(detail,null,2) }}</pre></aside>
  </main>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import axios from 'axios'
axios.defaults.baseURL='/v1'
const token=ref(localStorage.getItem('llm-user-token')||''),username=ref(''),password=ref(''),error=ref('')
const role=ref(localStorage.getItem('llm-user-role')||'auditor'),isAdmin=computed(()=>role.value==='admin')
const tab=ref('scenarios'),scenarios=ref([]),runs=ref([]),reports=ref([]),sessions=ref([]),retests=ref([]),metricRows=ref([]),detail=ref(null)
const tabs=[{id:'scenarios',name:'剧本'},{id:'sessions',name:'会话'},{id:'runs',name:'回放'},{id:'retests',name:'复测'},{id:'reports',name:'报告'},{id:'metrics',name:'指标'}]
function bindToken(value){if(value)axios.defaults.headers.common.Authorization=`Bearer ${value}`;else delete axios.defaults.headers.common.Authorization}
bindToken(token.value)
async function login(){try{const {data}=await axios.post('/auth/login',{username:username.value,password:password.value});token.value=data.token;role.value=data.role||'auditor';localStorage.setItem('llm-user-token',data.token);localStorage.setItem('llm-user-role',role.value);bindToken(data.token);await load()}catch(e){error.value=e.response?.data?.detail||'登录失败'}}
function logout(){localStorage.removeItem('llm-user-token');localStorage.removeItem('llm-user-role');token.value='';role.value='auditor';bindToken('')}
async function load(){if(!token.value)return;if(tab.value==='scenarios')scenarios.value=(await axios.get('/scenarios')).data;if(tab.value==='sessions')sessions.value=(await axios.get('/sessions')).data;if(tab.value==='runs')runs.value=(await axios.get('/runs')).data;if(tab.value==='retests')retests.value=(await axios.get('/evaluations/retests')).data;if(tab.value==='reports')reports.value=(await axios.get('/reports')).data;if(tab.value==='metrics')metricRows.value=(await axios.get('/telemetry/metrics')).data}
async function review(id,action){await axios.post(`/scenarios/${id}/review`,{action});await load()}
async function runScenario(id){const scenario=(await axios.get(`/scenarios/${id}`)).data;const version=scenario.versions.find(v=>['approved','active'].includes(v.status));if(!version){error.value='没有已审核版本';return}await axios.post('/runs',{scenarioVersionId:version.id});tab.value='runs';await load()}
async function stopRun(id){await axios.delete(`/runs/${id}`);await load()}
async function openScenario(id){detail.value=(await axios.get(`/scenarios/${id}`)).data}
async function openRun(id){detail.value=(await axios.get(`/runs/${id}`)).data}
async function openReport(id){detail.value=(await axios.get(`/reports/${id}`)).data}
function fmt(v){return typeof v==='number'?v.toFixed(3):'-'}
onMounted(()=>{if(token.value)load()})
</script>
