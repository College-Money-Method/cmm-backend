"""Cohere Embed v4 on Bedrock: one vector per chunk, one per question.

Chunks are embedded as ``search_document`` and questions as ``search_query``;
Cohere trains the two sides to meet, which is what lets "divorced" find a
chunk about separated parents with no shared word.
"""

from __future__ import annotations

import json
from functools import lru_cache

import boto3
from botocore.config import Config

from src.assistant.models import EMBEDDING_DIMENSIONS
from src.config import settings

# Cohere's per-request limit on the number of texts.
_BATCH_SIZE = 96


class EmbeddingError(RuntimeError):
    """Bedrock refused the request or returned an unexpected shape."""


@lru_cache(maxsize=1)
def _client():
    return boto3.client(
        "bedrock-runtime",
        region_name=settings.bedrock_region,
        # Empty strings fall back to the default credential chain (task role).
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        # A chat request holds a DB connection while it embeds the question, so a
        # stalled call must fail fast rather than wait out boto's 60s default.
        config=Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 2}),
    )


def _embed(texts: list[str], input_type: str) -> list[list[float]]:
    vectors: list[list[float]] = []
    for start in range(0, len(texts), _BATCH_SIZE):
        body = {
            "texts": texts[start : start + _BATCH_SIZE],
            "input_type": input_type,
            "embedding_types": ["float"],
            "output_dimension": EMBEDDING_DIMENSIONS,
        }
        try:
            response = _client().invoke_model(
                modelId=settings.assistant_embed_model_id,
                body=json.dumps(body),
                accept="application/json",
                contentType="application/json",
            )
            batch = json.loads(response["body"].read())["embeddings"]["float"]
        except Exception as exc:  # noqa: BLE001 - boto raises a wide family of errors
            raise EmbeddingError(f"Embedding request failed: {exc}") from exc
        vectors.extend(batch)
    return vectors


def embed_documents(texts: list[str]) -> list[list[float]]:
    return _embed(texts, "search_document")


def embed_query(text: str) -> list[float]:
    return _embed([text], "search_query")[0]
