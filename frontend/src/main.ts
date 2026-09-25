// Reviewer console for content-addressed calibration blocks.
// All edits are staged locally, then submitted as one numbered revision.

interface Change { op: "add" | "remove"; sha: string; name?: string }
interface Entry { sha: string; name: string; size: number; added_at?: string }
interface Package { name: string; entry_count: number; entries: Entry[] }
interface State {
  directories: { rev: number; directories: Package[] };
  stats: { rev: number; directories: number; objects: number; candidates: string[]; refcount: Record<string, number> };
  revisions: Array<{ rev: number; parent_rev: number; directory: string; note: string; summary: { added: number; removed: number }; timestamp: string }>;
}

const $ = <T extends HTMLElement = HTMLInputElement>(id: string) => document.getElementById(id) as T;

const staged = new Map<string, Change>();
let currentRev = 0;
let targetPkg = "";
let lastState: State | null = null;

async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) throw Object.assign(new Error(body.error || `HTTP ${res.status}`), { status: res.status, body });
  return body as T;
}

function short(sha: string) { return sha.slice(0, 12); }

function setResult(el: HTMLElement, msg: string, kind: "ok" | "err" | "" = "") {
  el.textContent = msg;
  el.className = `result ${kind}`;
}

// ---------------------------------------------------------------- staging
function stage(ch: Change) {
  staged.set(ch.sha, ch);
  renderStaged();
}
function unstage(sha: string) { staged.delete(sha); renderStaged(); }

function renderStaged() {
  const ul = $("staged-list");
  ul.innerHTML = "";
  if (staged.size === 0) {
    ul.innerHTML = '<li style="color:var(--muted)">暂无暂存改动</li>';
  }
  for (const ch of staged.values()) {
    const li = document.createElement("li");
    const label = ch.op === "add" ? `新增 ${ch.name ?? short(ch.sha)}` : `撤下 ${short(ch.sha)}`;
    li.innerHTML = `<span><span class="op-${ch.op}">${ch.op === "add" ? "＋" : "－"}</span> ${label} <code style="color:var(--muted)">${short(ch.sha)}…</code></span>`;
    const btn = document.createElement("button");
    btn.textContent = "撤销暂存";
    btn.onclick = () => unstage(ch.sha);
    li.appendChild(btn);
    ul.appendChild(li);
  }
  $("rev-badge").textContent = currentRev ? `当前修订 r${currentRev} → 提交后 r${currentRev + 1}` : "";
}

// --------------------------------------------------------------- rendering
function render(state: State) {
  lastState = state;
  currentRev = state.stats.rev;
  $("base-rev").value = String(currentRev);
  if (targetPkg && !state.directories.directories.some((p) => p.name === targetPkg)) {
    // keep the typed target even if the directory has no entries yet
  }

  const wrap = $("packages");
  wrap.innerHTML = "";
  if (state.directories.directories.length === 0) {
    wrap.innerHTML = '<p class="hint">还没有目录。上传块后在上方指定目录名并提交即可创建。</p>';
  }
  for (const pkg of state.directories.directories) {
    const card = document.createElement("div");
    card.className = "pkg" + (pkg.name === targetPkg ? " active" : "");
    const rows = pkg.entries.map((e) => `
      <tr>
        <td>${e.name}</td>
        <td class="sha" title="${e.sha}">${short(e.sha)}…</td>
        <td>${e.size} B</td>
        <td>引用 ×${state.stats.refcount[e.sha] ?? 0}</td>
        <td>
          <button class="mini" data-keep="${e.sha}">保留</button>
          <button class="mini rm" data-rm="${e.sha}">撤下</button>
          <button class="mini q" data-why="${e.sha}">查看缘由</button>
        </td>
      </tr>`).join("");
    card.innerHTML = `
      <header>
        <h3>📦 ${pkg.name} <span class="rev-badge">${pkg.entry_count} 块</span></h3>
        <div>
          <button class="mini target-btn" data-target="${pkg.name}">设为提交目标</button>
          <button class="mini" data-unpublish="${pkg.name}">整包撤下</button>
        </div>
      </header>
      <table><thead><tr><th>名称</th><th>SHA-256</th><th>大小</th><th>引用</th><th>操作</th></tr></thead>
      <tbody>${rows || '<tr><td colspan="5" class="hint">目录为空</td></tr>'}</tbody></table>`;
    wrap.appendChild(card);
  }

  wrap.querySelectorAll<HTMLElement>("[data-target]").forEach((b) =>
    b.addEventListener("click", () => { targetPkg = b.dataset.target!; $("target-pkg").value = targetPkg; render(state); }));
  wrap.querySelectorAll<HTMLElement>("[data-rm]").forEach((b) =>
    b.addEventListener("click", () => stage({ op: "remove", sha: b.dataset.rm! })));
  wrap.querySelectorAll<HTMLElement>("[data-why]").forEach((b) =>
    b.addEventListener("click", () => queryBlock(b.dataset.why!)));
  wrap.querySelectorAll<HTMLElement>("[data-keep]").forEach((b) =>
    b.addEventListener("click", () => {
      const sha = b.dataset.keep!;
      staged.delete(sha);
      renderStaged();
      void queryBlock(sha);
    }));
  wrap.querySelectorAll<HTMLElement>("[data-unpublish]").forEach((b) =>
    b.addEventListener("click", () => unpublish(b.dataset.unpublish!, state)));

  // stats
  const refs = Object.entries(state.stats.refcount);
  $("stats").innerHTML = `
    <div>修订号：<code>r${state.stats.rev}</code> · 目录：<code>${state.stats.directories}</code> ·
    对象文件：<code>${state.stats.objects}</code> · 删除候选：<code>${state.stats.candidates.length}</code></div>
    <table class="ref-table"><thead><tr><th>SHA-256</th><th>引用数</th></tr></thead>
    <tbody>${refs.map(([sha, n]) => `<tr><td class="sha">${short(sha)}…</td><td>${n}</td></tr>`).join("") ||
      '<tr><td colspan="2" class="hint">无引用记录</td></tr>'}</tbody></table>`;

  // revisions
  $("revisions").innerHTML = state.revisions.map((r) => `
    <li><b>r${r.rev}</b>（基于 r${r.parent_rev}）· ${r.directory} · ${r.timestamp}
      <br/>新增 ${r.summary.added} / 撤下 ${r.summary.removed}${r.note ? ` · ${escapeHtml(r.note)}` : ""}</li>`).join("") ||
    '<li class="hint">尚无修订</li>';

  renderStaged();
}

function escapeHtml(s: string) {
  return s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c] as string);
}

async function refresh() {
  try {
    const state = await api<State>("/api/state");
    render(state);
    const h = await api<any>("/health");
    const dot = $("health-dot"), txt = $("health-text");
    dot.className = "dot " + (h.status === "ok" ? "ok" : "bad");
    txt.textContent = h.status === "ok"
      ? `对象目录可写 · r${h.rev} · ${h.objects} 对象`
      : `降级：可写=${h.objects_dir_writable} 完整性=${h.integrity.ok}`;
  } catch (e) {
    $("health-dot").className = "dot bad";
    $("health-text").textContent = "服务不可达";
  }
}

// ---------------------------------------------------------------- actions
async function unpublish(name: string, state: State) {
  const pkg = state.directories.directories.find((p) => p.name === name);
  if (!pkg || pkg.entries.length === 0) return;
  if (!confirm(`确认真的要整包撤下「${name}」的 ${pkg.entries.length} 个块吗？\n共享块仅在无其他引用时才会回收。`)) return;
  try {
    await api(`/api/packages/${encodeURIComponent(name)}/unpublish`, {
      method: "POST",
      body: JSON.stringify({ base_rev: state.stats.rev }),
    });
    setResult($("action-result"), `目录 ${name} 已整包撤下（新修订）`, "ok");
  } catch (e: any) {
    setResult($("action-result"), `撤下失败：${e.message}`, "err");
  }
  await refresh();
}

async function queryBlock(sha: string) {
  $("query-sha").value = sha;
  const pre = $("block-detail");
  try {
    const s = await api<any>(`/api/blocks/${sha}/status`);
    pre.textContent =
      `SHA-256 : ${s.sha}\n` +
      `可读性  : ${s.readable ? "可读取" : "不可读取"}\n` +
      `状态    : ${s.state}\n` +
      `引用数  : ${s.refcount}\n` +
      `引用目录: ${s.referrers.join(", ") || "（无）"}\n` +
      `保留缘由: ${s.reason}`;
  } catch (e: any) {
    pre.textContent = `查询失败：${e.message}`;
  }
}

function wireEvents() {
  $("create-pkg-btn").addEventListener("click", () => {
    targetPkg = $("pkg-name").value.trim();
    $("target-pkg").value = targetPkg;
    if (lastState) render(lastState);
  });

  $("upload-btn").addEventListener("click", async () => {
    const content = $("block-content").value;
    const name = $("block-name").value.trim();
    if (!content) return setResult($("upload-result"), "内容为空", "err");
    try {
      const r = await api<{ sha: string }>("/api/blocks", { method: "POST", body: content });
      stage({ op: "add", sha: r.sha, name: name || short(r.sha) });
      setResult($("upload-result"), `已上传并暂存「新增」：${r.sha}（${content.length} 字节）`, "ok");
      $("block-content").value = "";
      $("block-name").value = "";
    } catch (e: any) {
      setResult($("upload-result"), `上传被拒：${e.message}`, "err");
    }
  });

  $("submit-btn").addEventListener("click", async () => {
    const directory = $("target-pkg").value.trim() || targetPkg;
    const baseRev = Number($("base-rev").value);
    const note = $("note").value;
    if (!directory) return setResult($("action-result"), "请先指定目标目录", "err");
    if (staged.size === 0) return setResult($("action-result"), "暂存区为空", "err");
    try {
      const r = await api<{ rev: number; summary: { added: number; removed: number } }>("/api/submit", {
        method: "POST",
        body: JSON.stringify({ directory, base_rev: baseRev, changes: [...staged.values()], note }),
      });
      staged.clear();
      $("note").value = "";
      setResult($("action-result"), `已提交修订 r${r.rev}：新增 ${r.summary.added}，撤下 ${r.summary.removed}`, "ok");
    } catch (e: any) {
      if (e.status === 409 && e.body?.code === "revision_conflict") {
        setResult($("action-result"),
          `修订冲突：你基于的 r${baseRev} 已过期，当前为 r${e.body.current_rev}；目录、引用数与对象均未改动。请复核后按当前修订重新提交。`, "err");
      } else {
        setResult($("action-result"), `提交被拒：${e.message}`, "err");
      }
    }
    await refresh();
  });

  $("clear-staged-btn").addEventListener("click", () => { staged.clear(); renderStaged(); });

  for (const [id, mode] of [["gc-mark-btn", "mark"], ["gc-sweep-btn", "sweep"], ["gc-auto-btn", "auto"]] as const) {
    $(id).addEventListener("click", async () => {
      try {
        const r = await api<any>("/api/gc", { method: "POST", body: JSON.stringify({ mode }) });
        const detail = mode === "mark"
          ? `标记候选 ${r.marked.length}：${r.marked.map(short).join(", ") || "无"}`
          : `移除 ${r.removed.length}${r.released?.length ? `，释放候选 ${r.released.length}` : ""}`;
        setResult($("action-result"), `GC[${mode}] ${detail}`, "ok");
      } catch (e: any) {
        setResult($("action-result"), `GC 失败：${e.message}`, "err");
      }
      await refresh();
    });
  }

  $("query-btn").addEventListener("click", () => {
    const sha = $("query-sha").value.trim();
    if (sha) void queryBlock(sha);
  });
}

wireEvents();
renderStaged();
void refresh();
setInterval(refresh, 5000);
