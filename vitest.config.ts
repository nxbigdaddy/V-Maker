import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

/**
 * 前端单元测试配置。
 *
 * 与 vite.config.ts 分开，避免把测试相关的设置带进生产构建。
 * 这里同样定义 __VMAKER_BUILD_TIME__，因为被测组件会读取它。
 */
export default defineConfig({
  plugins: [react()],
  define: {
    __VMAKER_BUILD_TIME__: JSON.stringify('2026-01-01T00:00:00.000Z'),
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.{ts,tsx}'],
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
    unstubGlobals: true,
  },
})
