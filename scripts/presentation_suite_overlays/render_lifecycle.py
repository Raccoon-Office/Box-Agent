"""Checked lifecycle adaptation of the pinned Standard renderer, preserving QA."""

import ast
import hashlib
import textwrap


def replace(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"render lifecycle overlay needs review: {old[:80]!r}")
    return text.replace(old, new, 1)


def function(text, name):
    node = next(n for n in ast.parse(text).body if isinstance(n, ast.FunctionDef) and n.name == name)
    return "".join(text.splitlines(keepends=True)[node.lineno - 1:node.end_lineno])


def apply(relative, data):
    if relative == "skills/sn-ppt-standard/scripts/render.py":
        if hashlib.sha256(data).hexdigest() != "d15ba4f76d0277cfe729cc38896cfa988457aaebec844b41e6c1509f848b35bd":
            raise ValueError("render lifecycle overlay needs review: upstream renderer changed")
        text = data.decode()
        text = replace(text, "import signal\n", "")
        start = text.index("# —— 全局并发 chromium 上限")
        end = text.index("class BrowserUnavailable", start)
        text = text[:start] + '''import fcntl as _fcntl
from render_runtime import RenderSession, run_renderer, supervise


''' + text[end:]
        start = text.index("@contextmanager\ndef _alarm_timeout")
        end = text.index("def _ensure_browser_available", start)
        text = text[:start] + text[end:]
        old = function(text, "_render_once")
        rep = old[old.index('    rep = '):old.index('    owns_browser = ')]
        body = old[old.index('            runtime = rep["runtime"]'):old.index('        finally:\n            pg.close()')]
        body = textwrap.indent(textwrap.dedent(body), "        ")
        body = replace(body, '''        try:
            pg.evaluate("document.fonts && document.fonts.ready")
        except Exception:
            pass
''', '''        pg.wait_for_function("() => !document.fonts || document.fonts.status === 'loaded'", timeout=15000)
''')
        new = '''def _render_once(session, html, out, w, h):
    """Render one page in an explicitly owned, isolated browser context."""
''' + rep + '    with session.page(w, h, scale=2) as pg:\n' + body + '    return rep\n'
        text = replace(text, old, new)
        old = function(text, "render_batch")
        new = old[:old.index('    sync_playwright = _sync_playwright()')]
        new += '''    hard_pages = []
    with RenderSession(_sync_playwright(), _ensure_browser_available, LAUNCH_ARGS) as session:
'''
        body = old[old.index('        for number, slide in slides:'):old.index('    finally:')]
        body = replace(body, '''                playwright, os.path.abspath(slide), target, width, height,
                browser_exe=browser_exe, browser=browser,
''', '''                session, os.path.abspath(slide), target, width, height,
''')
        text = replace(text, old, new + body)
        old = function(text, "audit_player")
        new = old[:old.index('    sync_playwright = _sync_playwright()')]
        new += '''    with RenderSession(_sync_playwright(), _ensure_browser_available, LAUNCH_ARGS) as session:
        with session.page(1600, 900, scale=1) as page:
'''
        body = old[old.index('        page_errors = []'):old.index('    finally:')]
        body = replace(body, '        for number, ids, expected in targets:\n',
                       '        for number, ids, expected in targets:\n            session.renew_page_deadline()\n')
        text = replace(text, old, new + textwrap.indent(body, "    "))
        old = function(text, "_batch_cli")
        new = old[:old.index('    last_error = None')]
        new += '''    try:
        render_batch(args.root, args.pages, args.width, args.height)
    except (BrowserUnavailable, RenderQualityError) as exc:
        print(f"batch render failed: {exc}", file=sys.stderr)
        return 1
    return 0
'''
        text = replace(text, old, new)
        text = replace(text, '''    sync_playwright = _sync_playwright()
    last_err = None
    # 全局并发 chromium 上限:整个渲染(含 3 次重试)期间持一个 flock 槽;进程任何方式退出都释放。
    _slot_fd = _acquire_render_slot()
    if _slot_fd is not None:
        _atexit.register(_release_render_slot, _slot_fd)
    for attempt in range(3):                 # 高并发下 chromium 偶发崩(TargetClosed),重试 + 退避
        p = None
''', '''    # A complete attempt is supervised externally; retries require verified cleanup.
    for attempt in range(1):
''')
        text = replace(text, '''            p = _call_with_timeout(sync_playwright().start, 60, "Playwright start")
            browser_exe = _ensure_browser_available(p)
            rep = _render_once(p, html, out, w, h, browser_exe)
''', '''            with RenderSession(_sync_playwright(), _ensure_browser_available, LAUNCH_ARGS) as session:
                rep = _render_once(session, html, out, w, h)
''')
        text = replace(text, '''                print("✓ RENDER_OK: PNG 已生成。若上面有 greenlet/字体/CDN 等 stderr 警告,均为无害噪声——"
                      "环境已就绪,**切勿 pip install / 重装或调试 playwright/chromium**;有问题只改 HTML。")
''', '''                print("✓ RENDER_OK: PNG 已生成；质量结论以本次诊断报告和退出码为准。")
''')
        text = replace(text, '''        except Exception as e:
            last_err = e
        finally:
            if p is not None:
                _stop_playwright(p)
        time.sleep(1.5 * (attempt + 1))      # 退避,顺带错峰,缓解 sibling 同时起 chromium
    print(f"渲染失败(重试 3 次): {last_err}", file=sys.stderr)
''', '''        except Exception:
            raise
    print("渲染失败: 未生成有效 PNG", file=sys.stderr)
''')
        text = replace(text, '''if __name__ == "__main__":
    main()
''', '''if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] in ("-h", "--help"):
        main()
    else:
        raise SystemExit(supervise(__file__, sys.argv[1:]))
''')
        text = text.replace("RenderSession(_sync_playwright(), _ensure_browser_available, LAUNCH_ARGS)",
                            "RenderSession(_sync_playwright(), _ensure_browser_available, LAUNCH_ARGS, is_fatal=_is_fatal_browser_error)")
        text = replace(text, '    lower = msg.lower()\n    fatal_bits = (',
                       '    lower = "\\n".join(line for line in msg.lower().splitlines() if "<launching>" not in line)\n    fatal_bits = (')
        text = replace(text, '        "operation not permitted",\n',
                       '        "operation not permitted",\n        "permission denied",\n        "exec format error",\n        "bad interpreter",\n')
        text = replace(text, '''        except Exception as exc:
            print(f"player runtime audit failed: {exc}", file=sys.stderr)
            raise SystemExit(1)
''', '''        except Exception:
            raise
''')
        return text.encode()
    if relative == "skills/sn-ppt-standard/scripts/deck.py":
        text = replace(data.decode(), '''    proc = subprocess.run(
        [sys.executable, str(render), "--audit-player", str(root)],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )''', '''    from render_runtime import run_renderer

    proc = run_renderer(render, ["--audit-player", str(root)], timeout=180)''')
        return text.encode()
    if relative == "skills/sn-ppt-standard/scripts/font_bundle.py":
        text = replace(data.decode(), '''    result = subprocess.run(
        [sys.executable, str(renderer), "--batch", str(root)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=600,
    )''', '''    from render_runtime import run_renderer

    result = run_renderer(renderer, ["--batch", str(root)], timeout=600)''')
        return text.encode()
    if relative == "skills/sn-ppt-standard/requirements.txt":
        return replace(data.decode(), "playwright>=1.50.0\n", "playwright>=1.50.0\npsutil>=5.9\n").encode()
    if relative == "skills/sn-ppt-standard/scripts/install.sh":
        return replace(data.decode(), '"$PYBIN" -m pip install -q playwright 2>/dev/null',
                       '"$PYBIN" -m pip install -q playwright "psutil>=5.9" 2>/dev/null').encode()
    return data
