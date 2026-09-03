"""Embedding 封装：OpenAI 兼容 embeddings + Chroma EmbeddingFunction。

后端走 OpenAI 兼容端点（默认阿里云百炼，同 OPENAI_BASE_URL；可设
EMBEDDING_BASE_URL/API_KEY 独立指向别家），因此零新依赖——直接用现有
openai 客户端的 embeddings API。类名 DashScopeEmbeddingFunction 为历史
遗留（默认端点是百炼），先不动。
"""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import urlparse

from chromadb.api.types import Documents, EmbeddingFunction
from openai import OpenAI

from ..config import config

_client: OpenAI | None = None
# 记录 client 构建时的端点配置；配置变化时重建 client（测试可干净切换端点）
_client_cfg: tuple[str, str] | None = None


def _host_label(base_url: str) -> str:
    """从端点 URL 提取集合标识前缀（dashscope.aliyuncs.com → dashscope）。"""
    host = urlparse(base_url).hostname or ""
    if not host:
        return "openai"  # 空 base_url → openai SDK 默认官方端点
    return host.split(".")[0]


def get_client() -> OpenAI:
    """懒加载并复用 openai 客户端（按 base_url / api_key 缓存，配置变了自动重建）。"""
    global _client, _client_cfg
    cfg = (config.EMBEDDING_BASE_URL, config.EMBEDDING_API_KEY)
    if _client is None or _client_cfg != cfg:
        _client = OpenAI(api_key=cfg[1], base_url=cfg[0])
        _client_cfg = cfg
    return _client


def embed_texts(texts: list[str]) -> list[list[float]]:
    """批量将文本嵌入为向量。

    Args:
        texts: 待嵌入的文本列表

    Returns:
        list[list[float]]: 与输入顺序一致的向量列表；空输入返回 []
    """
    if not texts:
        return []

    batch_size = config.EMBEDDING_BATCH_SIZE
    vectors: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        resp = get_client().embeddings.create(model=config.EMBEDDING_MODEL, input=batch)
        # 按 index 排序，确保与输入顺序一致（服务端可能乱序返回）
        vectors.extend(
            d.embedding for d in sorted(resp.data, key=lambda x: x.index)
        )
    return vectors


class DashScopeEmbeddingFunction(EmbeddingFunction[Documents]):
    """把 OpenAI 兼容 embedding 适配成 Chroma 的 EmbeddingFunction（默认后端百炼 DashScope）。

    用法：``get_or_create_collection(..., embedding_function=DashScopeEmbeddingFunction())``。

    继承 ``chromadb.api.types.EmbeddingFunction`` 以补齐 Chroma 1.x 要求的
    ``name()`` / ``get_config()`` / ``build_from_config()`` 等协议方法。
    text-embedding-v3 对查询与文档不做区分，故 query 复用 __call__ 即可。
    类名 DashScopeEmbeddingFunction 为历史遗留，见模块 docstring。
    """

    def __init__(self) -> None:
        """无参构造，保证可被 Chroma 序列化。"""

    def __call__(self, input: Sequence[str]) -> list[list[float]]:
        """Chroma 调用入口。"""
        # Chroma 可能以 list / numpy 数组 / 单个字符串传入，统一规整为 list[str]
        texts = list(input) if not isinstance(input, str) else [input]
        return embed_texts([str(t) for t in texts])

    @staticmethod
    def name() -> str:
        """返回该 EmbeddingFunction 的唯一名称（Chroma 1.x 用它做集合配置校验）。

        名称 = {端点 host 前缀}-{模型}。默认百炼端点下仍是
        ``dashscope-text-embedding-v3``，与既有 .chroma/ 索引逐字节一致（存量零迁移）；
        切换 embedding 端点/模型后名称自然变化，Chroma 会拒绝用错配的
        embedding function 打开旧集合——此时删 .chroma/ 重建索引即可。
        """
        return f"{_host_label(config.EMBEDDING_BASE_URL)}-{config.EMBEDDING_MODEL}"

    def get_config(self) -> dict:
        """返回可序列化配置（Chroma 1.x 集合配置校验用，未来版本强制要求）。

        与 name() 同源（model + 端点），供 Chroma 持久化与将来
        ``build_from_config()`` 重建实例；不含密钥。
        """
        return {
            "model": config.EMBEDDING_MODEL,
            "base_url": config.EMBEDDING_BASE_URL,
        }

    @staticmethod
    def build_from_config(config: dict) -> DashScopeEmbeddingFunction:
        """从序列化配置重建实例（配置真实来源是环境变量，返回无参实例即可）。"""
        return DashScopeEmbeddingFunction()
