from trace_viewer.cli import main


def test_cli_defaults_to_loopback_binding(monkeypatch, repo_root):
    calls = []
    monkeypatch.setattr("trace_viewer.cli.uvicorn.run", lambda app, **kwargs: calls.append((app, kwargs)))
    assert main(["--repo-root", str(repo_root)]) == 0
    assert calls[0][1] == {"host": "127.0.0.1", "port": 8000}


def test_cli_allows_host_and_port_override(monkeypatch, repo_root):
    calls = []
    monkeypatch.setattr("trace_viewer.cli.uvicorn.run", lambda app, **kwargs: calls.append(kwargs))
    assert main(["--repo-root", str(repo_root), "--host", "127.0.0.1", "--port", "8899"]) == 0
    assert calls == [{"host": "127.0.0.1", "port": 8899}]


def test_remote_evaluation_opt_in_is_forwarded_to_the_application(monkeypatch, tmp_path):
    import trace_viewer.cli as cli
    configured = []
    monkeypatch.setattr(cli, "create_app", lambda root, **kwargs: configured.append((root, kwargs)) or object())
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: None)
    assert cli.main(["--repo-root", str(tmp_path), "--host", "0.0.0.0", "--allow-remote-evaluations"]) == 0
    assert configured == [(tmp_path, {"allow_remote_evaluations": True})]
