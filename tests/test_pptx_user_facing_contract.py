from pathlib import Path

import pytest


PPTX_SKILL_ROOT = (
    Path(__file__).resolve().parents[1]
    / "box_agent"
    / "skills"
    / "document-skills"
    / "pptx"
)


@pytest.mark.parametrize(
    ("locale", "complete", "usage_note", "details_heading", "qa_passed", "qa_notice"),
    (
        ("zh", "演示文稿已完成", "使用前注意", "## 生成详情", "质量检查", "检查提示"),
        (
            "en",
            "Presentation complete",
            "Before use",
            "## Generation details",
            "Quality checks",
            "Check notices",
        ),
        (
            "ja",
            "プレゼンテーションが完成しました",
            "ご利用前の注意",
            "## 生成の詳細",
            "品質チェック",
            "確認事項",
        ),
    ),
)
def test_pptx_skill_follows_the_supported_host_language_contract(
    locale,
    complete,
    usage_note,
    details_heading,
    qa_passed,
    qa_notice,
):
    skill_text = (PPTX_SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
    normalized_skill_text = " ".join(skill_text.split())

    assert "active user-visible response language" in normalized_skill_text
    assert "An explicit user language request wins" in normalized_skill_text
    assert "host `ui_language` instruction" in normalized_skill_text
    assert f"| `{locale}` |" in skill_text.split("| Role ", 1)[1].splitlines()[0]
    assert complete in skill_text
    assert usage_note in skill_text
    assert details_heading in skill_text
    assert qa_passed in skill_text
    assert qa_notice in skill_text
    assert "`deck.patch.json`, `qa/`, and `research/`" in skill_text
    assert "Use exact sections in this order" not in skill_text
    assert "must be concise Simplified Chinese" not in skill_text


def test_pptx_qa_reference_uses_localized_labels_and_keeps_artifact_paths():
    qa_text = (PPTX_SKILL_ROOT / "references" / "qa.md").read_text(encoding="utf-8")

    assert "active response language" in qa_text
    assert "Render `qa_ok` and `qa_warnings` with the matching" in qa_text
    assert "QA labels from `SKILL.md` §6" in qa_text
    assert "generation-details heading localized" in qa_text
    assert "`deck.patch.json`" in qa_text
    assert "`qa/` and `research/`" in qa_text
    assert "Simplified-Chinese user impact" not in qa_text


def test_pptx_keeps_intermediate_images_private_and_overview_optional():
    text = " ".join((PPTX_SKILL_ROOT / "SKILL.md").read_text().split())
    images = " ".join((PPTX_SKILL_ROOT / "references/image-assets.md").read_text().split())
    assert "`publish_artifact: false`" in text
    assert "`publish_artifact: false`" in images
    assert "Do not embed them in progress messages or the final reply" in text
    assert "Never block delivery on this preview" in text
    assert "Do not install dependencies just for this optional preview" in text
    assert "If any slide is missing, omit the overview" in text
    assert "scripts/make_contact_sheet.js qa/overview-slides" in text
    assert "--cols 4 --thumb-width 480" in text
    assert "12 slides form 4 columns by 3 rows" in text
    assert "screenshot each slide element separately" in text
    assert "Do not use a full-page scrolling screenshot" in text
    assert "Embed only the successfully created contact sheet once" in text
