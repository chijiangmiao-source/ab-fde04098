// Deterministic frontend bundle build: src/main.ts -> static/app.js
import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, "..");

await build({
  entryPoints: [resolve(root, "src/main.ts")],
  outfile: resolve(root, "static/app.js"),
  bundle: true,
  minify: true,
  sourcemap: false,
  target: ["es2020"],
  logLevel: "info",
});
console.log("frontend build: static/app.js");
