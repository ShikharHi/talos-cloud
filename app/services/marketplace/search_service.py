"""
Talos Cloud — Weaviate Cloud Search Service for Marketplace.

Architecture:
  Neon DB  = Authoritative Source of Truth
  Weaviate = Derived Search Index for Hybrid (Semantic + Lexical) & Filtered Discovery

This service is fully idempotent. If Weaviate is temporarily unreachable or not configured,
search falls back gracefully to authoritative SQL lexical search so marketplace discovery
never goes down.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Optional
import httpx

from app.config import get_settings

logger = logging.getLogger("talos.marketplace.search")


class WeaviateSearchService:
    """
    Manages indexing and hybrid searching of marketplace items in Weaviate Cloud.
    """

    def __init__(self):
        settings = get_settings()
        self.url = (settings.weaviate_url or "").rstrip("/")
        self.api_key = settings.weaviate_api_key
        self.class_name = settings.weaviate_class_name or "MarketplaceItem"

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.api_key)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def ensure_schema(self) -> bool:
        """
        Ensures the collection/class schema exists in Weaviate Cloud.
        """
        if not self.is_configured:
            return False

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.get(f"{self.url}/v1/schema/{self.class_name}", headers=self._headers())
                if res.status_code == 200:
                    return True
                if res.status_code == 404:
                    # Create class schema
                    schema_payload = {
                        "class": self.class_name,
                        "description": "Normalized marketplace item for hybrid search",
                        "vectorizer": "text2vec-openai",
                        "properties": [
                            {"name": "item_id", "dataType": ["text"], "indexFilterable": True, "indexSearchable": False},
                            {"name": "name", "dataType": ["text"], "indexFilterable": True, "indexSearchable": True},
                            {"name": "slug", "dataType": ["text"], "indexFilterable": True, "indexSearchable": True},
                            {"name": "publisher", "dataType": ["text"], "indexFilterable": True, "indexSearchable": True},
                            {"name": "kind", "dataType": ["text"], "indexFilterable": True, "indexSearchable": True},
                            {"name": "tagline", "dataType": ["text"], "indexSearchable": True},
                            {"name": "description", "dataType": ["text"], "indexSearchable": True},
                            {"name": "searchable_text", "dataType": ["text"], "indexSearchable": True},
                            {"name": "tags", "dataType": ["text[]"], "indexFilterable": True, "indexSearchable": True},
                            {"name": "capabilities", "dataType": ["text[]"], "indexFilterable": True, "indexSearchable": True},
                            {"name": "auth_types", "dataType": ["text[]"], "indexFilterable": True, "indexSearchable": True},
                            {"name": "verified", "dataType": ["boolean"], "indexFilterable": True},
                            {"name": "pricing_type", "dataType": ["text"], "indexFilterable": True},
                            {"name": "price_credits", "dataType": ["int"], "indexFilterable": True},
                            {"name": "install_count", "dataType": ["int"], "indexFilterable": True},
                            {"name": "status", "dataType": ["text"], "indexFilterable": True},
                        ],
                    }
                    create_res = await client.post(
                        f"{self.url}/v1/schema", json=schema_payload, headers=self._headers()
                    )
                    return create_res.status_code in (200, 201)
        except Exception as e:
            logger.warning("Weaviate schema verification failed: %s", e)
            return False
        return False

    async def index_item(self, doc: Dict[str, Any]) -> bool:
        """
        Idempotently indexes a marketplace item document in Weaviate.
        Uses object UUID derived from listing_id.
        """
        if not self.is_configured:
            return False

        item_id = str(doc.get("item_id"))
        obj_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"talos.marketplace.{item_id}"))

        payload = {
            "class": self.class_name,
            "id": obj_uuid,
            "properties": doc,
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.put(
                    f"{self.url}/v1/objects/{self.class_name}/{obj_uuid}",
                    json=payload,
                    headers=self._headers(),
                )
                if res.status_code in (200, 204):
                    return True
                # If PUT fails with 404, fallback to POST
                if res.status_code == 404:
                    post_res = await client.post(
                        f"{self.url}/v1/objects",
                        json=payload,
                        headers=self._headers(),
                    )
                    return post_res.status_code in (200, 201)
        except Exception as e:
            logger.warning("Failed to index item %s in Weaviate: %s", item_id, e)
            return False
        return False

    async def delete_item(self, item_id: str) -> bool:
        """
        Deletes a marketplace item from Weaviate index.
        """
        if not self.is_configured:
            return False

        obj_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"talos.marketplace.{item_id}"))
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.delete(
                    f"{self.url}/v1/objects/{self.class_name}/{obj_uuid}",
                    headers=self._headers(),
                )
                return res.status_code in (200, 204, 404)
        except Exception as e:
            logger.warning("Failed to delete item %s from Weaviate: %s", item_id, e)
            return False

    async def hybrid_search(
        self,
        query: str,
        kind: Optional[str] = None,
        tag: Optional[str] = None,
        pricing_type: Optional[str] = None,
        verified: Optional[bool] = None,
        auth_type: Optional[str] = None,
        limit: int = 50,
        alpha: float = 0.5,
    ) -> Optional[List[str]]:
        """
        Executes hybrid search (BM25 lexical + vector semantic) and returns ordered list of item_ids.
        Returns None if Weaviate is not configured or query fails, allowing SQL fallback.
        """
        if not self.is_configured or not query.strip():
            return None

        # Build GraphQL query
        where_clauses = ['{path: ["status"], operator: Equal, valueText: "approved"}']
        if kind and kind != "all":
            where_clauses.append(f'{{path: ["kind"], operator: Equal, valueText: "{kind.lower()}"}}')
        if pricing_type and pricing_type != "all":
            where_clauses.append(f'{{path: ["pricing_type"], operator: Equal, valueText: "{pricing_type.lower()}"}}')
        if verified is not None:
            where_clauses.append(f'{{path: ["verified"], operator: Equal, valueBoolean: {str(verified).lower()}}}')

        where_filter = ""
        if len(where_clauses) == 1:
            where_filter = f"where: {where_clauses[0]}"
        elif len(where_clauses) > 1:
            operands = ", ".join(where_clauses)
            where_filter = f"where: {{operator: And, operands: [{operands}]}}"

        clean_query = query.replace('"', '\\"')
        gql = f"""
        {{
          Get {{
            {self.class_name}(
              hybrid: {{
                query: "{clean_query}",
                alpha: {alpha}
              }}
              {where_filter}
              limit: {limit}
            ) {{
              item_id
              _additional {{
                score
              }}
            }}
          }}
        }}
        """

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    f"{self.url}/v1/graphql",
                    json={"query": gql},
                    headers=self._headers(),
                )
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("data", {}).get("Get", {}).get(self.class_name, [])
                    return [item["item_id"] for item in items if item.get("item_id")]
        except Exception as e:
            logger.warning("Weaviate hybrid search error (%s), will fallback to SQL", e)
            return None
        return None
