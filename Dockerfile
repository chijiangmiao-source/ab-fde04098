# 校准帧内容寻址重建包服务
# 单一镜像即可运行应用，也可运行 verify（node 仅用于前端零依赖构建门禁）。
FROM python:3.11-slim

# bookworm 的 nodejs（18.x）足以执行 scripts/build_frontend.mjs（node --check）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制构建脚本与源码，构建期完成前后端门禁
COPY backend ./backend
COPY frontend ./frontend
COPY scripts ./scripts

RUN python3 scripts/build_backend.py \
    && node scripts/build_frontend.mjs

# 持久化数据目录（目录边 / 对象文件 / 引用计数 / staging 均在此）
VOLUME ["/data"]

ENV BIND_HOST=0.0.0.0 \
    BIND_PORT=8080 \
    DATA_DIR=/data

EXPOSE 8080

# slim 无 curl，用标准库做容器健康探测
HEALTHCHECK --interval=5s --timeout=3s --start-period=3s --retries=12 \
    CMD python3 -c "import json,os,urllib.request,sys; \
u='http://127.0.0.1:%s/health'%os.environ.get('BIND_PORT','8080'); \
d=json.load(urllib.request.urlopen(u,timeout=2)); \
sys.exit(0 if d.get('objects_writable') else 1)"

CMD ["python3", "backend/server.py"]
