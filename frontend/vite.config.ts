import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { createHash } from 'node:crypto'
import { realpathSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

function projectIdentity(): Plugin {
  const projectPath = realpathSync(fileURLToPath(new URL('..', import.meta.url)))
    .replace(/\\/g, '/').replace(/\/+$/, '')
  const normalized = process.platform === 'win32' ? projectPath.toLowerCase() : projectPath
  const projectId = createHash('sha256').update(normalized, 'utf8').digest('hex')
  const apiTarget = (process.env.DOUYINMIND_API_TARGET || 'http://127.0.0.1:8000').replace(/\/+$/, '')
  const apiTargetId = createHash('sha256').update(apiTarget, 'utf8').digest('hex')
  return {
    name: 'douyinmind-project-identity',
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        if (request.url?.split('?')[0] !== '/__douyinmind_identity') return next()
        if (request.method !== 'GET') {
          response.statusCode = 405
          response.end()
          return
        }
        response.setHeader('Content-Type', 'application/json; charset=utf-8')
        response.setHeader('Cache-Control', 'no-store')
        response.end(JSON.stringify({ service: 'DouyinMindFrontend', project_id: projectId, api_target_id: apiTargetId }))
      })
    },
  }
}

export default defineConfig({
  plugins: [react(), projectIdentity()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.DOUYINMIND_API_TARGET || 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
