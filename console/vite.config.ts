import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/console/",
  plugins: [react()],
  build: {
    sourcemap: false,
    rolldownOptions: {
      input: {
        legacy: new URL("index.html", import.meta.url).pathname,
        workspace: new URL("workspace/index.html", import.meta.url).pathname,
      },
    },
  },
  test: { environment: "node", restoreMocks: true },
});
