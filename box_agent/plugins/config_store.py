"""Durable plugin-management state, separate from conversation history."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .manifest import IDENTIFIER, SKILL_NAME, PluginError, json_bytes

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


class PluginConfigStore:
    def __init__(self, root: Path, target_id: str, profile_id: str) -> None:
        self.root = Path(root).resolve()
        self.target_id = target_id
        self.profile_id = profile_id
        self.path = self.root / "state.json"
        with _LOCKS_GUARD:
            self._thread_lock = _THREAD_LOCKS.setdefault(str(self.root), threading.RLock())

    def empty(self) -> dict[str, Any]:
        return {"format_version": 1, "target_id": self.target_id, "profile_id": self.profile_id,
                "profile_revision": 0, "instances": {}, "active": {
                    "generation": "builtin", "profile_revision": 0, "instances": {}},
                "previous_active": None, "activation": None, "activation_history": {}, "receipts": {}, "audit": []}

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, (self.root / "state.lock").open("a+b") as stream:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + 10
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, PermissionError):
                    if time.monotonic() >= deadline:
                        raise PluginError("config_busy", "Another plugin-management operation holds the configuration lock")
                    time.sleep(0.02)
            try:
                state = self._read()
                before = copy.deepcopy(state)
                yield state
                if before != state:
                    self._write(state)
            finally:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, Any]:
        with self.transaction() as state:
            return copy.deepcopy(state)

    @staticmethod
    def _validate_instance(item: Any, *, snapshot: bool) -> None:
        revision_key = "config_revision" if snapshot else "saved_revision"
        if (not isinstance(item, dict) or type(item.get("enabled")) is not bool
                or type(item.get(revision_key)) is not int or item[revision_key] < 1
                or not isinstance(item.get("config"), dict)
                or not isinstance(item.get("aliases"), dict)
                or not isinstance(item.get("expose"), list)
                or any(not isinstance(value, str) or not re.fullmatch(IDENTIFIER, value) for value in item["expose"])
                or len(set(item["expose"])) != len(item["expose"])):
            raise ValueError("invalid instance values")
        if any(not isinstance(public, str) or not re.fullmatch(SKILL_NAME, public)
               or not isinstance(target, str) or not re.fullmatch(IDENTIFIER, target)
               for public, target in item["aliases"].items()):
            raise ValueError("invalid instance aliases")
        for key in ("package_digest", "schema_digest"):
            if not isinstance(item.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", item[key]):
                raise ValueError("invalid package digest")
        if snapshot:
            if not isinstance(item.get("package_root"), str) or not item["package_root"]:
                raise ValueError("invalid snapshot package root")
        elif any(not isinstance(item.get(key), str) or not item[key] for key in ("package_id", "package_version")):
            raise ValueError("invalid saved package identity")
        manifest = item.get("manifest")
        if (not isinstance(manifest, dict) or not isinstance(manifest.get("id"), str)
                or not isinstance(manifest.get("version"), str) or not isinstance(manifest.get("contributes"), dict)
                or not isinstance(manifest["contributes"].get("skills"), list)):
            raise ValueError("invalid cached manifest")
        for skill in manifest["contributes"]["skills"]:
            if not isinstance(skill, dict) or not isinstance(skill.get("id"), str) or not isinstance(skill.get("path"), str):
                raise ValueError("invalid cached Skill declaration")

    @classmethod
    def _validate_instances(cls, instances: Any, *, snapshot: bool) -> None:
        if not isinstance(instances, dict):
            raise ValueError("invalid instances mapping")
        for name, item in instances.items():
            if not isinstance(name, str) or not re.fullmatch(IDENTIFIER, name):
                raise ValueError("invalid instance identity")
            cls._validate_instance(item, snapshot=snapshot)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.empty()
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 16 * 1024 * 1024:
                raise ValueError("unexpected state file")
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if (not isinstance(state, dict) or type(state.get("format_version")) is not int or state["format_version"] != 1
                    or not isinstance(state.get("instances"), dict)
                    or not isinstance(state.get("active"), dict)
                    or type(state.get("profile_revision")) is not int or state["profile_revision"] < 0
                    or not isinstance(state.get("receipts"), dict) or not isinstance(state.get("audit"), list)):
                raise ValueError("invalid state structure")
            snapshots = [state["active"]]
            if state.get("previous_active") is not None:
                snapshots.append(state["previous_active"])
            activation = state.get("activation")
            if activation is not None:
                if (not isinstance(activation, dict) or not isinstance(activation.get("status"), str)
                        or activation["status"] not in {"prepared", "active", "failed"}
                        or not isinstance(activation.get("activation_id"), str)
                        or not isinstance(activation.get("snapshot_digest"), str)):
                    raise ValueError("invalid activation")
                snapshots.append(activation.get("snapshot"))
            for snapshot in snapshots:
                if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("generation"), str)
                        or type(snapshot.get("profile_revision")) is not int or snapshot["profile_revision"] < 0):
                    raise ValueError("invalid snapshot")
                self._validate_instances(snapshot.get("instances"), snapshot=True)
            self._validate_instances(state["instances"], snapshot=False)
        except (OSError, UnicodeError, ValueError) as exc:
            raise PluginError("config_corrupt", "Plugin configuration cannot be read; use a verified backup or safe mode") from exc
        if state.get("target_id") != self.target_id or state.get("profile_id") != self.profile_id:
            raise PluginError("scope_mismatch", "Plugin state belongs to another execution target or profile")
        return state

    def _write(self, state: dict[str, Any]) -> None:
        payload = json_bytes(state)
        if len(payload) > 16 * 1024 * 1024:
            raise PluginError("config_too_large", "Plugin management state exceeds 16 MiB")
        temporary = None
        replaced = False
        try:
            descriptor, name = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=self.root)
            temporary = Path(name)
            with os.fdopen(descriptor, "wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            replaced = True
            if os.name != "nt":
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        except OSError as exc:
            if replaced:
                raise PluginError("config_write_uncertain", "Configuration was replaced but durability confirmation failed; query status before retrying") from exc
            raise PluginError("config_write_failed", "Plugin configuration could not be saved") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
