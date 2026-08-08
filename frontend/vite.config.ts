import react from "@vitejs/plugin-react-swc";
import path from "path";
import { defineConfig } from "vite";

// The port is load-bearing: the backend's CORS allow-list names 3000.
export default defineConfig({
  server: {
    host: "127.0.0.1",
    port: 3000,
    hmr: { overlay: false },
  },
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  // One of Plotly's transitive dependencies reaches for Node's `global`. The
  // production build shims it and works; esbuild's dev pre-bundle does not, so
  // /graphs threw `global is not defined` and React unmounted the whole tree —
  // a blank page in dev and a correct one in the build, which is a miserable
  // thing to debug. Nothing in this app's own source uses `global`.
  define: { global: "globalThis" },
  optimizeDeps: {
    esbuildOptions: { define: { global: "globalThis" } },
    // Vite crawls static imports at startup to decide what to pre-bundle, and
    // it cannot see into a lazily imported route. Left to discover these on
    // the first request to /graphs it optimises them one at a time, stamping a
    // different `?v=` on each and invalidating the ones already sent — the
    // page then reload-loops and renders blank. Naming them here pre-bundles
    // all five in the startup pass.
    include: [
      "plotly.js/lib/core",
      "plotly.js/lib/bar",
      "plotly.js/lib/box",
      "plotly.js/lib/candlestick",
      "plotly.js/lib/heatmap",
    ],
  },
  build: {
    // Every browser that supports the APIs this app uses (IntersectionObserver,
    // dynamic import) is well past es2020, so don't ship transpiler output for
    // engines that will never load it.
    target: "es2020",
    cssMinify: true,
    // One page, one route: the only worthwhile split is the watchlist editor,
    // which React.lazy already carves out. Manual vendor chunks would just add
    // a second blocking request for code the first paint needs anyway.
    reportCompressedSize: true,
  },
});
