"""Evidence is outside the patch and failures remain pytest failures."""
import json
import hashlib
import subprocess
import sys
import platform
from importlib.metadata import version
from datetime import datetime, timezone
from pathlib import Path

import pytest

from .harness import BoxEngine, ModelServer, record


def pytest_addoption(parser):
    parser.addoption('--contract-evidence-root', help='New directory for immutable baseline evidence')


@pytest.fixture(scope='session')
def evidence_root(request, tmp_path_factory):
    value = request.config.getoption('--contract-evidence-root')
    root = Path(value).resolve() if value else tmp_path_factory.mktemp('behavior-baseline')
    if value:
        root.mkdir(parents=True, exist_ok=False)
    (root / 'manifest.json').write_text(json.dumps({
        'schema': 'raccoon-behavior-contract/v1', 'engine': 'box',
        'model': 'loopback scripted OpenAI fixture', 'scope': 'source ACP subprocess',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
        'python': sys.version, 'platform': platform.platform(), 'architecture': platform.machine(),
        'dependencies': {name: version(name) for name in ['pytest', 'pytest-asyncio', 'openai', 'agent-client-protocol']},
        'tracked_worktree_changes': subprocess.check_output(['git', 'diff', '--name-only', 'HEAD'], text=True).splitlines(),
        'test_files': {str(p.relative_to(Path(__file__).parents[2])): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in Path(__file__).parent.glob('*.py')},
        'claims': 'real tools and disk; not installed desktop, live model or account backend',
    }, indent=2))
    return root


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    root = item.funcargs.get('evidence_root')
    if root:
        record(root / 'results.jsonl', {'test': item.nodeid, 'phase': report.when,
            'outcome': report.outcome, 'duration': report.duration,
            'failure': str(report.longrepr) if report.failed else None})

        reports = [json.loads(line) for line in (root / 'results.jsonl').read_text().splitlines()]
        cases = {}
        for entry in reports:
            case = cases.setdefault(entry['test'], {'phases': {}, 'outcome': 'incomplete'})
            case['phases'][entry['phase']] = entry['outcome']
            values = case['phases'].values()
            if 'failed' in values:
                case['outcome'] = 'failed'
            elif 'skipped' in values:
                case['outcome'] = 'skipped'
            elif all(case['phases'].get(p) == 'passed' for p in ['setup', 'call', 'teardown']):
                case['outcome'] = 'passed'
        (root / 'summary.json').write_text(json.dumps({'cases': cases}, indent=2))


@pytest.fixture
async def lab(request, evidence_root):
    root = evidence_root / request.node.name
    root.mkdir()
    model = ModelServer(root)
    await model.start()
    engines = []

    async def create(name='box'):
        engine = BoxEngine(root / name, model)
        engines.append(engine)
        await engine.start()
        return engine

    try:
        yield root, model, create
    finally:
        for engine in engines:
            await engine.stop()
        await model.close()
        assert not model.errors, f'Model fixture errors: {model.errors}'
