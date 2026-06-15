import path from 'node:path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    fs: {
      allow: [path.resolve(__dirname, '..')],
    },
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/bot-api': {
        target: 'http://localhost:7878',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/bot-api/, ''),
      },
      '/firewall-ws': {
        target: 'ws://localhost:6789',
        changeOrigin: true,
        ws: true,
      },
    },
  },
  preview: {
    port: 4173,
  },
});
