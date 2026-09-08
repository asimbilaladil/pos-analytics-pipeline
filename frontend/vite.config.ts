import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    host: true,
    // The API is same-origin in production behind nginx; in dev we proxy so
    // the session cookie is first-party here too and nothing needs CORS.
    // Target is env-driven so a preview instance can point at a separate
    // API port without editing this file.
    proxy: { '/api': { target: process.env.VITE_API_TARGET || 'http://127.0.0.1:8700',
                       changeOrigin: true } },
  },
  build: { outDir: 'dist', sourcemap: false },
})
