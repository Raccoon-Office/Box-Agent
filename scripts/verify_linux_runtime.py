#!/usr/bin/env python3
"""Verify a packaged Box-Agent Linux runtime in a clean extraction directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import selectors
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path


ARCH_FILE_TOKENS = {
    "arm64": ("aarch64", "arm64"),
    "x64": ("x86-64", "x86_64"),
}


def _run(
    command: list[str],
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        input=input_text,
        timeout=timeout,
    )
    if result.returncode != 0:
        output = f"{result.stdout}\n{result.stderr}".strip()
        raise RuntimeError(
            f"Command failed ({result.returncode}): {' '.join(command)}\n{output[:4000]}"
        )
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_checksum(archive: Path) -> None:
    sidecar = archive.with_name(f"{archive.name}.sha256")
    if not sidecar.is_file():
        raise RuntimeError(f"Checksum sidecar missing: {sidecar}")
    fields = sidecar.read_text(encoding="utf-8").strip().split()
    if len(fields) != 2 or fields[1] != archive.name:
        raise RuntimeError(f"Invalid checksum sidecar: {sidecar}")
    actual = _sha256(archive)
    if fields[0].lower() != actual:
        raise RuntimeError(
            f"Archive checksum mismatch: expected {fields[0]}, got {actual}"
        )


def _safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            member_path = (destination / member.name).resolve()
            if member_path != destination and destination not in member_path.parents:
                raise RuntimeError(f"Unsafe archive path: {member.name}")
            if member.issym() or member.islnk():
                link_path = (member_path.parent / member.linkname).resolve()
                if link_path != destination and destination not in link_path.parents:
                    raise RuntimeError(
                        f"Unsafe archive link: {member.name} -> {member.linkname}"
                    )
        tar.extractall(destination)


def _assert_elf_arch(path: Path, arch: str) -> None:
    result = _run(["file", "-L", str(path)])
    output = result.stdout.lower()
    if "elf" not in output or not any(token in output for token in ARCH_FILE_TOKENS[arch]):
        raise RuntimeError(f"Unexpected ELF architecture for {path}: {result.stdout.strip()}")


def _assert_linked(path: Path) -> None:
    result = subprocess.run(
        ["ldd", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    output = f"{result.stdout}\n{result.stderr}".strip()
    if "not found" in output.lower():
        raise RuntimeError(f"Missing shared library for {path}:\n{output}")
    if result.returncode != 0 and "not a dynamic executable" not in output.lower():
        raise RuntimeError(f"ldd failed for {path}:\n{output}")


def _verify_acp_initialize(entry: Path, *, env: dict[str, str], request: str) -> None:
    process = subprocess.Popen(
        [str(entry)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        process.stdin.write(f"{request}\n")
        process.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            events = selector.select(timeout=max(0, deadline - time.monotonic()))
            if not events:
                break
            line = process.stdout.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if message.get("id") == 1 and "result" in message:
                return
        stderr = ""
        if process.poll() is not None and process.stderr is not None:
            stderr = process.stderr.read()
        raise RuntimeError(f"ACP initialize response missing. stderr={stderr[:2000]!r}")
    finally:
        selector.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _verify_external_resources(runtime: Path) -> Path:
    """Validate the resource layout used by the frozen program, without a fallback copy."""
    template = runtime / "config" / "config-example.yaml"
    if not template.is_file():
        raise RuntimeError(f"Bundled config template missing: {template}")
    if not (runtime / "skills" / "_manifest.json").is_file():
        raise RuntimeError("Bundled Skills manifest missing")
    for name in ("config", "skills"):
        if (runtime / "bin" / "_internal" / "box_agent" / name).exists():
            raise RuntimeError(f"Runtime contains duplicate internal {name} resources")
    return template


def verify(archive: Path, *, expected_arch: str) -> None:
    if platform.system() != "Linux":
        raise RuntimeError("Linux runtime verification must run on Linux")
    if expected_arch not in ARCH_FILE_TOKENS:
        raise RuntimeError(f"Unsupported architecture: {expected_arch}")
    if shutil.which("file") is None or shutil.which("ldd") is None:
        raise RuntimeError("Both `file` and `ldd` are required")

    archive = archive.resolve()
    _verify_checksum(archive)
    with tempfile.TemporaryDirectory(prefix="box-agent-linux-runtime-") as temp:
        temp_dir = Path(temp)
        _safe_extract(archive, temp_dir)
        runtime = temp_dir / "box-agent-runtime"
        manifest = json.loads((runtime / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("platform") != "linux" or manifest.get("arch") != expected_arch:
            raise RuntimeError(f"Unexpected runtime manifest target: {manifest}")
        if manifest.get("external_python_sandbox") is not True:
            raise RuntimeError("Linux runtime must use a host-managed Python sandbox")
        if manifest.get("bundled_stable_runtimes") != []:
            raise RuntimeError("Linux ACP runtime must not bundle stable tool runtimes")
        for relative in (Path("runtime/python"), Path("runtimes/node")):
            if (runtime / relative).exists():
                raise RuntimeError(f"Linux ACP runtime unexpectedly bundled {relative}")

        entry = runtime / str(manifest["entry"])
        if not entry.is_file():
            raise RuntimeError(f"Missing ACP executable: {entry}")
        _assert_elf_arch(entry, expected_arch)
        _assert_linked(entry)

        home = temp_dir / "home"
        home.mkdir()
        bundled_config = _verify_external_resources(runtime)
        config_dir = home / ".box-agent" / "config"
        config_dir.mkdir(parents=True)
        config_text = bundled_config.read_text(encoding="utf-8").replace(
            "YOUR_API_KEY_HERE",
            "runtime-verification-key",
        )
        (config_dir / "config.yaml").write_text(config_text, encoding="utf-8")
        external_runtime_vars = {
            "BOX_AGENT_BUNDLED_PYTHON",
            "BOX_AGENT_NODE",
            "BOX_AGENT_NPM",
            "BOX_AGENT_NPX",
            "BOX_AGENT_PYTHON",
            "BOX_AGENT_PYTHON3",
            "BOX_AGENT_SANDBOX_PYTHON",
        }
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in external_runtime_vars
        }
        env["HOME"] = str(home)
        initialize = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "runtime-verifier", "version": "1.0"},
                    "protocolVersion": 1,
                },
            }
        )
        _verify_acp_initialize(entry, env=env, request=initialize)

    print(f"Verified Linux runtime: {archive.name} ({expected_arch})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--arch", choices=sorted(ARCH_FILE_TOKENS), required=True)
    args = parser.parse_args()
    verify(args.archive, expected_arch=args.arch)


if __name__ == "__main__":
    main()
