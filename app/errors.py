"""定義 SPEC 第 16 章標準錯誤代碼與統一錯誤例外結構 (SPEC 15.6, 15.7, 16)。"""

from __future__ import annotations

from typing import Any, Optional


class ErrorCode:
    CONFIG_INVALID = "CONFIG_INVALID"
    CONFIG_WRITE_FAILED = "CONFIG_WRITE_FAILED"
    HTTP_PORT_IN_USE = "HTTP_PORT_IN_USE"
    SERVER_NOT_FOUND = "SERVER_NOT_FOUND"
    SERVER_IN_USE = "SERVER_IN_USE"
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    DEPLOYMENT_ALREADY_RUNNING = "DEPLOYMENT_ALREADY_RUNNING"
    LOCAL_FILE_NOT_FOUND = "LOCAL_FILE_NOT_FOUND"
    LOCAL_PATH_NOT_FILE = "LOCAL_PATH_NOT_FILE"
    LOCAL_FILE_ACCESS_DENIED = "LOCAL_FILE_ACCESS_DENIED"
    LOCAL_FILE_READ_FAILED = "LOCAL_FILE_READ_FAILED"
    REMOTE_DIRECTORY_NOT_FOUND = "REMOTE_DIRECTORY_NOT_FOUND"
    REMOTE_PERMISSION_DENIED = "REMOTE_PERMISSION_DENIED"
    REMOTE_PATH_INVALID = "REMOTE_PATH_INVALID"
    SFTP_CONNECTION_FAILED = "SFTP_CONNECTION_FAILED"
    SFTP_AUTH_FAILED = "SFTP_AUTH_FAILED"
    SFTP_TIMEOUT = "SFTP_TIMEOUT"
    SFTP_UPLOAD_FAILED = "SFTP_UPLOAD_FAILED"
    JOB_RECORD_WRITE_FAILED = "JOB_RECORD_WRITE_FAILED"
    JOB_INTERRUPTED = "JOB_INTERRUPTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class AppError(Exception):
    """結構化應用程式錯誤，對應 SPEC 15.7 錯誤格式。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        err_obj: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
        }
        if self.details is not None:
            err_obj["details"] = self.details
        return {"error": err_obj}
