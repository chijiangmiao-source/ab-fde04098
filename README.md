# 校准帧内容寻址重建包服务

同步辐射成像组的校准帧以**按 SHA-256 寻址的去重文本块**存储，并以**带修订号的
重建包目录**发布。复核员在网页上新增、保留或撤下目录内容后提交，可查看摘要、
修订与引用数。

## 一、核心业务规则（与验收点对应）

1. **同一持久化排他边界**：目录边（`packages/<name>/manifest.json` 修订）、
   对象文件（`objects/ab/cdef…`）与引用计数（`refs.json`）全部在同一把
   进程间排他文件锁（`store.lock`，fcntl + 线程锁）后变更，写盘均为
   临时文件 + fsync + `os.replace` 原子落盘。
2. **共享引用保留**：两个包共享同一块时引用计数为 2；撤下一包并 GC 后，
   只要另一包仍引用，该块保留且可通过 `GET /api/blocks/<sha256>` 读取，
   响应中的 `retention_reason` 说明保留缘由（仍被哪些包引用）。
3. **撤下最后引用**：引用计数归零后该块才可回收；回收后接口返回
   `404 block_unavailable`，摘要的引用统计与各包目录均不再包含它。
4. **只有零引用块可回收**：即使 `refs.json` 被外部损坏，任何存活包目录中
   的块都不会被删（`garbage_collect` 以包目录为最终防线）。
5. **两阶段清理**：先在 `staging/.gc-candidate-<digest>` 留下候选标记，
   再把对象移出 `objects/` 并删除；**发布路径从不读 `staging/`**，
   引用一个只存在于 staging 的旧对象的提交会被拒绝（`missing_blocks`），
   保证发布不接入删除中的旧对象。
6. **故障重启收敛**：若在标记后或移除中崩溃/被 SIGKILL，下次启动
   `PackageStore.recover()` 在排他锁内完成收敛；对象在引用计数归零前
   不离开 `objects/`，故**存活包始终不指向缺失内容**。收敛幂等。
7. **修订冲突**：提交携带 `expected_revision`；基于过期修订的提交被
   稳定拒绝（HTTP 409 `stale_revision`），且不改变既有目录、引用数或对象。
8. **健康接口** `GET /health`：报告 `objects_writable`（实测对象目录
   是否可写），不可写时返回 503。
9. **端口可配置**：`--port` / `BIND_PORT`（容器内监听）与 Compose 的
   `HOST_PORT`（宿主映射，默认 8080）。

## 二、HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 健康：`objects_writable` + 存储统计 |
| GET  | `/api/summary` | 每个包的摘要、修订与每块引用数 |
| GET  | `/api/packages/<name>` | 单包目录（块列表、修订号、提交说明） |
| POST | `/api/packages/<name>/commit` | 提交新修订（见下） |
| GET  | `/api/blocks/<sha256>` | 读块 + 引用数 + 引用方 + `retention_reason` |
| POST | `/api/gc` | 两阶段 GC；body `{"mode":"mark"}` 只做第一阶段 |
| POST | `/api/recover` | 手动触发中断收敛 |

提交示例：

```bash
curl -s -X POST localhost:8080/api/packages/beamline-a/commit \
  -H 'Content-Type: application/json' \
  -d '{"blocks":["文本块…"], "block_digests":["<保留的已有块 sha256>"],
       "expected_revision": 3, "reason":"复核后保留共享帧"}'
```

## 三、本地运行（无第三方 Python/Node 依赖）

```bash
python3 backend/server.py --port 8080 --data ./data
# 或
BIND_PORT=8080 DATA_DIR=./data python3 backend/server.py
```

打开 http://localhost:8080 即可使用复核台。

## 四、测试、构建与 verify

```bash
# 后端代码测试（13 项：去重/锁、共享保留、两阶段 GC、两种崩溃重启、修订冲突）
PYTHONPATH=backend python3 -m unittest discover -s backend/tests -v

# 前后端构建门禁
python3 scripts/build_backend.py     # py_compile
node scripts/build_frontend.mjs      # node --check + 打包到 backend/static

# API/HTTP 冒烟（真实子进程 + SIGKILL 故障注入；退出码按业务位）
python3 scripts/smoke.py
```

`python3 scripts/verify.py` 会**交错**执行上述全部步骤，并在最后对
`REMOTE_SMOKE_URL`（若给出）做远程冒烟，完成后以掩码退出码报告：

| 位 | 含义 |
| --- | --- |
| 1 | 共享引用保留失败 |
| 2 | 故障重启收敛失败 |
| 4 | 修订冲突失败 |
| 8 | 代码测试失败 |
| 16 | 构建失败 |
| 32 | 基础设施（健康/页面）失败 |

## 五、Docker Compose（含可执行 verify 服务）

```bash
HOST_PORT=9090 docker compose up -d --build app   # 端口可配置
docker compose run --rm verify                    # 跑验收，退出码即结果
docker compose up --build                         # 起 app 的同时跑一次 verify
```

`verify` 服务先在容器内完成代码测试、前后端构建和本地全套冒烟（含
SIGKILL 后重启收敛），再对已健康上线的 `app` 远程冒烟，然后退出。

## 六、目录布局

```
backend/app/store.py   存储核心：锁、内容寻址、引用计数、两阶段 GC、恢复
backend/server.py      标准库 HTTP 服务 + 静态页面托管
backend/tests/         unittest 代码测试
frontend/src/          复核台页面（无框架；构建仅做语法门禁与打包）
scripts/verify.py      verify 入口（测试 → 构建 → 冒烟，掩码退出码）
scripts/smoke.py       API/HTTP 冒烟（共享保留/故障重启/修订冲突）
data/ （挂载卷）
  store.lock           持久化排他边界
  refs.json            引用计数
  objects/ab/cdef…     SHA-256 内容寻址对象（去重）
  packages/<name>/manifest.json
  staging/             GC 候选标记与删除中转（发布路径不读取）
```
