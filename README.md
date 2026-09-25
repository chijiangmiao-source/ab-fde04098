# 同步辐射校准帧 · 内容寻址块与重建包目录服务

复核员把校准帧拆成 **SHA-256 寻址的去重文本块**，以**带修订号的重建包目录**发布；
可在网页新增 / 保留 / 撤下目录内容，一次提交后查看摘要、修订历史与引用计数。

## 快速开始

### 本机（无 Docker）

```bash
# 后端（自动使用/创建 .venv）
scripts/verify.sh                 # 一键验收，退出码即验收状态

# 单独启动服务（生产服务器 waitress）
python -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
DATA_DIR=./data HOST=0.0.0.0 PORT=8080 \
  STATIC_DIR=$PWD/frontend/static PYTHONPATH=$PWD/backend \
  .venv/bin/waitress-serve --host=0.0.0.0 --port=8080 app.wsgi:app
# 浏览器打开 http://localhost:8080

# 前端构建
cd frontend && npm install && npm run build     # tsc 类型检查 + esbuild
```

### Docker Compose（端口可配置）

```bash
cp .env.example .env          # APP_PORT=宿主访问端口（默认 8080）
APP_PORT=9090 docker compose up -d --build app

# 可执行 verify 服务：交错完成代码测试、前后端构建、API/HTTP 冒烟，
# 跑完即退出，并以退出码报告验收状态（0=通过）
docker compose run --build verify
echo "acceptance exit code: $?"
```

`HOST` / `PORT` 为部署入口环境变量；`APP_PORT` 是 Compose 发布到宿主的访问端口。

## 目录

```
backend/app/store.py   # 内容寻址存储 + 单排他边界事务 + 两阶段 GC + 崩溃收敛
backend/app/api.py     # Flask HTTP API 与静态页托管
backend/tests/         # 26 个单元/API/并发测试
frontend/src/main.ts   # 评审控制台（原生 TS，esbuild 构建）
scripts/smoke.py       # 真实进程 + 杀进程重启的 API/HTTP 冒烟（27 项检查）
scripts/verify.sh      # 验收编排：测试 → 前端构建 → 后端构建 → 冒烟
docker/Dockerfile.app  # 多阶段运行镜像（Node 构建前端，Python 运行）
docker/Dockerfile.verify
docker-compose.yml     # app + 一次性 verify 服务，端口可配置
```

## 主要接口

| 方法/路径 | 说明 |
| --- | --- |
| `POST /api/blocks` | 上传文本块，返回 SHA-256（内容寻址、去重） |
| `GET  /api/blocks/<sha>` | 读取块；删除候选中或已回收 → 404 |
| `GET  /api/blocks/<sha>/status` | 可读性、引用数、引用目录与**保留缘由** |
| `GET  /api/directories[/<name>]` | 重建包目录及其边 |
| `POST /api/submit` | `{directory, base_rev, changes:[add|remove], note}` 提交一个修订 |
| `POST /api/packages/<name>/unpublish` | 整包撤下（一次修订） |
| `POST /api/gc` | `mode=mark|sweep|auto` 两阶段垃圾回收 |
| `POST /api/admin/recover` | 手动触发重启收敛（启动时自动执行） |
| `GET  /api/revisions` `/api/stats` `/api/state` | 修订历史、引用统计、整页状态 |
| `GET  /health` | 200/503，报告**对象目录是否可写**与引用完整性 |

## 一致性设计（与业务要求对照）

- **单一持久化排他边界**：目录边（manifest）、对象文件（objects/）、引用计数
  的全部读写都在对 `state/store.lock` 的 `flock(LOCK_EX)` 中进行；锁按线程各自
  的打开文件描述持有，故跨线程、跨进程均互斥（见
  `tests/test_concurrency.py`：10 个并发提交恰有 1 个成功，其余为修订冲突）。
- **原子落盘**：所有元数据/标记均 `tmp + fsync + rename + fsync 目录`；提交顺序为
  不可变快照 → manifest 指针 → 追加式修订日志，崩溃后由快照对账日志。
- **共享块保留**：撤下一包仅删其独占边；共享块引用计数仍为 1，GC 不回收，
  另一包可读，`/status` 返回「仍被 N 个目录引用: …」。撤下**最后**引用后，
  标记并清理，块 404，目录边与引用计数同步消失。
- **只有零引用可回收**：`mark` 严格按 manifest 引用计数判定，正引用块不标记。
- **先标记后移除**：`mark` 写 `<sha>.cand`（不动字节）；`sweep` 先删对象再删标记。
  标记中的块不对外可读、也不能被新发布接入（`409 deleting_object`），
  防止发布接入删除中的旧对象。
- **中断后重启收敛**：启动 `_recover_locked` 清理残留临时上传；对候选标记：
  零引用则补完删除（无论中断在标记后还是删字节后），重新被引用则释放标记保留
  对象。冒烟用真实 `SIGTERM` 重启验证三种撕裂形态。
- **存活包不指向缺失内容**：健康检查对每条目录边核对对象文件存在且非候选；
  恢复逻辑绝不删除仍被引用的对象。
- **过期修订稳定拒绝**：提交时先比对 `base_rev == 当前 rev`，不符直接
  `409 revision_conflict`，在任何边/计数/对象改动之前返回；日志不新增条目。
- **健康接口**：在 objects/ 中做 create/fsync/unlink 探针，只读盘或无权限时
  `/health` 为 503 且 `objects_dir_writable=false`。

## 验收

`scripts/verify.sh`（容器中即 `verify` 服务）交错执行：

1. 后端代码测试（存储、引用、GC、冲突、并发、恢复）；
2. 前端类型检查与构建；
3. 后端字节码编译与导入校验；
4. API/HTTP 冒烟，围绕**共享引用保留、故障重启收敛、修订冲突**三大业务结果。

全部通过退出码 `0`，任一失败 `1`。

> 注：交付环境未安装 Docker 守护进程，镜像未在本机构建；`verify.sh` 与冒烟
> 已用本机 Python/Node 工具链完整跑通（26 单测 + 27 冒烟检查，exit 0），
> Dockerfile/Compose 按同一脚本与标准多阶段构建编写。
