"""Bounded discovery against configured endpoints, never arbitrary query URLs."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import ProviderKind
from app.models.profiles import ModelProfile


async def discover(
    profile: ModelProfile, client: httpx.AsyncClient | None = None
) -> dict[str, Any]:
    if profile.provider != ProviderKind.LOCAL:
        return {"status": "not_applicable", "models": []}
    own = client is None
    http = client or httpx.AsyncClient(
        timeout=min(profile.timeout_seconds, 10), follow_redirects=False, trust_env=False
    )
    try:
        if profile.local_server == "ollama":
            base = profile.base_url.removesuffix("/v1")
            response = await http.get(base + "/api/tags")
            response.raise_for_status()
            entries = response.json()["models"]
            models = []
            for item in entries[:100]:
                name = item["name"]
                capabilities = item.get("capabilities", [])
                if not capabilities:
                    try:
                        details = await http.post(base + "/api/show", json={"model": name})
                        details.raise_for_status()
                        capabilities = details.json().get("capabilities", [])
                    except (httpx.HTTPError, ValueError, TypeError):
                        pass
                chat_capable = (
                    True
                    if "completion" in capabilities
                    else (False if "embedding" in capabilities else None)
                )
                models.append(
                    {
                        "name": name,
                        "size": item.get("size"),
                        "chat_capable": chat_capable,
                        "capability_source": "provider" if capabilities else "unknown",
                        "capabilities": capabilities,
                        "available": True,
                    }
                )
        else:
            response = await http.get(profile.base_url + "/models")
            response.raise_for_status()
            models = [
                {
                    "name": item["id"],
                    "available": True,
                    "chat_capable": None,
                    "capabilities": [],
                    "capability_source": "unknown",
                }
                for item in response.json()["data"][:100]
            ]
        return {"status": "connected", "models": models}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return {"status": "unavailable", "models": []}
    finally:
        if own:
            await http.aclose()
