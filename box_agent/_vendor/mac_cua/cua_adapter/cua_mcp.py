"""Minimal serial MCP subprocess transport used by the guest CUA adapter."""
from __future__ import annotations

import json
import os
import queue
import select
import subprocess
import sys
import threading
import time

PROTOCOL = '2024-11-05'
DRIVER_TIMEOUT_SECONDS = 60


class McpClient:
    """One serial MCP connection. A timeout poisons it: never replay mutations."""

    def __init__(self, command: list[str], timeout: float = DRIVER_TIMEOUT_SECONDS, *, env=None):
        self.timeout = timeout
        self.deadline = time.monotonic() + timeout
        self.serial = 0
        self.inbox = queue.Queue()
        env = {k: v for k, v in (os.environ if env is None else env).items()
               if k not in {'OPENAI_API_KEY', 'OPENAI_BASE_URL'}}
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=sys.stderr, text=True, env=env)
        threading.Thread(target=self._read, daemon=True).start()
        try:
            os.set_blocking(self.process.stdin.fileno(), False)
            self.request('initialize', {'protocolVersion': PROTOCOL, 'capabilities': {},
                         'clientInfo': {'name': 'mac-cua-adapter', 'version': '1'}})
            self.send({'jsonrpc': '2.0', 'method': 'notifications/initialized'}, deadline=self.deadline)
            self.tools = {}
            cursor = None
            seen = set()
            while True:
                page = self.request('tools/list', {'cursor': cursor} if cursor else {})
                self.tools.update({tool['name']: tool for tool in page['tools']})
                cursor = page.get('nextCursor')
                if not cursor:
                    break
                if cursor in seen:
                    raise RuntimeError('Driver repeated tools/list cursor')
                seen.add(cursor)
            self.deadline = None
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self.process.stdout:
                self.inbox.put(json.loads(line))
        except (ValueError, OSError) as error:
            self.inbox.put(error)
        finally:
            self.inbox.put(EOFError('Driver MCP connection closed'))

    def remaining(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self.close()
            raise TimeoutError('Driver timed out; outcome unknown. Connection closed; do not replay.')
        return remaining

    def send(self, value: dict, *, deadline):
        self.remaining(deadline)
        pending = memoryview((json.dumps(value) + '\n').encode())
        descriptor = self.process.stdin.fileno()
        while pending:
            remaining = self.remaining(deadline)
            if not select.select([], [descriptor], [], remaining)[1]:
                self.remaining(deadline)
                continue
            try:
                written = os.write(descriptor, pending)
            except BlockingIOError:
                continue
            except OSError as error:
                self.close()
                raise RuntimeError('Driver connection failed; outcome unknown') from error
            if written <= 0:
                self.close()
                raise RuntimeError('Driver connection failed; outcome unknown')
            pending = pending[written:]
        self.remaining(deadline)

    def request(self, method: str, params: dict) -> dict:
        deadline = min(time.monotonic() + self.timeout,
                       self.deadline if self.deadline is not None else float('inf'))
        self.remaining(deadline)
        self.serial += 1
        request_id = self.serial
        self.send({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params}, deadline=deadline)
        while True:
            try:
                remaining = self.remaining(deadline)
                value = self.inbox.get(timeout=remaining)
            except queue.Empty:
                self.close()
                raise TimeoutError('Driver timed out; outcome unknown. Connection closed; do not replay.')
            self.remaining(deadline)
            if isinstance(value, Exception):
                self.close()
                raise RuntimeError('Driver connection failed; outcome unknown') from value
            if 'method' in value:
                if 'id' in value:
                    reply = {'result': {}} if value['method'] == 'ping' else {
                        'error': {'code': -32601, 'message': 'Client method not supported'}}
                    self.send({'jsonrpc': '2.0', 'id': value['id'], **reply}, deadline=deadline)
                continue
            if value.get('id') != request_id:
                continue
            if 'error' in value:
                raise RuntimeError('Driver RPC error: ' + json.dumps(value['error']))
            return value['result']

    def call(self, name: str, args: dict) -> dict:
        return self.request('tools/call', {'name': name, 'arguments': args})

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
