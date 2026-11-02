from pathlib import Path

import pytest

from box_agent.tools.skill_loader import SkillLoader
from box_agent.tools.skill_preload import build_auto_loaded_skills_prompt, turn_preload_skill_names


@pytest.mark.parametrize('skill_name,prompt', [
    ('mac-cua', '通过 Driver 打开 Word 文档'),
    ('office-gui-basics', '在 Word 中修改标题并另存为副本'),
])
def test_explicit_cua_skill_is_discoverable_and_loads_from_builtin_manifest(skill_name, prompt):
    root = Path(__file__).resolve().parents[1] / 'box_agent' / 'skills'
    loader = SkillLoader(sources=[(root, 'builtin')])
    loader.discover_skills()
    names = turn_preload_skill_names((), None, prompt,
                                     selected_skill_names=(skill_name,))
    loaded = build_auto_loaded_skills_prompt(loader, 'base system', names)
    assert loaded.loaded_names == (skill_name,)
    skill = loader.get_skill(skill_name)
    assert skill is not None
    assert skill.content
    assert skill.content in loaded.system_prompt
