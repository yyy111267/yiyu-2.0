"""认知原子仓库 —— SQLite 持久化 + 向量 + 关键词混合检索。

严格租户隔离：所有查询带 user_id。
混合检索：向量 cosine（语义近）+ 关键词 LIKE（精确匹配）去重融合。
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Optional

import numpy as np
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from store.models import Base, CognitionAtom, UserCase
from store.embedding import TfidfEmbedder, get_embedder

logger = logging.getLogger(__name__)


class CognitionRepo:
    """认知原子 + 用户案例 的持久化与检索。

    用法：
        repo = CognitionRepo("sqlite:///yiyu_agent.db")
        repo.upsert_atom(user_id="u1", statement="高毛利率等于强护城河", ...)
        hits = repo.search("护城河", user_id="u1", top_k=5, mode="hybrid")
    """

    def __init__(self, db_url: str = "sqlite:///yiyu_agent.db",
                 embedder=None):
        from sqlalchemy import create_engine
        self.engine = create_engine(db_url, echo=False, future=True)
        Base.metadata.create_all(self.engine)
        self._Session = sessionmaker(self.engine, expire_on_commit=False)
        self.embedder = embedder or get_embedder("tfidf")
        # 向量索引：{atom_id: np.ndarray}，懒加载
        self._vec_index: dict[str, np.ndarray] | None = None
        self._vec_fitted = False

    # ── 认知原子 CRUD ──────────────────────────────────────────

    def upsert_atom(
        self, *, user_id: str, statement: str, category: str = "",
        scope: str = "", basis: str = "", confidence: str = "medium",
        status: str = "candidate", supporting_evidence: list | None = None,
        counter_evidence: list | None = None, related_cases: list | None = None,
        source: str = "user_stated", atom_id: str | None = None,
    ) -> dict:
        """新增或更新认知原子。返回 {id, created}。"""
        aid = atom_id or f"ca_{uuid.uuid4().hex[:12]}"
        now = datetime.utcnow()
        with self._Session() as db:
            existing = db.get(CognitionAtom, aid)
            if existing:
                existing.statement = statement
                existing.category = category or existing.category
                existing.scope = scope or existing.scope
                existing.basis = basis or existing.basis
                existing.confidence = confidence or existing.confidence
                existing.status = status or existing.status
                if supporting_evidence is not None:
                    existing.supporting_evidence = supporting_evidence
                if counter_evidence is not None:
                    existing.counter_evidence = counter_evidence
                if related_cases is not None:
                    existing.related_cases = related_cases
                existing.source = source or existing.source
                existing.version += 1
                existing.is_revised = True
                existing.updated_at = now
                db.commit()
                self._vec_index = None  # 失效缓存
                return {"id": aid, "created": False}
            atom = CognitionAtom(
                id=aid, user_id=user_id, statement=statement, category=category,
                scope=scope, basis=basis, confidence=confidence, status=status,
                supporting_evidence=supporting_evidence or [],
                counter_evidence=counter_evidence or [],
                related_cases=related_cases or [],
                source=source, version=1, first_seen_at=now, updated_at=now,
            )
            db.add(atom)
            db.commit()
            self._vec_index = None
            return {"id": aid, "created": True}

    def get_atom(self, atom_id: str) -> dict | None:
        with self._Session() as db:
            a = db.get(CognitionAtom, atom_id)
            return _atom_to_dict(a) if a else None

    def list_atoms(self, user_id: str, status: str | None = None,
                   limit: int = 200) -> list[dict]:
        with self._Session() as db:
            stmt = select(CognitionAtom).where(CognitionAtom.user_id == user_id)
            if status:
                stmt = stmt.where(CognitionAtom.status == status)
            stmt = stmt.order_by(CognitionAtom.updated_at.desc()).limit(limit)
            return [_atom_to_dict(a) for a in db.scalars(stmt)]

    def revise(self, atom_id: str, new_status: str, note: str = "") -> bool:
        """修正认知原子状态（confirmed/contested/superseded）。"""
        with self._Session() as db:
            a = db.get(CognitionAtom, atom_id)
            if not a:
                return False
            a.status = new_status
            a.is_revised = True
            a.updated_at = datetime.utcnow()
            if note:
                ev = list(a.counter_evidence or [])
                ev.append({"note": note, "at": a.updated_at.isoformat()})
                a.counter_evidence = ev
            db.commit()
            return True

    # ── 检索 ──────────────────────────────────────────────────

    def search(self, query: str, *, user_id: str, top_k: int = 5,
               mode: str = "hybrid") -> list[dict]:
        """混合检索用户认知原子。

        mode:
          keyword — SQL LIKE 精确匹配
          vector  — 向量 cosine 语义近
          hybrid  — 两者融合去重（默认）
        严格租户隔离：只返回 user_id 名下的原子。
        """
        if mode not in ("keyword", "vector", "hybrid"):
            mode = "hybrid"
        results: list[dict] = []
        if mode in ("keyword", "hybrid"):
            results.extend(self._keyword_search(query, user_id, top_k))
        if mode in ("vector", "hybrid"):
            results.extend(self._vector_search(query, user_id, top_k))
        # 去重 + 按 score 降序
        seen: dict[str, dict] = {}
        for r in results:
            aid = r["id"]
            if aid not in seen or r["_score"] > seen[aid]["_score"]:
                seen[aid] = r
        out = sorted(seen.values(), key=lambda x: x["_score"], reverse=True)
        return out[:top_k]

    def _keyword_search(self, query: str, user_id: str, top_k: int) -> list[dict]:
        """SQL LIKE 关键词检索（statement/scope/category/basis 四字段）。"""
        pat = f"%{query}%" if len(query) >= 2 else None
        with self._Session() as db:
            stmt = select(CognitionAtom).where(CognitionAtom.user_id == user_id)
            if pat:
                stmt = stmt.where(
                    CognitionAtom.statement.like(pat)
                    | CognitionAtom.scope.like(pat)
                    | CognitionAtom.category.like(pat)
                    | CognitionAtom.basis.like(pat)
                )
            stmt = stmt.order_by(CognitionAtom.updated_at.desc()).limit(top_k * 2)
            out = []
            for a in db.scalars(stmt):
                d = _atom_to_dict(a)
                # 关键词命中数作为 score
                hits = sum(1 for f in (a.statement, a.scope, a.category, a.basis)
                           if query and query in (f or ""))
                d["_score"] = 0.5 + 0.1 * hits  # 关键词基线 0.5
                d["_match"] = "keyword"
                out.append(d)
            return out

    def _vector_search(self, query: str, user_id: str, top_k: int) -> list[dict]:
        """向量 cosine 检索。"""
        atoms = self.list_atoms(user_id, limit=500)
        if not atoms:
            return []
        self._ensure_vec_index(atoms)
        if not self._vec_index:
            return []
        q_vec = self._embed_query(query)
        if q_vec is None:
            return []
        scores: list[tuple[str, float]] = []
        for aid, vec in self._vec_index.items():
            sim = float(np.dot(q_vec, vec))
            scores.append((aid, sim))
        scores.sort(key=lambda x: x[1], reverse=True)
        by_id = {a["id"]: a for a in atoms}
        out = []
        for aid, sim in scores[:top_k]:
            if aid in by_id and sim > 0.05:
                d = dict(by_id[aid])
                d["_score"] = sim
                d["_match"] = "vector"
                out.append(d)
        return out

    def _ensure_vec_index(self, atoms: list[dict]) -> None:
        """构建/刷新向量索引（懒加载，atoms 变动后自动重建）。"""
        if self._vec_fitted and self._vec_index is not None:
            return
        corpus = [a["statement"] for a in atoms]
        if isinstance(self.embedder, TfidfEmbedder):
            self.embedder.fit(corpus + [""])  # 拟合 IDF
        vecs = self.embedder.embed(corpus)
        self._vec_index = {a["id"]: vecs[i] for i, a in enumerate(atoms)}
        self._vec_fitted = True

    def _embed_query(self, query: str) -> np.ndarray | None:
        if isinstance(self.embedder, TfidfEmbedder) and not self.embedder._vocab:
            # 未拟合（空库）——单 query 拟合
            self.embedder.fit([query])
        vec = self.embedder.embed([query])
        if vec.shape[0] == 0:
            return None
        return vec[0]

    # ── 用户案例 ──────────────────────────────────────────────

    def add_case(self, *, user_id: str, symbol: str, thesis: dict,
                 key_assumptions: list | None = None,
                 cognition_atom_ids: list | None = None) -> str:
        cid = f"uc_{uuid.uuid4().hex[:12]}"
        with self._Session() as db:
            db.add(UserCase(
                id=cid, user_id=user_id, symbol=symbol, thesis=thesis,
                key_assumptions=key_assumptions or [],
                cognition_atom_ids=cognition_atom_ids or [],
                outcome="pending",
            ))
            db.commit()
        return cid

    def resolve_case(self, case_id: str, outcome: str, lessons: list[str]) -> bool:
        with self._Session() as db:
            c = db.get(UserCase, case_id)
            if not c:
                return False
            c.outcome = outcome
            c.lessons = lessons
            c.resolved_at = datetime.utcnow()
            db.commit()
            return True

    def list_cases(self, user_id: str, symbol: str | None = None,
                   limit: int = 50) -> list[dict]:
        with self._Session() as db:
            stmt = select(UserCase).where(UserCase.user_id == user_id)
            if symbol:
                stmt = stmt.where(UserCase.symbol == symbol)
            stmt = stmt.order_by(UserCase.created_at.desc()).limit(limit)
            return [_case_to_dict(c) for c in db.scalars(stmt)]


def _atom_to_dict(a: CognitionAtom) -> dict:
    return {
        "id": a.id, "user_id": a.user_id, "statement": a.statement,
        "category": a.category, "scope": a.scope, "basis": a.basis,
        "confidence": a.confidence, "status": a.status, "is_revised": a.is_revised,
        "supporting_evidence": a.supporting_evidence or [],
        "counter_evidence": a.counter_evidence or [],
        "related_cases": a.related_cases or [],
        "source": a.source, "version": a.version,
        "first_seen_at": a.first_seen_at.isoformat() if a.first_seen_at else None,
        "updated_at": a.updated_at.isoformat() if a.updated_at else None,
    }


def _case_to_dict(c: UserCase) -> dict:
    return {
        "id": c.id, "user_id": c.user_id, "symbol": c.symbol,
        "thesis": c.thesis or {}, "key_assumptions": c.key_assumptions or [],
        "outcome": c.outcome, "lessons": c.lessons or [],
        "cognition_atom_ids": c.cognition_atom_ids or [],
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
    }
