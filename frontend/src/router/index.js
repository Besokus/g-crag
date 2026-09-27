import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'
import AppLayout from '../views/AppLayout.vue'

const routes = [
  { path: '/login', component: () => import('../views/Login.vue') },
  {
    path: '/',
    component: AppLayout,
    children: [
      { path: '', redirect: '/chat' },
      { path: 'chat', component: () => import('../views/Chat.vue'), meta: { auth: true, title: '智能问答', sub: '自然语言提问 · 自纠错 · 可信答案' } },
      { path: 'documents', component: () => import('../views/Documents.vue'), meta: { auth: true, title: '知识库', sub: '上传 · 解析 · 向量化 · 预览' } },
      { path: 'kg', component: () => import('../views/KgGraph.vue'), meta: { auth: true, title: '知识图谱', sub: '实体关系 · 多跳推理' } },
      { path: 'admin', component: () => import('../views/Admin.vue'), meta: { auth: true, roles: ['admin', 'auditor'], title: '系统管理', sub: '反馈 · 日志 · 配置' } },
      { path: 'retrieval-debug', component: () => import('../views/RetrievalDebug.vue'), meta: { auth: true, admin: true, title: '检索调试', sub: '全链路 trace · 命中归因' } },
      { path: 'trace', component: () => import('../views/TraceDiagnosis.vue'), meta: { auth: true, roles: ['admin', 'auditor'], title: '链路诊断', sub: '问答耗时 · 节点瓶颈 · 历史追溯' } },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.auth && !auth.token) return '/login'
  if (to.meta.admin && auth.role !== 'admin') return '/chat'
  // 细粒度角色白名单（如 /admin 允许 admin+auditor）
  if (to.meta.roles && !to.meta.roles.includes(auth.role)) return '/chat'
})

export default router
