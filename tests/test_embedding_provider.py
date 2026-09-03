"""adapters/embedding 单测（零网络）：chat/embedding 异源配置、批量分片、集合名回归。

运行：PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe -m pytest tests/test_embedding_provider.py -v
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.adapters import embedding as emb_mod
from src.adapters.embedding import DashScopeEmbeddingFunction, embed_texts, get_client
from src.config import config


class _RecordingClient:
    """记录构建参数与每次 embeddings.create 调用；返回批内顺序向量。"""

    def __init__(self, *, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url
        self.create_calls: list[dict] = []

    @property
    def embeddings(self):
        return self

    def create(self, **kwargs):
        self.create_calls.append(kwargs)
        n = len(kwargs["input"])
        data = [type("D", (), {"index": i, "embedding": [float(i)]})() for i in range(n)]
        return type("Resp", (), {"data": data})()


class _ClientFactory:
    """替换模块里的 OpenAI 构造器，暴露最近一次实例供断言。"""

    def __init__(self):
        self.last: _RecordingClient | None = None

    def __call__(self, *, api_key: str, base_url: str):
        self.last = _RecordingClient(api_key=api_key, base_url=base_url)
        return self.last


@pytest.fixture(autouse=True)
def _reset_client_cache(monkeypatch):
    """每个用例前清空 client 缓存，避免跨用例复用。"""
    monkeypatch.setattr(emb_mod, "_client", None)
    monkeypatch.setattr(emb_mod, "_client_cfg", None)


# ============ 异源配置：EMBEDDING_* 覆盖 / 回落 OPENAI_* ============


def test_config_embedding_defaults_fallback_to_openai():
    """未设 EMBEDDING_* 时，config 静态回落 OPENAI_*（与旧行为一致，零配置变更）。

    回落发生在 config 定义时刻（import 时快照），故在此直接对公式断言；
    环境未独立设 EMBEDDING_* 时再加强断言二者与 OPENAI_* 快照一致。
    """
    assert (os.environ.get("EMBEDDING_API_KEY") or os.environ.get("OPENAI_API_KEY", "")) == config.EMBEDDING_API_KEY
    assert (
        os.environ.get("EMBEDDING_BASE_URL") or os.environ.get("OPENAI_BASE_URL", "")
    ) == config.EMBEDDING_BASE_URL
    if "EMBEDDING_API_KEY" not in os.environ:
        assert config.EMBEDDING_API_KEY == config.OPENAI_API_KEY
    if "EMBEDDING_BASE_URL" not in os.environ:
        assert config.EMBEDDING_BASE_URL == config.OPENAI_BASE_URL


def test_embedding_overrides_openai_when_set(monkeypatch):
    """EMBEDDING_* 设了就走独立端点，chat 与 embedding 可异源。"""
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-chat")
    monkeypatch.setattr(config, "OPENAI_BASE_URL", "https://chat.example.com/v1")
    monkeypatch.setattr(config, "EMBEDDING_API_KEY", "sk-embed")
    monkeypatch.setattr(config, "EMBEDDING_BASE_URL", "https://embed.example.com/v1")
    factory = _ClientFactory()
    monkeypatch.setattr(emb_mod, "OpenAI", factory)

    client = get_client()

    assert client.api_key == "sk-embed"
    assert client.base_url == "https://embed.example.com/v1"


def test_client_rebuilt_when_embedding_config_changes(monkeypatch):
    """缓存按端点失效：配置变化重建 client，未变化则复用。"""
    monkeypatch.setattr(config, "EMBEDDING_API_KEY", "k1")
    monkeypatch.setattr(config, "EMBEDDING_BASE_URL", "https://a.example.com/v1")
    factory = _ClientFactory()
    monkeypatch.setattr(emb_mod, "OpenAI", factory)

    c1 = get_client()
    assert factory.last is c1

    monkeypatch.setattr(config, "EMBEDDING_BASE_URL", "https://b.example.com/v1")
    c2 = get_client()
    assert c2 is not c1
    assert c2.base_url == "https://b.example.com/v1"

    c3 = get_client()  # 配置未再变 → 复用
    assert c3 is c2


# ============ 批量分片 ============


def test_embed_texts_splits_by_configured_batch_size(monkeypatch):
    """按 EMBEDDING_BATCH_SIZE 分片调用 embeddings.create，结果按输入顺序。"""
    monkeypatch.setattr(config, "EMBEDDING_BATCH_SIZE", 2)
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-v3")
    factory = _ClientFactory()
    monkeypatch.setattr(emb_mod, "OpenAI", factory)

    out = embed_texts([f"t{i}" for i in range(5)])

    assert len(out) == 5  # 全部返回且顺序保持
    client = factory.last
    assert [len(c["input"]) for c in client.create_calls] == [2, 2, 1]
    assert all(c["model"] == "text-embedding-v3" for c in client.create_calls)


# ============ name() 集合标识回归 ============


def test_name_keeps_dashscope_prefix_on_default_host(monkeypatch):
    """默认百炼端点 → 名称与历史索引逐字节一致（存量零迁移）。"""
    monkeypatch.setattr(
        config, "EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-v3")

    assert DashScopeEmbeddingFunction.name() == "dashscope-text-embedding-v3"


def test_name_changes_with_host_and_model(monkeypatch):
    """切换端点/模型后名称自然变化，Chroma 防呆提示重建。"""
    monkeypatch.setattr(config, "EMBEDDING_BASE_URL", "https://openai.example.com/v1")
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-3-small")

    assert DashScopeEmbeddingFunction.name() == "openai-text-embedding-3-small"


def test_name_falls_back_to_openai_when_base_url_empty(monkeypatch):
    """空 base_url → openai SDK 默认官方端点，标识为 openai 前缀。"""
    monkeypatch.setattr(config, "EMBEDDING_BASE_URL", "")
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-v3")

    assert DashScopeEmbeddingFunction.name() == "openai-text-embedding-v3"


# ============ 边界行为与 Chroma 协议方法 ============


def test_embed_texts_empty_input_returns_without_client(monkeypatch):
    """空输入直接返回 []，不触发任何 OpenAI 构造 / 请求（旧行为回归）。"""
    factory = _ClientFactory()
    monkeypatch.setattr(emb_mod, "OpenAI", factory)

    assert embed_texts([]) == []
    assert factory.last is None  # 未创建 client → 零网络


def test_call_normalizes_single_string(monkeypatch):
    """Chroma 以单字符串传入时统一规整为单元素列表再嵌入。"""
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-v3")
    factory = _ClientFactory()
    monkeypatch.setattr(emb_mod, "OpenAI", factory)

    out = DashScopeEmbeddingFunction()("hello")

    assert len(out) == 1
    assert factory.last.create_calls[0]["input"] == ["hello"]


def test_get_config_and_build_from_config_roundtrip(monkeypatch):
    """get_config 返回可序列化配置（含 model + 端点、无密钥），build_from_config 可重建。"""
    monkeypatch.setattr(config, "EMBEDDING_MODEL", "text-embedding-v3")
    monkeypatch.setattr(
        config, "EMBEDDING_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    ef = DashScopeEmbeddingFunction()

    cfg = ef.get_config()
    assert cfg == {
        "model": "text-embedding-v3",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }
    rebuilt = DashScopeEmbeddingFunction.build_from_config(cfg)
    assert isinstance(rebuilt, DashScopeEmbeddingFunction)
    assert rebuilt.name() == ef.name()  # 重建后集合标识一致
