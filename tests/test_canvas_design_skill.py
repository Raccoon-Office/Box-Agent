import shutil
from pathlib import Path

from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import (
    build_auto_loaded_skills_prompt,
    resolve_explicit_skill_invocation,
)


def test_installed_canvas_design_preloads_bounded_review_after_complete_delivery(tmp_path):
    source = Path(__file__).resolve().parents[1] / "box_agent" / "skills" / "canvas-design"
    installed = tmp_path / "canvas-design"
    installed.mkdir()
    shutil.copyfile(source / "SKILL.md", installed / "SKILL.md")
    loader = SkillLoader(sources=[(tmp_path, "user")])
    loader.discover_skills()

    skill = resolve_explicit_skill_invocation(
        loader, "/canvas-design 制作家园成长图文，交付 PNG、Markdown 和单文件 HTML"
    )
    assert skill is not None
    loaded = build_auto_loaded_skills_prompt(loader, "base system", (skill.name,))
    assert loaded.loaded_names == ("canvas-design",)
    prompt = loaded.system_prompt
    prerequisite = prompt.index("Before the first visual review, verify the complete requested set on disk")
    baseline = prompt.index("Build the complete baseline before visual review")
    measurements = prompt.index("Use the render script to decide measurable layout issues")
    visual_review = prompt.index("Allow at most two visual repair rounds")
    assert prerequisite < baseline < measurements < visual_review
    assert "Save the requested Markdown copy and the self-contained HTML" in prompt[:prerequisite]
    assert "When PNG, Markdown, and HTML are requested" in prompt[baseline:measurements]
    assert "nonzero exit status" in prompt[measurements:visual_review]
    assert "Run the same checks on that fallback once" in prompt[measurements:visual_review]
    assert "Keep the count across the task" in prompt[visual_review:]
