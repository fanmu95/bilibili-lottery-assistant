import { createRouter, createWebHistory } from 'vue-router'
import Layout from '../layout/Layout.vue'

const routes = [
  {
    path: '/',
    component: Layout,
    redirect: '/accounts',
    children: [
      { path: 'accounts', name: 'accounts', component: () => import('../views/Accounts.vue'), meta: { title: '账号管理', icon: 'User' } },
      { path: 'discovery', name: 'discovery', component: () => import('../views/Discovery.vue'), meta: { title: '活动发现', icon: 'Search' } },
      { path: 'activities', name: 'activities', component: () => import('../views/Activities.vue'), meta: { title: '活动列表', icon: 'Trophy' } },
      { path: 'logs', name: 'logs', component: () => import('../views/Logs.vue'), meta: { title: '日志', icon: 'Document' } },
      { path: 'settings', name: 'settings', component: () => import('../views/Settings.vue'), meta: { title: '设置', icon: 'Setting' } },
    ],
  },
]

export default createRouter({
  history: createWebHistory(),
  routes,
})
