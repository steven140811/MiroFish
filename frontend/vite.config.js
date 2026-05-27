import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const envDir = path.resolve(__dirname, '..')
  const env = loadEnv(mode, envDir, '')
  const frontendPort = Number(env.FRONTEND_PORT || 3000)
  const backendHost = env.FLASK_HOST && env.FLASK_HOST !== '0.0.0.0'
    ? env.FLASK_HOST
    : 'localhost'
  const backendPort = Number(env.FLASK_PORT || 5001)
  const apiBaseUrl = env.VITE_API_BASE_URL || `http://${backendHost}:${backendPort}`

  return {
    envDir,
    plugins: [vue()],
    resolve: {
      alias: {
        '@': path.resolve(__dirname, 'src'),
        '@locales': path.resolve(__dirname, '../locales')
      }
    },
    server: {
      port: frontendPort,
      open: true,
      proxy: {
        '/api': {
          target: apiBaseUrl,
          changeOrigin: true,
          secure: false
        }
      }
    }
  }
})
