import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8017",
        changeOrigin: true,
        headers: { Origin: "http://localhost:8017" },
      },
    },
  },
  build: { sourcemap: false },
});
