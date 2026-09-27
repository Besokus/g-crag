"""term_service 词表来自当前领域包；后台编辑按领域包分文件持久化。"""
import pytest

from app.core import domain_pack
from app.services import term_service


@pytest.fixture
def use_pack(monkeypatch, tmp_path):
    def _use(name):
        monkeypatch.setattr(domain_pack.settings, "DOMAIN_PACK", name)
        monkeypatch.setattr(term_service, "_BACKEND_ROOT", tmp_path)   # 编辑文件写到临时目录
        domain_pack.get_pack.cache_clear()
        term_service._load_terms.cache_clear()
    yield _use
    domain_pack.get_pack.cache_clear()
    term_service._load_terms.cache_clear()


def test_general_pack_has_no_aliases(use_pack):
    use_pack("general")
    assert term_service.normalize("主变是核心设备") == "主变是核心设备"


def test_grid_pack_aliases(use_pack):
    use_pack("grid")
    assert "主变压器" in term_service.normalize("主变是核心设备")


def test_edits_persist_per_pack(use_pack, tmp_path):
    use_pack("general")
    term_service.add_term("菊厂", "华为")
    assert (tmp_path / "data" / "terms" / "general.json").exists()
    assert term_service.normalize("菊厂发布新品") == "华为发布新品"
    use_pack("grid")
    assert "菊厂" not in dict((t["alias"], t["standard"]) for t in term_service.list_terms())
