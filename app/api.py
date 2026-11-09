"""FastAPI 路由、統一錯誤處理與內嵌靜態前端資源服務 (SPEC 15, 16, 17)。"""

from __future__ import annotations

import os
import signal
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.errors import AppError, ErrorCode
from app.job_manager import JobManager
from app.paths import get_base_dir, get_static_dir
from app.sftp_client import test_sftp_server_connection
from app.storage import ConfigStore


async def _parse_json_body(request: Request) -> dict[str, Any]:
    """解析並驗證 Content-Type: application/json 請求主體 (SPEC 15.1, 15.6)。"""
    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type.lower():
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "寫入請求必須使用 Content-Type: application/json。",
            status_code=400,
        )
    try:
        body = await request.json()
    except Exception:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "請求內容不是有效的 JSON 格式。",
            status_code=400,
        ) from None

    if not isinstance(body, dict):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "JSON 請求主體必須為物件格式。",
            status_code=400,
        )
    return body


def create_app(
    base_dir: Optional[Path] = None,
    config_store: Optional[ConfigStore] = None,
    job_manager: Optional[JobManager] = None,
    connection_tester: Optional[Callable[..., dict[str, str]]] = None,
    shutdown_callback: Optional[Callable[[], None]] = None,
) -> FastAPI:
    """建立並初始化 FastAPI 應用程式實例。"""
    resolved_base_dir = base_dir or get_base_dir()
    store = config_store or ConfigStore(resolved_base_dir)
    if config_store is None:
        store.initialize_and_load()

    jobs = job_manager or JobManager(store)
    if job_manager is None:
        jobs.recover_interrupted_jobs_on_startup()

    tester = connection_tester or test_sftp_server_connection

    app = FastAPI(
        title="Windows 本機 SFTP 檔案上傳工具",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    app.state.config_store = store
    app.state.job_manager = jobs

    # -------------------- 統一例外處理 --------------------

    @app.exception_handler(AppError)
    async def handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        err = AppError(
            ErrorCode.CONFIG_INVALID,
            f"請求參數驗證失敗：{exc}",
            status_code=400,
        )
        return JSONResponse(status_code=400, content=err.to_dict())

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        err = AppError(
            ErrorCode.INTERNAL_ERROR,
            "伺服器發生未預期的內部錯誤。",
            status_code=500,
        )
        return JSONResponse(status_code=500, content=err.to_dict())

    # -------------------- 系統與設定 API --------------------

    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        cfg = store.get_settings()
        cfg["activeJobId"] = jobs.get_active_job_id()
        return cfg

    @app.post("/api/shutdown")
    async def shutdown_app() -> dict[str, Any]:
        """
        依 SPEC 10.4 安全退出：
        若有工作執行中，回傳 409 DEPLOYMENT_ALREADY_RUNNING 提示使用者等待工作完成。
        """
        active_id = jobs.get_active_job_id()
        if active_id is not None:
            raise AppError(
                ErrorCode.DEPLOYMENT_ALREADY_RUNNING,
                "目前仍有佈署工作正在執行中，請等待工作完成後再退出工具。",
                status_code=409,
                details={"jobId": active_id},
            )

        if shutdown_callback is not None:
            threading.Timer(0.2, shutdown_callback).start()
        else:
            def _stop_process() -> None:
                os.kill(os.getpid(), signal.SIGINT)

            threading.Timer(0.3, _stop_process).start()

        return {"status": "SHUTTING_DOWN", "message": "本機服務即將關閉。"}

    # -------------------- Server 管理 API --------------------

    @app.get("/api/servers")
    async def list_servers() -> dict[str, Any]:
        return {"servers": store.list_servers_public()}

    @app.post("/api/servers", status_code=201)
    async def create_server(request: Request) -> dict[str, Any]:
        payload = await _parse_json_body(request)
        return store.create_server(payload)

    @app.put("/api/servers/{server_id}")
    async def update_server(server_id: str, request: Request) -> dict[str, Any]:
        payload = await _parse_json_body(request)
        return store.update_server(server_id, payload)

    @app.delete("/api/servers/{server_id}", status_code=204)
    async def delete_server(server_id: str) -> Response:
        store.delete_server(server_id)
        return Response(status_code=204)

    @app.post("/api/servers/{server_id}/test-connection")
    async def test_server_connection(server_id: str) -> dict[str, Any]:
        srv = store.get_server_with_credentials(server_id)
        res = tester(
            host=srv["host"],
            port=srv["port"],
            username=srv["username"],
            password=srv["password"],
        )
        return res

    # -------------------- 佈署設定 (Profiles) API --------------------

    @app.get("/api/profiles")
    async def list_profiles() -> dict[str, Any]:
        return {"deploymentProfiles": store.list_profiles()}

    @app.get("/api/profiles/{profile_id}")
    async def get_profile(profile_id: str) -> dict[str, Any]:
        return store.get_profile(profile_id)

    @app.post("/api/profiles", status_code=201)
    async def create_profile(request: Request) -> dict[str, Any]:
        payload = await _parse_json_body(request)
        return store.create_profile(payload)

    @app.put("/api/profiles/{profile_id}")
    async def update_profile(profile_id: str, request: Request) -> dict[str, Any]:
        payload = await _parse_json_body(request)
        return store.update_profile(profile_id, payload)

    @app.delete("/api/profiles/{profile_id}", status_code=204)
    async def delete_profile(profile_id: str) -> Response:
        store.delete_profile(profile_id)
        return Response(status_code=204)

    # -------------------- 佈署工作與歷史紀錄 API --------------------

    @app.post("/api/deployments", status_code=202)
    async def create_deployment(request: Request) -> dict[str, Any]:
        payload = await _parse_json_body(request)
        profile_id = payload.get("profileId")
        return jobs.start_deployment(profile_id)

    @app.get("/api/deployments/active")
    async def get_active_deployment() -> dict[str, Any]:
        return {"activeJob": jobs.get_active_job_snapshot()}

    @app.get("/api/deployments")
    async def list_deployments(request: Request) -> dict[str, Any]:
        raw_page = request.query_params.get("page", "1")
        raw_page_size = request.query_params.get("pageSize", "20")
        try:
            page = int(raw_page)
            page_size = int(raw_page_size)
        except ValueError:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "分頁參數 page 與 pageSize 必須為有效整數。",
                status_code=400,
            ) from None
        return jobs.list_jobs_paginated(page=page, page_size=page_size)

    @app.get("/api/deployments/{job_id}")
    async def get_deployment(job_id: str) -> dict[str, Any]:
        return jobs.get_job(job_id)

    # -------------------- 內嵌前端靜態資源服務 --------------------

    static_dir = get_static_dir()
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/")
        async def serve_index() -> FileResponse:
            return FileResponse(static_dir / "index.html")

    return app
