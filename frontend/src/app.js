/* 校准帧重建包复核台前端（无框架、无构建依赖）。 */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  summary: null,
  editing: null,          // 正在编辑的包名
  baseRevision: 0,        // 读取到的修订号（乐观并发依据）
  kept: new Set(),        // 保留的块摘要
  dropped: new Set(),     // 本次撤下的块摘要
  additions: new Map(),   // 摘要 -> 文本（新增块）
};

async function http(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(url, opts);
  let data = null;
  try { data = await res.json(); } catch (_) { /* 非 JSON */ }
  return { ok: res.ok, status: res.status, data };
}

async function sha256Hex(text) {
  // 安全上下文（https 或 localhost）可用浏览器原生哈希；否则回退服务端。
  if (window.crypto && crypto.subtle) {
    const bytes = new TextEncoder().encode(text);
    const buf = await crypto.subtle.digest("SHA-256", bytes);
    return Array.from(new Uint8Array(buf))
      .map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  const { data } = await http("POST", "/api/digest", { text });
  return data.digest;
}

// ------------------------------------------------------------ 健康

async function refreshHealth() {
  const el = $("health");
  try {
    const { ok, data } = await http("GET", "/health");
    if (ok && data.objects_writable) {
      el.textContent = `健康：对象目录可写 · 包 ${data.storage.packages} · ` +
        `被引用块 ${data.storage.referenced_blocks} · 盘上对象 ${data.storage.objects_on_disk}`;
      el.className = "health ok";
    } else {
      el.textContent = `异常：对象目录不可写（${JSON.stringify(data)}）`;
      el.className = "health bad";
    }
  } catch (err) {
    el.textContent = "健康检查失败：" + err;
    el.className = "health bad";
  }
}

// ------------------------------------------------------------ 摘要列表

async function refreshSummary() {
  const { data } = await http("GET", "/api/summary");
  state.summary = data;
  const tbody = document.querySelector("#package-table tbody");
  tbody.innerHTML = "";
  for (const pkg of data.packages) {
    const tr = document.createElement("tr");
    const shared = pkg.blocks.filter((d) => pkg.reference_counts[d] > 1).length;
    tr.innerHTML =
      `<td class="mono"></td>` +
      `<td>${pkg.revision}</td>` +
      `<td>${pkg.block_count}` +
      (shared ? ` <span class="badge shared">${shared} 个共享块</span>` : "") +
      `</td>` +
      `<td><button>编辑目录</button></td>`;
    tr.querySelector("td.mono").textContent = pkg.package;
    tr.querySelector("button").addEventListener("click", () => openEditor(pkg.package));
    tbody.appendChild(tr);
  }
}

// ------------------------------------------------------------ 目录编辑

async function openEditor(name) {
  let data;
  const res = await http("GET", "/api/packages/" + encodeURIComponent(name));
  if (res.ok) {
    data = res.data;
  } else if (res.status === 404) {
    data = { revision: 0, blocks: [] };  // 尚未发布的新包
  } else {
    alert("读取包目录失败：" + JSON.stringify(res.data));
    return;
  }
  state.editing = name;
  state.baseRevision = data.revision;
  state.kept = new Set(data.blocks);
  state.dropped = new Set();
  state.additions = new Map();
  $("editor-panel").hidden = false;
  $("inspector-panel").hidden = true;
  $("editor-package").textContent = name;
  $("commit-result").textContent = "";
  $("conflict-banner").hidden = true;
  $("new-block-text").value = "";
  $("new-block-digest").textContent = "";
  $("commit-reason").value = "";
  renderBlockList();
}

function refCountOf(digest) {
  const pkg = state.summary.packages.find((p) => p.package === state.editing);
  return pkg ? (pkg.reference_counts[digest] || 0) : 0;
}

function renderBlockList() {
  $("editor-revision").textContent = state.baseRevision;
  const ul = $("block-list");
  ul.innerHTML = "";

  const renderRow = (digest, cls, label) => {
    const li = document.createElement("li");
    const count = refCountOf(digest);
    const shared = count > 1;
    li.innerHTML =
      `<span class="badge ${cls}">${label}</span>` +
      `<span class="badge ${shared ? "shared" : "sole"}">引用 ×${count}</span>` +
      `<span class="digest mono"></span>`;
    li.querySelector(".digest").textContent = digest;

    const inspect = document.createElement("button");
    inspect.textContent = "读取块";
    inspect.addEventListener("click", () => inspectBlock(digest));
    li.appendChild(inspect);

    if (state.kept.has(digest) || state.additions.has(digest)) {
      const toggle = document.createElement("button");
      toggle.textContent = "撤下";
      toggle.className = "warn";
      toggle.addEventListener("click", () => {
        if (state.additions.has(digest)) {
          state.additions.delete(digest);
        } else {
          state.kept.delete(digest);
          state.dropped.add(digest);
        }
        renderBlockList();
      });
      li.appendChild(toggle);
    } else if (state.dropped.has(digest)) {
      const undo = document.createElement("button");
      undo.textContent = "恢复保留";
      undo.addEventListener("click", () => {
        state.dropped.delete(digest);
        state.kept.add(digest);
        renderBlockList();
      });
      li.appendChild(undo);
    }
    ul.appendChild(li);
  };

  // 既有块 = 保留 ∪ 已标记撤下（撤下项仍展示，可恢复）
  const existing = new Set([...state.kept, ...state.dropped]);
  for (const d of [...existing].sort()) {
    if (state.dropped.has(d)) {
      renderRow(d, "drop", "待撤下");
    } else {
      renderRow(d, "keep", "保留");
    }
  }
  // 新增块（且不属于既有目录）单独标注
  for (const d of state.additions.keys()) {
    if (!existing.has(d)) renderRow(d, "keep", "新增");
  }
}

$("new-block-text").addEventListener("input", async () => {
  const text = $("new-block-text").value;
  $("new-block-digest").textContent =
    text.length ? "SHA-256：" + (await sha256Hex(text)) : "";
});

$("btn-stage-add").addEventListener("click", async () => {
  const text = $("new-block-text").value;
  if (!text.length) return;
  const digest = await sha256Hex(text);
  state.dropped.delete(digest);
  state.kept.add(digest);
  // 目录里已存在的块无需随提交重复发送文本；仅全新块记入新增映射。
  if (!state.summary.packages.some(
      (p) => p.package === state.editing && p.blocks.includes(digest))) {
    state.additions.set(digest, text);
  }
  $("new-block-text").value = "";
  $("new-block-digest").textContent = "";
  renderBlockList();
});

$("btn-reload").addEventListener("click", () => openEditor(state.editing));

$("btn-commit").addEventListener("click", async () => {
  const banner = $("conflict-banner");
  banner.hidden = true;
  // 提交集合 = 保留 ∪ 新增；新增块上传文本（服务端按 SHA-256 去重入库），
  // 其余保留块只提交摘要。
  const digests = new Set(state.kept);
  for (const d of state.additions.keys()) digests.add(d);
  const texts = [...state.additions.values()];
  const payload = {
    blocks: texts,
    block_digests: [...digests].filter((d) => !state.additions.has(d)),
    expected_revision: state.baseRevision,
    reason: $("commit-reason").value,
  };

  const { ok, status, data } = await http(
    "POST", "/api/packages/" + encodeURIComponent(state.editing) + "/commit", payload);

  if (ok) {
    $("commit-result").textContent =
      `已提交修订 ${data.revision}：新增 ${data.added.length}，撤下 ${data.removed.length}，保留 ${data.retained.length}`;
    await refreshSummary();
    await refreshHealth();
    await openEditor(state.editing);
  } else if (status === 409 && data && data.error === "stale_revision") {
    banner.textContent =
      `修订冲突：你基于修订 ${state.baseRevision}，当前已为 ${data.current_revision}。` +
      "本次提交被拒绝，既有目录、引用数与对象均未改变。请重载后复核再提交。";
    banner.hidden = false;
  } else {
    banner.textContent = "提交失败：" + JSON.stringify(data);
    banner.hidden = false;
  }
});

// ------------------------------------------------------------ 块检视

async function inspectBlock(digest) {
  const panel = $("inspector-panel");
  const body = $("inspector-body");
  panel.hidden = false;
  body.innerHTML = "<p class='hint'>读取中…</p>";
  const { ok, data } = await http("GET", "/api/blocks/" + digest);
  if (!ok) {
    body.innerHTML =
      `<p><b class="mono"></b></p>
       <p class="banner conflict">该块无法读取：最后引用撤下后已被回收（或从未发布）。
       目录与引用统计中也不再保留它。</p>`;
    body.querySelector(".mono").textContent = digest;
    return;
  }
  body.innerHTML =
    `<p class="mono"></p>
     <p>引用数：<b id="insp-ref"></b> · 引用方：<span id="insp-by" class="pill"></span></p>
     <p id="insp-reason" class="hint"></p>
     <pre id="insp-content"></pre>`;
  body.querySelector(".mono").textContent = data.digest;
  $("insp-ref").textContent = data.ref_count;
  $("insp-by").textContent = data.referenced_by.join("、") || "（无）";
  $("insp-reason").textContent = "保留缘由：" + data.retention_reason;
  $("insp-content").textContent = data.content;
}

$("btn-close-inspector").addEventListener("click", () => {
  $("inspector-panel").hidden = true;
});

// ------------------------------------------------------------ 顶部按钮

$("btn-new-package").addEventListener("click", () => {
  const name = $("new-package-name").value.trim();
  if (name) openEditor(name);
});

$("btn-refresh").addEventListener("click", async () => {
  await refreshSummary();
  await refreshHealth();
  if (state.editing) await openEditor(state.editing);
});

$("btn-gc").addEventListener("click", async () => {
  const { data } = await http("POST", "/api/gc");
  await refreshSummary();
  await refreshHealth();
  alert(`回收完成：候选标记 ${data.marked.length} 个，移除文件 ${data.removed.length} 个。`);
});

// ------------------------------------------------------------ 启动

(async function main() {
  await refreshHealth();
  await refreshSummary();
})();
