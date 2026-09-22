"""Constrained egress: no ambient proxies, credentials, uploads or local destinations."""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any, Literal, Protocol
from urllib.parse import unquote, urljoin, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from app.errors import ToolExecutionError
from app.observability.events import EventBus, EventType
from app.observability.prompts import PrivacyFilter
from app.tools.broker import CURRENT_TOOL_TASK
from app.tools.filesystem import Workspace

WEB_TOOLS = frozenset(
    {"web.search", "web.fetch", "docs.fetch", "assets.search_images", "assets.import_image"}
)
MAX_RESPONSE = 2_000_000
TEXT_MIMES = {"text/plain", "text/html", "text/markdown"}
IMAGE_MIMES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/avif": "avif"}


class WebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: Literal[
        "web.search", "web.fetch", "docs.fetch", "assets.search_images", "assets.import_image"
    ]
    query: str = Field(default="", max_length=300)
    url: str = Field(default="", max_length=2048)
    max_results: int = Field(default=5, ge=1, le=10)
    allowed_domains: list[str] = Field(default_factory=list, max_length=10)
    method: Literal["GET", "HEAD"] = "GET"
    offset: int = Field(default=0, ge=0, le=MAX_RESPONSE)
    candidate_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class SearchProvider(Protocol):
    """Trusted adapter; receives ONLY a validated query, never task context/files."""

    name: str

    async def search(self, query: str, *, images: bool) -> list[dict[str, Any]]: ...


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes


async def resolve_public(host: str) -> list[str]:
    records = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list({str(record[4][0]) for record in records})


class EgressPolicy:
    def __init__(
        self,
        privacy: PrivacyFilter,
        resolver: Callable[[str], Awaitable[list[str]]] = resolve_public,
    ):
        self.privacy = privacy
        self.resolver = resolver

    def public_text(self, text: str) -> str:
        # Fail closed on known secrets, credential syntax, code and local path syntax.
        decoded = unquote(text)
        if (
            not text.strip()
            or self.privacy.text(decoded) != decoded
            or re.search(
                r"(?i)((?<![a-z])[a-z]:[\\/]|\\|(?:^|\s)/(?:\S+)|\.env\b|\.\./|"
                r"\b(?:token|password|secret|api[_ -]?key|bearer|credential)\b|"
                r"-----BEGIN|[\r\n`{};]|[A-Za-z0-9_+/=-]{48,})",
                decoded,
            )
        ):
            raise ToolExecutionError("egress denied: sensitive or non-public input")
        return text.strip()

    def url(self, value: str) -> tuple[str, str]:
        self.public_text(value)
        try:
            parsed = urlsplit(value)
            host = (parsed.hostname or "").rstrip(".").lower().encode("idna").decode()
            if (
                parsed.scheme != "https"
                or not host
                or parsed.username
                or parsed.password
                or parsed.port not in {None, 443}
                or parsed.query
                or parsed.fragment
                or host == "localhost"
                or host.endswith((".localhost", ".local"))
                or "%" in host
            ):
                raise ValueError
        except (ValueError, UnicodeError):
            raise ToolExecutionError(
                "egress denied: public HTTPS URL required; no credentials/query/fragment"
            ) from None
        return host, parsed.path or "/"

    async def destination(self, value: str) -> tuple[str, str, str]:
        host, path = self.url(value)
        try:
            addresses = await self.resolver(host)
            if not addresses or any(not self.public_ip(a) for a in addresses):
                raise ValueError
        except (ValueError, OSError):
            raise ToolExecutionError(
                "egress denied: non-public or unresolved destination"
            ) from None
        return host, path, addresses[0]

    @staticmethod
    def public_ip(value: str) -> bool:
        address = ipaddress.ip_address(value)
        if not address.is_global or address.is_multicast:
            return False
        if isinstance(address, ipaddress.IPv6Address):
            # Avoid transition mechanisms that can encode a non-public IPv4 endpoint.
            return not (
                address.ipv4_mapped
                or address.sixtofour
                or address.teredo
                or address in ipaddress.ip_network("64:ff9b::/96")
                or address in ipaddress.ip_network("64:ff9b:1::/48")
            )
        return True


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str):
        super().__init__(host, timeout=10)
        self.address = address

    def connect(self) -> None:
        # Connect to the validated IP, but verify TLS and send Host for the original name.
        raw = socket.create_connection((self.address, 443), timeout=10)
        try:
            self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def request_pinned(host: str, path: str, address: str, method: str) -> Response:
    connection = PinnedHTTPS(host, address)
    deadline = time.monotonic() + 20
    try:
        connection.request(
            method,
            path,
            headers={"Accept-Encoding": "identity", "User-Agent": "AgentRuntime-Web/1"},
        )
        response = connection.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        if headers.get("content-encoding", "identity") != "identity":
            raise ToolExecutionError("compressed responses are not supported")
        body = b""
        while method != "HEAD":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ToolExecutionError("response deadline exceeded")
            if connection.sock:
                connection.sock.settimeout(min(10, remaining))
            chunk = response.read1(min(65536, MAX_RESPONSE + 1 - len(body)))
            if not chunk:
                break
            body += chunk
            if len(body) > MAX_RESPONSE:
                break
        if len(body) > MAX_RESPONSE:
            raise ToolExecutionError("response size limit exceeded")
        return Response(response.status, headers, body)
    finally:
        connection.close()


async def transport(host: str, path: str, address: str, method: str) -> Response:
    return await asyncio.to_thread(request_pinned, host, path, address, method)


class ReadableHTML(HTMLParser):
    """Emit inert Markdown/text, never HTML from the remote page."""

    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.parts: list[str] = []
        self.title: list[str] = []
        self.in_title = False
        self.blocked: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "iframe", "form", "object", "embed", "svg", "template"}:
            if tag != "embed":
                self.blocked.append(tag)
            return
        if self.blocked:
            return
        self.in_title = tag == "title" or self.in_title
        if re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n" + "#" * int(tag[1]) + " ")
        elif tag in {"p", "div", "br", "li"}:
            self.parts.append("\n")
        elif tag == "pre":
            self.parts.append("\n```\n")
        elif tag == "a":
            link = urljoin(self.source, dict(attrs).get("href") or "")
            self.links.append(link if urlsplit(link).scheme == "https" else "")

    def handle_endtag(self, tag: str) -> None:
        if self.blocked:
            if tag == self.blocked[-1]:
                self.blocked.pop()
            return
        if tag == "title":
            self.in_title = False
        elif tag == "pre":
            self.parts.append("\n```\n")
        elif tag == "a" and self.links:
            link = self.links.pop()
            if link:
                self.parts.append(f" ({link})")
        elif tag in {"p", "div"} or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.blocked:
            (self.title if self.in_title else self.parts).append(data)


class WebBroker:
    def __init__(
        self,
        *,
        workspace: Workspace,
        enabled: Callable[[], bool],
        privacy: PrivacyFilter,
        provider: SearchProvider | None = None,
        resolver: Callable[[str], Awaitable[list[str]]] = resolve_public,
        sender: Callable[[str, str, str, str], Awaitable[Response]] = transport,
        events: EventBus | None = None,
    ):
        self.workspace = workspace
        self.enabled = enabled
        self.policy = EgressPolicy(privacy, resolver)
        self.provider = provider
        self.sender = sender
        self.events = events
        self.candidates: dict[str, dict[str, Any]] = {}
        self.recent: deque[dict[str, Any]] = deque(maxlen=40)

    async def execute(self, request: WebRequest) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "operation": request.operation,
            "status": "active",
            "timestamp": datetime.now(UTC).isoformat(),
            "external_data_sent": [],
        }
        self.recent.append(entry)
        try:
            if not self.enabled():
                raise ToolExecutionError("Internet access disabled")
            async with asyncio.timeout(30):
                result = await self._execute(request, entry)
            entry["status"] = result.get("status", "completed")
            return result
        except (ToolExecutionError, OSError, ValueError, TimeoutError, http.client.HTTPException):
            # Never expose remote exception text, headers, credentials or rejected input.
            entry["status"] = "denied"
            entry["reason"] = (
                "Internet disabled"
                if not self.enabled()
                else "Request denied or failed validation/network limits"
            )
            return {"status": "denied", "error": entry["reason"], "sources": []}
        except asyncio.CancelledError:
            entry["status"] = "cancelled"
            raise
        finally:
            if self.events:
                await self.events.emit(
                    EventType.WEB_EGRESS, actor="web", task_id=CURRENT_TOOL_TASK.get(), **entry
                )

    async def _execute(self, request: WebRequest, entry: dict[str, Any]) -> dict[str, Any]:
        if request.operation in {"web.search", "assets.search_images"}:
            query = self.policy.public_text(request.query)
            if not re.fullmatch(r"[\w .,+?()\-]+", query):
                raise ToolExecutionError("search accepts public keywords, not paths or code")
            if not self.provider:
                return {
                    "status": "not_configured",
                    "error": "search provider not configured",
                    "results": [],
                    "sources": [],
                }
            domains = []
            for domain in request.allowed_domains:
                host, _ = self.policy.url("https://" + domain)
                if domain != host:
                    raise ToolExecutionError("invalid domain filter")
                domains.append(host)
            entry["external_data_sent"].append(query)
            candidates = await self.provider.search(
                query, images=request.operation == "assets.search_images"
            )
            results = []
            for candidate in candidates[:100]:
                url = str(candidate.get("url") or candidate.get("source_page_url") or "")
                try:
                    host, _, _ = await self.policy.destination(url)
                except ToolExecutionError:
                    continue
                if domains and not any(host == d or host.endswith("." + d) for d in domains):
                    continue
                fields = (
                    "title",
                    "snippet",
                    "source_page_url",
                    "image_url",
                    "creator",
                    "attribution",
                    "width",
                    "height",
                    "description",
                    "license",
                    "usage",
                )
                item = {
                    k: value[:2000]
                    if isinstance(value, str)
                    else value
                    if isinstance(value, (int, type(None)))
                    else None
                    for k in fields
                    for value in [candidate.get(k)]
                }
                item.update(url=url, domain=host, provider=self.provider.name)
                if request.operation == "assets.search_images":
                    try:
                        await self.policy.destination(str(item.get("image_url") or ""))
                    except ToolExecutionError:
                        continue
                    identifier = hashlib.sha256(
                        json.dumps(item, sort_keys=True).encode()
                    ).hexdigest()
                    item["candidate_id"] = identifier
                    if len(self.candidates) >= 100:
                        self.candidates.pop(next(iter(self.candidates)))
                    self.candidates[identifier] = self.policy.privacy.clean(item)
                results.append(self.policy.privacy.clean(item))
                if len(results) >= request.max_results:
                    break
            return {
                "status": "completed",
                "results": results,
                "sources": [r["url"] for r in results],
            }

        selected_candidate = self.candidates.get(request.candidate_id or "")
        if request.candidate_id and not selected_candidate:
            raise ToolExecutionError("unknown image candidate")
        url = str(selected_candidate["image_url"]) if selected_candidate else request.url
        for hop in range(4):
            if not self.enabled():
                raise ToolExecutionError("Internet access disabled")
            host, path, address = await self.policy.destination(url)
            if not self.enabled():
                raise ToolExecutionError("Internet access disabled")
            entry["external_data_sent"].append(url)
            response = await self.sender(host, path, address, request.method)
            if len(response.body) > MAX_RESPONSE:
                raise ToolExecutionError("response too large")
            if response.status in {301, 302, 303, 307, 308}:
                if hop == 3 or not response.headers.get("location"):
                    raise ToolExecutionError("redirect limit")
                url = urljoin(url, response.headers["location"])
                continue
            break
        if not 200 <= response.status < 300:
            raise ToolExecutionError("unsuccessful response")
        if not self.enabled():
            raise ToolExecutionError("Internet access disabled")
        mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if request.operation == "assets.import_image":
            data = response.body
            magic = (
                (mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
                or (mime == "image/jpeg" and data.startswith(b"\xff\xd8\xff"))
                or (mime == "image/webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP")
                or (
                    mime == "image/avif"
                    and data[4:8] == b"ftyp"
                    and data[8:12] in {b"avif", b"avis"}
                )
            )
            if mime not in IMAGE_MIMES or not magic:
                raise ToolExecutionError("unsupported image MIME or magic")
            digest = hashlib.sha256(data).hexdigest()
            relative = f"assets/imported/{digest}.{IMAGE_MIMES[mime]}"
            target = self.workspace.resolve(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target = self.workspace.resolve(relative)
            if not target.exists():
                with target.open("xb") as output:
                    output.write(data)
            metadata = {
                "url": url,
                "source_url": request.url,
                "sha256": digest,
                "mime": mime,
                "path": relative,
                "provider": "https",
                "creator": None,
                "license": None,
                "attribution": None,
            }
            if selected_candidate:
                metadata.update(
                    {
                        key: selected_candidate.get(key)
                        for key in (
                            "provider",
                            "source_page_url",
                            "image_url",
                            "creator",
                            "attribution",
                            "width",
                            "height",
                            "description",
                            "license",
                            "usage",
                        )
                    }
                )
            # Fixed, confined sidecar; the caller cannot choose a write path.
            sidecar = self.workspace.resolve(relative + ".json")
            if not sidecar.exists():
                with sidecar.open("x", encoding="utf-8") as output:
                    json.dump(metadata, output)
            return {"status": "completed", **metadata, "sources": [url]}
        if mime not in TEXT_MIMES:
            raise ToolExecutionError("unsupported text MIME")
        content = response.body.decode("utf-8", errors="replace")
        title = ""
        if mime == "text/html":
            parser = ReadableHTML(url)
            parser.feed(content)
            content, title = "".join(parser.parts), "".join(parser.title)
        content = self.policy.privacy.text(content)
        if request.offset > len(content):
            raise ToolExecutionError("offset exceeds content length")
        end = min(request.offset + 12000, len(content))
        return {
            "status": "completed",
            "url": url,
            "title": self.policy.privacy.text(title),
            "content": content[request.offset : end],
            "content_hash": hashlib.sha256(content.encode()).hexdigest(),
            "complete": end == len(content),
            "truncated": end < len(content),
            "next_offset": end if end < len(content) else None,
            "sources": [url],
        }
