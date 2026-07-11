"""Local embeddings via Ollama's /api/embed. Always hits settings.ollama_base_url
— there is deliberately no separate/hosted embedding endpoint anywhere in
config.py. This is the concrete mechanism behind the constitution's "embeddings
always local" guarantee (docs/04-constitution.md).
"""

import httpx

from app.config import settings


class EmbeddingError(RuntimeError):
    pass


def embed_local(text: str) -> list[float]:
    url = settings.ollama_base_url.removesuffix("/v1") + "/api/embed"
    try:
        resp = httpx.post(
            url,
            json={"model": settings.embedding_model, "input": text},
            timeout=30.0,
        )
        resp.raise_for_status()
    except httpx.RequestError as exc:
        raise EmbeddingError(
            f"Could not reach Ollama at {url}; is `ollama serve` running and is "
            f"`{settings.embedding_model}` pulled?"
        ) from exc
    return resp.json()["embeddings"][0]
