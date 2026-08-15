FROM ${BASE_IMAGE}

ARG SOURCE_REVISION="unknown"
ARG OVERLAY_SHA="unknown"
ARG SOURCE_URL="unknown"

LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${OVERLAY_SHA}" \
      org.opencontainers.image.licenses="CC-BY-NC-4.0"

WORKDIR /app
COPY milvus_client.py /app/app/clients/milvus_client.py
COPY config.py /app/app/config.py
COPY otel_genai.py /app/app/core/otel_genai.py
COPY init_db.py /app/app/db/init_db.py
COPY main.py /app/app/main.py
COPY system.py /app/app/routers/system.py
COPY backup_service.py /app/app/services/backup_service.py
COPY document_service.py /app/app/services/document_service.py
COPY knowledge_backbone.py /app/app/services/knowledge_backbone.py
COPY knowledge_evolution_service.py /app/app/services/knowledge_evolution_service.py
COPY retrieval_service.py /app/app/services/retrieval_service.py
COPY rewrite_evaluator.py /app/app/services/rewrite_evaluator.py

RUN mkdir -p /opt/huggingface/hub && HF_HOME=/opt/huggingface HF_HUB_CACHE=/opt/huggingface/hub \
    HF_ENDPOINT=https://hf-mirror.com python -c \
    "from sentence_transformers import SentenceTransformer; SentenceTransformer('BAAI/bge-small-zh-v1.5')"

EXPOSE 8001
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001"]
