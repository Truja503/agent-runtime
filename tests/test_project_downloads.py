"""Registry-only egress and artifact integrity precede any installation."""

from pathlib import Path

import pytest

from app.errors import ToolExecutionError
from app.tools.project_downloads import download_manifest, registry_bytes, supported_wheel
from app.tools.project_manifest import ProjectManifest


@pytest.mark.parametrize(
    "url",
    [
        "http://pypi.org/a",
        "https://127.0.0.1/a",
        "https://pypi.org:8443/a",
        "https://user:secret@pypi.org/a",
        "https://pypi.org/a?redirect=1",
        "file:///etc/passwd",
        "https://evil.example/a",
    ],
)
async def test_registry_rejects_unapproved_urls(url: str) -> None:
    with pytest.raises(ToolExecutionError, match="URL denied"):
        await registry_bytes(url)


async def test_registry_rejects_private_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(host: str) -> list[str]:
        return ["192.168.1.1"]

    monkeypatch.setattr("app.tools.project_downloads.resolve_public", resolve)
    with pytest.raises(ToolExecutionError, match="non-public"):
        await registry_bytes("https://pypi.org/a")


async def test_wrong_artifact_hash_is_not_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    async def fetch(url: str) -> bytes:
        if url.endswith("/json"):
            return json.dumps(
                {
                    "urls": [
                        {
                            "packagetype": "bdist_wheel",
                            "filename": "flask-3.1.2-py3-none-any.whl",
                            "url": "https://files.pythonhosted.org/package.whl",
                            "digests": {"sha256": "wrong"},
                        }
                    ]
                }
            ).encode()
        return b"modified wheel"

    monkeypatch.setattr("app.tools.project_downloads.registry_bytes", fetch)
    with pytest.raises(ToolExecutionError, match="hash mismatch"):
        await download_manifest(ProjectManifest(python={"Flask": "3.1.2"}), tmp_path)
    assert not list(tmp_path.iterdir())  # noqa: ASYNC240 - test fixture assertion


@pytest.mark.parametrize(
    "filename, allowed",
    [
        ("package-1.0-py3-none-any.whl", True),
        ("package-1.0-cp312-cp312-manylinux_2_17_x86_64.whl", True),
        ("package-1.0-cp312-cp312-win_amd64.whl", False),
        ("package-1.0.tar.gz", False),
    ],
)
def test_binary_platform_allowlist(filename: str, allowed: bool) -> None:
    assert supported_wheel(filename) is allowed
