"""管理背景佈署工作、單一工作鎖定、設定快照、斷線重連、WebLogic Redeploy、啟動復原與分頁歷史查詢 (SPEC v1.1.0)。"""

from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from app.errors import AppError, ErrorCode
from app.models import (
    CURRENT_SCHEMA_VERSION,
    DEFAULT_WEBLOGIC_REDEPLOY_PATH,
    DEFAULT_WEBLOGIC_REST_VERSION,
    DEFAULT_WEBLOGIC_TIMEOUT_SECONDS,
    default_profile_weblogic_redeploy_config,
    default_server_weblogic_config,
)
from app.sftp_client import SftpSession
from app.storage import ConfigStore, atomic_write_json, read_json_file
from app.weblogic_client import TW_TZ, run_weblogic_redeploy, tw_now_str


def generate_job_id() -> str:
    """
    產生包含台灣時間戳（精確至微秒以確保嚴格時序）的前綴 jobId (SPEC v1.1.0 第 9 章)：
    例如：job-20261114T010500123456-a1b2c3
    """
    ts = datetime.now(TW_TZ).strftime("%Y%m%dT%H%M%S%f")
    suffix = uuid.uuid4().hex[:6]
    return f"job-{ts}-{suffix}"


class JobStatus:
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIALLY_SUCCEEDED = "PARTIALLY_SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class FileStatus:
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class WeblogicStatus:
    DISABLED = "DISABLED"
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    INTERRUPTED = "INTERRUPTED"


def _default_job_weblogic_snapshot() -> dict[str, Any]:
    return {
        "enabled": False,
        "status": WeblogicStatus.DISABLED,
        "adminUrl": "",
        "appName": "",
        "targets": "",
        "startedAt": None,
        "finishedAt": None,
        "error": None,
        "logs": [],
    }


class JobManager:
    """管理單一背景佈署工作、快照執行、WebLogic Redeploy 與歷史紀錄。"""

    def __init__(
        self,
        config_store: ConfigStore,
        sftp_session_factory: Optional[Callable[..., SftpSession]] = None,
        weblogic_redeploy_runner: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.config_store = config_store
        self.logs_dir: Path = config_store.logs_dir
        self._sftp_session_factory = sftp_session_factory or SftpSession
        self._weblogic_redeploy_runner = weblogic_redeploy_runner or run_weblogic_redeploy

        self._lock = threading.RLock()
        self._active_job: Optional[dict[str, Any]] = None
        # 保存最近執行過但寫入磁碟失敗的工作於記憶體，確保查詢仍可得知警告狀態
        self._memory_jobs: dict[str, dict[str, Any]] = {}

    def recover_interrupted_jobs_on_startup(self) -> int:
        """
        依 SPEC 10.1 步驟 6 與 13.3 中斷復原：
        重新啟動時，若歷史工作仍為 QUEUED 或 RUNNING：
        - 整體標記為 INTERRUPTED。
        - 保留已保存的成功與失敗結果。
        - 尚未完成的檔案 (PENDING / UPLOADING) 標記為 INTERRUPTED。
        - 若 weblogicRedeploy 為 PENDING / RUNNING，亦標記為 INTERRUPTED。
        - 不自動續傳或重跑。
        - 若無法得知實際中斷時間，不以重新啟動時間冒充原工作結束時間 (finishedAt 保持 null)。
        """
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        recovered_count = 0

        for file_path in sorted(self.logs_dir.glob("*.json")):
            try:
                job_data = read_json_file(file_path)
            except Exception:
                continue

            if not isinstance(job_data, dict):
                continue

            status = job_data.get("status")
            if status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                continue

            job_data["status"] = JobStatus.INTERRUPTED
            # 不以重新啟動時間冒充原工作結束時間
            job_data["finishedAt"] = None

            files = job_data.get("files", [])
            success_cnt = 0
            failed_cnt = 0
            interrupted_cnt = 0

            if isinstance(files, list):
                for f_item in files:
                    if not isinstance(f_item, dict):
                        continue
                    f_status = f_item.get("status")
                    if f_status == FileStatus.SUCCEEDED:
                        success_cnt += 1
                    elif f_status == FileStatus.FAILED:
                        failed_cnt += 1
                    else:
                        f_item["status"] = FileStatus.INTERRUPTED
                        f_item["finishedAt"] = None
                        f_item["error"] = {
                            "code": ErrorCode.JOB_INTERRUPTED,
                            "message": "工作非正常中斷，無法確認檔案是否正常完成。",
                        }
                        interrupted_cnt += 1

            wl_info = job_data.get("weblogicRedeploy")
            if isinstance(wl_info, dict) and wl_info.get("enabled"):
                if wl_info.get("status") in (WeblogicStatus.PENDING, WeblogicStatus.RUNNING):
                    wl_info["status"] = WeblogicStatus.INTERRUPTED
                    wl_info["finishedAt"] = None
                    wl_info["error"] = {
                        "code": ErrorCode.JOB_INTERRUPTED,
                        "message": "工作非正常中斷，無法確認 WebLogic Redeploy 是否完成。",
                    }

            job_data["successCount"] = success_cnt
            job_data["failedCount"] = failed_cnt
            job_data["interruptedCount"] = interrupted_cnt
            job_data["totalCount"] = len(files) if isinstance(files, list) else 0

            try:
                atomic_write_json(
                    file_path,
                    job_data,
                    error_code=ErrorCode.JOB_RECORD_WRITE_FAILED,
                    keep_backup=False,
                )
                recovered_count += 1
            except Exception:
                pass

        return recovered_count

    def has_active_job(self) -> bool:
        with self._lock:
            return self._active_job is not None and self._active_job.get("status") in (
                JobStatus.QUEUED,
                JobStatus.RUNNING,
            )

    def get_active_job_id(self) -> Optional[str]:
        with self._lock:
            if self._active_job and self._active_job.get("status") in (
                JobStatus.QUEUED,
                JobStatus.RUNNING,
            ):
                return str(self._active_job["jobId"])
            return None

    def get_active_job_snapshot(self) -> Optional[dict[str, Any]]:
        with self._lock:
            if self._active_job and self._active_job.get("status") in (
                JobStatus.QUEUED,
                JobStatus.RUNNING,
            ):
                return copy.deepcopy(self._active_job)
            return None

    def start_deployment(self, profile_id: str) -> dict[str, Any]:
        """
        依 SPEC 建立工作並啟動背景執行緒：
        1. 驗證佈署設定存在。
        2. 驗證 Server 引用存在。
        3. 驗證設定結構有效。
        4. 確認沒有其他工作執行（若有則回 409 DEPLOYMENT_ALREADY_RUNNING）。
        5. 取得當下設定快照（密碼不寫入工作紀錄）。
        6. 建立初始工作紀錄（若寫入失敗，拋出 500 JOB_RECORD_WRITE_FAILED，不開始任何遠端寫入）。
        7. 回傳 jobId 與 status=QUEUED。
        8. 背景執行上傳與選填之 WebLogic Redeploy。
        """
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "profileId 為必填字串。",
                status_code=400,
            )

        with self._lock:
            if self._active_job is not None and self._active_job.get("status") in (
                JobStatus.QUEUED,
                JobStatus.RUNNING,
            ):
                active_id = self._active_job["jobId"]
                raise AppError(
                    ErrorCode.DEPLOYMENT_ALREADY_RUNNING,
                    "目前已有佈署工作執行中。",
                    status_code=409,
                    details={"jobId": active_id},
                )

            profile = self.config_store.get_profile(profile_id.strip())
            server_with_cred = self.config_store.get_server_with_credentials(profile["serverId"])

            wl_profile_cfg = profile.get("weblogicRedeploy") or default_profile_weblogic_redeploy_config()
            wl_srv_cfg = server_with_cred.get("weblogic") or default_server_weblogic_config()

            wl_enabled = bool(wl_profile_cfg.get("enabled", False))
            if wl_enabled:
                if not wl_srv_cfg.get("adminUrl") or not wl_srv_cfg.get("username") or not wl_srv_cfg.get("password"):
                    raise AppError(
                        ErrorCode.WEBLOGIC_NOT_CONFIGURED,
                        "此佈署設定已啟用 WebLogic Redeploy，但目標 Server 尚未完整設定 WebLogic Admin 連線資訊。",
                        status_code=400,
                    )

            job_id = generate_job_id()
            created_at = tw_now_str()

            files_snapshot: list[dict[str, Any]] = []
            for f in profile["files"]:
                files_snapshot.append(
                    {
                        "id": f["id"],
                        "localPath": f["localPath"],
                        "remotePath": f["remotePath"],
                        "sizeBytes": None,
                        "status": FileStatus.PENDING,
                        "startedAt": None,
                        "finishedAt": None,
                        "error": None,
                    }
                )

            weblogic_snapshot: dict[str, Any] = {
                "enabled": wl_enabled,
                "status": WeblogicStatus.PENDING if wl_enabled else WeblogicStatus.DISABLED,
                "adminUrl": wl_srv_cfg.get("adminUrl", "") if wl_enabled else "",
                "restVersion": wl_srv_cfg.get("restVersion", DEFAULT_WEBLOGIC_REST_VERSION) if wl_enabled else "",
                "username": wl_srv_cfg.get("username", "") if wl_enabled else "",
                "appName": wl_profile_cfg.get("appName", "") if wl_enabled else "",
                "targets": wl_profile_cfg.get("targets", "") if wl_enabled else "",
                "timeoutSeconds": wl_profile_cfg.get("timeoutSeconds", DEFAULT_WEBLOGIC_TIMEOUT_SECONDS),
                "redeployPath": wl_profile_cfg.get("redeployPath", DEFAULT_WEBLOGIC_REDEPLOY_PATH),
                "requestBodyJson": wl_profile_cfg.get("requestBodyJson", ""),
                "startedAt": None,
                "finishedAt": None,
                "error": None,
                "logs": [],
            }

            # 敏感登入資訊 (password / passwordBase64) 絕不寫入工作紀錄
            job_record: dict[str, Any] = {
                "schemaVersion": CURRENT_SCHEMA_VERSION,
                "jobId": job_id,
                "profileId": profile["id"],
                "profileName": profile["name"],
                "server": {
                    "id": server_with_cred["id"],
                    "name": server_with_cred["name"],
                    "host": server_with_cred["host"],
                    "port": server_with_cred["port"],
                    "username": server_with_cred["username"],
                },
                "createdAt": created_at,
                "startedAt": None,
                "finishedAt": None,
                "status": JobStatus.QUEUED,
                "totalCount": len(files_snapshot),
                "successCount": 0,
                "failedCount": 0,
                "interruptedCount": 0,
                "recordWriteFailed": False,
                "warningMessage": None,
                "files": files_snapshot,
                "weblogicRedeploy": weblogic_snapshot,
            }

            # Step 6: 建立初始工作紀錄（若失敗不開始任何遠端寫入，對應 AC-26）
            job_file_path = self.logs_dir / f"{job_id}.json"
            atomic_write_json(
                job_file_path,
                job_record,
                error_code=ErrorCode.JOB_RECORD_WRITE_FAILED,
                keep_backup=False,
            )

            self._active_job = job_record
            self._memory_jobs[job_id] = copy.deepcopy(job_record)

            # 僅將解碼後密碼傳給背景工作執行緒，不保存於 job_record
            sftp_password_plain = server_with_cred["password"]
            wl_password_plain = wl_srv_cfg.get("password", "")

            worker = threading.Thread(
                target=self._run_deployment_worker,
                args=(job_id, sftp_password_plain, wl_password_plain),
                name=f"DeployWorker-{job_id}",
                daemon=True,
            )
            worker.start()

            return {
                "jobId": job_id,
                "status": JobStatus.QUEUED,
            }

    def _job_file_path(self, job_id: str) -> Path:
        return self.logs_dir / f"{job_id}.json"

    def _try_persist_active_job(self) -> None:
        """
        工作執行期間更新工作紀錄檔 (SPEC 14.4, 14.5, AC-27)：
        若寫入失敗，繼續依既定政策處理檔案，但標記 recordWriteFailed=True 並提供 warningMessage。
        """
        with self._lock:
            if self._active_job is None:
                return
            job_id = self._active_job["jobId"]
            snapshot = copy.deepcopy(self._active_job)

        try:
            atomic_write_json(
                self._job_file_path(job_id),
                snapshot,
                error_code=ErrorCode.JOB_RECORD_WRITE_FAILED,
                keep_backup=False,
            )
        except Exception as exc:
            with self._lock:
                if self._active_job is not None and self._active_job["jobId"] == job_id:
                    self._active_job["recordWriteFailed"] = True
                    self._active_job["warningMessage"] = (
                        f"工作紀錄寫入磁碟失敗（{exc}），檔案上傳仍繼續進行，但結果可能無法永久保存。"
                    )
                    self._memory_jobs[job_id] = copy.deepcopy(self._active_job)
            return

        with self._lock:
            if self._active_job is not None and self._active_job["jobId"] == job_id:
                self._memory_jobs[job_id] = copy.deepcopy(self._active_job)

    def _recalculate_counts(self, job: dict[str, Any]) -> None:
        files = job.get("files", [])
        job["totalCount"] = len(files)
        job["successCount"] = sum(1 for f in files if f["status"] == FileStatus.SUCCEEDED)
        job["failedCount"] = sum(1 for f in files if f["status"] == FileStatus.FAILED)
        job["interruptedCount"] = sum(1 for f in files if f["status"] == FileStatus.INTERRUPTED)

    def _run_deployment_worker(
        self,
        job_id: str,
        sftp_password_plain: str,
        wl_password_plain: str = "",
    ) -> None:
        session: Optional[SftpSession] = None
        try:
            with self._lock:
                if self._active_job is None or self._active_job["jobId"] != job_id:
                    return
                self._active_job["status"] = JobStatus.RUNNING
                self._active_job["startedAt"] = tw_now_str()
                server_info = copy.deepcopy(self._active_job["server"])
                total_files = len(self._active_job["files"])

            self._try_persist_active_job()

            needs_reconnect = False

            for idx in range(total_files):
                with self._lock:
                    if self._active_job is None:
                        return
                    f_entry = self._active_job["files"][idx]
                    local_path = f_entry["localPath"]
                    remote_path = f_entry["remotePath"]
                    f_entry["status"] = FileStatus.UPLOADING
                    f_entry["startedAt"] = tw_now_str()
                self._try_persist_active_job()

                # 先快速檢查本機檔案是否存在／是否為一般檔案，避免本機不存在還去建立連線
                import os

                if not os.path.exists(local_path):
                    with self._lock:
                        f_entry = self._active_job["files"][idx]
                        f_entry["status"] = FileStatus.FAILED
                        f_entry["finishedAt"] = tw_now_str()
                        f_entry["error"] = {
                            "code": ErrorCode.LOCAL_FILE_NOT_FOUND,
                            "message": "本機來源檔案不存在。",
                        }
                        self._recalculate_counts(self._active_job)
                    self._try_persist_active_job()
                    continue

                if os.path.isdir(local_path) or not os.path.isfile(local_path):
                    with self._lock:
                        f_entry = self._active_job["files"][idx]
                        f_entry["status"] = FileStatus.FAILED
                        f_entry["finishedAt"] = tw_now_str()
                        f_entry["error"] = {
                            "code": ErrorCode.LOCAL_PATH_NOT_FILE,
                            "message": "本機來源路徑不是一般檔案。",
                        }
                        self._recalculate_counts(self._active_job)
                    self._try_persist_active_job()
                    continue

                # 確保 SFTP Session 已連線（初始連線或斷線後重連一次）
                if session is None or needs_reconnect or not session.is_active():
                    if session is not None:
                        session.close()
                        session = None

                    session = self._sftp_session_factory(
                        host=server_info["host"],
                        port=server_info["port"],
                        username=server_info["username"],
                        password=sftp_password_plain,
                    )
                    try:
                        session.connect()
                        needs_reconnect = False
                    except AppError as conn_err:
                        # 依 SPEC 12.6：Server 無法連線或認證失敗（或斷線後重連失敗）時，
                        # 當前與後續所有尚未完成的檔案一併記錄失敗及共同原因，不重複嘗試失敗登入。
                        now_str = tw_now_str()
                        with self._lock:
                            for rem_idx in range(idx, total_files):
                                rem_file = self._active_job["files"][rem_idx]
                                if rem_file["status"] in (FileStatus.PENDING, FileStatus.UPLOADING):
                                    if rem_file["startedAt"] is None:
                                        rem_file["startedAt"] = now_str
                                    rem_file["finishedAt"] = now_str
                                    rem_file["status"] = FileStatus.FAILED
                                    rem_file["error"] = {
                                        "code": conn_err.code,
                                        "message": conn_err.message,
                                    }
                            self._recalculate_counts(self._active_job)
                        self._try_persist_active_job()
                        break

                # 執行單檔上傳
                upload_res = session.upload_single_file(local_path, remote_path)
                finished_str = tw_now_str()

                with self._lock:
                    f_entry = self._active_job["files"][idx]
                    f_entry["sizeBytes"] = upload_res.size_bytes
                    f_entry["finishedAt"] = finished_str
                    if upload_res.succeeded:
                        f_entry["status"] = FileStatus.SUCCEEDED
                        f_entry["error"] = None
                    else:
                        f_entry["status"] = FileStatus.FAILED
                        f_entry["error"] = {
                            "code": upload_res.error_code or ErrorCode.SFTP_UPLOAD_FAILED,
                            "message": upload_res.error_message or "檔案上傳失敗。",
                        }
                    self._recalculate_counts(self._active_job)

                self._try_persist_active_job()

                if upload_res.connection_lost:
                    # 依 SPEC 12.6：上傳中斷線時，當前檔案失敗，不重試當前檔案，可為下一筆檔案重新建立連線
                    needs_reconnect = True
                    if session is not None:
                        session.close()
                        session = None

            # SFTP 傳輸結束後先關閉 SFTP Session
            if session is not None:
                session.close()
                session = None

            # 檢查是否需要觸發 WebLogic Redeploy (SPEC v1.1.0 第 7 章)
            with self._lock:
                if self._active_job is None or self._active_job["jobId"] != job_id:
                    return
                self._recalculate_counts(self._active_job)
                total_c = self._active_job["totalCount"]
                succ_c = self._active_job["successCount"]
                wl_snap = copy.deepcopy(
                    self._active_job.get("weblogicRedeploy") or _default_job_weblogic_snapshot()
                )

            wl_enabled = bool(wl_snap.get("enabled", False))
            wl_succeeded = True

            if wl_enabled:
                if succ_c == total_c and total_c > 0:
                    # 所有檔案 SFTP 上傳成功 -> 執行 WebLogic Redeploy
                    with self._lock:
                        if self._active_job is not None and self._active_job["jobId"] == job_id:
                            self._active_job["weblogicRedeploy"]["status"] = WeblogicStatus.RUNNING
                            self._active_job["weblogicRedeploy"]["startedAt"] = tw_now_str()
                    self._try_persist_active_job()

                    def _on_wl_log(log_entry: dict[str, str]) -> None:
                        with self._lock:
                            if self._active_job is not None and self._active_job["jobId"] == job_id:
                                self._active_job["weblogicRedeploy"]["logs"].append(log_entry)
                        self._try_persist_active_job()

                    wl_res = self._weblogic_redeploy_runner(
                        admin_url=wl_snap.get("adminUrl", ""),
                        username=wl_snap.get("username", ""),
                        password=wl_password_plain,
                        rest_version=wl_snap.get("restVersion", DEFAULT_WEBLOGIC_REST_VERSION),
                        app_name=wl_snap.get("appName", ""),
                        targets=wl_snap.get("targets", ""),
                        timeout_seconds=int(
                            wl_snap.get("timeoutSeconds") or DEFAULT_WEBLOGIC_TIMEOUT_SECONDS
                        ),
                        redeploy_path=wl_snap.get("redeployPath", DEFAULT_WEBLOGIC_REDEPLOY_PATH),
                        request_body_json=wl_snap.get("requestBodyJson", ""),
                        on_log_callback=_on_wl_log,
                    )

                    wl_succeeded = bool(wl_res.succeeded)
                    with self._lock:
                        if self._active_job is not None and self._active_job["jobId"] == job_id:
                            wl_state = self._active_job["weblogicRedeploy"]
                            wl_state["finishedAt"] = tw_now_str()
                            wl_state["status"] = (
                                WeblogicStatus.SUCCEEDED
                                if wl_res.succeeded
                                else WeblogicStatus.FAILED
                            )
                            wl_state["error"] = (
                                None
                                if wl_res.succeeded
                                else {
                                    "code": wl_res.error_code or ErrorCode.WEBLOGIC_REDEPLOY_FAILED,
                                    "message": wl_res.error_message or "WebLogic Redeploy 失敗。",
                                }
                            )
                            if wl_res.logs:
                                wl_state["logs"] = wl_res.logs
                    self._try_persist_active_job()
                else:
                    # 有任何一筆檔案失敗 -> 略過 WebLogic Redeploy (SPEC v1.1.0 7.1, AC-35)
                    skip_msg = "因部分或全部檔案 SFTP 上傳失敗，已自動略過 WebLogic Redeploy。"
                    now_skip = tw_now_str()
                    with self._lock:
                        if self._active_job is not None and self._active_job["jobId"] == job_id:
                            wl_state = self._active_job["weblogicRedeploy"]
                            wl_state["status"] = WeblogicStatus.SKIPPED
                            wl_state["finishedAt"] = now_skip
                            wl_state["logs"].append(
                                {
                                    "time": now_skip,
                                    "level": "err",
                                    "message": skip_msg,
                                }
                            )
                    self._try_persist_active_job()

            # 工作結束，依 SPEC v1.1.0 7.2 計算最終狀態
            with self._lock:
                if self._active_job is not None and self._active_job["jobId"] == job_id:
                    self._recalculate_counts(self._active_job)
                    total_c = self._active_job["totalCount"]
                    succ_c = self._active_job["successCount"]
                    if succ_c == total_c and total_c > 0:
                        if wl_enabled and not wl_succeeded:
                            final_status = JobStatus.PARTIALLY_SUCCEEDED
                        else:
                            final_status = JobStatus.SUCCEEDED
                    elif 0 < succ_c < total_c:
                        final_status = JobStatus.PARTIALLY_SUCCEEDED
                    else:
                        final_status = JobStatus.FAILED

                    self._active_job["status"] = final_status
                    self._active_job["finishedAt"] = tw_now_str()

            self._try_persist_active_job()

        except Exception as unexpected_exc:
            now_str = tw_now_str()
            with self._lock:
                if self._active_job is not None and self._active_job["jobId"] == job_id:
                    for f_item in self._active_job.get("files", []):
                        if f_item["status"] in (FileStatus.PENDING, FileStatus.UPLOADING):
                            f_item["status"] = FileStatus.FAILED
                            f_item["finishedAt"] = now_str
                            f_item["error"] = {
                                "code": ErrorCode.INTERNAL_ERROR,
                                "message": f"非預期的系統錯誤：{unexpected_exc}",
                            }
                    self._recalculate_counts(self._active_job)
                    succ_c = self._active_job["successCount"]
                    self._active_job["status"] = (
                        JobStatus.PARTIALLY_SUCCEEDED if succ_c > 0 else JobStatus.FAILED
                    )
                    self._active_job["finishedAt"] = now_str
                    self._active_job["warningMessage"] = f"系統非預期錯誤：{unexpected_exc}"
            self._try_persist_active_job()
        finally:
            if session is not None:
                session.close()
            with self._lock:
                if self._active_job is not None and self._active_job["jobId"] == job_id:
                    self._memory_jobs[job_id] = copy.deepcopy(self._active_job)
                    self._active_job = None

    # -------------------- 查詢與分頁介面 --------------------

    def get_job(self, job_id: str) -> dict[str, Any]:
        """查詢單一工作與各檔案詳細結果 (向下相容舊版無 weblogicRedeploy 欄位之紀錄)。"""
        if not isinstance(job_id, str) or not job_id.strip():
            raise AppError(ErrorCode.CONFIG_INVALID, "無效的 jobId。", status_code=400)

        clean_id = job_id.strip()
        with self._lock:
            if self._active_job is not None and self._active_job["jobId"] == clean_id:
                return copy.deepcopy(self._active_job)
            if clean_id in self._memory_jobs and self._memory_jobs[clean_id].get("recordWriteFailed"):
                return copy.deepcopy(self._memory_jobs[clean_id])

        # 防止路徑穿越
        if "/" in clean_id or "\\" in clean_id or ".." in clean_id:
            raise AppError(ErrorCode.CONFIG_INVALID, "無效的 jobId 格式。", status_code=400)

        file_path = self._job_file_path(clean_id)
        if not file_path.exists():
            with self._lock:
                if clean_id in self._memory_jobs:
                    return copy.deepcopy(self._memory_jobs[clean_id])
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"找不到指定的工作紀錄：{clean_id}",
                status_code=404,
            )

        data = read_json_file(file_path)
        if "recordWriteFailed" not in data:
            data["recordWriteFailed"] = False
        if "warningMessage" not in data:
            data["warningMessage"] = None
        if "weblogicRedeploy" not in data or not isinstance(data.get("weblogicRedeploy"), dict):
            data["weblogicRedeploy"] = _default_job_weblogic_snapshot()
        return data

    def list_jobs_paginated(self, page: int = 1, page_size: int = 20) -> dict[str, Any]:
        """
        分頁查詢歷史工作摘要 (SPEC 14.1, 14.4, 15.5, 18.3)：
        - 預設依建立時間由新到舊排序。
        - 透過檔名倒序排序並切片，只讀取當頁對應的 JSON 檔，避免一次讀入所有歷史工作檔案。
        - 摘要不含 files 陣列，但若執行中則包含目前處理檔案提示 (currentFile) 及 WebLogic 狀態摘要。
        """
        if not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "page 必須為大於或等於 1 的整數。",
                status_code=400,
            )
        if not isinstance(page_size, int) or isinstance(page_size, bool) or page_size < 1 or page_size > 100:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "pageSize 必須為 1 到 100 之間的整數。",
                status_code=400,
            )

        self.logs_dir.mkdir(parents=True, exist_ok=True)
        all_files = sorted(
            self.logs_dir.glob("*.json"),
            key=lambda p: p.name,
            reverse=True,
        )

        total = len(all_files)
        start_idx = (page - 1) * page_size
        end_idx = start_idx + page_size
        page_files = all_files[start_idx:end_idx]

        items: list[dict[str, Any]] = []
        for fp in page_files:
            job_id_stem = fp.stem
            job_obj: Optional[dict[str, Any]] = None
            with self._lock:
                if self._active_job is not None and self._active_job["jobId"] == job_id_stem:
                    job_obj = copy.deepcopy(self._active_job)
                elif (
                    job_id_stem in self._memory_jobs
                    and self._memory_jobs[job_id_stem].get("recordWriteFailed")
                ):
                    job_obj = copy.deepcopy(self._memory_jobs[job_id_stem])

            if job_obj is None:
                try:
                    job_obj = read_json_file(fp)
                except Exception:
                    continue

            if not isinstance(job_obj, dict):
                continue

            # 找出目前正在上傳的檔案（供清單摘要顯示）
            current_file = None
            for f_item in job_obj.get("files", []):
                if isinstance(f_item, dict) and f_item.get("status") == FileStatus.UPLOADING:
                    current_file = f_item.get("localPath")
                    break

            wl_obj = job_obj.get("weblogicRedeploy")
            if not isinstance(wl_obj, dict):
                wl_obj = _default_job_weblogic_snapshot()

            summary = {
                "jobId": job_obj.get("jobId"),
                "profileId": job_obj.get("profileId"),
                "profileName": job_obj.get("profileName"),
                "server": job_obj.get("server"),
                "createdAt": job_obj.get("createdAt"),
                "startedAt": job_obj.get("startedAt"),
                "finishedAt": job_obj.get("finishedAt"),
                "status": job_obj.get("status"),
                "totalCount": job_obj.get("totalCount", 0),
                "successCount": job_obj.get("successCount", 0),
                "failedCount": job_obj.get("failedCount", 0),
                "interruptedCount": job_obj.get("interruptedCount", 0),
                "recordWriteFailed": bool(job_obj.get("recordWriteFailed", False)),
                "warningMessage": job_obj.get("warningMessage"),
                "currentFile": current_file,
                "weblogicRedeploy": {
                    "enabled": bool(wl_obj.get("enabled", False)),
                    "status": wl_obj.get("status", WeblogicStatus.DISABLED),
                    "appName": wl_obj.get("appName", ""),
                },
            }
            items.append(summary)

        return {
            "page": page,
            "pageSize": page_size,
            "total": total,
            "items": items,
        }
