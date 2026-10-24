#!/usr/bin/env python3
"""Small Serper-compatible web-search fallback for the PPT Skill family."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any
from urllib import error, parse, request

from env_config import load_ppt_env


DEFAULT_BASE_URL = "https://google.serper.dev"
DEFAULT_TIMEOUT_SECONDS = 30
ENV_LOAD = load_ppt_env()


def _first_env(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _emit(status: str, **payload: Any) -> None:
    print(json.dumps({"status": status, "provider": "bundled", **payload}, ensure_ascii=False))


def _valid_url(value: str) -> bool:
    parsed = parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _configuration() -> tuple[str, str]:
    explicit_url = _first_env("SN_PPT_WEB_SEARCH_URL")
    if explicit_url:
        url = explicit_url
    else:
        base = _first_env("SN_PPT_SEARCH_BASE_URL", "SERPER_BASE_URL") or DEFAULT_BASE_URL
        url = f"{base.rstrip('/')}/search"
    key = _first_env(
        "SN_PPT_WEB_SEARCH_API_KEY",
        "SN_PPT_SEARCH_API_KEY",
        "SERPER_API_KEY",
    )
    return url, key


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run bundled PPT web search.")
    parser.add_argument("query", nargs="?", help="Search query.")
    parser.add_argument(
        "--query",
        dest="query_option",
        help="Alias for the positional query.",
    )
    parser.add_argument("--num", type=int, default=None, help="Maximum result count.")
    parser.add_argument("--count", type=int, default=None, help="Alias for --num.")
    parser.add_argument("--page", type=int, default=1, help="Search result page.")
    parser.add_argument("--gl", default="", help="Optional country bias.")
    parser.add_argument("--hl", default="", help="Optional language bias.")
    return parser


def _resolved_query_and_num(
    parser: argparse.ArgumentParser,
    args: argparse.Namespace,
) -> tuple[str, int]:
    query_values = [
        value.strip()
        for value in (args.query, args.query_option)
        if value and value.strip()
    ]
    if not query_values:
        parser.error("a query is required (positional QUERY or --query QUERY)")
    if len(set(query_values)) > 1:
        parser.error("positional QUERY and --query must match when both are provided")

    if args.num is not None and args.count is not None and args.num != args.count:
        parser.error("--num and --count must match when both are provided")
    result_count = args.num if args.num is not None else args.count
    return query_values[0], 10 if result_count is None else result_count


def _post(url: str, key: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-API-KEY": key},
        method="POST",
    )
    timeout = float(
        os.environ.get("SN_PPT_TOOL_TIMEOUT_SECONDS", str(DEFAULT_TIMEOUT_SECONDS))
    )
    with request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8", errors="replace")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("search endpoint returned a non-object response")
    return data


def main(argv: list[str] | None = None) -> int:
    _emit("unavailable", reason="offline_package")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
