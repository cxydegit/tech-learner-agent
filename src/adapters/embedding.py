"""Embedding 封装：DashScope text-embedding-v3 + Chroma EmbeddingFunction。

后端走阿里云百炼的 OpenAI 兼容端点（同 OPENAI_BASE_URL），因此零新依赖——
直接用现有 openai 客户端的 embeddings API，复用 .env 里的 API Key。
"""

from __future__ import annotations

from typing import Sequence

from openai import OpenAI
from chromadb.api.types import Documents, EmbeddingFunction

from ..config import config

# 百炼 embeddings 单次请求上限为 10，多于此会报 InvalidParameter
_BATCH_SIZE = 10

_client: OpenAI | None = None


def get_client() -> OpenAI:
    """懒加载并复用 openai 客户端（同一 base_url / api_key）。"""
    global _client
    if _client is None:
        _client = OpenAI(
            api_key=config.OPENAI_API_KEY,
            base_url=config.OPENAI_BASE_URL,
        )
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

    vectors: list[list[float]] = []
    for i in range(0, len(texts), _BATCH_SIZE):
        batch = texts[i : i + _BATCH_SIZE]
        resp = get_client().embeddings.create(model=config.EMBEDDING_MODEL, input=batch)
        # 按 index 排序，确保与输入顺序一致（服务端可能乱序返回）
        vectors.extend(
            d.embedding for d in sorted(resp.data, key=lambda x: x.index)
        )
    return vectors


class DashScopeEmbeddingFunction(EmbeddingFunction[Documents]):
    """把 DashScope embedding 适配成 Chroma 的 EmbeddingFunction。

    用法：``get_or_create_collection(..., embedding_function=DashScopeEmbeddingFunction())``。

    继承 ``chromadb.api.types.EmbeddingFunction`` 以补齐 Chroma 1.x 要求的
    ``name()`` / ``embed_query()`` / ``embed_documents()`` 等协议方法。
    text-embedding-v3 对查询与文档不做区分，故 query 复用 __call__ 即可。
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

        名称含模型名：换 embedding 模型会导致集合维度/配置不匹配，应重建索引。
        """
        return f"dashscope-{config.EMBEDDING_MODEL}"
