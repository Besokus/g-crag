"""init_db 必须注册 app/models 下的全部表（全新部署 create_all 才能建齐）。"""
import pkgutil
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"


def test_init_db_registers_every_model_table():
    # 新进程里只 import init_db，避免被本进程里其他用例已 import 的模型掩盖
    code = (
        "import app.db.init_db, sys\n"
        "from app.db.base import Base\n"
        "need = {'evidence_gap', 'rewrite_event', 'kg_triples', 'qa_trace', 'chunks', 'documents'}\n"
        "missing = need - set(Base.metadata.tables)\n"
        "print(sorted(missing)); sys.exit(1 if missing else 0)\n"
    )
    import app  # 与本进程使用同一份 backend 代码
    root = str(Path(app.__file__).resolve().parents[1])
    r = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True,
                       env={"PYTHONPATH": root, "PATH": "/usr/local/bin:/usr/bin:/bin"})
    assert r.returncode == 0, r.stdout + r.stderr[-2000:]
