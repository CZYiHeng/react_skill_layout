import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发模式：Vite(5173) 把 /api 代理到 Python 后端(8000)，避免跨域与 CORS 配置。
// 生产模式：npm run build 产出 dist/，由 starlette 静态托管（见 react/webapi.py）。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
