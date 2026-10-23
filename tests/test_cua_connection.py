import json

import pytest

from box_agent.plugins.cua.runtime.connection import (
    DEFAULT_DRIVER, DRIVER_ENV, HOST_RUNTIME_ENV, DriverConnection,
)
from box_agent.plugins.cua.config import CuaConfig, CuaRuntimeConfig
from box_agent.plugins.cua.ready_tool import CuaService


@pytest.mark.parametrize('platform,expected', [('darwin', 'app'), ('linux', 'driver'), ('win32', 'driver')])
def test_default_interface_selects_app_on_macos(monkeypatch, platform, expected):
    from box_agent.plugins.cua import config

    monkeypatch.setattr(config.sys, 'platform', platform)
    assert CuaConfig().tool_interface == expected
    assert CuaConfig(tool_interface='driver').tool_interface == 'driver'


@pytest.mark.parametrize('mode', ['standalone', 'external'])
def test_wrapper_and_readiness_use_identical_resolved_driver_command(mode):
    runtime = CuaRuntimeConfig(enabled=True, mode=mode,
                               driver_path='/host/driver', endpoint='/host/private.sock')
    service = CuaService(CuaConfig(runtime=runtime))
    try:
        expected = service.driver_server_config
        connection = DriverConnection.from_environment({
            HOST_RUNTIME_ENV: runtime.model_dump_json(), DRIVER_ENV: '/wrong/driver'})
        assert connection.command() == [expected['command'], *expected['args']]
        assert ('--embedded' in connection.command()) is (mode == 'external')
    finally:
        service.adapter.cleanup()


@pytest.mark.parametrize('runtime', [
    {'enabled': False, 'driver_path': '/host/driver', 'endpoint': '/host/socket'},
    {'enabled': True, 'driver_path': '/host/driver'},
    {'enabled': True, 'endpoint': '/host/socket'},
    {'enabled': True, 'mode': 'external'},
])
def test_unresolved_or_disabled_host_config_never_falls_back_to_default_driver(runtime):
    with pytest.raises(ValueError):
        DriverConnection.from_environment({HOST_RUNTIME_ENV: json.dumps(runtime),
                                            DRIVER_ENV: '/wrong/driver'})


def test_legacy_wrapper_launch_without_host_config_is_preserved():
    assert DriverConnection.from_environment({}).command() == [DEFAULT_DRIVER, 'mcp']
    assert DriverConnection.from_environment({DRIVER_ENV: '/custom/driver'}).command() == [
        '/custom/driver', 'mcp']


@pytest.mark.parametrize('frozen', [False, True])
def test_app_interface_wraps_the_same_host_endpoint(monkeypatch, frozen):
    import sys
    monkeypatch.setattr(sys, 'frozen', frozen, raising=False)
    runtime = CuaRuntimeConfig(enabled=True, mode='external',
                               driver_path='/host/driver', endpoint='/host/private.sock')
    service = CuaService(CuaConfig(tool_interface='app', runtime=runtime))
    try:
        config = service.server_config
        assert config['command'] == sys.executable
        assert config['args'] == (['--cua-mcp'] if frozen else
                                  ['-m', 'box_agent.mcp_servers.cua_server'])
        connection = DriverConnection.from_environment(config['env'])
        expected = service.driver_server_config
        assert connection.command() == [expected['command'], *expected['args']]
    finally:
        service.adapter.cleanup()
