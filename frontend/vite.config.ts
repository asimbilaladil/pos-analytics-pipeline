import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5174,
    host: true,
    // The API is same-origin in production behind nginx; in dev we proxy so
    // the session cookie is first-party here too and nothing needs CORS.
    proxy: { '/api': { target: 'http://127.0.0.1:8700', changeOrigin: true } },
  },
  build: { outDir: 'dist', sourcemap: false },
})
