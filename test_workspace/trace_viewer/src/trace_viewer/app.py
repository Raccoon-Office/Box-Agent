"""FastAPI application factory."""

from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup
from pydantic import BaseModel, Field

from trace_viewer.evaluation import EvaluationLaunchError, EvaluationRunner
from trace_viewer.presentation import annotate_timing, json_text, present_record
from trace_viewer.repository import EvaluationRepository, NotFoundError
from trace_viewer.timeline import RECORDS_PER_PAGE, page_records, source_records, unified_timeline


PROJECT_DIR = Path(__file__).resolve().parents[2]


class EvaluationLaunchRequest(BaseModel):
    dataset_id: str = Field(min_length=1, max_length=200)
    model_id: str = Field(min_length=1, max_length=300)
    task_type: str | None = Field(default=None, min_length=1, max_length=100)
    execution_count: int = Field(ge=1, le=10_000)
    approved_data_disclosure: bool = False


def create_app(
    repo_root: Path,
    evaluation_runner: EvaluationRunner | None = None,
) -> FastAPI:
    app = FastAPI(title="ACP 离线评测查看器", docs_url=None, redoc_url=None)
    repository = EvaluationRepository(repo_root)
    runner = evaluation_runner or EvaluationRunner(repo_root)
    launches: dict[str, dict[str, Any]] = {}
    launches_lock = Lock()
    markdown = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])
    templates = Jinja2Templates(directory=PROJECT_DIR / "templates")
    app.mount("/static", StaticFiles(directory=PROJECT_DIR / "static"), name="static")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return templates.TemplateResponse(request, "index.html", {"runs": repository.list_runs()})

    @app.get("/api/evaluation-options")
    def evaluation_options() -> dict[str, Any]:
        try:
            return runner.list_options()
        except EvaluationLaunchError as error:
            raise HTTPException(503, str(error)) from error

    def execute_launch(
        launch_id: str,
        dataset_id: str,
        model: dict[str, Any],
        task_type: str | None,
        execution_count: int,
    ) -> None:
        with launches_lock:
            launches[launch_id]["status"] = "running"
        try:
            result = runner.run(
                dataset_id=dataset_id,
                model=str(model["id"]),
                model_max_tokens=model.get("max_tokens"),
                launch_id=launch_id,
                model_binding=model.get("binding"),
                task_type=task_type,
                execution_count=execution_count,
            )
        except Exception as error:  # noqa: BLE001 - preserve launch failure for UI polling
            detail = str(error) if isinstance(error, EvaluationLaunchError) else f"{type(error).__name__}: {error}"
            with launches_lock:
                launches[launch_id].update(
                    {
                        "status": "failed",
                        "finished_at": datetime.now(timezone.utc).isoformat(),
                        "error": detail[:2000],
                    }
                )
            return
        with launches_lock:
            launches[launch_id].update(
                {
                    "status": (
                        "completed"
                        if result.get("return_code") == 0
                        else "completed_with_failures"
                    ),
                    "finished_at": datetime.now(timezone.utc).isoformat(),
                    "run_name": result.get("run_name"),
                    "return_code": result.get("return_code"),
                    "error": None,
                }
            )

    @app.post("/api/evaluation-runs", status_code=202)
    def launch_evaluation(
        request: EvaluationLaunchRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        try:
            options = runner.list_options()
        except EvaluationLaunchError as error:
            raise HTTPException(503, str(error)) from error
        dataset = next(
            (
                item
                for item in options.get("datasets", [])
                if item.get("id") == request.dataset_id
            ),
            None,
        )
        model = next(
            (
                item
                for item in options.get("models", [])
                if item.get("id") == request.model_id
            ),
            None,
        )
        if dataset is None:
            raise HTTPException(422, "所选数据集不存在或已下线")
        if model is None:
            raise HTTPException(422, "所选模型不存在或已下线")
        selected_stats = None
        if request.task_type is not None:
            selected_stats = next(
                (
                    item
                    for item in dataset.get("task_type_stats", [])
                    if item.get("name") == request.task_type
                ),
                None,
            )
            if selected_stats is None:
                raise HTTPException(422, "所选数据集不包含该任务类型")
        available_count = (
            selected_stats.get("item_count", 0)
            if selected_stats is not None
            else dataset.get("item_count", 0)
        )
        if request.execution_count > available_count:
            raise HTTPException(
                422,
                f"执行条数必须在 1 到 {available_count} 之间",
            )
        attachment_count = (
            selected_stats.get("attachment_count", 0)
            if selected_stats is not None
            else dataset.get("attachment_count", 0)
        )
        if attachment_count and not request.approved_data_disclosure:
            raise HTTPException(422, "包含附件的数据集需要明确确认数据披露")

        launch_id = f"launch-{uuid4().hex[:12]}"
        document = {
            "launch_id": launch_id,
            "status": "queued",
            "dataset_id": request.dataset_id,
            "dataset_name": dataset.get("name"),
            "model": model.get("id"),
            "task_type": request.task_type,
            "execution_count": request.execution_count,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "run_name": None,
            "return_code": None,
            "error": None,
        }
        with launches_lock:
            launches[launch_id] = document
        background_tasks.add_task(
            execute_launch,
            launch_id,
            request.dataset_id,
            model,
            request.task_type,
            request.execution_count,
        )
        return dict(document)

    @app.get("/api/evaluation-runs/{launch_id}")
    def evaluation_launch_status(launch_id: str) -> dict[str, Any]:
        with launches_lock:
            document = launches.get(launch_id)
            if document is None:
                raise HTTPException(404, "评测启动记录不存在")
            return dict(document)

    @app.get("/runs/{run_name}", response_class=HTMLResponse)
    def run_page(request: Request, run_name: str, q: str = ""):
        try:
            cases = repository.list_cases(run_name, q)
        except NotFoundError as error:
            raise HTTPException(404, "评测目录不存在") from error
        return templates.TemplateResponse(request, "run.html", {"run_name": run_name, "cases": cases, "query": q})

    @app.get("/runs/{run_name}/cases/{case_id}", response_class=HTMLResponse)
    def case_overview(request: Request, run_name: str, case_id: str):
        try:
            case = repository.get_case(run_name, case_id)
        except NotFoundError as error:
            raise HTTPException(404, "任务不存在") from error
        return templates.TemplateResponse(
            request,
            "case.html",
            {
                "run_name": run_name,
                "case": case,
                "input_text": json_text(case["input"]),
                "active": "overview",
            },
        )

    def render_records(request: Request, run_name: str, case_id: str, source: str, page: int):
        try:
            case = repository.get_case(run_name, case_id)
        except NotFoundError as error:
            raise HTTPException(404, "任务不存在") from error
        records = unified_timeline(case["attempt_path"]) if source == "timeline" else source_records(case["attempt_path"], source)
        records = annotate_timing(records)
        tool_calls = []
        for position, record in enumerate(records, 1):
            record["anchor"] = f"record-{source}-{position}"
            payload = record.get("payload")
            if source != "agent" or not isinstance(payload, dict):
                continue
            event = payload.get("event") or payload.get("type")
            data = payload.get("data")
            tool_name = data.get("tool_name") if isinstance(data, dict) else None
            if event == "tool.request" and isinstance(tool_name, str) and tool_name:
                tool_calls.append(
                    {
                        "anchor": record["anchor"],
                        "index": record["index"],
                        "name": tool_name,
                        "page": (position - 1) // RECORDS_PER_PAGE + 1,
                    }
                )
        shown, next_page = page_records(records, page)
        shown = [present_record(record) for record in shown]
        context = {
            "run_name": run_name,
            "case": case,
            "active": source,
            "source": source,
            "records": shown,
            "next_page": next_page,
            "page": max(1, page),
            "tool_calls": tool_calls,
        }
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(request, "_records.html", context)
        return templates.TemplateResponse(request, "source.html", context)

    @app.get("/runs/{run_name}/cases/{case_id}/timeline", response_class=HTMLResponse)
    def timeline_page(request: Request, run_name: str, case_id: str, page: int = 1):
        return render_records(request, run_name, case_id, "timeline", page)

    @app.get("/runs/{run_name}/cases/{case_id}/agent", response_class=HTMLResponse)
    def agent_page(request: Request, run_name: str, case_id: str, page: int = 1):
        return render_records(request, run_name, case_id, "agent", page)

    @app.get("/runs/{run_name}/cases/{case_id}/acp", response_class=HTMLResponse)
    def acp_page(request: Request, run_name: str, case_id: str, page: int = 1):
        return render_records(request, run_name, case_id, "acp", page)

    @app.get("/runs/{run_name}/cases/{case_id}/process", response_class=HTMLResponse)
    def process_page(request: Request, run_name: str, case_id: str, page: int = 1):
        return render_records(request, run_name, case_id, "process", page)

    @app.get("/runs/{run_name}/cases/{case_id}/diagnosis", response_class=HTMLResponse)
    def diagnosis_page(request: Request, run_name: str, case_id: str):
        try:
            case = repository.get_case(run_name, case_id)
            content = repository.diagnosis_text(run_name, case_id)
        except NotFoundError as error:
            raise HTTPException(404, "任务不存在") from error
        rendered = Markup(markdown.render(content)) if content is not None else None
        return templates.TemplateResponse(
            request,
            "diagnosis.html",
            {
                "run_name": run_name,
                "case": case,
                "active": "diagnosis",
                "diagnosis": rendered,
            },
        )

    @app.get("/runs/{run_name}/cases/{case_id}/diagnosis/raw", response_class=PlainTextResponse)
    def diagnosis_raw(run_name: str, case_id: str):
        try:
            content = repository.diagnosis_text(run_name, case_id)
        except NotFoundError as error:
            raise HTTPException(404, "任务不存在") from error
        if content is None:
            raise HTTPException(404, "尚未生成诊断")
        return PlainTextResponse(content)

    @app.get("/runs/{run_name}/cases/{case_id}/diagnosis/download")
    def diagnosis_download(run_name: str, case_id: str):
        try:
            path = repository.diagnosis_path(run_name, case_id)
        except NotFoundError as error:
            raise HTTPException(404, "任务不存在") from error
        if path is None:
            raise HTTPException(404, "尚未生成诊断")
        return FileResponse(path, filename="diagnosis.md")

    @app.get("/runs/{run_name}/cases/{case_id}/files", response_class=HTMLResponse)
    def files_page(request: Request, run_name: str, case_id: str):
        try:
            case = repository.get_case(run_name, case_id)
        except NotFoundError as error:
            raise HTTPException(404, "任务不存在") from error
        files = [path.relative_to(case["attempt_path"]).as_posix() for path in sorted(case["attempt_path"].rglob("*")) if path.is_file()]
        return templates.TemplateResponse(request, "files.html", {"run_name": run_name, "case": case, "active": "files", "files": files})

    @app.get(
        "/runs/{run_name}/cases/{case_id}/effect",
        response_class=HTMLResponse,
    )
    def effect_page(request: Request, run_name: str, case_id: str):
        try:
            case = repository.get_case(run_name, case_id)
        except NotFoundError as error:
            raise HTTPException(404, "任务不存在") from error
        effect = case.get("effect_evaluation")
        metrics = effect.get("metrics", []) if isinstance(effect, dict) else []
        if not isinstance(metrics, list):
            metrics = []
        process_metrics = [
            metric
            for metric in metrics
            if isinstance(metric, dict) and metric.get("phase") == "process"
        ]
        result_metrics = [
            metric
            for metric in metrics
            if isinstance(metric, dict) and metric.get("phase") == "result"
        ]
        return templates.TemplateResponse(
            request,
            "effect.html",
            {
                "run_name": run_name,
                "case": case,
                "active": "effect",
                "effect": effect,
                "process_metrics": process_metrics,
                "result_metrics": result_metrics,
                "process_available": sum(
                    metric.get("status") == "complete" for metric in process_metrics
                ),
                "result_available": sum(
                    metric.get("status") == "complete" for metric in result_metrics
                ),
            },
        )

    @app.get("/runs/{run_name}/cases/{case_id}/download/{relative_path:path}")
    def download(run_name: str, case_id: str, relative_path: str):
        try:
            path = repository.resolve_case_path(run_name, case_id, relative_path)
        except NotFoundError as error:
            raise HTTPException(404, "文件不存在") from error
        return FileResponse(path, filename=path.name)

    return app
