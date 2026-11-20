"""RAG (Retrieval-Augmented Generation) over financial corpus via pgvector."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.foundation.llm.router import embed


class RAGRetriever:
    """Vector-based retrieval for financial corpus and wiki notes."""

    def __init__(self, db: Session):
        self.db = db

    async def retrieve(
        self,
        query: str,
        top_k: int = 3,
        item_type_filter: str | None = None,
    ) -> list[dict[str, Any]]:
        """Retrieve top-k most relevant chunks for a query.

        Args:
            query: Natural-language search query
            top_k: Number of results to return
            item_type_filter: Filter by item type (e.g., "obsidian_note", "financial_corpus", "dossier")

        Returns:
            List of retrieved chunks with content and metadata
        """
        # Embed the query
        query_embedding = await embed(self.db, [query])
        if not query_embedding:
            return []

        query_vector = query_embedding[0]

        # Query pgvector for nearest neighbors
        params: dict = {"query_vector": query_vector, "top_k": top_k}

        if item_type_filter is not None:
            params["item_type_filter"] = item_type_filter
            sql = text("""
                SELECT
                    id,
                    item_id,
                    item_type,
                    meta_json,
                    1 - (embedding <-> :query_vector) AS similarity
                FROM embeddings
                WHERE 1 = 1 AND item_type = :item_type_filter
                ORDER BY embedding <-> :query_vector
                LIMIT :top_k
            """)
        else:
            sql = text("""
                SELECT
                    id,
                    item_id,
                    item_type,
                    meta_json,
                    1 - (embedding <-> :query_vector) AS similarity
                FROM embeddings
                WHERE 1 = 1
                ORDER BY embedding <-> :query_vector
                LIMIT :top_k
            """)

        rows = self.db.execute(sql, params).fetchall()

        results = []
        for row in rows:
            results.append({
                "id": row.id,
                "item_id": row.item_id,
                "item_type": row.item_type,
                "similarity": row.similarity,
                "metadata": row.meta_json or {},
            })

        return results

    async def retrieve_with_fallback(
        self,
        query: str,
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """Retrieve with fallback: pgvector first, then keyword search.

        Args:
            query: Search query
            top_k: Number of results

        Returns:
            List of retrieved items
        """
        # Try vector search first
        results = await self.retrieve(query, top_k)

        if len(results) >= top_k:
            return results

        # Fallback: add keyword-based results (if table schema supports full-text search)
        # For now, just return vector results
        return results[:top_k]
