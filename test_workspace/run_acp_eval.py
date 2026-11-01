"""通过 ACP 入口运行标准离线评测。"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import secrets
import subprocess
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from test_workspace.refresh_box_agent_auth import (
        AuthRefreshError,
        ensure_fresh_auth,
    )
except ModuleNotFoundError:  # Direct execution puts test_workspace on sys.path.
    from refresh_box_agent_auth import AuthRefreshError, ensure_fresh_auth


DEFAULT_DATASET = Path("test_workspace/inputs/smoke_test/dataset.jsonl")


def load_case_ids(dataset: Path) -> list[str]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(dataset.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise ValueError(f"评测集第 {line_number} 行缺少有效 id")
        records.append(value)
    case_ids = [record["id"] for record in records]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("评测集包含重复 case id")
    if not case_ids:
        raise ValueError("评测集为空")
    return case_ids


def choose_cases(
    available: Sequence[str],
    count: int,
    seed: int,
    requested: Sequence[str],
) -> list[str]:
    if requested:
        if len(requested) != len(set(requested)):
            raise ValueError("--case-id 不能重复")
        missing = [case_id for case_id in requested if case_id not in available]
        if missing:
            raise ValueError(f"评测集不存在 case: {', '.join(missing)}")
        return list(requested)
    if count < 1 or count > len(available):
        raise ValueError(f"--count 必须在 1 到 {len(available)} 之间")
    return random.Random(seed).sample(list(available), count)


def run_name(now: datetime | None = None, title: str = "smoke-test") -> str:
    if not title or title != title.strip() or title in {".", ".."} or "/" in title or "\\" in title:
        raise ValueError("--title 必须是一个非空目录名称，且不能包含路径分隔符")
    return f"{(now or datetime.now()).strftime('%y%m%d-%H%M')}-{title}"


def build_command(
    repo_root: Path,
    dataset: Path,
    output_dir: Path,
    case_ids: Sequence[str],
    timeout_seconds: float | None,
    parallelism: int,
    effect_eval_url: str | None = None,
    effect_eval_timeout_seconds: float = 180.0,
    model: str | None = None,
    model_max_tokens: int | None = None,
    model_binding: dict[str, Any] | None = None,
    rate_limit_retries: int = 2,
    rate_limit_backoff_seconds: float = 120.0,
) -> list[str]:
    command = [
        "uv",
        "run",
        "--project",
        str(repo_root / "test_workspace/acp_eval"),
        "acp-eval",
        "--repo-root",
        str(repo_root),
        "--dataset",
        str(dataset),
        "--run-dir",
        str(output_dir),
        "--parallelism",
        str(parallelism),
        "--rate-limit-retries", str(rate_limit_retries),
        "--rate-limit-backoff-seconds", str(rate_limit_backoff_seconds),
    ]
    if timeout_seconds is not None:
        command.extend(("--timeout-seconds", str(timeout_seconds)))
    for case_id in case_ids:
        command.extend(("--case-id", case_id))
    if effect_eval_url:
        command.extend(
            (
                "--effect-eval-url",
                effect_eval_url,
                "--effect-eval-timeout-seconds",
                str(effect_eval_timeout_seconds),
            )
        )
    if model_binding is not None:
        command.extend(
            (
                "--model-binding-json",
                json.dumps(model_binding, ensure_ascii=False, separators=(",", ":")),
            )
        )
    elif model:
        command.extend(("--model", model))
    if model_max_tokens is not None:
        command.extend(("--model-max-tokens", str(model_max_tokens)))
    return command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="仅通过 ACP 入口运行离线评测")
    parser.add_argument("--repo-root", type=Path)
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--count", type=int, default=3)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--title", default="smoke-test")
    parser.add_argument("--case-id", action="append", default=[])
    parser.add_argument("--timeout-seconds", type=float,
                        help="显式指定单次 ACP 时限；默认按运行时等待预算计算，至少 2700 秒")
    parser.add_argument("--parallelism", type=int)
    parser.add_argument("--rate-limit-retries", type=int, choices=range(6), default=2,
                        help="尚未执行工具的 429 最多重试次数，默认 2；0 禁用重试")
    parser.add_argument("--rate-limit-backoff-seconds", type=float, default=120.0,
                        help="批内共享指数退避起点，默认 120 秒，单次最多 600 秒")
    parser.add_argument(
        "--effect-eval-url",
        default=os.environ.get("BOX_AGENT_EFFECT_EVAL_URL"),
        help="agents-eval 服务地址；默认读取 BOX_AGENT_EFFECT_EVAL_URL",
    )
    parser.add_argument(
        "--effect-eval-timeout-seconds",
        type=float,
        default=float(os.environ.get("BOX_AGENT_EFFECT_EVAL_TIMEOUT_SECONDS", "180")),
    )
    parser.add_argument("--model", help="本次评测使用的被测模型")
    parser.add_argument("--model-max-tokens", type=int)
    parser.add_argument(
        "--model-binding-json",
        help="完整的 ACP llm_binding JSON；用于自动路由等高级模型配置",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    default_root = Path(__file__).resolve().parent.parent
    repo_root = (args.repo_root or default_root).resolve()
    dataset = (args.dataset or repo_root / DEFAULT_DATASET).resolve()
    if not dataset.is_file():
        raise FileNotFoundError(f"评测集不存在: {dataset}")
    if args.timeout_seconds is not None and (not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0):
        raise ValueError("--timeout-seconds 必须为有限正数")
    if args.effect_eval_timeout_seconds <= 0:
        raise ValueError("--effect-eval-timeout-seconds 必须大于 0")
    if not math.isfinite(args.rate_limit_backoff_seconds) or not 0 < args.rate_limit_backoff_seconds <= 600:
        raise ValueError("--rate-limit-backoff-seconds 必须在 (0, 600] 内")
    if args.model_max_tokens is not None and args.model_max_tokens < 1:
        raise ValueError("--model-max-tokens 必须大于 0")
    if args.model is not None and not args.model.strip():
        raise ValueError("--model 不能为空")
    if args.model_max_tokens is not None and args.model is None:
        raise ValueError("--model-max-tokens 需要同时设置 --model")
    if args.model_binding_json is not None and args.model is not None:
        raise ValueError("--model-binding-json 不能与 --model 同时设置")
    model_binding = None
    if args.model_binding_json is not None:
        try:
            model_binding = json.loads(args.model_binding_json)
        except json.JSONDecodeError as error:
            raise ValueError("--model-binding-json 必须是有效 JSON") from error
        if not isinstance(model_binding, dict):
            raise ValueError("--model-binding-json 必须是 JSON 对象")

    uses_builtin_auth = (
        isinstance(model_binding, dict) and model_binding.get("source") == "builtin"
    ) or (isinstance(args.model, str) and args.model.strip().startswith("sn-"))
    if uses_builtin_auth:
        try:
            ensure_fresh_auth()
        except AuthRefreshError as error:
            raise RuntimeError(
                f"办公小浣熊登录自动刷新失败，尚未创建评测输出: {error}"
            ) from error

    seed = args.seed if args.seed is not None else secrets.randbits(64)
    available = load_case_ids(dataset)
    selected = choose_cases(available, args.count, seed, args.case_id)
    parallelism = args.parallelism or min(5, len(selected))
    if parallelism < 1:
        raise ValueError("--parallelism 必须大于 0")

    output_dir = repo_root / "test_workspace/outputs" / run_name(title=args.title)
    if output_dir.exists():
        raise FileExistsError(f"输出目录已存在，请下一分钟重试: {output_dir}")
    output_dir.mkdir(parents=True)
    try:
        dataset_reference = str(dataset.relative_to(repo_root))
    except ValueError:
        dataset_reference = str(dataset)
    selection = {
        "schema_version": "box-agent-acp-smoke-selection/v1",
        "mode": "explicit" if args.case_id else "random",
        "title": args.title,
        "seed": seed,
        "case_ids": selected,
        "dataset": dataset_reference,
        "model": args.model,
        "model_max_tokens": args.model_max_tokens,
        "model_binding": model_binding,
        "requested_timeout_seconds": args.timeout_seconds,
        "rate_limit_retries": args.rate_limit_retries,
        "rate_limit_backoff_seconds": args.rate_limit_backoff_seconds,
    }
    (output_dir / "selection.json").write_text(
        json.dumps(selection, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    command = build_command(
        repo_root,
        dataset,
        output_dir,
        selected,
        args.timeout_seconds,
        parallelism,
        args.effect_eval_url,
        args.effect_eval_timeout_seconds,
        args.model,
        args.model_max_tokens,
        model_binding,
        args.rate_limit_retries,
        args.rate_limit_backoff_seconds,
    )
    print(f"输出目录: {output_dir}")
    print(f"随机种子: {seed}")
    print(f"样本: {', '.join(selected)}")
    completed = subprocess.run(command, cwd=repo_root, check=False)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
