"""向量化后端 —— 可插拔设计。

默认用 TfidfEmbedder（numpy，零外部依赖，对中文短文本可用）。
装上 chromadb 后可一行切换到 ChromaEmbedder（语义更准）。

接口约定：embed(texts) -> np.ndarray (n, dim)，L2 归一化。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Protocol

import numpy as np

logger = logging.getLogger(__name__)


class EmbeddingProvider(Protocol):
    """向量化后端协议。"""
    def embed(self, texts: list[str]) -> np.ndarray: ...
    @property
    def dim(self) -> int: ...


def _char_ngrams(text: str, n_range: tuple[int, int] = (2, 4)) -> list[str]:
    """中文友好的 char n-gram（不依赖分词，对短文本鲁棒）。"""
    text = re.sub(r"\s+", "", text)
    grams: list[str] = []
    lo, hi = n_range
    for n in range(lo, hi + 1):
        if len(text) < n:
            continue
        grams.extend(text[i:i + n] for i in range(len(text) - n + 1))
    return grams


class TfidfEmbedder:
    """基于 char n-gram TF-IDF + L2 归一化的轻量向量化。

    优势：零外部依赖、对中文短文本（认知原子几十字）效果可用、确定可复现。
    局限：无语义理解，同义不同字召回弱（可后续切 chromadb/OpenAI embedding）。
    """

    def __init__(self, dim: int = 512):
        self._dim = dim
        self._idf: dict[str, float] = {}
        self._vocab: dict[str, int] = {}  # ngram -> col index

    @property
    def dim(self) -> int:
        return self._dim

    def fit(self, corpus: list[str]) -> "TfidfEmbedder":
        """在语料上拟合 IDF + 构建词表（按文档频率取 top-dim）。"""
        df: Counter[str] = Counter()
        n_docs = 0
        for text in corpus:
            grams = set(_char_ngrams(text))
            if not grams:
                continue
            n_docs += 1
            for g in grams:
                df[g] += 1
        if n_docs == 0:
            return self
        import math
        # IDF = ln(N / (df+1))，取文档频率最高的 top-dim
        top = df.most_common(self._dim)
        self._vocab = {g: i for i, (g, _) in enumerate(top)}
        self._idf = {g: math.log((n_docs + 1) / (c + 1)) + 1 for g, c in top}
        return self

    def embed(self, texts: list[str]) -> np.ndarray:
        if not self._vocab:
            # 未拟合或空语料：返回零向量（调用方需处理）
            return np.zeros((len(texts), self._dim), dtype=np.float32)
        out = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, text in enumerate(texts):
            grams = _char_ngrams(text)
            tf = Counter(g for g in grams if g in self._vocab)
            for g, c in tf.items():
                col = self._vocab[g]
                out[i, col] = c * self._idf.get(g, 1.0)
        # L2 归一化
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return out / norms


class ChromaEmbedder:
    """chromadb 后端适配（装上 chromadb 后启用）。

    用法：from store.embedding import ChromaEmbedder
    embedder = ChromaEmbedder()  # 用 chromadb 默认 onnx MiniLM
    """

    def __init__(self):
        try:
            import chromadb  # noqa: F401
            from chromadb.utils import embedding_functions  # noqa: F401
        except ImportError as e:
            raise ImportError(
                "chromadb 未安装。pip install chromadb，或用默认 TfidfEmbedder。"
            ) from e
        self._ef = embedding_functions.DefaultEmbeddingFunction()
        self._dim_val: int | None = None

    @property
    def dim(self) -> int:
        if self._dim_val is None:
            v = self._ef([("__probe__",)])
            self._dim_val = len(v[0])
        return self._dim_val

    def embed(self, texts: list[str]) -> np.ndarray:
        vecs = self._ef(texts)
        arr = np.array(vecs, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        return arr / norms


def get_embedder(prefer: str = "tfidf") -> EmbeddingProvider:
    """工厂：优先 chromadb，失败回退 TfidfEmbedder。"""
    if prefer == "chroma":
        try:
            return ChromaEmbedder()
        except ImportError:
            logger.warning("chromadb 不可用，回退 TfidfEmbedder")
    return TfidfEmbedder()
