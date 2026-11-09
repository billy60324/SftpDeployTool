"""Paramiko SFTP 連線、認證、父目錄檢查與分塊串流上傳封裝 (SPEC 11.1, 12.4~12.9, 16, 18.2)。"""

from __future__ import annotations

import errno
import os
import posixpath
import socket
import stat
from typing import Optional

import paramiko

from app.errors import AppError, ErrorCode

CONNECT_TIMEOUT_SEC = 15.0
AUTH_TIMEOUT_SEC = 30.0
TRANSFER_TIMEOUT_SEC = 60.0
STREAM_CHUNK_SIZE = 64 * 1024  # 64 KB 區塊串流讀寫


class SftpFileUploadResult:
    """單一檔案上傳結果。"""

    def __init__(
        self,
        succeeded: bool,
        size_bytes: Optional[int] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        connection_lost: bool = False,
    ) -> None:
        self.succeeded = succeeded
        self.size_bytes = size_bytes
        self.error_code = error_code
        self.error_message = error_message
        self.connection_lost = connection_lost


def _is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    msg = str(exc).lower()
    return "timed out" in msg or "timeout" in msg


class SftpSession:
    """封裝單一 SFTP 連線工作階段。"""

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        connect_timeout: float = CONNECT_TIMEOUT_SEC,
        auth_timeout: float = AUTH_TIMEOUT_SEC,
        transfer_timeout: float = TRANSFER_TIMEOUT_SEC,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self.connect_timeout = connect_timeout
        self.auth_timeout = auth_timeout
        self.transfer_timeout = transfer_timeout

        self._sock: Optional[socket.socket] = None
        self._transport: Optional[paramiko.Transport] = None
        self._sftp: Optional[paramiko.SFTPClient] = None

    def connect(self) -> None:
        """
        建立 TCP 連線、SSH 交握、密碼認證與開啟 SFTP 子系統。
        失敗時拋出 AppError（代碼為 SFTP_TIMEOUT / SFTP_AUTH_FAILED / SFTP_CONNECTION_FAILED）。
        """
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)
        except Exception as exc:
            self.close()
            if _is_timeout_exception(exc):
                raise AppError(
                    ErrorCode.SFTP_TIMEOUT,
                    f"連線至 SFTP Server ({self.host}:{self.port}) 逾時。",
                    status_code=504,
                ) from None
            raise AppError(
                ErrorCode.SFTP_CONNECTION_FAILED,
                f"無法連線至 SFTP Server ({self.host}:{self.port})：{exc}",
                status_code=502,
            ) from None

        try:
            self._transport = paramiko.Transport(self._sock)
            self._transport.banner_timeout = self.auth_timeout
            self._transport.auth_timeout = self.auth_timeout
            self._transport.start_client(timeout=self.auth_timeout)
        except Exception as exc:
            self.close()
            if _is_timeout_exception(exc):
                raise AppError(
                    ErrorCode.SFTP_TIMEOUT,
                    f"SFTP Server ({self.host}:{self.port}) SSH 交握逾時。",
                    status_code=504,
                ) from None
            raise AppError(
                ErrorCode.SFTP_CONNECTION_FAILED,
                f"SFTP Server ({self.host}:{self.port}) SSH 協定交握失敗：{exc}",
                status_code=502,
            ) from None

        try:
            self._transport.auth_password(
                username=self.username,
                password=self._password,
                 fallback=True,
            )
        except paramiko.AuthenticationException:
            self.close()
            raise AppError(
                ErrorCode.SFTP_AUTH_FAILED,
                f"SFTP 帳號或密碼認證失敗（使用者：{self.username}）。",
                status_code=502,
            ) from None
        except Exception as exc:
            self.close()
            if _is_timeout_exception(exc):
                raise AppError(
                    ErrorCode.SFTP_TIMEOUT,
                    "SFTP 認證逾時。",
                    status_code=504,
                ) from None
            raise AppError(
                ErrorCode.SFTP_CONNECTION_FAILED,
                f"SFTP 認證過程發生連線錯誤：{exc}",
                status_code=502,
            ) from None

        try:
            self._sftp = paramiko.SFTPClient.from_transport(self._transport)
            if self._sftp is None:
                raise RuntimeError("無法初始化 SFTPClient")
            chan = self._sftp.get_channel()
            if chan is not None:
                chan.settimeout(self.transfer_timeout)
        except Exception as exc:
            self.close()
            if _is_timeout_exception(exc):
                raise AppError(
                    ErrorCode.SFTP_TIMEOUT,
                    "開啟 SFTP 子系統逾時。",
                    status_code=504,
                ) from None
            raise AppError(
                ErrorCode.SFTP_CONNECTION_FAILED,
                f"無法開啟 SFTP 子系統：{exc}",
                status_code=502,
            ) from None

    def is_active(self) -> bool:
        if self._transport is None or not self._transport.is_active():
            return False
        if self._sftp is None:
            return False
        chan = self._sftp.get_channel()
        if chan is not None and chan.closed:
            return False
        return True

    def close(self) -> None:
        if self._sftp is not None:
            try:
                self._sftp.close()
            except Exception:
                pass
            self._sftp = None
        if self._transport is not None:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def upload_single_file(self, local_path: str, remote_path: str) -> SftpFileUploadResult:
        """
        依 SPEC 12.4 逐檔處理流程執行單一檔案檢查與上傳：
        1. 檢查本機來源（不存在 / 是資料夾或非一般檔案 / 無權限 / 讀取失敗）
        2. 檢查遠端父目錄（不存在 -> REMOTE_DIRECTORY_NOT_FOUND，不建立目錄；無權限 -> REMOTE_PERMISSION_DENIED）
        3. 檢查遠端目標路徑型態（若已存在且為資料夾 -> REMOTE_PATH_INVALID）
        4. 開啟遠端目標直接覆蓋，分塊串流寫入並完成關閉。
        """
        # Step 1: 檢查本機來源檔案
        try:
            if not os.path.exists(local_path):
                return SftpFileUploadResult(
                    succeeded=False,
                    error_code=ErrorCode.LOCAL_FILE_NOT_FOUND,
                    error_message="本機來源檔案不存在。",
                )
            if os.path.isdir(local_path) or not os.path.isfile(local_path):
                return SftpFileUploadResult(
                    succeeded=False,
                    error_code=ErrorCode.LOCAL_PATH_NOT_FILE,
                    error_message="本機來源路徑是資料夾或不是一般檔案。",
                )
            file_size = os.path.getsize(local_path)
            local_fp = open(local_path, "rb")
        except PermissionError:
            return SftpFileUploadResult(
                succeeded=False,
                error_code=ErrorCode.LOCAL_FILE_ACCESS_DENIED,
                error_message="無權限讀取本機來源檔案。",
            )
        except OSError as exc:
            return SftpFileUploadResult(
                succeeded=False,
                error_code=ErrorCode.LOCAL_FILE_READ_FAILED,
                error_message=f"讀取本機來源檔案失敗：{exc}",
            )

        with local_fp:
            if not self.is_active() or self._sftp is None:
                return SftpFileUploadResult(
                    succeeded=False,
                    size_bytes=file_size,
                    error_code=ErrorCode.SFTP_CONNECTION_FAILED,
                    error_message="SFTP 連線已中斷。",
                    connection_lost=True,
                )

            sftp = self._sftp
            parent_dir = posixpath.dirname(remote_path) or "/"

            # Step 2: 檢查遠端父目錄
            try:
                parent_stat = sftp.stat(parent_dir)
                if parent_stat.st_mode is not None and not stat.S_ISDIR(parent_stat.st_mode):
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.REMOTE_PATH_INVALID,
                        error_message=f"遠端父路徑不是目錄：{parent_dir}",
                    )
            except FileNotFoundError:
                return SftpFileUploadResult(
                    succeeded=False,
                    size_bytes=file_size,
                    error_code=ErrorCode.REMOTE_DIRECTORY_NOT_FOUND,
                    error_message=f"遠端父目錄不存在（不自動建立目錄）：{parent_dir}",
                )
            except PermissionError:
                return SftpFileUploadResult(
                    succeeded=False,
                    size_bytes=file_size,
                    error_code=ErrorCode.REMOTE_PERMISSION_DENIED,
                    error_message=f"無權存取遠端父目錄：{parent_dir}",
                )
            except OSError as exc:
                err_no = getattr(exc, "errno", None)
                if err_no == errno.ENOENT:
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.REMOTE_DIRECTORY_NOT_FOUND,
                        error_message=f"遠端父目錄不存在（不自動建立目錄）：{parent_dir}",
                    )
                if err_no == errno.EACCES:
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.REMOTE_PERMISSION_DENIED,
                        error_message=f"無權存取遠端父目錄：{parent_dir}",
                    )
                if _is_timeout_exception(exc):
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.SFTP_TIMEOUT,
                        error_message="檢查遠端父目錄時發生逾時。",
                        connection_lost=not self.is_active(),
                    )
                return SftpFileUploadResult(
                    succeeded=False,
                    size_bytes=file_size,
                    error_code=ErrorCode.SFTP_UPLOAD_FAILED,
                    error_message=f"檢查遠端父目錄失敗：{exc}",
                    connection_lost=not self.is_active(),
                )
            except Exception as exc:
                if _is_timeout_exception(exc):
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.SFTP_TIMEOUT,
                        error_message="檢查遠端父目錄時發生逾時。",
                        connection_lost=True,
                    )
                return SftpFileUploadResult(
                    succeeded=False,
                    size_bytes=file_size,
                    error_code=ErrorCode.SFTP_CONNECTION_FAILED,
                    error_message=f"SFTP 連線中斷或異常：{exc}",
                    connection_lost=True,
                )

            # Step 3: 檢查遠端目標路徑是否已存在且為資料夾
            try:
                target_stat = sftp.stat(remote_path)
                if target_stat.st_mode is not None and stat.S_ISDIR(target_stat.st_mode):
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.REMOTE_PATH_INVALID,
                        error_message=f"遠端目標路徑已存在且為資料夾，無法作為檔案覆蓋：{remote_path}",
                    )
            except FileNotFoundError:
                pass
            except PermissionError:
                return SftpFileUploadResult(
                    succeeded=False,
                    size_bytes=file_size,
                    error_code=ErrorCode.REMOTE_PERMISSION_DENIED,
                    error_message=f"無權存取遠端目標路徑：{remote_path}",
                )
            except OSError as exc:
                err_no = getattr(exc, "errno", None)
                if err_no == errno.ENOENT:
                    pass
                elif err_no == errno.EACCES:
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.REMOTE_PERMISSION_DENIED,
                        error_message=f"無權存取遠端目標路徑：{remote_path}",
                    )
                elif _is_timeout_exception(exc):
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.SFTP_TIMEOUT,
                        error_message="檢查遠端目標路徑逾時。",
                        connection_lost=not self.is_active(),
                    )
                else:
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.SFTP_UPLOAD_FAILED,
                        error_message=f"檢查遠端目標路徑失敗：{exc}",
                        connection_lost=not self.is_active(),
                    )
            except Exception as exc:
                return SftpFileUploadResult(
                    succeeded=False,
                    size_bytes=file_size,
                    error_code=ErrorCode.SFTP_CONNECTION_FAILED,
                    error_message=f"SFTP 連線異常：{exc}",
                    connection_lost=True,
                )

            # Step 4: 開啟遠端目標（存在則截斷覆蓋）並以分塊串流傳送
            remote_fp = None
            try:
                remote_fp = sftp.open(remote_path, "wb")
                remote_fp.set_pipelined(False)

                while True:
                    try:
                        chunk = local_fp.read(STREAM_CHUNK_SIZE)
                    except PermissionError:
                        return SftpFileUploadResult(
                            succeeded=False,
                            size_bytes=file_size,
                            error_code=ErrorCode.LOCAL_FILE_ACCESS_DENIED,
                            error_message="讀取本機來源檔案時權限不足。",
                        )
                    except OSError as read_exc:
                        return SftpFileUploadResult(
                            succeeded=False,
                            size_bytes=file_size,
                            error_code=ErrorCode.LOCAL_FILE_READ_FAILED,
                            error_message=f"讀取本機來源檔案內容失敗：{read_exc}",
                        )

                    if not chunk:
                        break

                    remote_fp.write(chunk)

                remote_fp.flush()
                remote_fp.close()
                remote_fp = None
                return SftpFileUploadResult(succeeded=True, size_bytes=file_size)

            except PermissionError:
                return SftpFileUploadResult(
                    succeeded=False,
                    size_bytes=file_size,
                    error_code=ErrorCode.REMOTE_PERMISSION_DENIED,
                    error_message=f"遠端目標檔案無寫入權限：{remote_path}",
                )
            except OSError as exc:
                err_no = getattr(exc, "errno", None)
                if err_no == errno.EACCES:
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.REMOTE_PERMISSION_DENIED,
                        error_message=f"遠端目標檔案無寫入權限：{remote_path}",
                    )
                if err_no == errno.ENOENT:
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.REMOTE_DIRECTORY_NOT_FOUND,
                        error_message=f"遠端目標路徑或父目錄不存在：{remote_path}",
                    )
                if _is_timeout_exception(exc):
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.SFTP_TIMEOUT,
                        error_message="檔案傳輸無進展逾時。",
                        connection_lost=True,
                    )
                return SftpFileUploadResult(
                    succeeded=False,
                    size_bytes=file_size,
                    error_code=ErrorCode.SFTP_UPLOAD_FAILED,
                    error_message=f"SFTP 上傳檔案失敗：{exc}",
                    connection_lost=not self.is_active(),
                )
            except Exception as exc:
                if _is_timeout_exception(exc):
                    return SftpFileUploadResult(
                        succeeded=False,
                        size_bytes=file_size,
                        error_code=ErrorCode.SFTP_TIMEOUT,
                        error_message="檔案傳輸逾時。",
                        connection_lost=True,
                    )
                return SftpFileUploadResult(
                    succeeded=False,
                    size_bytes=file_size,
                    error_code=ErrorCode.SFTP_UPLOAD_FAILED,
                    error_message=f"SFTP 上傳期間發生錯誤：{exc}",
                    connection_lost=True,
                )
            finally:
                if remote_fp is not None:
                    try:
                        remote_fp.close()
                    except Exception:
                        pass


def test_sftp_server_connection(
    host: str,
    port: int,
    username: str,
    password: str,
    connect_timeout: float = CONNECT_TIMEOUT_SEC,
    auth_timeout: float = AUTH_TIMEOUT_SEC,
) -> dict[str, str]:
    """
    測試已保存的 Server 連線資訊 (SPEC 11.1, 15.2, 15.6)：
    驗證能否建立 SFTP 連線及完成認證。
    """
    session = SftpSession(
        host=host,
        port=port,
        username=username,
        password=password,
        connect_timeout=connect_timeout,
        auth_timeout=auth_timeout,
    )
    try:
        session.connect()
        return {
            "status": "OK",
            "message": f"成功連線至 {host}:{port} 並完成 SFTP 認證。",
        }
    finally:
        session.close()
