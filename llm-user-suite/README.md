# Grid-QA LLM as a User Suite

这是一个独立、可选运行的旁路评测套件。Grid-QA 只在显式开启探针后异步发送脱敏 OTel 事件；Collector、控制面、Worker、Scheduler、Console 和临时 Runner Job 均独立部署，不进入业务 Pod，也不改变业务 Service 路由。

## 数据闭环

1. Grid-QA 产生登录、会话、问答、SSE/WS、反馈、路由、降级和异常语义事件。
2. Telemetry Intake 接收 OTLP traces/logs；Prometheus/OTLP metrics 只按 allowlist 做分钟聚合。
3. Sessionizer 以租户、HMAC 用户、conversation/trace 和 30 分钟空闲窗口重建会话。
4. Dreamer 每 6 小时归一化、聚类并生成版本化剧本；新版本必须人工审核。
5. Runner 在白名单测试实例按 OpenAPI、SSE 和 WebSocket 客户端渐进披露并回放。
6. `eval-core` 先做确定性契约检查，再做 RAG、反馈对齐、轨迹和性能评测。
7. 低分结果通过 HMAC Domain Event 回调 Grid-QA；知识缺口只生成待人工审核的草稿。
8. 草稿人工批准并显式回流后，Grid-QA 发出 `indexed` 事件，套件自动复测同一剧本版本并计算 lift。

## 本地运行

```bash
python -m venv .venv-llm-user
. .venv-llm-user/bin/activate
pip install -r llm-user-suite/requirements.txt pytest pytest-asyncio
export PYTHONPATH="$PWD/llm-user-suite:$PWD/backend"
export LLM_USER_AUTH_DISABLED=true
export LLM_USER_ALLOWED_TARGET_HOSTS=localhost,testserver
uvicorn llm_user_suite.main:app --app-dir llm-user-suite --host 127.0.0.1 --port 8080
```

控制台：

```bash
cd llm-user-suite/frontend
npm ci
npm run dev
```

测试：

```bash
pytest -q llm-user-suite/tests
DATABASE_URL=sqlite+aiosqlite:////tmp/grid-qa-tests.db pytest -q tests/test_llm_user_integration.py
```

## 关键配置

| 变量 | 说明 |
|---|---|
| `LLM_USER_DATABASE_URL` | 独立 PostgreSQL/SQLite 元数据数据库 |
| `LLM_USER_ALLOWED_TARGET_HOSTS` | Runner 目标主机白名单；默认强制要求配置 |
| `LLM_USER_DENIED_TARGET_HOSTS` | 额外禁止的主机列表 |
| `LLM_USER_CREDENTIAL_PROFILES_JSON` | 测试账号 Profile，只能从 Secret 注入 |
| `LLM_USER_DREAMER_*` / `ACTOR_*` / `JUDGE_*` | 三个角色独立 OpenAI-compatible Provider |
| `LLM_USER_CHALLENGER_*` | 高风险或临界 Case 的第二 Judge |
| `LLM_USER_OLLAMA_*` | 各角色云 Provider 不可用时的本地回退 |
| `LLM_USER_GRID_CALLBACK_URL` | 套件向 Grid-QA 提交评测 Domain Event 的地址 |
| `LLM_USER_GRID_CALLBACK_SECRET` | 套件 → Grid-QA HMAC Secret |
| `LLM_USER_GRID_EVENT_SECRET` | Grid-QA → 套件草稿回流事件 HMAC Secret |
| `LLM_USER_METRIC_ALLOWLIST` | 可持久化的低基数指标名单 |
| `LLM_USER_RAW_CAPTURE_ENABLE` | 授权原文加密保存，默认 `false` |
| `LLM_USER_RAW_CAPTURE_REQUIRE_KMS` | 默认 `true`，生产原文采集必须由 KMS 包装随机数据密钥 |
| `LLM_USER_RAW_CAPTURE_KMS_*` | KMS 包装端点、Key ID 和只从 Secret 注入的 Token |
| `LLM_USER_RAW_CAPTURE_KEY` | 仅本地开发关闭 KMS 门禁后可用的 32 字节 URL-safe base64 KEK |

密码、JWT、Cookie、Authorization、API Key 和 Secret 在任何模式下都不会进入行为事件。`RAW_CAPTURE_ENABLE` 只允许管理员显式上传授权附件；服务为每个附件生成独立数据密钥，使用 AES-256-GCM 加密正文，并默认要求 KMS 包装数据密钥。磁盘和 MinIO/S3 中只保存密文与信封元数据。

## Control API

- `GET /v1/scenarios`、`POST /v1/scenarios/{id}/review`：剧本浏览和审核
- `GET /v1/runs`、`POST /v1/runs`、`DELETE /v1/runs/{id}`：发起、查看和取消回放
- `GET /v1/evaluations`：确定性与 LLM Judge 维度
- `GET /v1/evaluations/retests`：草稿回流前后 lift
- `GET /v1/reports`：HTML、Markdown、JSON 报告
- `POST /v1/traces|logs|metrics`：标准 OTLP HTTP Intake
- `GET /v1/telemetry/metrics`：分钟聚合指标
- `POST /v1/integrations/grid-qa/evolution-events`：Grid-QA 回流事件入口
- `GET /health`、`GET /metrics`：健康和套件自身 Prometheus 指标

## Kubernetes

1. 构建并推送后端与控制台镜像，更新 `deploy/k8s/kustomization.yaml` 的 tag/digest。
2. 将 `deploy/k8s/config.yaml` 的 `LLM_USER_RUNNER_IMAGE` 固定为同一后端镜像 digest；Kustomize 的 `images` 变换不会改写 ConfigMap 字符串。
3. 替换 `REPLACE_GRID_QA_NAMESPACE` 和镜像信息，再从 `secret.example.yaml` 创建真实 Secret；不要提交渲染后的 Secret。
4. `kubectl kustomize llm-user-suite/deploy/k8s` 检查资源。
5. `kubectl apply -k llm-user-suite/deploy/k8s` 部署独立命名空间。
6. 复制 `deploy/k8s/examples/` 中的 Secret 和 Deployment patch 到 Grid-QA 测试命名空间，替换占位符后再接入探针。

示例 patch 打开了脱敏 query/最终答案摘要，以便 Dreamer 还原业务意图；未完成数据授权时应保持 `LLM_USER_OBSERVER_CAPTURE_TEXT=false`，此时仍会采集接口、时序、状态、路由和反馈类别，但剧本语义会更弱。

关闭接入只需把 `LLM_USER_OBSERVER_ENABLED=false` 或移除 observer patch；旁路服务故障不会阻塞 Grid-QA 请求。生产主机标记、非白名单主机、用户/Secret/系统配置接口以及未显式授权的 DELETE 操作会被 Runner 拒绝。
