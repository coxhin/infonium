import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const apiPort = process.env.API_PORT;
if (!apiPort) {
  throw new Error("API_PORT is not set. Start the UI with `make dev` or `make ui`.");
}

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: `http://127.0.0.1:${apiPort}`,
        // When the service ends in the middle of a response, the proxy would keep the page's
        // connection open and the page would wait for the rest forever; it is broken off
        // instead, so that the page sees the connection end as it does without the proxy.
        configure: (proxy) => {
          proxy.on("proxyRes", (proxyRes, _, response) => {
            proxyRes.on("close", () => {
              if (!proxyRes.complete) {
                response.destroy();
              }
            });
          });
        },
      },
    },
  },
});
