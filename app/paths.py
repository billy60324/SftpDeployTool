"""處理 EXE 同層目錄與 PyInstaller 內嵌靜態資源路徑規則 (SPEC 5.1)。"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def get_base_dir() -> Path:
    """
    取得設定檔與 logs/ 所在的基準目錄。
    - 若設定環境變數 SFTP_DEPLOY_BASE_DIR（供自動化測試隔離目錄使用），優先使用。
    - 若為 PyInstaller 打包之 EXE 執行環境 (sys.frozen = True)，使用 sys.executable 所在目錄。
    - 否則使用專案根目錄（main.py 所在目錄）。
    不因目前工作目錄 (os.getcwd()) 或捷徑開始位置而改變設定檔位置。
    """
    env_dir = os.environ.get("SFTP_DEPLOY_BASE_DIR")
    if env_dir:
        return Path(env_dir).resolve()

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent

    return Path(__file__).resolve().parent.parent


def get_static_dir() -> Path:
    """
    取得內嵌前端靜態資源 (HTML/CSS/JS) 所在目錄。
    - 在 PyInstaller 環境下由 sys._MEIPASS 讀取。
    - 在開發環境下由 app/static 讀取。
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        meipass_static = Path(getattr(sys, "_MEIPASS")) / "app" / "static"
        if meipass_static.exists():
            return meipass_static
        exe_static = Path(sys.executable).resolve().parent / "app" / "static"
        if exe_static.exists():
            return exe_static

    return Path(__file__).resolve().parent / "static"
