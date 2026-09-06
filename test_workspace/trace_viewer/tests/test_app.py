from fastapi.testclient import TestClient

from trace_viewer.app import create_app


class FakeEvaluationRunner:
    def __init__(self, attachment_count: int = 0):
        self.calls = []
        self.options = {
            "datasets": [
                {
                    "id": "qs-smoke",
                    "name": "Smoke 数据集",
                    "item_count": 3,
                    "attachment_count": attachment_count,
                    "task_types": ["text"],
                    "task_type_stats": [
                        {
                            "name": "text",
                            "item_count": 3,
                            "attachment_count": attachment_count,
                        }
                    ],
                }
            ],
            "models": [
                {
                    "id": "sn-deepseek-v4-pro",
                    "name": "sn-deepseek-v4-pro",
                    "max_tokens": 100000,
                    "binding": {
                        "source": "builtin",
                        "model": "sn-deepseek-v4-pro",
                        "maxTokens": 100000,
                    },
                }
            ],
        }

    def list_options(self):
        return self.options

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "return_code": 0,
            "run_name": "260827-1200-auto-smoke",
            "output": "",
        }


def test_three_level_navigation(client):
    home = client.get("/")
    assert home.status_code == 200
    assert "eval-one" in home.text
    assert "2 个任务" in home.text
    assert 'id="open-evaluation-dialog"' in home.text
    assert 'id="evaluation-dialog"' in home.text
    assert 'id="evaluation-task-type"' in home.text
    assert 'id="evaluation-count"' in home.text
    assert "执行评估" in home.text

    run = client.get("/runs/eval-one")
    assert run.status_code == 200
    assert "Q1" in run.text and "Q2" in run.text
    assert run.text.count("type=\"search\"") == 1

    case = client.get("/runs/eval-one/cases/Q1")
    assert case.status_code == 200
    assert "Agent 轨迹" in case.text
    assert "最终回答" in case.text
    assert "answer Q1" in case.text


def test_case_overview_shows_complete_input_as_unicode(client, repo_root):
    input_path = repo_root / "test_workspace/outputs/eval-one/cases/Q1/input.json"
    input_path.write_text(
        '{"id":"Q1","query":"给我生成报告","input_files":["数据.csv"]}',
        encoding="utf-8",
    )

    response = client.get("/runs/eval-one/cases/Q1")

    assert response.status_code == 200
    assert "任务输入" in response.text
    assert "给我生成报告" in response.text
    assert "数据.csv" in response.text
    assert r"\u7ed9\u6211\u751f\u6210" not in response.text


def test_case_overview_shows_latest_turn_output_instead_of_accumulated_chunks(
    client, repo_root
):
    attempt = (
        repo_root
        / "test_workspace/outputs/eval-one/cases/Q1/attempts/attempt-q1"
    )
    (attempt / "assistant.txt").write_text(
        "intermediate streamed replylatest streamed reply",
        encoding="utf-8",
    )
    trace = attempt / "agent/trace.jsonl"
    trace.write_text(
        "\n".join(
            [
                '{"event":"turn.output","timestamp":"2026-08-21T09:59:59+00:00","data":{"content":"earlier turn output"}}',
                '{"event":"turn.output","timestamp":"2026-08-21T10:00:00+00:00","data":{"content":"authoritative final answer"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    response = client.get("/runs/eval-one/cases/Q1")

    assert response.status_code == 200
    assert "authoritative final answer" in response.text
    assert "earlier turn output" not in response.text
    assert "intermediate streamed reply" not in response.text


def test_case_overview_falls_back_to_assistant_text_without_turn_output(
    client, repo_root
):
    attempt = (
        repo_root
        / "test_workspace/outputs/eval-one/cases/Q1/attempts/attempt-q1"
    )
    (attempt / "assistant.txt").write_text("legacy captured answer", encoding="utf-8")

    response = client.get("/runs/eval-one/cases/Q1")

    assert response.status_code == 200
    assert "legacy captured answer" in response.text


def test_case_search(client):
    response = client.get("/runs/eval-one?q=Q2")
    assert response.status_code == 200
    assert "Q2" in response.text
    assert "Q1" not in response.text


def test_run_and_case_pages_show_effect_metric_summaries(client):
    run = client.get("/runs/eval-one")

    assert run.status_code == 200
    for heading in ("任务类型", "结果指标", "过程指标", "成本"):
        assert heading in run.text
    assert "text" in run.text
    assert "55 / 60" in run.text
    assert "32 / 40" in run.text
    assert "120 tokens" in run.text
    assert "Agent 总 Token" in run.text
    assert run.text.count("未评估") >= 2
    assert "未采集" in run.text

    case = client.get("/runs/eval-one/cases/Q1")

    assert case.status_code == 200
    assert "55 / 60" in case.text
    assert "32 / 40" in case.text
    assert "120 tokens" in case.text


def test_evaluation_options_are_loaded_from_the_runner(repo_root):
    runner = FakeEvaluationRunner()
    client = TestClient(create_app(repo_root, evaluation_runner=runner))

    response = client.get("/api/evaluation-options")

    assert response.status_code == 200
    assert response.json()["datasets"][0]["id"] == "qs-smoke"
    assert response.json()["models"][0]["id"] == "sn-deepseek-v4-pro"


def test_evaluation_launch_runs_in_background_and_exposes_status(repo_root):
    runner = FakeEvaluationRunner()
    client = TestClient(create_app(repo_root, evaluation_runner=runner))

    launch = client.post(
        "/api/evaluation-runs",
        json={
            "dataset_id": "qs-smoke",
            "model_id": "sn-deepseek-v4-pro",
            "task_type": "text",
            "execution_count": 2,
            "approved_data_disclosure": False,
        },
    )

    assert launch.status_code == 202
    launch_id = launch.json()["launch_id"]
    status = client.get(f"/api/evaluation-runs/{launch_id}")
    assert status.status_code == 200
    assert status.json()["status"] == "completed"
    assert status.json()["run_name"] == "260827-1200-auto-smoke"
    assert runner.calls == [
        {
            "dataset_id": "qs-smoke",
            "model": "sn-deepseek-v4-pro",
            "model_max_tokens": 100000,
            "launch_id": launch_id,
            "model_binding": {
                "source": "builtin",
                "model": "sn-deepseek-v4-pro",
                "maxTokens": 100000,
            },
            "task_type": "text",
            "execution_count": 2,
        }
    ]


def test_attachment_dataset_requires_explicit_disclosure(repo_root):
    runner = FakeEvaluationRunner(attachment_count=1)
    client = TestClient(create_app(repo_root, evaluation_runner=runner))

    response = client.post(
        "/api/evaluation-runs",
        json={
            "dataset_id": "qs-smoke",
            "model_id": "sn-deepseek-v4-pro",
            "task_type": "text",
            "execution_count": 3,
            "approved_data_disclosure": False,
        },
    )

    assert response.status_code == 422
    assert "明确确认数据披露" in response.json()["detail"]
    assert runner.calls == []


def test_evaluation_launch_rejects_task_type_and_count_outside_dataset(repo_root):
    runner = FakeEvaluationRunner()
    client = TestClient(create_app(repo_root, evaluation_runner=runner))

    missing_type = client.post(
        "/api/evaluation-runs",
        json={
            "dataset_id": "qs-smoke",
            "model_id": "sn-deepseek-v4-pro",
            "task_type": "PPT生成",
            "execution_count": 1,
        },
    )
    too_many = client.post(
        "/api/evaluation-runs",
        json={
            "dataset_id": "qs-smoke",
            "model_id": "sn-deepseek-v4-pro",
            "task_type": "text",
            "execution_count": 4,
        },
    )

    assert missing_type.status_code == 422
    assert "不包含该任务类型" in missing_type.json()["detail"]
    assert too_many.status_code == 422
    assert "1 到 3" in too_many.json()["detail"]
    assert runner.calls == []


def test_incomplete_case_remains_visible(client):
    response = client.get("/runs/eval-one")
    assert "incomplete" in response.text
    assert "error" in response.text


def test_unknown_run_and_case_return_404(client):
    assert client.get("/runs/missing").status_code == 404
    assert client.get("/runs/eval-one/cases/missing").status_code == 404


def test_all_case_evidence_pages(client):
    base = "/runs/eval-one/cases/Q1"
    expected = {
        "timeline": "统一时间线",
        "agent": "turn.start",
        "acp": "initialize",
        "process": "process.started",
        "files": "files-before.json",
        "effect": "结果指标",
    }
    for suffix, text in expected.items():
        response = client.get(f"{base}/{suffix}")
        assert response.status_code == 200
        assert text in response.text


def test_effect_page_shows_scores_evidence_performance_cost_and_unavailable(client):
    response = client.get("/runs/eval-one/cases/Q1/effect")

    assert response.status_code == 200
    assert 'class="active" href="/runs/eval-one/cases/Q1/effect"' in response.text
    assert "任务完成度" in response.text
    assert "结果指标" in response.text
    assert "过程指标" in response.text
    assert "5 / 5" in response.text
    assert "ev-final-answer · assistant.txt" in response.text
    assert "answer Q1" in response.text
    assert "任务总耗时" in response.text and "2000 ms" in response.text
    assert "Agent 总 Token" in response.text and "120 tokens" in response.text
    assert "计划质量" in response.text
    assert "无法评估" in response.text
    assert "未配置效果评估模型 API" in response.text
    assert "当前无法评估的指标" not in response.text
    assert "deepseek-v4-flash" in response.text


def test_effect_page_has_empty_state_for_old_attempt(client):
    response = client.get("/runs/eval-one/cases/Q2/effect")

    assert response.status_code == 200
    assert "尚未生成效果评估" in response.text


def test_diagnosis_page_has_an_empty_state_without_a_markdown_file(client):
    response = client.get("/runs/eval-one/cases/Q1/diagnosis")

    assert response.status_code == 200
    assert "诊断结果" in response.text
    assert "尚未生成诊断" in response.text
    assert 'class="active" href="/runs/eval-one/cases/Q1/diagnosis"' in response.text


def test_diagnosis_page_renders_arbitrary_case_level_markdown(client, repo_root):
    case = repo_root / "test_workspace/outputs/eval-one/cases/Q1"
    (case / "diagnosis.md").write_text(
        "# 自由标题\n\n- 中文结论\n\n`stderr`\n\n| 项目 | 结论 |\n| --- | --- |\n| 轨迹 | 正常 |\n\n<aside data-note=\"kept\">任意 HTML</aside>\n",
        encoding="utf-8",
    )

    response = client.get("/runs/eval-one/cases/Q1/diagnosis")

    assert response.status_code == 200
    assert "<h1>自由标题</h1>" in response.text
    assert "<li>中文结论</li>" in response.text
    assert "<code>stderr</code>" in response.text
    assert "<table>" in response.text
    assert "<td>正常</td>" in response.text
    assert '<aside data-note="kept">任意 HTML</aside>' in response.text
    assert "查看原始 Markdown" in response.text
    assert "下载 Markdown" in response.text


def test_diagnosis_raw_and_download_return_the_unmodified_case_file(client, repo_root):
    case = repo_root / "test_workspace/outputs/eval-one/cases/Q1"
    content = "# 任意内容\n\n\\u7ed9 and 中文\n"
    (case / "diagnosis.md").write_text(content, encoding="utf-8")

    raw = client.get("/runs/eval-one/cases/Q1/diagnosis/raw")
    download = client.get("/runs/eval-one/cases/Q1/diagnosis/download")

    assert raw.status_code == 200
    assert raw.text == content
    assert raw.headers["content-type"].startswith("text/plain")
    assert download.status_code == 200
    assert download.content == content.encode()
    assert "attachment" in download.headers["content-disposition"]


def test_missing_diagnosis_raw_and_download_return_404(client):
    base = "/runs/eval-one/cases/Q1/diagnosis"

    assert client.get(f"{base}/raw").status_code == 404
    assert client.get(f"{base}/download").status_code == 404


def test_record_pages_show_elapsed_times_unicode_summary_and_collapsible_raw_data(client, repo_root):
    trace = repo_root / "test_workspace/outputs/eval-one/cases/Q1/attempts/attempt-q1/agent/trace.jsonl"
    trace.write_text(
        "\n".join(
            [
                '{"event":"turn.input","timestamp":"2026-08-21T09:59:59+00:00","data":{"content":"给我生成页面"}}',
                '{"event":"tool.request","timestamp":"2026-08-21T10:00:00.250+00:00","session_id":"hidden-session","data":{"tool_name":"write_file","arguments":{"path":"页面.html","content":"完整原文"}}}',
                '{"event":"tool.response","timestamp":"2026-08-21T10:00:02.500+00:00","data":{"tool_name":"write_file","success":true,"content":"写入成功"}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    response = client.get("/runs/eval-one/cases/Q1/agent")

    assert response.status_code == 200
    assert "工具调用" in response.text
    assert "write_file" in response.text
    assert "页面.html" in response.text
    assert "给我生成页面" in response.text
    assert r"\u7ed9\u6211\u751f\u6210" not in response.text
    assert "+0.000s" in response.text
    assert "+1.250s" in response.text
    assert "Δ1.250s" in response.text
    assert "+3.500s" in response.text
    assert "Δ2.250s" in response.text
    assert response.text.count('class="raw-data"') == 3
    assert "Raw Data" in response.text
    assert 'src="/static/chevron.svg"' in response.text


def test_record_header_and_timing_share_a_layout_row(client):
    response = client.get("/runs/eval-one/cases/Q1/agent")

    assert response.status_code == 200
    row_start = response.text.index('<div class="record-meta-row">')
    row_end = response.text.index("</div>", row_start)
    row = response.text[row_start:row_end]
    assert 'class="record-header"' in row
    assert 'class="record-timing"' in row


def test_record_header_and_timing_stay_in_a_flex_row_on_narrow_screens(client):
    response = client.get("/static/styles.css")

    assert response.status_code == 200
    narrow_screen_rules = response.text.rsplit("@media(max-width:700px)", 1)[-1]
    assert ".record-meta-row{display:flex" in narrow_screen_rules


def test_agent_page_lists_each_tool_call_and_links_to_its_record(client, repo_root):
    trace = repo_root / "test_workspace/outputs/eval-one/cases/Q1/attempts/attempt-q1/agent/trace.jsonl"
    trace.write_text(
        "\n".join(
            [
                '{"event":"turn.input","data":{"content":"start"}}',
                '{"event":"tool.request","data":{"tool_name":"write_file","arguments":{}}}',
                '{"event":"tool.response","data":{"tool_name":"write_file","success":true}}',
                '{"event":"tool.request","data":{"tool_name":"read_file","arguments":{}}}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    response = client.get("/runs/eval-one/cases/Q1/agent")

    assert response.status_code == 200
    assert 'class="tool-call-index"' in response.text
    assert '<ul>' in response.text
    assert '<a href="#record-agent-2"><span>#2</span> write_file</a>' in response.text
    assert '<a href="#record-agent-4"><span>#4</span> read_file</a>' in response.text
    assert response.text.index("write_file</a>") < response.text.index("read_file</a>")
    assert 'id="record-agent-2"' in response.text
    assert 'id="record-agent-4"' in response.text


def test_agent_tool_call_index_links_to_records_on_later_pages(client, repo_root):
    trace = repo_root / "test_workspace/outputs/eval-one/cases/Q1/attempts/attempt-q1/agent/trace.jsonl"
    trace.write_text(
        "".join(
            [
                '{"event":"turn.input","data":{"content":"record %d"}}\n' % index
                for index in range(200)
            ]
        )
        + '{"event":"tool.request","data":{"tool_name":"late_tool","arguments":{}}}\n',
        encoding="utf-8",
    )

    response = client.get("/runs/eval-one/cases/Q1/agent")

    assert response.status_code == 200
    assert '<a href="?page=2#record-agent-201"><span>#201</span> late_tool</a>' in response.text


def test_raw_data_summary_has_a_compact_label(client):
    response = client.get("/runs/eval-one/cases/Q1/agent")

    assert response.status_code == 200
    assert '<span class="raw-data-label">Raw Data</span>' in response.text


def test_each_timestamped_record_page_uses_the_same_elapsed_labels(client):
    base = "/runs/eval-one/cases/Q1"

    for source in ("timeline", "agent", "acp", "process"):
        response = client.get(f"{base}/{source}")
        assert response.status_code == 200
        assert "距开始" in response.text
        assert "+0.000s" in response.text


def test_process_and_timeline_have_start_elapsed_seconds_for_every_block(client):
    base = "/runs/eval-one/cases/Q1"

    for source in ("timeline", "process"):
        response = client.get(f"{base}/{source}")
        assert response.status_code == 200
        assert "距开始 <strong>—</strong>" not in response.text


def test_unified_timeline_elapsed_time_uses_full_order_before_paging(client):
    response = client.get("/runs/eval-one/cases/Q1/timeline")

    assert response.status_code == 200
    assert "+0.500s" in response.text
    assert "Δ0.500s" in response.text
    assert "+1.000s" in response.text


def test_htmx_record_page_returns_fragment(client):
    response = client.get(
        "/runs/eval-one/cases/Q1/timeline?page=1",
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "<!doctype html>" not in response.text.lower()
    assert "record-card" in response.text


def test_load_more_replaces_its_previous_control(client, repo_root):
    protocol = repo_root / "test_workspace/outputs/eval-one/cases/Q1/attempts/attempt-q1/protocol.jsonl"
    protocol.write_text(
        "".join('{"sequence": %d, "message": {}}\n' % index for index in range(205)),
        encoding="utf-8",
    )
    response = client.get(
        "/runs/eval-one/cases/Q1/timeline?page=1",
        headers={"HX-Request": "true"},
    )
    assert "load-more" in response.text
    assert 'hx-target="this"' in response.text
    assert 'hx-swap="outerHTML"' in response.text


def test_download_is_contained_in_attempt(client, repo_root):
    attempt = repo_root / "test_workspace/outputs/eval-one/cases/Q1/attempts/attempt-q1"
    (attempt / "workspace").mkdir()
    (attempt / "workspace/demo.txt").write_text("hello", encoding="utf-8")
    response = client.get("/runs/eval-one/cases/Q1/download/workspace/demo.txt")
    assert response.status_code == 200
    assert response.text == "hello"
    assert client.get("/runs/eval-one/cases/Q1/download/../../../../etc/passwd").status_code in {404, 422}
