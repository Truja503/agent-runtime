"""Trusted offline adapter. Runs only inside the network-disabled container."""

import json
import pathlib
import shutil
import subprocess
import tarfile

root = pathlib.Path("/project")
artifacts = json.loads(pathlib.Path("/artifacts/artifacts.json").read_text())
for environment in (root / ".venv", root / "node_modules"):
    if environment.is_symlink():
        raise ValueError("environment cannot be a symlink")
    if environment.exists():
        shutil.rmtree(environment)
subprocess.run(["/usr/local/bin/python", "-I", "-m", "venv", "/project/.venv"], check=True)
wheels = ["/artifacts/" + a["file"] for a in artifacts if a["ecosystem"] == "python"]
if wheels:
    subprocess.run(  # noqa: S603 - fixed offline adapter argv
        [
            "/project/.venv/bin/python",
            "-I",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            "--only-binary=:all:",
            *wheels,
        ],
        check=True,
    )
subprocess.run(["/project/.venv/bin/python", "-I", "-m", "pip", "check"], check=True)
for artifact in artifacts:
    if artifact["ecosystem"] != "npm":
        continue
    package = root / "node_modules" / artifact["name"]
    package.mkdir(parents=True, exist_ok=True)
    total = 0
    with tarfile.open("/artifacts/" + artifact["file"]) as archive:
        for member in archive:
            parts = pathlib.PurePosixPath(member.name).parts
            if (
                not parts
                or parts[0] not in {"package", artifact["name"].split("/")[-1]}
                or ".." in parts
                or member.issym()
                or member.islnk()
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("unsafe npm archive member")
            total += member.size
            if total > 100_000_000:
                raise ValueError("npm extraction size exceeded")
            destination = package.joinpath(*parts[1:]).resolve()
            if not destination.is_relative_to(package.resolve()):
                raise ValueError("npm extraction escaped package")
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source:
                    destination.write_bytes(source.read())
                if member.mode & 0o111:
                    destination.chmod(0o755)
    installed = json.loads((package / "package.json").read_text())
    if (installed["name"], installed["version"]) != (artifact["name"], artifact["version"]):
        raise ValueError("npm package identity mismatch")
# No npm scripts, hooks, installers or package executables run during extraction.
if any(a["ecosystem"] == "npm" for a in artifacts):
    subprocess.run(  # noqa: S603 - trusted metadata validator, no package code
        ["/usr/local/bin/node", "/adapter/check-dependencies.cjs"],
        check=True,
    )
print("Project-local offline dependency installation completed")
