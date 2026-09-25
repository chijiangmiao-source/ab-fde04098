#!/usr/bin/env node
// 前端构建（零第三方依赖）：
//  1. 用 node --check 对 app.js 做语法校验（等价于前端“构建”门禁）；
//  2. 将 index.html / app.js / styles.css 打包到 backend/static，供后端直接托管。
import { spawnSync } from "node:child_process";
import { mkdirSync, copyFileSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const src = join(root, "frontend", "src");
const out = join(root, "backend", "static");

const checked = spawnSync(process.execPath, ["--check", join(src, "app.js")],
  { encoding: "utf-8" });
if (checked.status !== 0) {
  process.stderr.write(checked.stderr || "app.js 语法校验失败\n");
  process.exit(1);
}

// index.html 中的 /static/app.js 路径在托管时保持不变；静态文件平铺输出。
mkdirSync(out, { recursive: true });
for (const file of ["index.html", "app.js", "styles.css"]) {
  const body = readFileSync(join(src, file));
  copyFileSync(join(src, file), join(out, file));
  process.stdout.write(`[build-frontend] ${file} -> backend/static/${file}\n`);
}

// 再对输出产物做一次校验，确保发布物本身可解析。
const recheck = spawnSync(process.execPath, ["--check", join(out, "app.js")],
  { encoding: "utf-8" });
if (recheck.status !== 0) {
  process.stderr.write(recheck.stderr || "产物 app.js 校验失败\n");
  process.exit(1);
}

// 简单完整性断言：HTML 引用的资源都已产出。
const html = readFileSync(join(out, "index.html"), "utf-8");
for (const ref of ["/static/app.js", "/static/styles.css"]) {
  if (!html.includes(ref)) {
    process.stderr.write(`index.html 缺少资源引用 ${ref}\n`);
    process.exit(1);
  }
}
writeFileSync(join(out, "BUILD_OK"), `built at ${new Date().toISOString()}\n`);
process.stdout.write("[build-frontend] OK\n");
