import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiPort = process.env.API_PORT;
if (!apiPort) {
  throw new Error("API_PORT is not set. Start the UI with `make dev` or `make ui`.");
}

export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": `http://127.0.0.1:${apiPort}` } },
});
