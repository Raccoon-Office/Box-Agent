from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from box_agent.tools.skill_loader import SkillLoader


SKILLS_ROOT = Path(__file__).resolve().parents[1] / "box_agent" / "skills"
MIDU_SKILL_NAMES = {
    "midu-pinyin",
    "midu-proofread",
    "midu-proofread-pinyin",
    "midu-st-convert",
    "midu-writing",
}
REMOVED_MIDU_SKILL_NAMES = {
    "midu-ai-info-recognition",
    "midu-chinese-text",
    "midu-hot-search",
    "midu-political-search",
    "midu-video-content",
}
MIDU_BUSINESS_SCRIPTS = [
    ("midu-writing/scripts/midu_write.py", "midu_write"),
    ("midu-proofread/scripts/midu_proofread.py", "midu_proofread"),
    (
        "midu-proofread-pinyin/scripts/midu_proofread_pinyin.py",
        "midu_proofread_pinyin",
    ),
    ("midu-pinyin/scripts/midu_pinyin.py", "midu_pinyin"),
    ("midu-st-convert/scripts/midu_st_convert.py", "midu_st_convert"),
]


def _load_auth_module():
    script = SKILLS_ROOT / "_midu_shared" / "midu_auth.py"
    spec = importlib.util.spec_from_file_location("box_agent_test_midu_auth", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_script_module(relative_path: str, module_name: str):
    script = SKILLS_ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def _call_business_api(module, module_suffix: str, credential_source: str) -> None:
    if module_suffix == "midu_write":
        module.post_json(
            module.API_URL,
            {"thread_id": "test", "user_input": "测试"},
            30,
            "secret-must-not-leak",
            "123",
            credential_source,
        )
    elif module_suffix == "midu_pinyin":
        module._http_post_json(
            module.API_URL,
            {"text": "测试"},
            {"Authorization": "Bearer secret-must-not-leak"},
            credential_source,
        )
    elif module_suffix in {"midu_proofread", "midu_proofread_pinyin"}:
        module.call_proofread_api(
            "测试",
            "secret-must-not-leak",
            "123",
            credential_source,
        )
    elif module_suffix == "midu_st_convert":
        module.call_st_convert_api(
            "測試",
            1,
            "secret-must-not-leak",
            "123",
            credential_source,
        )
    else:  # pragma: no cover - protected by the fixed parameter table
        raise AssertionError(f"unknown Midu module: {module_suffix}")


def test_midu_skills_are_marketplace_sources_not_builtin() -> None:
    manifest = json.loads((SKILLS_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    entries = {item["name"]: item for item in manifest["skills"]}

    assert not MIDU_SKILL_NAMES & entries.keys()
    assert not REMOVED_MIDU_SKILL_NAMES & entries.keys()
    for skill_name in REMOVED_MIDU_SKILL_NAMES:
        assert not (SKILLS_ROOT / skill_name).exists()

    builtin_loader = SkillLoader(sources=[(SKILLS_ROOT, "builtin")])
    builtin_loader.discover_skills()
    for name in MIDU_SKILL_NAMES:
        assert (SKILLS_ROOT / name / "SKILL.md").is_file()
        assert builtin_loader.get_skill(name) is None


def test_midu_skills_share_one_authentication_implementation() -> None:
    shared_script = SKILLS_ROOT / "_midu_shared" / "midu_auth.py"
    shared_doc = SKILLS_ROOT / "_midu_shared" / "AUTH.md"
    assert shared_script.is_file()
    assert shared_doc.is_file()

    for name in MIDU_SKILL_NAMES:
        skill_root = SKILLS_ROOT / name
        assert not list(skill_root.rglob("midu_auth.py"))
        skill_text = (skill_root / "SKILL.md").read_text(encoding="utf-8")
        assert "../_midu_shared/AUTH.md" in skill_text


def test_midu_auth_prefers_host_credentials_and_never_outputs_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    auth = _load_auth_module()
    monkeypatch.setenv("MIDU_APP_SECRET", "host-secret")
    monkeypatch.setenv("MIDU_USER_ID", "12345")
    monkeypatch.setattr(
        auth,
        "_load_keys_file",
        lambda: {"MIDU_APP_SECRET": "legacy-secret", "MIDU_USER_ID": "999"},
    )

    assert auth.load_credentials() == ("host-secret", "12345", "environment")
    assert auth.main(["--action", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status == {
        "configured": True,
        "source": "environment",
        "userId": "12345",
    }
    assert "host-secret" not in json.dumps(status)

    monkeypatch.setattr(
        auth,
        "verify_sms",
        lambda *_args: {
            "code": "0000",
            "data": {"userVo": {"appSecret": "service-secret", "id": 88}},
        },
    )
    monkeypatch.setattr(auth, "save_credentials", lambda *_args: None)
    assert (
        auth.main(
            [
                "--action",
                "verify",
                "--mobile",
                "13800138000",
                "--sms-code",
                "123456",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "service-secret" not in output
    assert json.loads(output)["configured"] is True


def test_midu_auth_rejects_zero_user_id_without_a_valid_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = _load_auth_module()
    monkeypatch.setenv("MIDU_APP_SECRET", "host-secret")
    monkeypatch.setenv("MIDU_USER_ID", "0")
    monkeypatch.setattr(auth, "_load_keys_file", lambda: {})

    assert auth.load_credentials() == ("", "", "none")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"data": {"userVo": {"appSecret": "nested", "id": 11}}},
            ("nested", "11"),
        ),
        (
            {"userVo": {"appSecret": "top-level", "id": 12}},
            ("top-level", "12"),
        ),
        (
            {"data": {"appSecret": "flat-data", "userId": 13}},
            ("flat-data", "13"),
        ),
        (
            {"appSecret": "flat-root", "userId": 14},
            ("flat-root", "14"),
        ),
    ],
)
def test_midu_auth_preserves_original_verify_response_variants(
    payload: dict[str, object], expected: tuple[str, str]
) -> None:
    auth = _load_auth_module()
    assert auth.extract_credentials(payload) == expected


def test_midu_business_auth_hint_targets_the_executable_shared_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hint_module = _load_script_module(
        "_midu_shared/auth_hint.py", "box_agent_test_midu_auth_hint"
    )
    shared_auth = SKILLS_ROOT / "_midu_shared" / "midu_auth.py"
    hint = hint_module.build_auth_hint(sys.executable)

    assert shared_auth.is_file()
    assert str(shared_auth.resolve()) in hint
    assert "--action send --mobile <手机号>" in hint
    assert "--action verify --mobile <手机号> --sms_code <验证码>" in hint

    auth = _load_auth_module()
    parsed = auth._build_parser().parse_args(
        [
            "--action",
            "verify",
            "--mobile",
            "13800138000",
            "--sms_code",
            "123456",
        ]
    )
    assert parsed.sms_code == "123456"

    writing = _load_script_module(
        "midu-writing/scripts/midu_write.py", "box_agent_test_midu_write_auth_hint"
    )
    monkeypatch.setattr(writing, "load_credentials", lambda: ("", "", "none"))
    with pytest.raises(writing.MiduWriteError, match="通过对话完成蜜度登录") as exc_info:
        writing.load_business_credentials()
    assert str(shared_auth.resolve()) in str(exc_info.value)

    for skill_root in SKILLS_ROOT.glob("midu-*"):
        for script in skill_root.rglob("*.py"):
            text = script.read_text(encoding="utf-8")
            assert "scripts/midu_auth.py" not in text, script


@pytest.mark.parametrize(("relative_path", "module_suffix"), MIDU_BUSINESS_SCRIPTS)
def test_midu_business_scripts_share_host_and_standalone_credential_priority(
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    module_suffix: str,
) -> None:
    module = _load_script_module(
        relative_path, f"box_agent_test_{module_suffix}_credential_priority"
    )
    shared_auth = sys.modules[module.load_credentials.__module__]
    monkeypatch.setattr(
        shared_auth,
        "_load_keys_file",
        lambda: {"MIDU_APP_SECRET": "legacy-secret", "MIDU_USER_ID": "999"},
    )

    monkeypatch.setenv("MIDU_APP_SECRET", "host-secret")
    monkeypatch.setenv("MIDU_USER_ID", "12345")
    assert module.load_business_credentials() == (
        "host-secret",
        "12345",
        "environment",
    )

    monkeypatch.setenv("MIDU_USER_ID", "0")
    assert module.load_business_credentials() == (
        "legacy-secret",
        "999",
        "legacy_file",
    )

    monkeypatch.delenv("MIDU_APP_SECRET")
    monkeypatch.delenv("MIDU_USER_ID")
    assert module.load_business_credentials() == (
        "legacy-secret",
        "999",
        "legacy_file",
    )


@pytest.mark.parametrize(("relative_path", "module_suffix"), MIDU_BUSINESS_SCRIPTS)
@pytest.mark.parametrize("auth_status", [401, 403])
@pytest.mark.parametrize(
    ("credential_source", "expected_text", "unexpected_text"),
    [
        ("environment", "Officev3", "--action send"),
        ("legacy_file", "--action send", "不能覆盖宿主注入"),
    ],
)
def test_midu_auth_failure_uses_source_specific_recovery_without_leaking_secret(
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    module_suffix: str,
    auth_status: int,
    credential_source: str,
    expected_text: str,
    unexpected_text: str,
) -> None:
    module = _load_script_module(
        relative_path,
        f"box_agent_test_{module_suffix}_{credential_source}_auth_failure",
    )

    class Response:
        status_code = auth_status
        ok = False
        text = ""

    monkeypatch.setattr(module.requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(RuntimeError) as exc_info:
        _call_business_api(module, module_suffix, credential_source)

    message = str(exc_info.value)
    assert expected_text in message
    assert unexpected_text not in message
    assert "secret-must-not-leak" not in message


@pytest.mark.parametrize(
    ("convert_type", "source_text", "converted_text", "direction"),
    [
        (1, "繁體文字", "繁体文字", "繁体转简体"),
        (2, "简体文字", "簡體文字", "简体转繁体"),
    ],
)
def test_midu_st_convert_sends_documented_direction_without_network(
    monkeypatch: pytest.MonkeyPatch,
    convert_type: int,
    source_text: str,
    converted_text: str,
    direction: str,
) -> None:
    module = _load_script_module(
        "midu-st-convert/scripts/midu_st_convert.py",
        f"box_agent_test_midu_st_convert_{convert_type}",
    )
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        ok = True
        text = ""

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "code": "0000",
                "transactionId": f"tx-{convert_type}",
                "charCount": len(source_text),
                "data": {"convertedText": converted_text},
            }

    def fake_post(url, *, json, headers, timeout):
        captured.update(url=url, json=json, headers=headers, timeout=timeout)
        return Response()

    monkeypatch.setattr(module.requests, "post", fake_post)

    monkeypatch.setattr(
        module,
        "load_credentials",
        lambda: ("test-secret", "123", "legacy_file"),
    )
    result = module.convert_text(source_text, convert_type=convert_type)

    assert captured["json"] == {"convertType": convert_type, "text": source_text}
    assert captured["headers"]["X-User-Id"] == "123"
    assert module.CONVERT_TYPE_DESC[convert_type] == direction
    assert result.converted_text == converted_text

    skill_text = (SKILLS_ROOT / "midu-st-convert" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "简体转繁体使用 `--convert-type 2`" in skill_text
    assert "繁体转简体使用 `--convert-type 1`" in skill_text


def test_midu_pinyin_sanitizes_provider_html_before_preview() -> None:
    module = _load_script_module(
        "midu-pinyin/scripts/midu_pinyin.py",
        "box_agent_test_midu_pinyin_html_sanitizer",
    )
    result = module.PinyinResult(
        ruby_html=(
            '<ruby onclick="steal()">中<rt onerror="steal()">zhōng</rt></ruby>'
            '<script>steal()</script><img src="x" onerror="steal()">'
            '<a href="https://example.com">外链</a>'
        ),
        annotated_text="中(zhōng)",
        pinyin_text="zhōng",
        transaction_id="tx-safe-html",
        char_count=1,
    )

    preview = module.render_preview_html(result)

    assert "<ruby>中<rt>zhōng</rt></ruby>" in preview
    for forbidden in ("<script", "onclick", "onerror", "<img", "href=", "src="):
        assert forbidden not in preview.lower()


@pytest.mark.parametrize(
    ("relative_path", "module_suffix"),
    [
        ("midu-proofread/scripts/midu_proofread.py", "midu_proofread"),
        (
            "midu-proofread-pinyin/scripts/midu_proofread_pinyin.py",
            "midu_proofread_pinyin",
        ),
    ],
)
def test_midu_proofread_does_not_fetch_provider_result_urls(
    monkeypatch: pytest.MonkeyPatch,
    relative_path: str,
    module_suffix: str,
) -> None:
    module = _load_script_module(
        relative_path,
        f"box_agent_test_{module_suffix}_no_provider_get",
    )

    class Response:
        status_code = 200
        ok = True
        text = ""

        @staticmethod
        def json() -> dict[str, object]:
            return {
                "code": "0000",
                "transactionId": "tx-no-provider-get",
                "charCount": 2,
                "data": {
                    "proofResultJsonUrl": "http://127.0.0.1:9/result.json",
                    "erratumExcelUrl": "http://127.0.0.1:9/result.xlsx",
                    "erratumMdUrl": "http://127.0.0.1:9/internal",
                },
            }

    monkeypatch.setattr(module.requests, "post", lambda *_args, **_kwargs: Response())
    monkeypatch.setattr(
        module.requests,
        "get",
        lambda *_args, **_kwargs: pytest.fail("provider result URL must not be fetched"),
    )
    monkeypatch.setattr(
        module,
        "load_credentials",
        lambda: ("test-secret", "123", "legacy_file"),
    )

    if module_suffix == "midu_proofread_pinyin":
        outputs = module.proofread_text("测试", assemble=False)
    else:
        outputs = module.proofread_text("测试")

    assert outputs.erratum_md == ""
    assert outputs.urls.erratum_md_url == "http://127.0.0.1:9/internal"


@pytest.mark.parametrize("status_code", [401, 403])
def test_midu_writing_treats_http_entitlement_failure_as_recharge(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    module = _load_script_module(
        "midu-writing/scripts/midu_write.py",
        f"box_agent_test_midu_write_http_quota_{status_code}",
    )

    class Response:
        ok = False
        text = "quota exceeded"

        def __init__(self) -> None:
            self.status_code = status_code

    monkeypatch.setattr(module.requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(module.MiduWriteError) as exc_info:
        module.post_json(
            module.API_URL,
            {"user_input": "测试"},
            30,
            "test-secret",
            "123",
            "environment",
        )

    message = str(exc_info.value)
    assert "权益不足" in message
    assert "Officev3" not in message


def test_midu_writing_treats_body_quota_as_recharge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script_module(
        "midu-writing/scripts/midu_write.py",
        "box_agent_test_midu_write_body_quota",
    )

    class Response:
        status_code = 200
        ok = True
        text = ""

        @staticmethod
        def json() -> dict[str, str]:
            return {"code": "QUOTA_EXCEEDED", "message": "账号余额不足"}

    monkeypatch.setattr(module.requests, "post", lambda *_args, **_kwargs: Response())

    with pytest.raises(module.MiduWriteError) as exc_info:
        module.post_json(
            module.API_URL,
            {"user_input": "测试"},
            30,
            "test-secret",
            "123",
            "legacy_file",
        )

    message = str(exc_info.value)
    assert "权益不足" in message
    assert "--action send" not in message


@pytest.mark.parametrize(
    "relative_path",
    [
        "midu-proofread/scripts/midu_proofread.py",
        "midu-proofread-pinyin/scripts/midu_proofread_pinyin.py",
        "midu-pinyin/scripts/midu_pinyin.py",
        "midu-st-convert/scripts/midu_st_convert.py",
        "midu-writing/scripts/midu_write.py",
        "_midu_shared/midu_auth.py",
    ],
)
def test_midu_script_entrypoints_support_help(relative_path: str) -> None:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, str(SKILLS_ROOT / relative_path), "--help"],
        capture_output=True,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    if relative_path != "_midu_shared/midu_auth.py":
        assert "--api-key" not in result.stdout
        assert "--user-id" not in result.stdout
        assert "appSecret" not in result.stdout


def test_midu_scripts_keep_credentials_on_official_tls_endpoints() -> None:
    auth_text = (SKILLS_ROOT / "_midu_shared" / "midu_auth.py").read_text(
        encoding="utf-8"
    )
    assert "https://api.midu.com" in auth_text
    assert "verify=False" not in auth_text
    assert "CERT_NONE" not in auth_text
    assert "check_hostname = False" not in auth_text

    skill_text = "\n".join(
        path.read_text(encoding="utf-8")
        for skill_name in MIDU_SKILL_NAMES
        for path in (SKILLS_ROOT / skill_name).rglob("*.py")
    )
    assert "CERT_NONE" not in skill_text
    assert "check_hostname = False" not in skill_text
