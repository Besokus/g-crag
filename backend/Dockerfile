FROM python:3.11-slim-bookworm

ARG SOURCE_REVISION="unknown"
ARG OVERLAY_SHA="unknown"
ARG HF_ENDPOINT="https://hf-mirror.com"
ARG SOURCE_URL="unknown"
# 演示镜像是否预下载本地 BGE 进镜像（自包含）。本机 compose 栈用宿主机 HF 缓存(HF_HUB_OFFLINE=1)
# 且容器构建直连 hf-mirror 不通，故 compose 构建时传 BGE_PRELOAD=false 跳过，避免镜像构建失败。
ARG BGE_PRELOAD="true"

LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${OVERLAY_SHA}" \
      org.opencontainers.image.licenses="CC-BY-NC-4.0"

WORKDIR /app

# 系统依赖：OpenCV/rapidocr 运行所需 + gcc
# 国内用阿里云 Debian 镜像加速
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true && \
    apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 gcc curl && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 先装 CPU-only torch（避开 GPU/CUDA 几 GB；bge 走 CPU 推理够用），再装其余依赖
RUN pip install --no-cache-dir torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu --trusted-host download.pytorch.org && \
    pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com

COPY . .

# 演示镜像预下载本地 BGE，运行时可完全使用 PVC/镜像缓存，不依赖首次联网。
RUN if [ "${BGE_PRELOAD}" = "true" ]; then \
        mkdir -p /opt/huggingface/hub && HF_HOME=/opt/huggingface HF_HUB_CACHE=/opt/huggingface/hub \
        HF_ENDPOINT=${HF_ENDPOINT} python -c \
        "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"; \
    fi

EXPOSE 8001
# WORKDIR=/app，import app.main 即可；配置优先读环境变量（compose env_file 注入）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
