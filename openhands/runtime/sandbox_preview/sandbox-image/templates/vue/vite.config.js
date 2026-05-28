import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Meoo Vue 脚手架 - Vite 配置
// 关键约束（与 oneday-api BuildService 对齐）：
// A. dev server 必须监听 3015（沙箱唯一代理端口）
// B. 产物 OSS 结构与 React+Webpack 一致：dist/index.html + assets/ + 相对路径引用
// C. 必须显式配置 @ -> src 别名（Vite 不会自动读取 tsconfig.paths）
const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  optimizeDeps: {
    include: ['vue', 'vue-router', 'pinia'],
  },
  server: {
    host: '0.0.0.0',
    port: 3015,        // 约束 A：沙箱唯一代理端口
    strictPort: true,  // 端口被占直接失败，避免漂移到 3016
    allowedHosts: true,
    // HMR 默认关闭：沙箱预览 iframe 下 HMR 的整页 reload 会放大任何 transform error
    // 为了预览稳定优先，开发时依靠手动刷新。如需开启热更，改为:
    //   hmr: { clientPort: 443, protocol: 'wss' }
    hmr: false,
  },
  base: './',          // 约束 B：相对路径引用，配合消费端 HomeController.replaceAssetUrls
  build: {
    outDir: 'dist',
    assetsDir: 'assets',
    emptyOutDir: true,
    // 方案 A：<=1MB 的图片/图标/字体内联为 dataURL
    // 消费端 HomeController 只替换 link[href].css 与 script[src].js，
    // 非 js/css 资源走内联即可零改动预览
    assetsInlineLimit: 1024 * 1024,
    rollupOptions: {
      output: {
        entryFileNames: 'assets/[name]-[hash].js',
        chunkFileNames: 'assets/[name]-[hash].js',
        assetFileNames: 'assets/[name]-[hash][extname]',
      },
    },
  },
});
