#!/usr/bin/env bash
# 在 grid-backend 容器内跑测试（宿主机不需要装后端依赖），与在线数据隔离：
#   MySQL → grid_qa_test 库（首次自动建表）；Redis → 15 号库；Neo4j 用例自带随机前缀并清理。
# 注意：不要直接用容器默认环境跑测试——部分集成测试会写入 pending 证据缺口，
# 在线后端的证据补全定时任务会把它们转成 FAQ 文档入库（2026-09-27 实际发生过）。
#
# 用法：scripts/test_in_container.sh [pytest 参数...]
#   scripts/test_in_container.sh                       # 全量（不含 tests/redteam）
#   scripts/test_in_container.sh tests/test_kg_provenance.py -q
set -euo pipefail
cd "$(dirname "$0")/.."
C=${BACKEND_CONTAINER:-grid-backend}
TEST_DB_URL=${TEST_DATABASE_URL:-mysql+aiomysql://grid:grid123456@mysql:3306/grid_qa_test?charset=utf8mb4}
TEST_REDIS_URL=${TEST_REDIS_URL:-redis://:grid-langfuse-pass@redis:6379/15}

docker exec "$C" sh -c 'rm -rf /tmp/gt && mkdir -p /tmp/gt'
docker cp backend/app "$C":/tmp/gt/app
docker cp backend/domain_packs "$C":/tmp/gt/domain_packs
docker cp backend/scorer_thresholds "$C":/tmp/gt/scorer_thresholds
docker cp tests "$C":/tmp/gt/tests
docker cp scripts "$C":/tmp/gt/scripts

# DOMAIN_PACK 固定为默认值 general：用例不应依赖当前部署的 .env（需要电网包的用例用 grid_pack 夹具）
ENVS=(-e PYTHONPATH=/tmp/gt:/app -e DATABASE_URL="$TEST_DB_URL" -e REDIS_URL="$TEST_REDIS_URL"
      -e EVIDENCE_GAP_DEEP_INTERVAL=0 -e DOMAIN_PACK=general)
# 先 import app.main：部分模型（evidence_gap、rewrite_event 等）只经路由间接 import，init_db 自身不 import
docker exec -w /tmp/gt "${ENVS[@]}" "$C" python -c \
  "import asyncio, app.main; from app.db.init_db import init_db; asyncio.run(init_db())" >/dev/null 2>&1

if [ $# -eq 0 ]; then
  set -- tests -q --ignore=tests/redteam
fi
# pytest 参数里的 tests/... 路径对应容器内 /tmp/gt/tests/...
docker exec -w /tmp/gt "${ENVS[@]}" "$C" python -m pytest -p no:cacheprovider "$@"
