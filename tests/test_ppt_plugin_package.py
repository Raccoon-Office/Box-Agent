"""The real PPT plugin installs and runs resource helpers outside the builtin tree."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import subprocess
import sys
import zipfile

import pytest

from box_agent.plugins.manifest import load_manifest
from box_agent.plugins.package_store import PackageStore, package_digest
from box_agent.plugins.skill_sources import build_skill_plan
from box_agent.tools.skill_loader import SkillLoader
from tests.pptx_test_support import PPT_ALIASES


REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "plugins/ppt"
BUILDER = runpy.run_path(str(REPO / "scripts/build_ppt_plugin.py"))
IDS = {"pptx", "ppt_fast", "sn_ppt_entry", "sn_ppt_story", "sn_ppt_standard",
       "sn_ppt_dazzle", "sn_ppt_tools", "sn_ppt_doctor"}
NODE = shutil.which("node")


@pytest.fixture(scope="module")
def installed(tmp_path_factory):
    store = PackageStore(tmp_path_factory.mktemp("ppt-store"))
    receipt = store.install(PACKAGE)
    root = Path(receipt["package_root"])
    yield root, receipt
    # Managed snapshots are deliberately read-only; make only our temporary
    # test fixture removable for pytest's normal cleanup.
    for directory, _, files in os.walk(store.root):
        Path(directory).chmod(0o700)
        for filename in files:
            (Path(directory) / filename).chmod(0o600)


def test_real_package_inventory_covers_every_file_and_hash():
    inventory = json.loads((PACKAGE / "package-files.json").read_text())
    actual = {p.relative_to(PACKAGE).as_posix(): p
              for p in BUILDER["package_files"](PACKAGE) if p.name != "package-files.json"}
    assert actual.keys() == inventory["files"].keys()
    for relative, path in actual.items():
        assert inventory["files"][relative] == {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size,
        }, relative
    assert sum(p.stat().st_size for p in actual.values()) < 512 * 1024 * 1024
    assert max(p.stat().st_size for p in actual.values()) < 64 * 1024 * 1024


def test_all_eight_skills_load_from_immutable_installed_snapshot(installed):
    root, receipt = installed
    manifest = load_manifest(root)
    declarations = manifest.data["contributes"]["skills"]
    assert {item["id"] for item in declarations} == IDS
    assert package_digest(root) == receipt["package_digest"]
    assert not (root / "plugin.yaml").stat().st_mode & 0o222
    standard = next(item for item in declarations if item["id"] == "sn_ppt_standard")
    assert "generate_image" not in standard["requires_host_tools"]
    snapshot = {"generation": "test", "instances": {"team": {
        "manifest": manifest.data, "package_root": str(root), "enabled": True,
        "aliases": {"team-ppt": "pptx"}, "config": {"language": "en-US", "preferred_mode": "fast"},
        "config_revision": 1, "package_digest": receipt["package_digest"],
    }}}
    plan = build_skill_plan(snapshot)
    assert len(plan.skills) == 8
    bound = {skill.metadata["plugin"]["contribution_id"]: skill for skill in plan.skills}
    assert set(bound) == IDS and bound["pptx"].name == "team-ppt"
    assert len({skill.name for skill in plan.skills}) == 8
    assert all(bound[name].name.startswith(f"p__team__{name}") for name in IDS - {"pptx"})
    for skill in plan.skills:
        assert skill.skill_path.is_relative_to(root)
        assert "{{skill:" not in skill.content
    entry = bound["sn_ppt_entry"]
    assert 'skill_name="team-ppt"' in entry.content
    assert bound["sn_ppt_story"].name in entry.content


def test_disabling_installed_ppt_never_reveals_legacy_builtin_files(installed, tmp_path):
    root, receipt = installed
    builtin = REPO / "box_agent/skills"
    snapshot = {"generation": "ppt-enabled", "instances": {"ppt": {
        "manifest": load_manifest(root).data, "package_root": str(root),
        "package_digest": receipt["package_digest"], "config_revision": 1,
        "config": {"language": "auto", "preferred_mode": "ask"},
        "enabled": True, "aliases": PPT_ALIASES,
    }}}
    def catalog():
        plan = build_skill_plan(snapshot)
        loader = SkillLoader(sources=[(builtin, "builtin")],
                             skill_settings_path=tmp_path / "settings.json",
                             **plan.loader_arguments())
        loader.discover_skills()
        return loader

    enabled = catalog()
    for name in PPT_ALIASES:
        assert enabled.get_skill(name).source == "plugin"
        assert enabled.get_skill(name).skill_path.is_relative_to(root)
    snapshot["instances"]["ppt"]["enabled"] = False
    snapshot["generation"] = "ppt-disabled"
    disabled = catalog()
    assert (builtin / "pptx/SKILL.md").is_file()
    assert (builtin / "document-skills/pptx/SKILL.md").is_file()
    assert not set(disabled.list_skills()).intersection(PPT_ALIASES)
    assert all(disabled.get_skill(name) is None for name in PPT_ALIASES)
    assert disabled.get_skill("docx") is not None


def test_user_ppt_switch_controls_all_seven_methods_without_changing_plugin_activation(installed, tmp_path):
    root, receipt = installed
    snapshot = {"generation": "ppt-user-switch", "instances": {"ppt": {
        "manifest": load_manifest(root).data, "package_root": str(root),
        "package_digest": receipt["package_digest"], "config_revision": 1,
        "config": {"language": "zh-CN", "preferred_mode": "fast"},
        "enabled": True, "aliases": PPT_ALIASES,
    }}}
    plan = build_skill_plan(snapshot)
    settings = tmp_path / "skill-settings.json"
    settings.write_text(json.dumps({"disabledPluginSkills": [{
        "plugin_id": "com.box.ppt", "instance_id": "ppt", "contribution_id": "pptx",
    }]}))
    loader = SkillLoader(sources=[], skill_settings_path=settings, **plan.loader_arguments())
    loader.discover_skills()
    assert not set(loader.list_skills()).intersection(PPT_ALIASES)
    rows = loader.list_skills_metadata(include_disabled=True)
    assert len(rows) == 8 and all(row["disabled"] for row in rows)
    assert [row["name"] for row in rows if row["user_visible"]] == ["pptx"]
    assert all(skill.plugin_enabled_by == ("pptx",) for skill in plan.skills if skill.name != "pptx")
    settings.write_text(json.dumps({"disabledPluginSkills": []}))
    assert loader.maybe_reload()
    assert set(loader.list_skills()) == set(PPT_ALIASES)
    assert snapshot["instances"]["ppt"]["enabled"] is True
    assert plan.generation == "ppt-user-switch"
    assert package_digest(root) == receipt["package_digest"]


def test_configuration_defaults_and_explicit_mode_confirmation_are_preserved():
    manifest = load_manifest(PACKAGE)
    config, errors = manifest.normalize_config({})
    assert not errors and config == {"language": "auto", "preferred_mode": "ask"}
    assert manifest.normalize_config({"language": "ja"})[1]
    assert manifest.normalize_config({"api_key": "not-a-real-key"})[1]
    public = (PACKAGE / "skills/pptx/SKILL.md").read_text()
    assert "不能跳过选择卡" in public and "用户本轮明确要求" in public
    assert "request_user_decision" in public
    entry = (PACKAGE / "skills/presentation-suite/skills/sn-ppt-entry/SKILL.md").read_text()
    example = json.loads(re.findall(r"```json\n(.*?)\n```", entry, re.S)[0])
    assert example["choices"]["static_postprocess"] == ["pptx"]
    assert example["ppt_mode"] == "standard"
    refs = set()
    for path in (PACKAGE / "skills").rglob("*.md"):
        refs.update(re.findall(r"\{\{skill:([^}]+)\}\}", path.read_text()))
        if path.name != "SKILL.md":
            assert "{{skill:" not in path.read_text(), path
    assert refs <= IDS and {"pptx", "ppt_fast", "sn_ppt_entry"} <= refs


def test_every_language_configured_entry_preserves_one_content_language_contract():
    """A direct backend load must not silently revert to prompt/UI language."""
    declarations = load_manifest(PACKAGE).data["contributes"]["skills"]
    contracts = {}
    for item in declarations:
        if "language" not in item.get("config_keys", []):
            continue
        text = (PACKAGE / item["path"] / "SKILL.md").read_text()
        matches = re.findall(
            r"^## Presentation content language\n(.*?)(?=^## |\Z)", text, re.S | re.M
        )
        assert len(matches) == 1, item["id"]
        # Entry introductions may follow the contract before their next heading.
        contract = matches[0].split("report any unmet requirement accurately.", 1)[0]
        assert "report any unmet requirement accurately." in matches[0], item["id"]
        contracts[item["id"]] = contract
    assert set(contracts) == IDS - {"sn_ppt_tools", "sn_ppt_doctor"}
    assert len(set(contracts.values())) == 1
    contract = " ".join(next(iter(contracts.values())).split())
    for rule in (
        "Read `values.language`",
        "explicit request for the presentation's output language takes priority",
        "output language explicitly confirmed for this same task",
        "language used to write a prompt is not, by itself, an explicit output-language choice",
        "newly authored or rewritten content, including a new note added to an existing deck",
        "`zh-CN` means Simplified Chinese; `en-US` means English",
        "With `auto` or no injected setting, follow the existing deck's language when editing",
        "Preserve existing text that is outside the requested edit",
        "explicitly asks to keep or quote verbatim, in its original language",
        "does not authorize translating the rest of a deck",
        "Keep response language separate from presentation content language",
        "Pass the resolved content language and verbatim-preservation constraints",
        "check newly added or rewritten text against that value",
    ):
        assert rule in contract
    fast = (PACKAGE / "skills/document-skills/pptx/SKILL.md").read_text()
    assert "This section governs chat replies only, not presentation content." in fast
    standard = (PACKAGE / "skills/presentation-suite/skills/sn-ppt-standard/SKILL.md").read_text()
    assert "默认采用用户 query 的主要语言；用户明确指定交付语言时单独覆盖" not in standard
    assert "不能用 query 语言覆盖实例默认语言" in standard


def test_fonts_provenance_and_relative_import_closure_survive_relocation(installed, monkeypatch):
    root, _ = installed
    suite = root / "skills/presentation-suite"
    assert len(list((suite / "fonts").glob("*.ttf"))) == 45
    assert (suite / "fonts/OFL-1.1.txt").is_file()
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    helper = runpy.run_path(str(suite / "skills/sn-ppt-standard/scripts/font_bundle.py"))
    assert suite / "fonts" in helper["_font_source_dirs"]()
    provenance = json.loads((suite / "source.json").read_text())
    actual = {p.relative_to(suite).as_posix() for p in suite.rglob("*") if p.is_file()}
    assert actual == set(provenance["files"]) | {"source.json"}
    for relative, record in provenance["files"].items():
        data = (suite / relative).read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            pass
        else:
            data = data.replace(b"\r\n", b"\n")
        assert hashlib.sha256(data).hexdigest() == record["sha256"], relative
    exporter = suite / "skills/sn-ppt-standard/scripts/export_pptx"
    for path in exporter.rglob("*.mjs"):
        text = path.read_text()
        for specifier in re.findall(r"(?:from\s*|import\s*\()['\"](\.[^'\"]+)['\"]", text):
            assert (path.parent / specifier).is_file(), (path, specifier)
        if "test" not in path.relative_to(exporter).parts:
            for specifier in re.findall(r"new URL\(['\"](\.[^'\"]+)['\"], import.meta.url\)", text):
                assert (path.parent / specifier).is_file(), (path, specifier)
    notices = (suite / "THIRD_PARTY_NOTICES.md").read_text()
    for relative in re.findall(r"`([^`]+)`", notices):
        assert (suite / relative).exists(), relative
    for path in (root / "skills").rglob("*.md"):
        assert "box_agent/skills/" not in path.read_text(), path


def test_media_configuration_never_discovers_other_applications(tmp_path, monkeypatch):
    module = runpy.run_path(str(PACKAGE / "skills/presentation-suite/skills/sn-ppt-tools/scripts/env_config.py"))
    monkeypatch.delenv("SN_PPT_ENV_FILE", raising=False)
    monkeypatch.setenv("OPENCLAW_SHELL", "1")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for app in (".hermes", ".openclaw"):
        (tmp_path / app).mkdir()
        (tmp_path / app / ".env").write_text("PPT_TEST_SENTINEL=must-not-be-loaded\n")
    assert module["resolve_env_path"]() == (None, "host_process")
    assert module["load_ppt_env"]().loaded_keys == ()
    assert "PPT_TEST_SENTINEL" not in os.environ
    configured = tmp_path / "explicit.env"
    configured.write_text("PPT_TEST_EXISTING=file\nPPT_TEST_ADDED=value\nINVALID LINE\n")
    monkeypatch.setenv("SN_PPT_ENV_FILE", str(configured))
    monkeypatch.setenv("PPT_TEST_EXISTING", "host")
    monkeypatch.delenv("PPT_TEST_ADDED", raising=False)
    result = module["load_ppt_env"]()
    assert os.environ["PPT_TEST_EXISTING"] == "host"
    assert os.environ["PPT_TEST_ADDED"] == "value"
    assert result.invalid_lines == (3,)
    assert result.loaded_keys == ("PPT_TEST_ADDED",)
    assert "value" not in json.dumps(result.to_dict())


def test_archive_is_deterministic_and_does_not_need_builtin_sources(tmp_path):
    package = tmp_path / "standalone"
    package.mkdir()
    (package / "plugin.yaml").write_text("id: test.ppt\nversion: 1.0.0\n")
    (package / "resource.txt").write_text("独立资源\n")
    first = BUILDER["build_archive"](package, tmp_path / "first.zip")
    second = BUILDER["build_archive"](package, tmp_path / "second.zip")
    assert first["sha256"] == second["sha256"]
    with zipfile.ZipFile(first["path"]) as archive:
        assert set(archive.namelist()) == {"plugin.yaml", "resource.txt", "package-files.json"}
    (package / "node_modules").mkdir()
    with pytest.raises(ValueError, match="cache/dependency"):
        BUILDER["build_archive"](package, tmp_path / "blocked.zip")


@pytest.mark.parametrize("version,output_name", [
    ("1.0.3", None),
    ("2.4.0-preview.1", None),
    ("2.4.0-preview.1", "custom-ppt.zip"),
])
def test_cli_archive_name_tracks_manifest_version_and_respects_explicit_output(
    tmp_path, monkeypatch, capsys, version, output_name,
):
    package = tmp_path / "standalone"
    package.mkdir()
    (package / "plugin.yaml").write_text(f"id: test.ppt\nversion: {version}\n")
    (package / "resource.txt").write_text("standalone resource\n")
    args = ["build_ppt_plugin.py", "--package-root", str(package)]
    expected = tmp_path / "workspace/plugin-dist" / f"ppt-plugin-{version}.zip"
    if output_name:
        expected = tmp_path / output_name
        args.extend(["--output", str(expected)])
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setitem(BUILDER["main"].__globals__, "REPO", tmp_path)

    BUILDER["main"]()

    assert Path(json.loads(capsys.readouterr().out)["path"]) == expected
    assert list(tmp_path.rglob("*.zip")) == [expected]
    with zipfile.ZipFile(expected) as archive:
        assert archive.read("plugin.yaml") == (package / "plugin.yaml").read_bytes()


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the resource CLI")
def test_exporter_fails_without_host_dependencies_and_never_installs(tmp_path, installed):
    root, receipt = installed
    exporter = root / "skills/presentation-suite/skills/sn-ppt-standard/scripts/export_pptx/html_to_pptx.mjs"
    env = dict(os.environ, NODE_PATH="", BOX_AGENT_PLAYWRIGHT_MODULE_PATH=str(tmp_path / "missing-playwright"),
               PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run([NODE, str(exporter), "--deck-dir", str(tmp_path)], env=env,
                            text=True, capture_output=True, timeout=15)
    assert result.returncode != 0
    report = json.loads(result.stdout)
    assert report["status"] == "failed" and report["converted"] == 0
    assert "host dependencies unavailable" in report["detail"]
    assert not list(root.rglob("node_modules"))
    assert package_digest(root) == receipt["package_digest"]
    help_result = subprocess.run([NODE, str(exporter), "--help"], env=env,
                                 text=True, capture_output=True, timeout=10)
    assert help_result.returncode == 0 and "--deck-dir" in help_result.stdout


@pytest.mark.skipif(NODE is None, reason="Node is needed for the HTML bridge contract test")
def test_html_bridge_preserves_drafts_and_revision_bound_saves(tmp_path):
    # Execute the actual single-file UI script against a minimal DOM/port host.
    # Browser sandbox/CSP enforcement belongs to the frontend's integration test.
    script = r'''
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');
const html = fs.readFileSync(process.argv[2], 'utf8');
const source = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const saveTag = html.match(/<button\b[^>]*\bid="save"[^>]*>/)[0];
assert.match(saveTag, /\btype="button"/);
const flush = () => new Promise(resolve => setImmediate(resolve));
function setup(locale, theme) {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {value:'', textContent:'', dataset:{}, handlers:{},
      classList:{toggle(){}}, setAttribute(){}, addEventListener(type, fn){this.handlers[type]=fn;}});
    return elements.get(id);
  }
  const translated = [...html.matchAll(/data-text="([^"]+)"/g)].map((match, i) => {
    const item = element('text-'+i); item.dataset.text = match[1]; return item;
  });
  const doc = {documentElement:{dataset:{}}, getElementById:element, querySelectorAll:()=>translated};
  const handlers = {}, parent = {}, messages = [], timers = new Map(); let timer=0;
  let mode='save', saved=3;
  const snapshot = () => ({config:{language:'auto',preferred_mode:'ask'},saved_revision:saved,
    active_revision:2, active_enabled:true});
  const port = {start(){},close(){},postMessage(message){
    messages.push(message);
    if(message.type !== 'request') return;
    const respond = (ok,result,error) => queueMicrotask(()=>port.onmessage({data:{type:'response',id:message.id,ok,result,error}}));
    if(message.method==='config.get') respond(true,snapshot());
    else if(message.method==='config.validate') respond(true,{valid:true,errors:[],config:message.params.config});
    else if(message.method==='config.save') {
      if(mode==='conflict') respond(false,null,{code:'config_conflict'});
      else if(mode!=='timeout') { saved++; respond(true,{...snapshot(),config:message.params.config}); }
    } else throw Error('Unexpected bridge method');
  }};
  const context = {document:doc,parent,crypto:webcrypto,console,Uint32Array,Map,Promise,
    setTimeout(fn){timers.set(++timer,fn);return timer;},clearTimeout(id){timers.delete(id);},
    window:{addEventListener(type,fn){handlers[type]=fn;}}};
  vm.runInNewContext(source,context);
  const init = {data:{type:'box-plugin-config:init',bridge_version:1,nonce:'0123456789abcdef',locale,theme},ports:[port]};
  handlers.message({...init,source:{}}); assert.equal(messages.length,0);
  handlers.message({...init,source:parent});
  return {element,doc,port,messages,timers,handlers,setMode:value=>mode=value};
}
(async()=>{
  for(const locale of ['zh','en','ja']) for(const theme of ['light','dark']) {
    const ui=setup(locale,theme); await flush();
    assert.equal(ui.doc.documentElement.lang,{zh:'zh-CN',en:'en-US',ja:'ja'}[locale]);
    assert.equal(ui.doc.documentElement.dataset.theme,theme);
    assert.equal(ui.messages[0].type,'box-plugin-config:ready');
    ui.element('language').value='en-US'; ui.element('preferred-mode').value='design';
    ui.port.onmessage({data:{type:'context.changed',locale:'ja',theme:'dark'}});
    assert.equal(ui.element('language').value,'en-US');
    assert.equal(ui.element('preferred-mode').value,'design');
    const beforeSubmit = ui.messages.length; let prevented = false;
    ui.element('config-form').handlers.submit({preventDefault(){prevented=true;}});
    await flush(); assert.equal(prevented,true); assert.equal(ui.messages.length,beforeSubmit);
    ui.setMode('conflict'); await ui.element('save').handlers.click();
    assert.match(ui.element('status').textContent,/別のウィンドウ/);
    assert.equal(ui.element('language').value,'en-US');
    const request=ui.messages.find(m=>m.method==='config.save');
    assert.equal(request.params.expected_revision,3);
    assert.equal(Object.hasOwn(request.params,'instance_id'),false);
    ui.setMode('timeout'); const pending=ui.element('save').handlers.click();
    await flush(); for(const timer of [...ui.timers.values()]) timer(); await pending;
    ui.setMode('save'); await ui.element('save').handlers.click();
    const saves=ui.messages.filter(m=>m.method==='config.save');
    assert.equal(new Set(saves.map(m=>m.params.request_id)).size,1);
    assert.equal(ui.element('language').value,'en-US');
    assert.match(ui.element('status').textContent,/保存しました/);
    assert.ok(ui.messages.filter(m=>m.type==='request').every(m=>['config.get','config.validate','config.save'].includes(m.method)));
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    harness = tmp_path / "ui-contract.cjs"
    harness.write_text(script)
    result = subprocess.run([NODE, str(harness), str(PACKAGE / "ui/config.html")],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
