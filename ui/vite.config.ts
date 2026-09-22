import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";
export default defineConfig({
  base: "/dashboard/",
  plugins: [vue()],
  server: {
    proxy: {
      "/api": {
        target: process.env.RUNTIME_API_URL || "http://127.0.0.1:8000",
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
