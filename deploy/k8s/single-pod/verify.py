"""单 Pod 演示环境的页面、登录、健康、文档和 SSE 验收。"""
import json
import os

import httpx


BASE = os.getenv("GRID_QA_BASE_URL", "http://term-extractor-test:18500").rstrip("/")
USER = os.environ["ADMIN_USERNAME"]
PASSWORD = os.environ["ADMIN_PASSWORD"]
QUESTION = "SF6 断路器气体泄漏时如何处置？"
EXPECTED = {
    "故障案例-110kV_SF6断路器灭弧室气体泄漏.txt",
    "故障案例-GIS支撑绝缘子内部气泡局部放电.txt",
    "故障案例-特高压GIS_TA气室盆式绝缘子放电.txt",
    "安全规程-电力安全工作规程变电站两票与技术措施.txt",
    "操作规程-主变压器由运行转检修操作规程.txt",
    "操作规程-变电站倒闸操作规程与典型操作票.txt",
    "运维手册-SF6断路器检修维护规程.txt",
    "运维手册-继电保护装置运行维护规程.txt",
}


def main() -> None:
    timeout = httpx.Timeout(1200, connect=30)
    with httpx.Client(timeout=timeout) as client:
        page = client.get(f"{BASE}/")
        page.raise_for_status()
        assert "<!doctype html" in page.text.lower()

        health = client.get(f"{BASE}/health")
        health.raise_for_status()
        health_data = health.json()["data"]
        assert health_data["status"] == "healthy", health_data
        assert health_data["providers"]["llm"] == {
            "provider": "ollama", "keyConfigured": True,
        }
        assert health_data["providers"]["embedding"] == {
            "provider": "bge", "keyConfigured": True,
        }

        login = client.post(
            f"{BASE}/api/system/login",
            json={"username": USER, "password": PASSWORD},
        )
        login.raise_for_status()
        token = login.json()["data"]["token"]
        headers = {"Authorization": f"Bearer {token}"}

        docs = client.get(
            f"{BASE}/api/document/list", headers=headers,
            params={"page": 1, "size": 100},
        )
        docs.raise_for_status()
        items = {x["docName"]: x for x in docs.json()["data"]["list"]}
        assert EXPECTED.issubset(items), EXPECTED - set(items)
        assert all(items[name]["status"] == "vectorized" for name in EXPECTED)

        stats = client.get(f"{BASE}/api/document/stats", headers=headers)
        stats.raise_for_status()
        assert stats.json()["data"]["byStatus"].get("vectorized", 0) >= 8

        sources = []
        answer = []
        done = None
        with client.stream(
            "POST", f"{BASE}/api/qa/answer/stream?regen=true",
            headers={**headers, "Content-Type": "application/json"},
            json={"query": QUESTION, "modelType": "ollama"},
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                event = json.loads(payload)
                if event.get("type") == "meta":
                    sources = event.get("sources") or []
                elif event.get("type") == "token":
                    answer.append(event.get("content") or "")
                elif event.get("type") == "done":
                    done = event

        assert sources, "SSE 未返回引用来源"
        assert "".join(answer).strip() or (done or {}).get("annotatedAnswer", "").strip()
        assert done and done.get("modelType") == "ollama", done
        print(json.dumps({
            "page": "ok", "health": "healthy", "login": "ok",
            "vectorizedDocuments": 8, "sseSources": len(sources),
            "modelType": done["modelType"],
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
