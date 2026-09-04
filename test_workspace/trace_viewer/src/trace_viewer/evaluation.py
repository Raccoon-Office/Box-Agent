"""Launch the existing ACP evaluator from RaccoonOps query-set metadata."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class EvaluationLaunchError(RuntimeError):
    """A safe, user-facing launch or upstream API failure."""


_AUTH_REFRESH_LOCK = threading.Lock()


def _access_token_expiry(token: str) -> int | None:
    parts = token.strip().split(".")
    if len(parts) < 2:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        expiry = claims["exp"]
        if isinstance(expiry, bool):
            return None
        return int(expiry)
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None


def _ensure_fresh_builtin_auth(
    repo_root: Path,
    model_binding: Mapping[str, Any] | None,
) -> None:
    """Refresh an expiring hosted token before fetching or materializing cases."""
    if not isinstance(model_binding, Mapping) or model_binding.get("source") != "builtin":
        return

    configured_path = os.environ.get("BOX_AGENT_EVAL_AUTH_FILE", "").strip()
    auth_path = (
        Path(configured_path).expanduser()
        if configured_path
        else Path.home() / ".box-agent" / "config" / "auth.json"
    )
    with _AUTH_REFRESH_LOCK:
        try:
            document = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise EvaluationLaunchError(
                "办公小浣熊登录状态不可用，请打开桌面应用重新登录后再执行评测"
            ) from error
        token = document.get("access_token") if isinstance(document, Mapping) else None
        expiry = (
            _access_token_expiry(token)
            if isinstance(token, str) and token.strip()
            else None
        )
        if isinstance(token, str) and token.strip() and (
            expiry is None or expiry > int(time.time()) + 300
        ):
            return

        refresh_tool = repo_root / "test_workspace" / "refresh_box_agent_auth.py"
        if not refresh_tool.is_file():
            raise EvaluationLaunchError("评测登录刷新工具不存在，无法继续启动评测")
        command = [
            sys.executable,
            str(refresh_tool),
            "--auth-file",
            str(auth_path),
            "--refresh-window-seconds",
            "300",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=repo_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EvaluationLaunchError(
                "办公小浣熊登录自动刷新失败，请打开桌面应用重新登录后再执行评测"
            ) from error
        if completed.returncode != 0:
            raise EvaluationLaunchError(
                "办公小浣熊登录自动刷新失败，请打开桌面应用重新登录后再执行评测"
            )

        try:
            refreshed_document = json.loads(auth_path.read_text(encoding="utf-8"))
            refreshed_token = refreshed_document["access_token"]
            refreshed_expiry = _access_token_expiry(refreshed_token)
        except (OSError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as error:
            raise EvaluationLaunchError("自动刷新后无法重新读取办公小浣熊登录状态") from error
        if (
            not isinstance(refreshed_token, str)
            or not refreshed_token.strip()
            or refreshed_expiry is None
            or refreshed_expiry <= int(time.time()) + 60
        ):
            raise EvaluationLaunchError("自动刷新后的办公小浣熊登录状态仍不可用")


def _item_task_types(item: Mapping[str, Any]) -> list[str]:
    values: list[str] = []
    raw_values = item.get("task_types", [])
    if isinstance(raw_values, list):
        values.extend(value.strip() for value in raw_values if isinstance(value, str))
    raw_value = item.get("task_type")
    if isinstance(raw_value, str):
        values.append(raw_value.strip())
    return sorted({value for value in values if value})


def _attachment_count(items: list[Any]) -> int:
    return sum(
        len(item.get("attachments", []))
        for item in items
        if isinstance(item, Mapping)
        and isinstance(item.get("attachments", []), list)
    )


@dataclass(frozen=True)
class OpsSettings:
    base_url: str
    project_key: str
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls) -> "OpsSettings":
        return cls(
            base_url=os.environ.get(
                "BOX_AGENT_OPS_URL",
                "http://127.0.0.1:8080",
            ).rstrip("/"),
            project_key=os.environ.get(
                "BOX_AGENT_OPS_PROJECT_KEY",
                "office-raccoon",
            ),
            timeout_seconds=float(
                os.environ.get("BOX_AGENT_OPS_TIMEOUT_SECONDS", "10")
            ),
        )


class OpsClient:
    """Read datasets and model-bound ACP cases from the existing Ops API."""

    def __init__(self, settings: OpsSettings):
        self.settings = settings

    def _get(self, path: str, query: Mapping[str, str] | None = None) -> Any:
        url = f"{self.settings.base_url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(
                request,
                timeout=self.settings.timeout_seconds,
            ) as response:
                document = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:1000]
            raise EvaluationLaunchError(
                f"Ops API 返回 HTTP {error.code}: {detail or error.reason}"
            ) from error
        except (OSError, TimeoutError, urllib.error.URLError) as error:
            raise EvaluationLaunchError(f"无法连接 Ops API: {error}") from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise EvaluationLaunchError("Ops API 返回了无效 JSON") from error
        return document

    def list_options(self) -> dict[str, list[dict[str, Any]]]:
        query_sets = self._get(
            "/api/query-sets",
            {"project_key": self.settings.project_key},
        )
        cases = self._get(
            "/api/cases",
            {
                "project_key": self.settings.project_key,
                "runner_type": "acp",
                "status": "active",
            },
        )
        if not isinstance(query_sets, list) or not isinstance(cases, list):
            raise EvaluationLaunchError("Ops API 数据结构不符合预期")

        datasets: list[dict[str, Any]] = []
        for item in query_sets:
            if not isinstance(item, Mapping):
                continue
            query_set_id = item.get("query_set_id")
            rows = item.get("items")
            if not isinstance(query_set_id, str) or not isinstance(rows, list):
                continue
            task_types = sorted(
                {
                    task_type
                    for row in rows
                    if isinstance(row, Mapping)
                    for task_type in _item_task_types(row)
                }
            )
            declared_task_types = [
                value.strip()
                for value in item.get("task_types", [])
                if isinstance(value, str) and value.strip()
            ]
            if not task_types:
                task_types = sorted(set(declared_task_types))
            task_type_stats = []
            for task_type in task_types:
                matching = [
                    row
                    for row in rows
                    if isinstance(row, Mapping)
                    and (
                        task_type in _item_task_types(row)
                        or (
                            not _item_task_types(row)
                            and len(task_types) == 1
                        )
                    )
                ]
                task_type_stats.append(
                    {
                        "name": task_type,
                        "item_count": len(matching),
                        "attachment_count": _attachment_count(matching),
                    }
                )
            datasets.append(
                {
                    "id": query_set_id,
                    "name": str(item.get("name") or query_set_id),
                    "item_count": len(rows),
                    "attachment_count": _attachment_count(rows),
                    "task_types": task_types,
                    "task_type_stats": task_type_stats,
                }
            )

        models_by_name: dict[str, dict[str, Any]] = {}
        for case in cases:
            if not isinstance(case, Mapping):
                continue
            definition = case.get("definition")
            if not isinstance(definition, Mapping):
                continue
            prompt = definition.get("prompt")
            if not isinstance(prompt, str) or "{query}" not in prompt:
                continue
            metadata = definition.get("meta")
            binding = (
                metadata.get("llm_binding")
                if isinstance(metadata, Mapping)
                else None
            )
            if not isinstance(binding, Mapping):
                continue
            model = binding.get("model")
            if not isinstance(model, str) or not model.strip():
                continue
            model = model.strip()
            max_tokens = binding.get("maxTokens")
            if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
                max_tokens = None
            candidate = {
                "id": model,
                "name": model,
                "max_tokens": max_tokens,
                "case_config_id": case.get("case_id"),
                "case_name": case.get("name"),
            }
            existing = models_by_name.get(model)
            if existing is None:
                models_by_name[model] = candidate
            elif max_tokens is not None and (
                existing["max_tokens"] is None
                or max_tokens < existing["max_tokens"]
            ):
                models_by_name[model] = candidate

        return {
            "datasets": sorted(datasets, key=lambda item: (item["name"], item["id"])),
            "models": sorted(models_by_name.values(), key=lambda item: item["name"]),
        }

    def get_query_set(self, query_set_id: str) -> dict[str, Any]:
        quoted = urllib.parse.quote(query_set_id, safe="")
        document = self._get(f"/api/query-sets/{quoted}")
        if not isinstance(document, dict):
            raise EvaluationLaunchError("Ops 数据集详情结构不符合预期")
        if document.get("project_key") != self.settings.project_key:
            raise EvaluationLaunchError("Ops 数据集不属于当前评测项目")
        return document


class EvaluationRunner:
    """Materialize one Ops dataset and run the established test_workspace entry."""

    def __init__(self, repo_root: Path, ops: OpsClient | None = None):
        self.repo_root = Path(repo_root).resolve()
        self.ops = ops or OpsClient(OpsSettings.from_env())

    def list_options(self) -> dict[str, list[dict[str, Any]]]:
        options = self.ops.list_options()
        configured = self._configured_models()
        if configured:
            options["models"] = configured
        return options

    def _configured_models(self) -> list[dict[str, Any]]:
        configured_path = os.environ.get("BOX_AGENT_EVAL_MODEL_CATALOG")
        path = (
            Path(configured_path).expanduser()
            if configured_path
            else self.repo_root / "test_workspace" / "evaluation_models.json"
        )
        if not path.is_file():
            return []
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise EvaluationLaunchError(f"被测模型目录无法读取: {error}") from error
        raw_models = document.get("models") if isinstance(document, Mapping) else None
        if not isinstance(raw_models, list) or not raw_models:
            raise EvaluationLaunchError("被测模型目录不包含 models 配置")
        models: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_model in raw_models:
            if not isinstance(raw_model, Mapping):
                raise EvaluationLaunchError("被测模型目录包含无效模型配置")
            model_id = raw_model.get("id")
            name = raw_model.get("name")
            binding = raw_model.get("binding")
            if (
                not isinstance(model_id, str)
                or not model_id.strip()
                or model_id in seen
                or not isinstance(name, str)
                or not name.strip()
                or not isinstance(binding, Mapping)
                or binding.get("source") != "builtin"
                or not isinstance(binding.get("model"), str)
                or not str(binding.get("model")).strip()
            ):
                raise EvaluationLaunchError("被测模型目录包含无效模型配置")
            seen.add(model_id)
            models.append(
                {
                    "id": model_id,
                    "name": name.strip(),
                    "multiplier": raw_model.get("multiplier"),
                    "binding": dict(binding),
                    "max_tokens": binding.get("maxTokens"),
                }
            )
        return models

    @staticmethod
    def _safe_case_id(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value in {".", ".."}
            or "/" in value
            or "\\" in value
            or "\x00" in value
        ):
            raise EvaluationLaunchError("Ops 数据集包含无效 query_id")
        return value

    @staticmethod
    def _safe_filename(value: Any, fallback: str) -> str:
        name = str(value or fallback)
        if Path(name).name != name or name in {"", ".", ".."} or "\x00" in name:
            raise EvaluationLaunchError("Ops 数据集包含无效附件名称")
        return name

    def _materialize_query_set(
        self,
        query_set: Mapping[str, Any],
        root: Path,
        task_type: str | None = None,
    ) -> tuple[Path, int]:
        items = query_set.get("items")
        if not isinstance(items, list) or not items:
            raise EvaluationLaunchError("Ops 数据集不包含可执行任务")
        if task_type is not None:
            declared_task_types = {
                value.strip()
                for value in query_set.get("task_types", [])
                if isinstance(value, str) and value.strip()
            }
            items = [
                item
                for item in items
                if isinstance(item, Mapping)
                and (
                    task_type in _item_task_types(item)
                    or (
                        not _item_task_types(item)
                        and declared_task_types == {task_type}
                    )
                )
            ]
            if not items:
                raise EvaluationLaunchError("所选数据集不包含该任务类型")
        dataset_path = root / "dataset.jsonl"
        records: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for raw_item in items:
            if not isinstance(raw_item, Mapping):
                raise EvaluationLaunchError("Ops 数据集任务结构不符合预期")
            case_id = self._safe_case_id(raw_item.get("query_id"))
            if case_id in seen_ids:
                raise EvaluationLaunchError(f"Ops 数据集包含重复 query_id: {case_id}")
            query = raw_item.get("query")
            if not isinstance(query, str) or not query.strip():
                raise EvaluationLaunchError(f"Ops 任务 {case_id} 缺少有效 query")
            attachments = raw_item.get("attachments", [])
            if not isinstance(attachments, list):
                raise EvaluationLaunchError(f"Ops 任务 {case_id} 附件结构无效")
            input_files: list[str] = []
            destination_names: set[str] = set()
            for raw_attachment in attachments:
                if not isinstance(raw_attachment, Mapping):
                    raise EvaluationLaunchError(f"Ops 任务 {case_id} 附件结构无效")
                source_value = raw_attachment.get("path")
                if not isinstance(source_value, str) or not source_value:
                    raise EvaluationLaunchError(
                        f"Ops 任务 {case_id} 的附件没有同机可读路径"
                    )
                try:
                    source = Path(source_value).expanduser().resolve(strict=True)
                except OSError as error:
                    raise EvaluationLaunchError(
                        f"Ops 任务 {case_id} 的附件不存在: {source_value}"
                    ) from error
                if not source.is_file():
                    raise EvaluationLaunchError(
                        f"Ops 任务 {case_id} 的附件不是普通文件: {source_value}"
                    )
                name = self._safe_filename(raw_attachment.get("name"), source.name)
                if name in destination_names:
                    raise EvaluationLaunchError(
                        f"Ops 任务 {case_id} 包含同名附件: {name}"
                    )
                relative = Path("inputs") / case_id / name
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                input_files.append(relative.as_posix())
                destination_names.add(name)
            record = {
                "id": case_id,
                "query": query,
                "input_files": input_files,
                "benchmark_case_id": raw_item.get("benchmark_case_id"),
                "task_type": raw_item.get("task_type"),
                "task_types": raw_item.get("task_types", []),
                "tags": raw_item.get("tags", []),
                "metadata": raw_item.get("metadata", {}),
                "source": {
                    "type": "raccoon-ops",
                    "query_set_id": query_set.get("query_set_id"),
                },
            }
            records.append(record)
            seen_ids.add(case_id)
        dataset_path.write_text(
            "".join(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        return dataset_path, len(records)

    @staticmethod
    def _title(dataset_id: str, model: str, launch_id: str) -> str:
        value = re.sub(
            r"[^A-Za-z0-9._-]+",
            "-",
            f"auto-{dataset_id}-{model}-{launch_id[:8]}",
        ).strip("-.")
        return value[:100] or f"auto-{launch_id[:8]}"

    def run(
        self,
        *,
        dataset_id: str,
        model: str,
        model_max_tokens: int | None,
        launch_id: str,
        model_binding: Mapping[str, Any] | None = None,
        task_type: str | None = None,
        execution_count: int | None = None,
    ) -> dict[str, Any]:
        _ensure_fresh_builtin_auth(self.repo_root, model_binding)
        query_set = self.ops.get_query_set(dataset_id)
        with tempfile.TemporaryDirectory(prefix="box-agent-eval-") as temp_dir:
            dataset, available_count = self._materialize_query_set(
                query_set,
                Path(temp_dir),
                task_type,
            )
            count = execution_count if execution_count is not None else available_count
            if count < 1 or count > available_count:
                raise EvaluationLaunchError(
                    f"执行条数必须在 1 到 {available_count} 之间"
                )
            command = [
                sys.executable,
                str(self.repo_root / "test_workspace" / "run_acp_eval.py"),
                "--repo-root",
                str(self.repo_root),
                "--dataset",
                str(dataset),
                "--count",
                str(count),
                "--parallelism",
                "1",
                "--title",
                self._title(dataset_id, model, launch_id),
            ]
            if model_binding is not None:
                command.extend(
                    (
                        "--model-binding-json",
                        json.dumps(
                            dict(model_binding),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    )
                )
            else:
                command.extend(("--model", model))
                if model_max_tokens is not None:
                    command.extend(("--model-max-tokens", str(model_max_tokens)))
            completed = subprocess.run(
                command,
                cwd=self.repo_root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        output = completed.stdout[-8000:]
        output_path: Path | None = None
        for line in output.splitlines():
            if line.startswith("输出目录:"):
                output_path = Path(line.partition(":")[2].strip())
                break
        if output_path is None:
            raise EvaluationLaunchError(
                f"评测进程没有返回输出目录（退出码 {completed.returncode}）"
            )
        try:
            run_name = output_path.resolve().relative_to(
                self.repo_root / "test_workspace" / "outputs"
            ).as_posix()
        except ValueError as error:
            raise EvaluationLaunchError("评测进程返回了非法输出目录") from error
        if "/" in run_name or not run_name:
            raise EvaluationLaunchError("评测进程返回了非法运行目录")
        return {
            "return_code": completed.returncode,
            "run_name": run_name,
            "output": output,
        }
