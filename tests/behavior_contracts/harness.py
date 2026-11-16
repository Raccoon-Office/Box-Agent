"""Test-only loopback model and real-process Box adapter.

A future DSH adapter implements the same operations; it must not implement the
behavior under test. Raw wire evidence is retained, not normalized into success.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import os
import sys
from pathlib import Path
from typing import Protocol

import yaml

ROOT = Path(__file__).resolve().parents[2]


def record(path: Path, value: object) -> None:
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')


class Engine(Protocol):
    """Only test operations; results remain observable, engine-specific evidence."""
    async def open(self, workspace: Path, identity: str) -> str: ...
    async def prompt(self, session: str, scenario: str, **meta) -> dict: ...
    async def cancel(self, session: str) -> None: ...
    async def restore(self, session: str, workspace: Path) -> dict: ...
    async def stop(self, *, kill: bool = False) -> None: ...


class ModelServer:
    """Scripted OpenAI wire responses. Tools and filesystem are never mocked."""
    def __init__(self, evidence: Path):
        self.evidence = evidence
        self.scripts: dict[str, list[dict]] = {}
        self.indices: dict[str, int] = {}
        self.entered: dict[str, asyncio.Event] = {}
        self.release: dict[str, asyncio.Event] = {}
        self.requests: list[dict] = []
        self.errors: list[str] = []
        self.connections: set[asyncio.Task] = set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.url = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1'

    def script(self, name: str, *steps: dict):
        assert name not in self.scripts
        self.scripts[name] = list(steps)
        record(self.evidence / 'scripts.jsonl', {'scenario': name, 'steps': steps})
        self.indices[name] = 0
        self.entered[name] = asyncio.Event()
        self.release[name] = asyncio.Event()

    async def wait(self, name: str):
        await asyncio.wait_for(self.entered[name].wait(), 20)

    async def close(self):
        for gate in self.release.values():
            gate.set()
        self.server.close()
        await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)
        try:
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 20)
            length = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n')
                          if line.lower().startswith(b'content-length:'))
            body = json.loads(await reader.readexactly(length))
            self.requests.append(body)
            record(self.evidence / 'model.jsonl', {'path': header.splitlines()[0].decode(), 'body': body})
            name = None
            for msg in reversed(body.get('messages', [])):
                if msg.get('role') == 'user':
                    text = str(msg.get('content', ''))
                    name = next((k for k in self.scripts if f'CONTRACT:{k}' in text), None)
                    if name:
                        break
            # Auxiliary classifiers have no tool schemas; their answer cannot
            # execute tools or manufacture the tested file/permission result.
            if name is None or not body.get('tools'):
                step = {'text': '{"continue":false}'}
            else:
                index = self.indices[name]
                if index >= len(self.scripts[name]):
                    raise AssertionError(f'unexpected extra main model request: {name}/{index}')
                step = self.scripts[name][index]
                if 'tool' in step:
                    offered = {x['function']['name'] for x in body['tools']}
                    tool, arguments = step['tool'], step['args']
                    if tool not in offered:
                        refs = []
                        for msg in body['messages']:
                            try:
                                value = json.loads(msg['content'])
                                if isinstance(value, dict):
                                    refs.extend(value.get('activated', []))
                                    if 'tool_ref' in value:
                                        refs.append(value)
                            except (KeyError, ValueError, TypeError, AttributeError):
                                pass
                        ref = next((x['tool_ref'] for x in reversed(refs)
                                    if x.get('name') == tool), None)
                        if ref:
                            step = {'tool': 'call_tool', 'args': {
                                'tool_ref': ref, 'arguments_json': json.dumps(arguments)}}
                        else:
                            step = {'tool': 'tool_search', 'args': {'tool_names': [tool]}}
                            self.indices[name] -= 1  # discovery is a wire-only adapter step
                self.indices[name] += 1
            if step.get('hold'):
                self.entered[name].set()
                await self.release[name].wait()
            if 'tool' in step:
                delta = {'role': 'assistant', 'tool_calls': [{'index': 0, 'id': f'call-{len(self.requests)}',
                    'type': 'function', 'function': {'name': step['tool'],
                    'arguments': json.dumps(step['args'])}}]}
                finish = 'tool_calls'
            else:
                delta, finish = {'role': 'assistant', 'content': step.get('text', 'Done.')}, 'stop'
            chunks = [{'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': 'contract-fixture', 'choices': [{'index': 0, 'delta': delta,
                                                              'finish_reason': None}]},
                      {'id': 'fixture', 'object': 'chat.completion.chunk', 'created': 1,
                       'model': 'contract-fixture', 'choices': [{'index': 0, 'delta': {},
                                                              'finish_reason': finish}]}]
            if body.get('stream'):
                payload = ''.join('data: ' + json.dumps(c) + '\n\n' for c in chunks) + 'data: [DONE]\n\n'
                mime = 'text/event-stream'
            else:
                message = dict(delta)
                for call in message.get('tool_calls', []):
                    call.pop('index', None)
                payload = json.dumps({'id': 'fixture', 'object': 'chat.completion', 'created': 1,
                    'model': 'contract-fixture', 'choices': [{'index': 0, 'message': message,
                                                          'finish_reason': finish}],
                    'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}})
                mime = 'application/json'
            record(self.evidence / 'model-responses.jsonl', {'scenario': name, 'step': step, 'payload': payload})
            encoded = payload.encode()
            writer.write(f'HTTP/1.1 200 OK\r\nContent-Type: {mime}\r\nContent-Length: {len(encoded)}\r\nConnection: close\r\n\r\n'.encode() + encoded)
            await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        except Exception as exc:
            self.errors.append(repr(exc))
            record(self.evidence / 'model-errors.jsonl', {'error': repr(exc)})
        finally:
            writer.close()
            await writer.wait_closed()
            self.connections.discard(task)


class BoxEngine:
    def __init__(self, root: Path, model: ModelServer):
        self.root, self.model = root, model
        self.root.mkdir(parents=True, exist_ok=True)
        self.profile = root / 'profile'
        config = self.profile / 'config'
        config.mkdir(parents=True, exist_ok=True)
        (config / 'config.yaml').write_text(yaml.safe_dump({
            'api_key': 'contract-fixture-key', 'api_base': model.url,
            'provider': 'openai', 'model': 'contract-fixture', 'max_steps': 12,
            'enable_memory': False, 'enable_memory_extraction': False,
            'memory_maintainer_enabled': False,
            'tools': {'enable_file_tools': True, 'enable_bash': False,
                      'enable_todo': False, 'enable_plan': True,
                      'enable_sub_agent': False, 'enable_skills': False,
                      'enable_mcp': False, 'allow_full_access': False},
        }))
        self.pending: dict[int, asyncio.Future] = {}
        self.notifications: list[dict] = []
        self.permissions: asyncio.Queue = asyncio.Queue()
        self.counter = 0
        self.generation = 0
        self.tasks = []

    async def start(self):
        self.generation += 1
        # Explicit environment: no developer provider credentials/config/proxies.
        env = {k: v for k, v in os.environ.items() if k in
               {'PATH', 'SYSTEMROOT', 'WINDIR', 'LANG', 'LC_ALL', 'TMPDIR', 'TEMP', 'TMP'}}
        env.update(HOME=str(self.model.evidence), USERPROFILE=str(self.model.evidence),
                   BOX_AGENT_HOME=str(self.profile), PYTHONPATH=str(ROOT),
                   PLAYWRIGHT_BROWSERS_PATH=str(self.profile / 'browsers'),
                   BOX_AGENT_SESSION_TRACE_ENABLED='0', NO_PROXY='127.0.0.1,localhost')
        (self.root / 'home').mkdir(exist_ok=True)
        self.process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'box_agent.acp.server',
            cwd=ROOT, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        record(self.root / 'process.jsonl', {'event': 'start', 'pid': self.process.pid,
                                           'generation': self.generation})
        self.tasks = [asyncio.create_task(self.read()), asyncio.create_task(self.stderr())]
        await self.rpc('initialize', {'protocolVersion': 1, 'clientCapabilities': {},
                                    'clientInfo': {'name': 'behavior-contract', 'version': '1'}})

    async def stderr(self):
        with (self.root / f'stderr-{self.generation}.log').open('wb') as stream:
            while chunk := await self.process.stderr.read(65536):
                stream.write(chunk)
                stream.flush()

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                with (self.root / f'stdout-{self.generation}.raw').open('ab') as stream:
                    stream.write(line)
                msg = json.loads(line)
                record(self.root / 'protocol.jsonl', {'direction': 'received', 'message': msg})
                if 'method' in msg:
                    if 'id' in msg:
                        if msg['method'] == 'session/request_permission':
                            await self.permissions.put(msg)
                        else:
                            await self.send({'jsonrpc': '2.0', 'id': msg['id'],
                                'error': {'code': -32601, 'message': 'unsupported test host method'}})
                    else:
                        self.notifications.append(msg)
                elif msg.get('id') in self.pending:
                    future = self.pending[msg['id']]
                    if not future.done():
                        future.set_result(msg)
        except Exception as exc:
            record(self.root / 'protocol-errors.jsonl', {'error': repr(exc)})
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(EOFError('ACP process closed stdout; inspect evidence'))

    async def send(self, message):
        record(self.root / 'protocol.jsonl', {'direction': 'sent', 'message': message})
        self.process.stdin.write((json.dumps(message) + '\n').encode())
        await self.process.stdin.drain()

    async def rpc(self, method, params):
        self.counter += 1
        ident = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self.send({'jsonrpc': '2.0', 'id': ident, 'method': method, 'params': params})
            response = await asyncio.wait_for(future, 30)
            if 'error' in response:
                raise RuntimeError(json.dumps(response['error']))
            return response.get('result', {})
        finally:
            self.pending.pop(ident, None)

    async def open(self, workspace: Path, identity: str) -> str:
        workspace.mkdir(parents=True, exist_ok=True)
        result = await self.rpc('session/new', {'cwd': str(workspace), 'mcpServers': [], '_meta': {
            'session_id': identity, 'permission_mode': 'default',
            'filesystem_policy': {'session_workspace_root': str(workspace),
                                  'allowed_directories': [], 'filesystem_scope': 'session_workspace'},
            'follow_up_suggestions': False}})
        return result['sessionId']

    async def prompt(self, session: str, scenario: str, **meta) -> dict:
        return await self.rpc('session/prompt', {'sessionId': session,
            'prompt': [{'type': 'text', 'text': f'CONTRACT:{scenario}'}], '_meta': meta})

    async def cancel(self, session: str):
        await self.send({'jsonrpc': '2.0', 'method': 'session/cancel', 'params': {'sessionId': session}})

    async def permission(self):
        return await asyncio.wait_for(self.permissions.get(), 20)

    async def answer(self, request, *, allow: bool):
        kind = 'allow_once' if allow else 'reject_once'
        option = next(x for x in request['params']['options'] if x['kind'] == kind)
        await self.send({'jsonrpc': '2.0', 'id': request['id'], 'result': {
            'outcome': {'outcome': 'selected', 'optionId': option['optionId']}}})

    async def restore(self, session: str, workspace: Path):
        return await self.rpc('session/load', {'sessionId': session, 'cwd': str(workspace), 'mcpServers': []})

    async def stop(self, *, kill=False):
        if self.process.returncode is None:
            if kill:
                self.process.kill()
            else:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 8)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        record(self.root / 'process.jsonl', {'event': 'exit', 'pid': self.process.pid,
                                           'returncode': self.process.returncode})
        # Preserve the actual durable session records alongside wire evidence.
        record(self.root / 'files.jsonl', {'files': sorted(str(p.relative_to(self.root))
               for p in self.root.rglob('*') if p.is_file())})
        record(self.root / 'workspace-snapshots.jsonl', {'generation': self.generation, 'files': [
            {'path': str(p.relative_to(self.model.evidence)), 'size': p.stat().st_size,
             'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
            for workspace in self.model.evidence.glob('workspace*') if workspace.is_dir()
            for p in sorted(workspace.rglob('*')) if p.is_file()]})
