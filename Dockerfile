# ============================================================
# 抽奖助手 - 多架构 Docker 镜像
#   · NAS（arm64）：直接 `docker build -t lottery-app .` 即得 arm 架构镜像
#   · 任意架构构建：`docker buildx build --platform linux/arm64 -t lottery-app .`
#   · 数据持久化：挂载卷到 /app/data（SQLite/账号cookies/配置）
# ============================================================

# ---- 阶段1：构建前端 ----
FROM node:20-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json ./
RUN npm install --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 阶段2：后端运行 ----
FROM python:3.13-slim
WORKDIR /app

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY --from=frontend /build/frontend/dist ./frontend/dist

# 数据目录（挂载卷持久化：配置/账号cookies/活动数据）
ENV BILI_DATA_DIR=/app/data
VOLUME ["/app/data"]

WORKDIR /app/backend
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
