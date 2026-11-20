"""Persistent JavaScript worker lifecycle for the guest CUA adapter."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid

_image_sibling = Path(__file__).with_name('cua_images.py')
_image_source = _image_sibling.parent.parent / 'cua_images.py'
_images_spec = importlib.util.spec_from_file_location(
    'cua_images', _image_sibling if _image_sibling.is_file() else _image_source)
image_output = importlib.util.module_from_spec(_images_spec)
_images_spec.loader.exec_module(image_output)


class ReplService:
    """Independent supervisor: blocked JS cannot block its wall-clock deadline."""

    def __init__(self, driver_factory, node, script, evidence_dir=None, *, browser_config=None,
                 browser_client_factory=None, documentation=None, browser_backend_factory=None,
                 capability="all", dispatch_check=None, worker_env=None, worker_cwd=None,
                 adapter_factory, action_error, result_data,
                 evidence_contract, mcp_client, driver_timeout_seconds):
        if capability not in {'all', 'cua', 'browser'}:
            raise ValueError('Invalid REPL capability')
        self.capability = capability
        self.browser_backend_factory = browser_backend_factory
        self.dispatch_check = dispatch_check
        self.worker_env, self.worker_cwd = worker_env, worker_cwd
        self.cancelled = threading.Event()
        self.factory, self.node, self.script = driver_factory, node, str(script)
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self.worker = self.connection = self.adapter = self.log = None
        self.serial = 0
        self.log_cell = None
        self.invocation_id = None
        self.browser_config = browser_config
        self.browser_client_factory = browser_client_factory
        self.browser = None
        self.browser_counts = {}
        self.documentation = documentation
        self.adapter_factory = adapter_factory
        self.action_error = action_error
        self.result_data = result_data
        self.evidence_contract = evidence_contract
        self.mcp_client = mcp_client
        self.driver_timeout_seconds = driver_timeout_seconds

    def browser_record(self, value):
        self._cell_receipt(value)
        if value['phase'] in {'dispatch', 'error'}:
            self.browser_counts[value['phase']] = self.browser_counts.get(value['phase'], 0) + 1

    def browser_control(self, method, handle, args, deadline):
        if self.adapter:
            self.adapter.pending_window_action = None
            self.adapter.pending_popup_action = None
            for state in self.adapter.handles.values():
                state.pop('fallback_origin', None)
                state.pop('popup_focus_relation', None)
        if self.browser_backend_factory is not None:
            if self.browser is None:
                self.browser = self.browser_backend_factory()
            return self.browser.control(method, handle, args, deadline=deadline)
        if self.capability == 'browser':
            raise ValueError('Browser backend injection is required; no harness fallback')
        if not self.browser_config:
            if method == 'browser.availability':
                return []
            raise ValueError('Browser capability is not configured for this task')
        if self.browser is None:
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'cua_browser', Path(__file__).with_name('cua_browser.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            factory = self.browser_client_factory or (lambda timeout, env: self.mcp_client(
                [self.browser_config['python'], '-m', 'browser_harness.mcp_cli', '--managed'],
                timeout, env=env))
            self.browser = module.BrowserBackend(factory, self.browser_config, self.browser_record)
        return self.browser.control(method, handle, args, deadline=deadline)

    def start(self):
        if self.worker:
            return
        parent, child = socket.socketpair()
        env = dict(self.worker_env) if self.worker_env is not None else {k: v for k, v in os.environ.items() if k not in {'OPENAI_API_KEY', 'OPENAI_BASE_URL'}}
        env['MAC_CUA_CAPABILITY'] = self.capability
        env['MAC_CUA_REPL_FD'] = str(child.fileno())
        try:
            self.worker = subprocess.Popen([self.node, self.script], pass_fds=(child.fileno(),),
                stdin=subprocess.DEVNULL, stdout=sys.stderr, stderr=sys.stderr, env=env, cwd=self.worker_cwd)
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.connection = parent
        self.inbox = queue.Queue()
        inbox = self.inbox

        def read():
            try:
                with parent.makefile('r') as source:
                    for line in source:
                        inbox.put(json.loads(line))
            except (ValueError, OSError) as error:
                inbox.put(error)
            finally:
                inbox.put(EOFError('JS worker closed'))

        threading.Thread(target=read, daemon=True).start()

    def send(self, value):
        self.connection.sendall((json.dumps(value, ensure_ascii=False) + '\n').encode())

    def _cell_receipt(self, value):
        if self.log:
            self.evidence_contract.validate_phase(value, self.evidence_contract.FORMAT_VERSION)
            self.log.write(json.dumps({'invocation_id': self.invocation_id, 'cell': self.serial,
                                       **value}, ensure_ascii=False) + '\n')
            self.log.flush()

    def _start_cell_receipt(self, code):
        if not self.evidence_dir:
            return
        if self.log:
            self.log.close()
        self.evidence_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.log = (self.evidence_dir / (uuid.uuid4().hex + '.jsonl')).open('x')
        self.log_cell = self.serial
        self._cell_receipt({'phase': 'cell_start',
                            'format_version': self.evidence_contract.FORMAT_VERSION, 'code': code})

    def _end_cell_receipt(self, status):
        counts = self.adapter.counts(self.serial) if self.adapter else {}
        self._cell_receipt({'phase': 'cell_end', 'status': status,
                            'driver_dispatches': counts.get('dispatches', 0) + self.browser_counts.get('dispatch', 0),
                            'driver_errors': counts.get('errors', 0) + self.browser_counts.get('error', 0)})

    def backend(self, deadline):
        if self.adapter is None:
            timeout = max(0.001, min(self.driver_timeout_seconds, deadline - time.monotonic()))
            self.adapter = self.adapter_factory(self.factory(timeout), self.log)
        self.adapter.set_context(
            evidence=self.log, deadline=deadline, cell=self.serial,
            invocation_id=self.invocation_id)
        return self.adapter

    def abort(self):
        # Do not close the adapter concurrently with an in-flight native action.
        self.cancelled.set()

    def execute(self, code, title=None, timeout_ms=60000):
        if not isinstance(code, str) or not code.strip() or type(timeout_ms) is not int or not 1 <= timeout_ms <= 120000:
            raise ValueError('js requires code and timeout_ms in 1..120000')
        if title is not None and not isinstance(title, str):
            raise ValueError('title must be a string')
        self.start()
        self.serial += 1
        self.browser_counts = {}
        self.invocation_id = uuid.uuid4().hex
        cell = self.serial
        deadline = time.monotonic() + timeout_ms / 1000
        content = []
        error = None
        interrupted = False
        try:
            self._start_cell_receipt(code)
            self.send({'kind': 'execute', 'cell': cell, 'code': code})
            while True:
                if self.cancelled.is_set():
                    raise RuntimeError('Cell cancelled; dispatched effects may be unknown. Observe before retrying.')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('JS timed out; already dispatched effects may be unknown. Session reset; do not replay.')
                try:
                    message = self.inbox.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue
                if isinstance(message, Exception):
                    raise message
                if message.get('cell') != cell:
                    if message.get('kind') == 'call':
                        self.send({'kind': 'reply', 'id': message['id'], 'error': 'JS call has ended'})
                    continue
                if message['kind'] == 'emit':
                    part = message['content']
                    if part.get('type') == 'image':
                        content.extend(image_output.image_content(part, message.get('image_dimensions')))
                    else:
                        content.append(part)
                elif message['kind'] == 'done':
                    error = message.get('error')
                    break
                elif message['kind'] == 'call':
                    try:
                        method = message['method']
                        if method != 'documentation':
                            is_browser = method.startswith('browser.')
                            if ((self.capability == 'cua' and is_browser)
                                    or (self.capability == 'browser' and not is_browser)):
                                raise ValueError('CapabilityDenied: method is outside this REPL')
                            if self.dispatch_check is not None:
                                self.dispatch_check(method, message.get('handle'), message.get('args', {}))
                        if message['method'] == 'documentation':
                            if self.documentation is not None:
                                value = self.documentation
                            else:
                                sibling = Path(self.script).with_name('cua_api.md')
                                source_layout = sibling.parent.parent / 'cua_api.md'
                                value = (sibling if sibling.is_file() else source_layout).read_text()
                            if not isinstance(value, str) or not value.strip():
                                raise ValueError('CUA operation manual must be nonempty text')
                        elif message['method'].startswith('browser.'):
                            value = self.browser_control(
                                message['method'], message.get('handle'), message.get('args'), deadline)
                            if self.adapter:
                                self.adapter.invalidate()
                        else:
                            if self.browser:
                                self.browser.invalidate()
                            backend = self.backend(deadline)
                            value = backend.control(
                                message['method'], message.get('handle'), message.get('args'))
                        self.send({'kind': 'reply', 'id': message['id'], 'value': value})
                    except (ValueError, KeyError, TypeError, self.action_error) as failure:
                        if isinstance(failure, self.action_error):
                            report = self.result_data(failure.result)
                            if report.get('code') == 'element_outside_target_window':
                                detail = ('TargetUnverified: the element is no longer verified for the '
                                    'current window. Call getAXState() or getAXStateAndScreenshot() again '
                                    'and use an element ID from the new observation. No input was sent.')
                            else:
                                detail = json.dumps({k: v for k, v in report.items()
                                    if k in {'effect', 'status', 'error', 'reason', 'refusal', 'message'}},
                                    ensure_ascii=False) + '\n' + '\n'.join(
                                        c.get('text', '') for c in failure.result.get('content', [])
                                        if c.get('type') == 'text')
                        else:
                            detail = str(failure)
                        self.send({'kind': 'reply', 'id': message['id'], 'error': detail})
        except (TimeoutError, EOFError, OSError, RuntimeError) as failure:
            error = str(failure)
            interrupted = True
        except BaseException:
            interrupted = True
            raise
        finally:
            if self.log_cell == cell:
                self._end_cell_receipt('interrupted' if interrupted else 'error' if error else 'completed')
            if interrupted:
                self.close()
            self.invocation_id = None
        if error:
            content.append({'type': 'text', 'text': error})
        return {'content': content, 'isError': bool(error)}

    def close(self):
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            finally:
                self.browser = None
        if self.worker:
            self.worker.terminate()
            try:
                self.worker.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.worker.kill()
                self.worker.wait()
            self.worker = None
        if self.connection:
            self.connection.close()
            self.connection = None
        if self.adapter:
            try:
                self.adapter.set_context()
                self.adapter.close()
            except Exception:
                pass
            finally:
                self.adapter = None
        if self.log:
            self.log.close()
            self.log = None
        self.log_cell = None


def serve(service, source, sink, *, protocol, instructions, tools):
    for line in source:
        request_id = None
        try:
            request = json.loads(line)
            request_id = request.get('id')
            if 'id' not in request:
                continue
            method = request['method']
            if method == 'initialize':
                result = {'protocolVersion': protocol, 'capabilities': {'tools': {}},
                          'serverInfo': {'name': 'cua_repl', 'version': '1'},
                          'instructions': instructions}
            elif method == 'ping':
                result = {}
            elif method == 'tools/list':
                result = {'tools': tools}
            elif method == 'tools/call':
                params = request['params']
                args = params.get('arguments', {})
                try:
                    if params['name'] == 'js':
                        result = service.execute(**args)
                    elif params['name'] == 'js_reset' and args == {}:
                        service.close()
                        result = {'content': [{'type': 'text',
                            'text': 'JS bindings cleared; applications left open.'}]}
                    else:
                        raise ValueError('Unknown tool or invalid reset arguments')
                except (ValueError, TypeError, OSError) as error:
                    result = {'isError': True, 'content': [{'type': 'text', 'text': str(error)}]}
            else:
                raise ValueError('Unknown method')
            reply = {'jsonrpc': '2.0', 'id': request_id, 'result': result}
        except (ValueError, KeyError, TypeError, AttributeError) as error:
            reply = {'jsonrpc': '2.0', 'id': request_id,
                     'error': {'code': -32600, 'message': str(error)}}
        sink.write(json.dumps(reply, ensure_ascii=False) + '\n')
        sink.flush()
