import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // A visible build stamp lets us tell a freshly loaded bundle from a stale cached page.
  define: {
    __VMAKER_BUILD_TIME__: JSON.stringify(new Date().toISOString()),
  },
  server: {
    host: '127.0.0.1',
    port: 1420,
    strictPort: true,
    watch: {
      // 编辑器的原子写入会在 src 下短暂建出 <name>.<pid>.<hash>.tmpdir 目录；
      // 在 Windows 上监视这些临时路径会抛 EBUSY，并让整个开发服务器直接退出。
      ignored: ['**/*.tmp', '**/*.tmpdir/**', '**/.*.tmpdir/**', '**/~*'],
    },
  },
})
