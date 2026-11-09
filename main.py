"""Windows 本機 SFTP 檔案上傳工具 (SftpDeployTool) 啟動入口點 (SPEC 7.2, 10.1 ~ 10.4)。"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

import uvicorn

from app.api import create_app
from app.errors import AppError, ErrorCode
from app.job_manager import JobManager
from app.paths import get_base_dir
from app.storage import ConfigStore


class SingleInstanceGuard:
    """
    確保同一工具目錄下同時僅有一個實例執行 (SPEC 10.1 步驟 1)：
    結合 Windows Named Mutex (Local\\SftpDeployTool_<hash>)。
    """

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, base_dir: Path) -> None:
        norm = str(base_dir.resolve()).lower()
        digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]
        self.mutex_name = f"Local\\SftpDeployTool_{digest}"
        self._handle: Optional[int] = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, self.mutex_name)
        last_err = kernel32.GetLastError()
        if not handle:
            return False
        if last_err == self.ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if os.name == "nt" and self._handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None


def check_port_available(host: str, port: int) -> None:
    """
    檢查 127.0.0.1:<httpPort> 是否可用 (SPEC 7.2, AC-05)：
    若 Port 已被其他服務占用，明確拋出 HTTP_PORT_IN_USE 並停止啟動，不自動改用其他 Port。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        # 先嘗試連線檢測是否已有服務正在監聽該 Port
        probe.settimeout(0.5)
        if probe.connect_ex((host, port)) == 0:
            raise AppError(
                ErrorCode.HTTP_PORT_IN_USE,
                f"本機 HTTP Port ({host}:{port}) 已被其他程式占用，請修改 appsettings.json 的 httpPort 後重新啟動。",
                status_code=500,
            )

    # 再嘗試實際 bind
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bind_sock:
        try:
            bind_sock.bind((host, port))
        except OSError as exc:
            raise AppError(
                ErrorCode.HTTP_PORT_IN_USE,
                f"無法綁定本機 HTTP Port ({host}:{port})：{exc}",
                status_code=500,
            ) from None


def show_startup_error(title: str, message: str, show_messagebox: bool = True) -> None:
    """於 Console 輸出錯誤訊息，必要時顯示 Windows 原生對話框，防止雙擊 EXE 閃退看不到錯誤 (SPEC 10.2)。"""
    sys.stderr.write(f"\n[啟動失敗] {title}\n{message}\n\n")
    sys.stderr.flush()
    if show_messagebox and os.name == "nt":
        try:
            MB_ICONERROR = 0x00000010
            ctypes.windll.user32.MessageBoxW(0, message, title, MB_ICONERROR)
        except Exception:
            pass


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Windows 本機 SFTP 檔案上傳工具")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="啟動時不自動開啟預設瀏覽器",
    )
    parser.add_argument(
        "--no-messagebox",
        action="store_true",
        help="啟動失敗時不彈出 Windows MessageBox（供自動化測試使用）",
    )
    args = parser.parse_args(argv)

    base_dir = get_base_dir()
    show_mb = not args.no_messagebox and os.environ.get("SFTP_DEPLOY_NO_MESSAGEBOX") != "1"

    # Step 1: 確認同一工具目錄沒有其他實例執行
    guard = SingleInstanceGuard(base_dir)
    if not guard.acquire():
        show_startup_error(
            "SFTP 檔案上傳工具已在執行中",
            f"同一目錄下已有另一個 SftpDeployTool 實例正在執行：\n{base_dir}",
            show_messagebox=show_mb,
        )
        return 1

    try:
        # Step 2 ~ 5: 確認目錄可讀寫、建立缺少的設定檔、驗證既有格式並載入
        store = ConfigStore(base_dir)
        store.initialize_and_load()

        # Step 6: 處理上一輪未正常結束的工作紀錄
        jobs = JobManager(store)
        recovered = jobs.recover_interrupted_jobs_on_startup()
        if recovered > 0:
            print(f"[啟動檢查] 已將 {recovered} 筆上次未正常結束的工作標記為 INTERRUPTED。")

        # Step 7: 檢查 HTTP Port 並啟動 127.0.0.1 HTTP 服務
        http_port = store.get_http_port()
        host = "127.0.0.1"
        check_port_available(host, http_port)

        server_ref: dict[str, Optional[uvicorn.Server]] = {"server": None}

        def request_shutdown() -> None:
            srv = server_ref.get("server")
            if srv is not None:
                srv.should_exit = True

        fastapi_app = create_app(
            base_dir=base_dir,
            config_store=store,
            job_manager=jobs,
            shutdown_callback=request_shutdown,
        )

        config = uvicorn.Config(
            app=fastapi_app,
            host=host,
            port=http_port,
            log_level="warning",
            access_log=False,
        )
        uv_server = uvicorn.Server(config)
        server_ref["server"] = uv_server

        url = f"http://{host}:{http_port}"
        print("============================================================")
        print("  Windows 本機 SFTP 檔案上傳工具 已啟動")
        print(f"  設定檔目錄 : {base_dir}")
        print(f"  操作介面   : {url}")
        print("  關閉方式   : 請於網頁右上角點擊「退出工具」或按 Ctrl+C")
        print("============================================================")

        # Step 8: 開啟預設瀏覽器
        if not args.no_browser and os.environ.get("SFTP_DEPLOY_NO_BROWSER") != "1":
            def _open_browser_when_ready() -> None:
                for _ in range(30):
                    if getattr(uv_server, "started", False):
                        webbrowser.open(url)
                        return
                    time.sleep(0.1)

            threading.Thread(target=_open_browser_when_ready, daemon=True).start()

        uv_server.run()
        print("\n[系統訊息] SFTP 檔案上傳工具已安全退出。")
        return 0

    except AppError as app_err:
        show_startup_error(
            f"SFTP 檔案上傳工具啟動失敗 ({app_err.code})",
            app_err.message,
            show_messagebox=show_mb,
        )
        return 2
    except Exception as exc:
        show_startup_error(
            "SFTP 檔案上傳工具啟動異常",
            f"啟動過程發生非預期錯誤：{exc}",
            show_messagebox=show_mb,
        )
        return 3
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
