"""Approved, exact registry artifacts; pinned public HTTPS, no installer on the host."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from app.errors import ToolExecutionError
from app.tools.project_manifest import ProjectManifest
from app.tools.web import EgressPolicy, PinnedHTTPS, resolve_public

HOSTS = frozenset({"pypi.org", "files.pythonhosted.org", "registry.npmjs.org"})
MAX_ARTIFACT = 50_000_000


async def registry_bytes(url: str) -> bytes:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or host not in HOSTS
        or parsed.port not in {None, 443}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ToolExecutionError("dependency registry URL denied")
    addresses = await resolve_public(host)
    if not addresses or any(not EgressPolicy.public_ip(ip) for ip in addresses):
        raise ToolExecutionError("dependency registry resolved to a non-public address")

    def fetch() -> bytes:
        connection = PinnedHTTPS(host, addresses[0])
        try:
            connection.request("GET", parsed.path, headers={"Accept-Encoding": "identity"})
            response = connection.getresponse()
            if response.status != 200:  # Redirects are never followed.
                raise ToolExecutionError(f"dependency registry HTTP {response.status}")
            data = bytearray()
            deadline = time.monotonic() + 60
            while len(data) <= MAX_ARTIFACT:
                if time.monotonic() > deadline:
                    raise ToolExecutionError("dependency download deadline exceeded")
                chunk = response.read1(min(65536, MAX_ARTIFACT + 1 - len(data)))
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
            raise ToolExecutionError("dependency artifact exceeds 50 MB")
        finally:
            connection.close()

    for attempt in range(2):
        try:
            return await asyncio.to_thread(fetch)
        except TimeoutError as exc:
            if attempt:
                raise ToolExecutionError(
                    f"dependency registry timed out: {host}{parsed.path}"
                ) from exc
    raise AssertionError("unreachable")


def supported_wheel(filename: str) -> bool:
    # The fixed toolchain platform is CPython 3.12 on glibc Linux amd64.
    if filename.endswith(("-py3-none-any.whl", "-py2.py3-none-any.whl")):
        return True
    return bool(re.search(r"-cp312-(?:cp312|abi3)-[^/]*manylinux[^/]*x86_64.whl$", filename))


async def download_manifest(manifest: ProjectManifest, target: Path) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    total = 0
    for ecosystem, packages in (("python", manifest.python), ("npm", manifest.npm)):
        for name, version in sorted(packages.items()):
            endpoint = (
                f"https://pypi.org/pypi/{name}/{version}/json"
                if ecosystem == "python"
                else f"https://registry.npmjs.org/{quote(name, safe='')}/{version}"
            )
            metadata = json.loads(await registry_bytes(endpoint))
            if ecosystem == "python":
                wheels = [
                    item
                    for item in metadata.get("urls", [])
                    if item.get("packagetype") == "bdist_wheel"
                    and supported_wheel(item.get("filename", ""))
                    and not item.get("yanked")
                ]
                if not wheels:
                    raise ToolExecutionError(f"no supported binary wheel: {name}=={version}")
                item = sorted(wheels, key=lambda w: w["filename"])[0]
                filename = item["filename"]
                if Path(filename).name != filename or "\\" in filename:
                    raise ToolExecutionError("invalid wheel filename")
                data = await registry_bytes(item["url"])
                if hashlib.sha256(data).hexdigest() != item["digests"]["sha256"]:
                    raise ToolExecutionError("wheel hash mismatch")
            else:
                dist = metadata["dist"]
                data = await registry_bytes(dist["tarball"])
                integrity = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
                if integrity not in str(dist.get("integrity", "")).split():
                    raise ToolExecutionError("npm artifact requires matching SHA-512 integrity")
                filename = f"npm-{len(artifacts)}.tgz"
            total += len(data)
            if total > 250_000_000:
                raise ToolExecutionError("dependency request exceeds 250 MB")
            (target / filename).write_bytes(data)
            artifacts.append(
                {
                    "ecosystem": ecosystem,
                    "name": name,
                    "version": version,
                    "file": filename,
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
    return artifacts
