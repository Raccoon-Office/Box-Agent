"""Instructions and result validation for an independent cloud task."""

import json
from pathlib import Path


def cloud_handoff_instruction(
    user_text: str, workspace: Path, *, ui_language: str = "",
) -> str:
    """Expand the user's explicit handoff action before normal persistence."""
    if ui_language == "zh":
        return (
            "请基于当前本地对话，整理一个可独立执行的云端任务。"
            "只返回一个 JSON 对象，且仅包含以下字段："
            '{"instruction":"...","dependency_paths":["..."]}。'
            "instruction 必须自包含，写明目标、已有结论、约束、进度和后续工作。"
            "云端任务使用不同的会话，无法看到当前对话或本地文件。"
            "请总结任务，不要继续执行任务。"
            "只列出对话中已知、必要且现存的依赖文件，不扫描工作区，不猜测未知路径。"
            "使用本地文件的绝对路径，不使用 URL 或目录；没有已知依赖时返回 []。"
            "未知依赖由用户后续补充。不要上传文件或创建云端会话。"
            "本轮是总结请求，不代表批准任何待审批计划。"
            "不要输出 Markdown 围栏或额外说明。"
            f"当前本地会话的工作目录为 {str(workspace)!r}。\n\n"
            f"用户的交接请求：\n{user_text}"
        )
    return (
        "Prepare an independent cloud task from the current local conversation. "
        "Return ONLY a JSON object with exactly these fields: "
        '{"instruction":"...","dependency_paths":["..."]}. '
        "The instruction must be self-contained: include the goal, established findings, "
        "constraints, progress and remaining work. The cloud task has a different session "
        "and cannot see this conversation or local files. Summarize the work; do not "
        "continue executing it. List only known, necessary existing dependency files "
        "from the conversation. Do not scan the workspace or guess unknown paths. "
        "Use absolute local file paths (not URLs or directories); use [] if none are known. "
        "Unknown dependencies can be supplied by the user later. Do not upload files "
        "or create a cloud session. Treat this as a summary request, not approval of "
        "any pending plan. Do not include Markdown fences or additional prose. "
        f"The local session working directory is {str(workspace)!r}.\n\n"
        f"User's handoff request:\n{user_text}"
    )


def validate_cloud_handoff(content: str) -> None:
    """Reject incomplete JSON and unusable local dependency references."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    try:
        value = json.loads(content, object_pairs_hook=unique_object)
    except (ValueError, TypeError) as exc:
        raise ValueError("Cloud handoff must be a JSON object") from exc
    if not isinstance(value, dict) or set(value) != {"instruction", "dependency_paths"}:
        raise ValueError("Cloud handoff requires instruction and dependency_paths only")
    if not isinstance(value["instruction"], str) or not value["instruction"].strip():
        raise ValueError("Cloud handoff instruction must be nonempty")
    paths = value["dependency_paths"]
    if not isinstance(paths, list):
        raise ValueError("Cloud handoff dependency_paths must be a list")
    for path in paths:
        if not isinstance(path, str) or not path.strip() or "\x00" in path:
            raise ValueError("Cloud handoff dependency paths must be nonempty strings")
        try:
            candidate = Path(path)
            valid = candidate.is_absolute() and candidate.is_file()
        except (OSError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Cloud handoff dependencies must be existing absolute file paths")
