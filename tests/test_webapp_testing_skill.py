from pathlib import Path


SKILL_ROOT = (
    Path(__file__).resolve().parents[1]
    / "box_agent"
    / "skills"
    / "webapp-testing"
)


def test_static_html_guidance_uses_ephemeral_http_and_cleanup() -> None:
    instructions = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "http.server 0 --bind 127.0.0.1" in instructions
    assert "bash_output" in instructions
    assert "bash_kill" in instructions
    assert 'lifetime="turn"' in instructions
    assert 'lifetime="runtime"' in instructions
    assert "Using file:// URLs for local HTML" not in instructions


def test_static_html_example_owns_server_lifecycle() -> None:
    example = (
        SKILL_ROOT / "examples" / "static_html_automation.py"
    ).read_text(encoding="utf-8")

    assert "ThreadingHTTPServer(('127.0.0.1', 0)" in example
    assert "page.goto(page_url)" in example
    assert "page.goto(file_url)" not in example
    assert "server.shutdown()" in example
    assert "server.server_close()" in example
