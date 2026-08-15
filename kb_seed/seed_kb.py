"""把 kb_seed/*.txt 幂等上传、解析、向量化到知识库。

docType 由文件名前缀推断（故障案例/操作规程/运维手册/安全规程）。
环境变量：GRID_QA_BASE_URL、ADMIN_USERNAME、ADMIN_PASSWORD。
"""
import asyncio
import os
from pathlib import Path

import httpx

BASE = os.getenv("GRID_QA_BASE_URL", "http://127.0.0.1:8001/api").rstrip("/")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
KB_DIR = Path(__file__).resolve().parent
PREFIX_TYPES = ("故障案例", "操作规程", "运维手册", "安全规程", "标准条文", "检修规程", "应急预案")


def doc_type_of(name: str) -> str:
    for p in PREFIX_TYPES:
        if name.startswith(p):
            return p
    return "运维手册"


def action_for(item: dict | None) -> tuple[bool, bool, bool]:
    """返回 (upload, parse, vectorize)，供 Seed 幂等决策和单元测试复用。"""
    if item is None:
        return True, True, True
    status = item.get("status")
    if status == "vectorized":
        return False, False, False
    return False, status == "pending", True


async def main():
    async with httpx.AsyncClient(timeout=300) as c:
        # Deployment 刚 Ready 时后台组件仍可能短暂收敛，最多等待 10 分钟。
        for attempt in range(120):
            try:
                r = await c.post(
                    f"{BASE}/system/login",
                    json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
                )
                r.raise_for_status()
                tok = r.json()["data"]["token"]
                break
            except Exception:
                if attempt == 119:
                    raise
                await asyncio.sleep(5)
        H = {"Authorization": f"Bearer {tok}"}

        files = sorted(KB_DIR.glob("*.txt"))
        print(f"待入库 {len(files)} 个文件")

        async def list_all() -> dict[str, dict]:
            r = await c.get(f"{BASE}/document/list", headers=H, params={"page": 1, "size": 100})
            r.raise_for_status()
            return {item["docName"]: item for item in (r.json().get("data", {}).get("list") or [])}

        existing = await list_all()
        for path in files:
            item = existing.get(path.name)
            upload, parse, vectorize = action_for(item)
            if not (upload or parse or vectorize):
                print(f"[跳过] {path.name} 已向量化")
                continue
            if upload:
                r = await c.post(
                    f"{BASE}/document/upload", headers=H,
                    files={"files": (path.name, path.read_bytes(), "text/plain")},
                    data={"docType": doc_type_of(path.name)},
                )
                r.raise_for_status()
                failures = (r.json().get("data", {}) or {}).get("failList") or []
                if failures:
                    raise RuntimeError(f"上传失败 {path.name}: {failures}")
                existing = await list_all()
                item = existing[path.name]
                print(f"[上传] {path.name}")
                _upload, parse, vectorize = action_for(item)

            doc_id = item["docId"]
            if parse:
                r = await c.post(f"{BASE}/document/parse", headers=H, json={"docIds": [doc_id]})
                r.raise_for_status()
                print(f"[解析] {path.name}")
            if vectorize:
                r = await c.post(f"{BASE}/document/vector/generate", headers=H, json={"docId": doc_id})
                r.raise_for_status()
                rd = (r.json() or {}).get("data") or {}
                print(f"[向量化] {path.name}: {rd.get('vectorCount')} 向量 / {rd.get('embeddingRoute')}")

        # 统计
        r = await c.get(f"{BASE}/document/stats", headers=H)
        r.raise_for_status()
        stats = r.json().get("data") or {}
        print("\n[stats]", stats)
        final = await list_all()
        incomplete = [p.name for p in files if (final.get(p.name) or {}).get("status") != "vectorized"]
        if incomplete:
            raise RuntimeError(f"示例文档未全部向量化: {incomplete}")


if __name__ == "__main__":
    asyncio.run(main())
