"""
自動化打包腳本：使用 PyInstaller (--onedir) 建置免安裝 Windows x64 交付包 (SPEC 第 3, 5, 20 章)。
產出位置：release/SftpDeployTool/
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


def build_release() -> None:
    root_dir = Path(__file__).resolve().parent
    dist_dir = root_dir / "dist"
    build_dir = root_dir / "build"
    release_root = root_dir / "release"
    target_pkg_dir = release_root / "SftpDeployTool"

    # 清理舊的建置與交付目錄
    for d in (dist_dir, build_dir, release_root):
        if d.exists():
            shutil.rmtree(d)

    static_src = root_dir / "app" / "static"
    add_data_arg = f"{static_src};app/static"

    excluded_modules = [
        "numpy",
        "pandas",
        "polars",
        "pyarrow",
        "PIL",
        "cv2",
        "pygame",
        "customtkinter",
        "tkinter",
        "yt_dlp",
        "bs4",
        "beautifulsoup4",
        "curl_cffi",
        "pytest",
    ]
    exclude_args: list[str] = []
    for mod in excluded_modules:
        exclude_args.extend(["--exclude-module", mod])

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--console",
        "--name",
        "SftpDeployTool",
        "--contents-directory",
        "_internal",
        "--add-data",
        add_data_arg,
        *exclude_args,
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols",
        "--hidden-import",
        "uvicorn.protocols.http",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.lifespan",
        "--hidden-import",
        "uvicorn.lifespan.on",
        str(root_dir / "main.py"),
    ]

    print("[1/3] 正在執行 PyInstaller 打包 SftpDeployTool.exe ...")
    subprocess.run(cmd, cwd=str(root_dir), check=True)

    print("[2/3] 正在組裝交付目錄 release/SftpDeployTool/ ...")
    release_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(dist_dir / "SftpDeployTool", target_pkg_dir)

    # 建立初始設定檔與 logs/deployments 目錄 (SPEC 5, 20.4)
    (target_pkg_dir / "appsettings.json").write_text(
        json.dumps({"schemaVersion": 1, "httpPort": 18080}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (target_pkg_dir / "servers.json").write_text(
        json.dumps({"schemaVersion": 1, "servers": []}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (target_pkg_dir / "deployments.json").write_text(
        json.dumps({"schemaVersion": 1, "deploymentProfiles": []}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (target_pkg_dir / "logs" / "deployments").mkdir(parents=True, exist_ok=True)

    # 複製操作與風險說明文件 (SPEC 20.5, 20.6)
    shutil.copy2(root_dir / "README.md", target_pkg_dir / "README.md")

    # 同時建立範例設定參考檔 examples/
    examples_dir = target_pkg_dir / "examples"
    examples_dir.mkdir(parents=True, exist_ok=True)
    (examples_dir / "appsettings.example.json").write_text(
        json.dumps({"schemaVersion": 1, "httpPort": 18080}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (examples_dir / "servers.example.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "servers": [
                    {
                        "id": "server-test-01",
                        "name": "測試 Server 01",
                        "host": "192.168.10.50",
                        "port": 22,
                        "username": "deploy",
                        "passwordBase64": "dGVzdC1wYXNzd29yZA==",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (examples_dir / "deployments.example.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "deploymentProfiles": [
                    {
                        "id": "profile-test-app",
                        "name": "Test App 佈署",
                        "serverId": "server-test-01",
                        "files": [
                            {
                                "id": "file-01",
                                "localPath": "C:\\Build\\App.Test.dll",
                                "remotePath": "/opt/test-app/App.dll",
                            },
                            {
                                "id": "file-02",
                                "localPath": "C:\\Build\\appsettings.Test.json",
                                "remotePath": "/opt/test-app/appsettings.json",
                            },
                        ],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print("[3/3] 正在壓縮可攜式交付包 release/SftpDeployTool-win64.zip ...")
    shutil.make_archive(
        str(release_root / "SftpDeployTool-win64"),
        "zip",
        root_dir=str(release_root),
        base_dir="SftpDeployTool",
    )

    # 清理暫存 build 與 dist 目錄，保持工作區整潔
    for d in (dist_dir, build_dir):
        if d.exists():
            shutil.rmtree(d)
    spec_file = root_dir / "SftpDeployTool.spec"
    if spec_file.exists():
        spec_file.unlink()

    print(f"\n打包完成！交付目錄：{target_pkg_dir}")


if __name__ == "__main__":
    build_release()
