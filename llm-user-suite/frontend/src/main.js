import { createApp } from 'vue'
import axios from 'axios'
import App from './App.vue'
import './style.css'

axios.defaults.baseURL = '/v1'
axios.interceptors.request.use((config) => {
  const token = localStorage.getItem('llm-user-token')
  if (token && token !== 'local-dev') config.headers.Authorization = `Bearer ${token}`
  return config
})

createApp(App).mount('#app')
