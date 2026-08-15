# 构建阶段
FROM node:20-alpine AS build
ARG SOURCE_REVISION="unknown"
ARG OVERLAY_SHA="unknown"
WORKDIR /app
COPY package*.json ./
RUN npm install --registry https://registry.npmmirror.com
COPY . .
RUN npm run build

# 运行阶段：nginx 托管静态资源 + 反向代理 /api 到后端
FROM nginx:alpine
ARG SOURCE_REVISION="unknown"
ARG OVERLAY_SHA="unknown"
ARG SOURCE_URL="unknown"
LABEL org.opencontainers.image.source="${SOURCE_URL}" \
      org.opencontainers.image.revision="${SOURCE_REVISION}" \
      org.opencontainers.image.version="${OVERLAY_SHA}" \
      org.opencontainers.image.licenses="CC-BY-NC-4.0"
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
