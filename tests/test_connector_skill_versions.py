"""Connector receipts select exactly one immutable Skill version for the Agent."""

import json
from pathlib import Path

from box_agent.tools.skill_loader import SkillLoader


def test_switching_receipts_reloads_only_the_published_version(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    receipts = root / ".installations"
    receipts.mkdir(parents=True)
    legacy = root / "connector-kdocs" / "kdocs" / "SKILL.md"
    revision = "v-2.0.0-" + "a" * 32
    current = tmp_path / "skill-versions" / "connector-kdocs" / revision / "kdocs" / "SKILL.md"
    for file, version in [(legacy, "v1"), (current, "v2")]:
        file.parent.mkdir(parents=True)
        file.write_text(f"---\nname: kdocs\ndescription: Docs\n---\n{version}", encoding="utf-8")
    loader = SkillLoader(sources=[(root, "connector")])
    loader.discover_skills()
    assert loader.get_skill("kdocs").skill_path == legacy

    receipt = receipts / "kdocs.json"
    entry = {"scope": "connector", "directory": "connector-kdocs", "revision": revision}
    receipt.write_text(json.dumps({"connectorId": "kdocs", "entries": [entry]}), encoding="utf-8")
    loader.maybe_reload()
    skill = loader.get_skill("kdocs")
    assert (skill.skill_path, skill.owner_id) == (current, "kdocs")
    assert "v2" in skill.content
    assert "v1" in legacy.read_text(encoding="utf-8")

    # Even a broken or retired publication cannot silently resurrect the v1 directory.
    invalid_path = {**entry, "revision": "../../outside"}
    for bad in [
        b"{",
        json.dumps({"connectorId": "kdocs", "entries": [invalid_path]}).encode(),
        json.dumps({"connectorId": "kdocs", "entries": [entry, entry]}).encode(),
        b'{"connectorId":"kdocs","entries":[]}',
    ]:
        receipt.write_bytes(bad)
        loader.maybe_reload()
        assert loader.get_skill("kdocs") is None
