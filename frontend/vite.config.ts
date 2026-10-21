import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath } from "node:url";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  server: {
    host: "127.0.0.1",
    port: Number(process.env.LEARNING_APP_FRONTEND_PORT || 5173),
    strictPort: true,
    headers: { "X-Learning-Instance": process.env.LEARNING_APP_INSTANCE_ID || "standalone" },
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${process.env.LEARNING_APP_BACKEND_PORT || 8765}`,
        changeOrigin: false,
        ws: false,
      },
    },
  },
  test: { environment: "jsdom", setupFiles: ["./tests/setup.ts"] },
});
