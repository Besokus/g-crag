ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG SOURCE_REVISION
ARG OVERLAY_SHA
ARG SOURCE_URL="unknown"
LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${OVERLAY_SHA}" \
      org.opencontainers.image.licenses="CC-BY-NC-4.0"
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 18500
