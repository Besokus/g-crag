#!/bin/sh
set -eu

KUBECONFIG_PATH="${KUBECONFIG_PATH:-${KUBECONFIG:-$HOME/.kube/config/prod-ai2-kubeconfig.yaml}}"
NAMESPACE="${NAMESPACE:-nmt-llm}"
DEPLOYMENT="${DEPLOYMENT:-term-extractor-test}"
SOURCE_REVISION="995091bae9fe47391d50806b6fc9f1a764828785"
SOURCE_URL="${SOURCE_URL:-unknown}"
EXPECTED_CONTEXT="prod-ai2"
EXPECTED_REVISION="107"
ROOT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/../../.." && pwd)"
DEPLOY_DIR="$ROOT_DIR/deploy/k8s/single-pod"
STATE_DIR="${STATE_DIR:-$ROOT_DIR/.deploy-state/single-pod}"

K="kubectl --kubeconfig $KUBECONFIG_PATH -n $NAMESPACE"
MUTATED=0
SNAPSHOT=""

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

need() {
  command -v "$1" >/dev/null 2>&1 || fail "缺少命令: $1"
}

rand_secret() {
  openssl rand -base64 48 | tr -d '\n/+=' | cut -c1-48
}

render() {
  # 只替换清单模板变量，保留容器命令中的 $MYSQL_ROOT_PASSWORD 等运行时变量。
  envsubst '
    $SOURCE_REVISION $SOURCE_URL $SOURCE_GIT_CONTEXT $OVERLAY_SHA $BUILD_ID
    $BACKEND_TAG $FRONTEND_TAG $BACKEND_BASE_TAG $FRONTEND_BASE_TAG
    $BACKEND_IMAGE $FRONTEND_IMAGE
    $MYSQL_IMAGE $REDIS_IMAGE $MINIO_IMAGE $ETCD_IMAGE
    $MILVUS_IMAGE $NEO4J_IMAGE $OLLAMA_IMAGE
    $MIRROR_JOB $SOURCE_IMAGE $DEST_IMAGE
  ' < "$1"
}

resolve_source_git_context() {
  if [ -n "${SOURCE_GIT_CONTEXT:-}" ]; then
    printf '%s' "$SOURCE_GIT_CONTEXT"
    return
  fi
  remote_url="$(git -C "$ROOT_DIR" remote get-url origin 2>/dev/null || true)"
  case "$remote_url" in
    git://*) printf '%s' "$remote_url" ;;
    https://*|http://*) printf 'git://%s' "${remote_url#*://}" ;;
    git@*:* )
      remote_host_path="${remote_url#git@}"
      remote_host="${remote_host_path%%:*}"
      remote_path="${remote_host_path#*:}"
      printf 'git://%s/%s' "$remote_host" "$remote_path"
      ;;
    *) fail "无法从 origin 解析 Git 源码地址，请设置 SOURCE_GIT_CONTEXT" ;;
  esac
}

wait_job() {
  job="$1"
  timeout="$2"
  if ! $K wait --for=condition=complete "job/$job" --timeout="$timeout"; then
    $K describe "job/$job" >&2 || true
    $K logs "job/$job" --all-containers=true --tail=1000 >&2 || true
    return 1
  fi
}

start_mirror() {
  MIRROR_JOB="$1"
  SOURCE_IMAGE="$2"
  DEST_IMAGE="$3"
  export MIRROR_JOB SOURCE_IMAGE DEST_IMAGE
  $K delete job "$MIRROR_JOB" --ignore-not-found >/dev/null
  render "$DEPLOY_DIR/kaniko-mirror-job.yaml" | $K apply -f - >/dev/null
}

job_digest() {
  job="$1"
  container="$2"
  # Job 重试时可能同时保留失败和成功 Pod；从所有 Pod 中选择有效 digest。
  digest="$($K get pods -l "job-name=$job" -o json | jq -r --arg c "$container" '
    [
      .items[]
      | .status.containerStatuses[]?
      | select(.name == $c)
      | (.state.terminated.message // empty)
      | select(test("^sha256:[0-9a-f]{64}$"))
    ] | last // empty
  ')"
  printf '%s' "$digest" | grep -Eq '^sha256:[0-9a-f]{64}$' || fail "无法从 $job 获取镜像 digest"
  printf '%s' "$digest"
}

collect_diagnostics() {
  printf '%s\n' '--- deployment ---' >&2
  $K get deployment "$DEPLOYMENT" -o wide >&2 || true
  printf '%s\n' '--- pods ---' >&2
  $K get pods -l workload.user.cattle.io/workloadselector=apps.deployment-nmt-llm-term-extractor-test -o wide >&2 || true
  printf '%s\n' '--- events ---' >&2
  $K get events --sort-by=.lastTimestamp | tail -80 >&2 || true
  for pod in $($K get pods -l workload.user.cattle.io/workloadselector=apps.deployment-nmt-llm-term-extractor-test -o name 2>/dev/null); do
    $K describe "$pod" >&2 || true
    $K logs "$pod" --all-containers=true --tail=300 >&2 || true
  done
}

rollback() {
  [ "$MUTATED" -eq 1 ] || return 0
  printf '%s\n' '部署失败，收集诊断并恢复部署前 revision 107 / replicas=0。' >&2
  collect_diagnostics
  if [ -n "$SNAPSHOT" ] && [ -f "$SNAPSHOT" ]; then
    $K replace -f "$SNAPSHOT" >&2 || true
    $K scale "deployment/$DEPLOYMENT" --replicas=0 >&2 || true
    $K get deployment "$DEPLOYMENT" -o jsonpath='restored replicas={.spec.replicas}{" image="}{.spec.template.spec.containers[0].image}{"\n"}' >&2 || true
  fi
}

trap 'rc=$?; if [ "$rc" -ne 0 ]; then rollback; fi; exit "$rc"' EXIT INT TERM

for bin in kubectl git jq openssl envsubst shasum; do need "$bin"; done
[ -r "$KUBECONFIG_PATH" ] || fail "kubeconfig 不可读: $KUBECONFIG_PATH"
SOURCE_GIT_CONTEXT="$(resolve_source_git_context)"
export SOURCE_URL SOURCE_GIT_CONTEXT

context="$(kubectl --kubeconfig "$KUBECONFIG_PATH" config current-context)"
[ "$context" = "$EXPECTED_CONTEXT" ] || fail "kube context 不匹配: $context != $EXPECTED_CONTEXT"
[ "$(git -C "$ROOT_DIR" branch --show-current)" = "dev/fix-deploy-local" ] || fail "请在 dev/fix-deploy-local 分支执行"
[ "$(git -C "$ROOT_DIR" rev-parse HEAD)" = "$SOURCE_REVISION" ] || fail "当前 HEAD 不是固定基线 $SOURCE_REVISION"

revision="$($K get deployment "$DEPLOYMENT" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}')"
replicas="$($K get deployment "$DEPLOYMENT" -o jsonpath='{.spec.replicas}')"
[ "$revision" = "$EXPECTED_REVISION" ] || fail "目标 revision 已变化: $revision"
[ "$replicas" = "0" ] || fail "目标 replicas 已变化: $replicas"

mkdir -p "$STATE_DIR"
stamp="$(date +%Y%m%d-%H%M%S)"
SNAPSHOT="$STATE_DIR/term-extractor-test-revision-107-$stamp.json"
$K get deployment "$DEPLOYMENT" -o json | jq 'del(.status,.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.generation,.metadata.managedFields)' > "$SNAPSHOT"

PATCH_FILE="$STATE_DIR/overlay-$stamp.patch"
git -C "$ROOT_DIR" diff --binary -- backend frontend kb_seed > "$PATCH_FILE"
[ -s "$PATCH_FILE" ] || fail "overlay patch 为空"
PATCH_SHA="$(shasum -a 256 "$PATCH_FILE" | awk '{print $1}')"
OVERLAY_SHA="$(
  {
    printf '%s\n' "$PATCH_SHA"
    shasum -a 256 \
      "$DEPLOY_DIR/nginx.conf" \
      "$DEPLOY_DIR/deployment.yaml" \
      "$DEPLOY_DIR/seed-job.yaml" \
      "$DEPLOY_DIR/verify.py" \
      "$DEPLOY_DIR/build-context-backend/Dockerfile" \
      "$DEPLOY_DIR/build-context-frontend/Dockerfile" \
      "$DEPLOY_DIR/kaniko-backend-job.yaml" \
      "$DEPLOY_DIR/kaniko-frontend-job.yaml" \
      "$DEPLOY_DIR/kaniko-mirror-job.yaml" \
      | awk '{print $1}'
  } | shasum -a 256 | awk '{print substr($1,1,12)}'
)"
BUILD_ID="$(printf '%s' "$OVERLAY_SHA" | cut -c1-10)"
[ "$(wc -c < "$PATCH_FILE" | tr -d ' ')" -lt 900000 ] || fail "overlay patch 超过 ConfigMap 安全大小"

BACKEND_TAG="harbor-registry.inner.youdao.com/ai/grid-qa-backend:995091b-localdemo-$OVERLAY_SHA"
FRONTEND_TAG="harbor-registry.inner.youdao.com/ai/grid-qa-frontend:995091b-localdemo-$OVERLAY_SHA"
BACKEND_BASE_TAG="harbor-registry.inner.youdao.com/ai/grid-qa-backend:995091b-base"
FRONTEND_BASE_TAG="harbor-registry.inner.youdao.com/ai/grid-qa-frontend:995091b-base"
export SOURCE_REVISION OVERLAY_SHA BUILD_ID BACKEND_TAG FRONTEND_TAG BACKEND_BASE_TAG FRONTEND_BASE_TAG

printf '构建 overlay=%s\n' "$OVERLAY_SHA"
$K create configmap "grid-qa-backend-context-$BUILD_ID" \
  --from-file=Dockerfile="$DEPLOY_DIR/build-context-backend/Dockerfile" \
  --from-file=milvus_client.py="$ROOT_DIR/backend/app/clients/milvus_client.py" \
  --from-file=config.py="$ROOT_DIR/backend/app/config.py" \
  --from-file=otel_genai.py="$ROOT_DIR/backend/app/core/otel_genai.py" \
  --from-file=init_db.py="$ROOT_DIR/backend/app/db/init_db.py" \
  --from-file=main.py="$ROOT_DIR/backend/app/main.py" \
  --from-file=system.py="$ROOT_DIR/backend/app/routers/system.py" \
  --from-file=backup_service.py="$ROOT_DIR/backend/app/services/backup_service.py" \
  --from-file=document_service.py="$ROOT_DIR/backend/app/services/document_service.py" \
  --from-file=knowledge_backbone.py="$ROOT_DIR/backend/app/services/knowledge_backbone.py" \
  --from-file=knowledge_evolution_service.py="$ROOT_DIR/backend/app/services/knowledge_evolution_service.py" \
  --from-file=retrieval_service.py="$ROOT_DIR/backend/app/services/retrieval_service.py" \
  --from-file=rewrite_evaluator.py="$ROOT_DIR/backend/app/services/rewrite_evaluator.py" \
  --dry-run=client -o yaml | $K apply -f -
$K create configmap "grid-qa-frontend-context-$BUILD_ID" \
  --from-file=Dockerfile="$DEPLOY_DIR/build-context-frontend/Dockerfile" \
  --from-file=nginx.conf="$DEPLOY_DIR/nginx.conf" \
  --dry-run=client -o yaml | $K apply -f -

backend_job="grid-qa-build-backend-$BUILD_ID"
frontend_job="grid-qa-build-frontend-$BUILD_ID"
$K delete job "$backend_job" "$frontend_job" --ignore-not-found >/dev/null
render "$DEPLOY_DIR/kaniko-backend-job.yaml" | $K apply -f -
render "$DEPLOY_DIR/kaniko-frontend-job.yaml" | $K apply -f -

wait_job "$backend_job" 30m & bpid=$!
wait_job "$frontend_job" 30m & fpid=$!
wait "$bpid"
wait "$fpid"

backend_digest="$(job_digest "$backend_job" build-and-push)"
frontend_digest="$(job_digest "$frontend_job" build-and-push)"
BACKEND_IMAGE="${BACKEND_TAG%:*}@$backend_digest"
FRONTEND_IMAGE="${FRONTEND_TAG%:*}@$frontend_digest"
export BACKEND_IMAGE FRONTEND_IMAGE
printf '镜像已构建并固定 digest：backend=%s frontend=%s\n' "$backend_digest" "$frontend_digest"

# 生产节点不稳定访问 Docker Hub/Quay；先通过国内代理将运行镜像同步到内部 Harbor，
# Deployment 最终只引用 Harbor digest，不依赖 Pod 启动时出网。
$K create configmap grid-qa-mirror-context \
  --from-file=Dockerfile="$DEPLOY_DIR/build-context-mirror/Dockerfile" \
  --dry-run=client -o yaml | $K apply -f - >/dev/null

start_mirror grid-qa-mirror-mysql-8043 \
  docker.m.daocloud.io/library/mysql:8.0.43 \
  harbor-registry.inner.youdao.com/ai/grid-qa-mirror-mysql:8.0.43
start_mirror grid-qa-mirror-redis-746 \
  docker.m.daocloud.io/library/redis:7.4.6-alpine \
  harbor-registry.inner.youdao.com/ai/grid-qa-mirror-redis:7.4.6-alpine
start_mirror grid-qa-mirror-minio-20250907 \
  docker.m.daocloud.io/minio/minio:RELEASE.2025-09-07T16-13-09Z \
  harbor-registry.inner.youdao.com/ai/grid-qa-mirror-minio:RELEASE.2025-09-07T16-13-09Z
start_mirror grid-qa-mirror-etcd-355 \
  quay.m.daocloud.io/coreos/etcd:v3.5.5 \
  harbor-registry.inner.youdao.com/ai/grid-qa-mirror-etcd:v3.5.5
start_mirror grid-qa-mirror-milvus-2410 \
  docker.m.daocloud.io/milvusdb/milvus:v2.4.10 \
  harbor-registry.inner.youdao.com/ai/grid-qa-mirror-milvus:v2.4.10
start_mirror grid-qa-mirror-neo4j-52629 \
  docker.m.daocloud.io/library/neo4j:5.26.29-community \
  harbor-registry.inner.youdao.com/ai/grid-qa-mirror-neo4j:5.26.29-community
start_mirror grid-qa-mirror-ollama-0329 \
  docker.m.daocloud.io/ollama/ollama:0.32.9 \
  harbor-registry.inner.youdao.com/ai/grid-qa-mirror-ollama:0.32.9

mirror_jobs="
grid-qa-mirror-mysql-8043
grid-qa-mirror-redis-746
grid-qa-mirror-minio-20250907
grid-qa-mirror-etcd-355
grid-qa-mirror-milvus-2410
grid-qa-mirror-neo4j-52629
grid-qa-mirror-ollama-0329
"
mirror_pids=""
for job in $mirror_jobs; do
  wait_job "$job" 30m & mirror_pids="$mirror_pids $!"
done
for pid in $mirror_pids; do wait "$pid"; done

MYSQL_IMAGE="harbor-registry.inner.youdao.com/ai/grid-qa-mirror-mysql@$(job_digest grid-qa-mirror-mysql-8043 mirror)"
REDIS_IMAGE="harbor-registry.inner.youdao.com/ai/grid-qa-mirror-redis@$(job_digest grid-qa-mirror-redis-746 mirror)"
MINIO_IMAGE="harbor-registry.inner.youdao.com/ai/grid-qa-mirror-minio@$(job_digest grid-qa-mirror-minio-20250907 mirror)"
ETCD_IMAGE="harbor-registry.inner.youdao.com/ai/grid-qa-mirror-etcd@$(job_digest grid-qa-mirror-etcd-355 mirror)"
MILVUS_IMAGE="harbor-registry.inner.youdao.com/ai/grid-qa-mirror-milvus@$(job_digest grid-qa-mirror-milvus-2410 mirror)"
NEO4J_IMAGE="harbor-registry.inner.youdao.com/ai/grid-qa-mirror-neo4j@$(job_digest grid-qa-mirror-neo4j-52629 mirror)"
OLLAMA_IMAGE="harbor-registry.inner.youdao.com/ai/grid-qa-mirror-ollama@$(job_digest grid-qa-mirror-ollama-0329 mirror)"
export MYSQL_IMAGE REDIS_IMAGE MINIO_IMAGE ETCD_IMAGE MILVUS_IMAGE NEO4J_IMAGE OLLAMA_IMAGE

render "$DEPLOY_DIR/deployment.yaml" | $K replace --dry-run=server -f - >/dev/null

$K apply -f "$DEPLOY_DIR/pvc.yaml"
$K wait --for=jsonpath='{.status.phase}'=Bound pvc/term-extractor-test-grid-data --timeout=5m

if ! $K get secret term-extractor-test-grid-env >/dev/null 2>&1; then
  jwt_secret="$(rand_secret)"
  # 单 Pod 内部演示环境使用与登录页一致的固定管理员密码。
  # 其他数据库、JWT 和对象存储凭据仍然随机生成且不写入仓库。
  admin_password="admin123"
  mysql_root_password="$(rand_secret)"
  mysql_password="$(rand_secret)"
  minio_docs_access_key="grid$(openssl rand -hex 8)"
  minio_docs_secret_key="$(rand_secret)"
  milvus_minio_access_key="milvus$(openssl rand -hex 6)"
  milvus_minio_secret_key="$(rand_secret)"
  neo4j_password="$(rand_secret)"
  database_url="mysql+aiomysql://grid:$mysql_password@127.0.0.1:3306/grid_qa?charset=utf8mb4"
  neo4j_auth="neo4j/$neo4j_password"
  $K create secret generic term-extractor-test-grid-env \
    --from-literal=JWT_SECRET="$jwt_secret" \
    --from-literal=ADMIN_USERNAME=admin \
    --from-literal=ADMIN_PASSWORD="$admin_password" \
    --from-literal=MYSQL_ROOT_PASSWORD="$mysql_root_password" \
    --from-literal=MYSQL_PASSWORD="$mysql_password" \
    --from-literal=DATABASE_URL="$database_url" \
    --from-literal=MINIO_DOCS_ACCESS_KEY="$minio_docs_access_key" \
    --from-literal=MINIO_DOCS_SECRET_KEY="$minio_docs_secret_key" \
    --from-literal=MILVUS_MINIO_ACCESS_KEY="$milvus_minio_access_key" \
    --from-literal=MILVUS_MINIO_SECRET_KEY="$milvus_minio_secret_key" \
    --from-literal=NEO4J_PASSWORD="$neo4j_password" \
    --from-literal=NEO4J_AUTH="$neo4j_auth" >/dev/null
fi

$K create configmap term-extractor-test-grid-seed \
  --from-file=seed_kb.py="$ROOT_DIR/kb_seed/seed_kb.py" \
  --from-file=doc01.txt="$ROOT_DIR/kb_seed/故障案例-110kV_SF6断路器灭弧室气体泄漏.txt" \
  --from-file=doc02.txt="$ROOT_DIR/kb_seed/故障案例-GIS支撑绝缘子内部气泡局部放电.txt" \
  --from-file=doc03.txt="$ROOT_DIR/kb_seed/故障案例-特高压GIS_TA气室盆式绝缘子放电.txt" \
  --from-file=doc04.txt="$ROOT_DIR/kb_seed/安全规程-电力安全工作规程变电站两票与技术措施.txt" \
  --from-file=doc05.txt="$ROOT_DIR/kb_seed/操作规程-主变压器由运行转检修操作规程.txt" \
  --from-file=doc06.txt="$ROOT_DIR/kb_seed/操作规程-变电站倒闸操作规程与典型操作票.txt" \
  --from-file=doc07.txt="$ROOT_DIR/kb_seed/运维手册-SF6断路器检修维护规程.txt" \
  --from-file=doc08.txt="$ROOT_DIR/kb_seed/运维手册-继电保护装置运行维护规程.txt" \
  --dry-run=client -o yaml | $K apply -f -
$K create configmap term-extractor-test-grid-verify \
  --from-file=verify.py="$DEPLOY_DIR/verify.py" \
  --dry-run=client -o yaml | $K apply -f -

MUTATED=1
render "$DEPLOY_DIR/deployment.yaml" | $K replace -f -
$K rollout status "deployment/$DEPLOYMENT" --timeout=30m

$K delete job term-extractor-test-grid-seed --ignore-not-found >/dev/null
render "$DEPLOY_DIR/seed-job.yaml" | $K apply -f -
wait_job term-extractor-test-grid-seed 30m
$K logs job/term-extractor-test-grid-seed --tail=1000

pod="$($K get pod -l workload.user.cattle.io/workloadselector=apps.deployment-nmt-llm-term-extractor-test -o jsonpath='{.items[0].metadata.name}')"
[ "$($K get pod "$pod" -o json | jq '[.status.containerStatuses[] | select(.ready==true)] | length')" = "10" ] || fail "并非 10/10 containers Ready"
[ "$($K get endpoints term-extractor-test -o json | jq '.subsets[0].addresses | length')" -gt 0 ] || fail "Service Endpoint 为空"
[ "$($K get deployment "$DEPLOYMENT" -o jsonpath='{.status.readyReplicas}')" = "1" ] || fail "Deployment 非 1/1"

observed_images="$($K get deployment "$DEPLOYMENT" -o jsonpath='{range .spec.template.spec.containers[*]}{.name}{"="}{.image}{"\n"}{end}')"
printf '%s\n' "$observed_images" | grep -F "backend=$BACKEND_IMAGE" >/dev/null || fail "backend digest 未固定"
printf '%s\n' "$observed_images" | grep -F "frontend=$FRONTEND_IMAGE" >/dev/null || fail "frontend digest 未固定"
printf '%s\n' "$observed_images" | grep -F "mysql=$MYSQL_IMAGE" >/dev/null || fail "mysql digest 未固定"
printf '%s\n' "$observed_images" | grep -F "redis=$REDIS_IMAGE" >/dev/null || fail "redis digest 未固定"
printf '%s\n' "$observed_images" | grep -F "minio-docs=$MINIO_IMAGE" >/dev/null || fail "minio digest 未固定"
printf '%s\n' "$observed_images" | grep -F "etcd=$ETCD_IMAGE" >/dev/null || fail "etcd digest 未固定"
printf '%s\n' "$observed_images" | grep -F "milvus=$MILVUS_IMAGE" >/dev/null || fail "milvus digest 未固定"
printf '%s\n' "$observed_images" | grep -F "neo4j=$NEO4J_IMAGE" >/dev/null || fail "neo4j digest 未固定"
printf '%s\n' "$observed_images" | grep -F "ollama=$OLLAMA_IMAGE" >/dev/null || fail "ollama digest 未固定"

$K delete job term-extractor-test-grid-verify --ignore-not-found >/dev/null
render "$DEPLOY_DIR/verify-job.yaml" | $K apply -f -
wait_job term-extractor-test-grid-verify 20m
$K logs job/term-extractor-test-grid-verify --tail=1000

old_pod="$pod"
$K delete pod "$old_pod" --wait=false >/dev/null
$K rollout status "deployment/$DEPLOYMENT" --timeout=30m
new_pod="$($K get pod -l workload.user.cattle.io/workloadselector=apps.deployment-nmt-llm-term-extractor-test -o jsonpath='{.items[0].metadata.name}')"
[ "$new_pod" != "$old_pod" ] || fail "Recreate 重启未产生新 Pod"
$K exec "$new_pod" -c ollama -- ollama show qwen2.5:1.5b-instruct-q4_K_M >/dev/null

$K delete job term-extractor-test-grid-seed --ignore-not-found >/dev/null
render "$DEPLOY_DIR/seed-job.yaml" | $K apply -f -
wait_job term-extractor-test-grid-seed 15m
$K logs job/term-extractor-test-grid-seed --tail=1000 | grep -c '\[跳过\]' | grep -q '^8$' || fail "重启后 Seed 幂等/8 文档持久化验证失败"
$K delete job term-extractor-test-grid-verify --ignore-not-found >/dev/null
render "$DEPLOY_DIR/verify-job.yaml" | $K apply -f -
wait_job term-extractor-test-grid-verify 20m
$K logs job/term-extractor-test-grid-verify --tail=1000

trap - EXIT INT TERM
printf '%s\n' "SUCCESS: $EXPECTED_CONTEXT/$NAMESPACE deployment/$DEPLOYMENT 1/1，10 容器 Ready，镜像 digest 已固定，8 文档已持久化。"
printf '%s\n' "snapshot=$SNAPSHOT"
