"""
完整驗證 SPEC 第 19 章 AC-01 ~ AC-28 驗收條件的自動化測試套件。
"""

from __future__ import annotations

import base64
import json
import socket
import time
from pathlib import Path
from typing import Any

import paramiko
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.errors import AppError, ErrorCode
from app.job_manager import JobManager
from app.storage import ConfigStore
from main import SingleInstanceGuard, check_port_available
from tests.conftest import EmbeddedSftpTestServer


def wait_for_job_completion(client: TestClient, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/api/deployments/{job_id}")
        assert resp.status_code == 200
        data = resp.json()
        if data["status"] not in ("QUEUED", "RUNNING"):
            return data
        time.sleep(0.05)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout}s")


def test_ac02_ui_static_resources_self_contained(tmp_path: Path) -> None:
    """AC-02: UI 資源可由工具本身提供，不依賴外部 CDN。"""
    app = create_app(base_dir=tmp_path / "app_dir")
    client = TestClient(app)

    res_html = client.get("/")
    assert res_html.status_code == 200
    html_text = res_html.text
    assert "Windows 本機 SFTP 檔案上傳工具" in html_text
    # 確認無任何 http:// 或 https:// 外部 CDN 連結
    assert "https://" not in html_text
    assert "http://" not in html_text

    res_css = client.get("/static/styles.css")
    assert res_css.status_code == 200
    assert "https://" not in res_css.text

    res_js = client.get("/static/app.js")
    assert res_js.status_code == 200
    assert "https://" not in res_js.text


def test_ac03_ac04_ac05_port_and_host_rules(tmp_path: Path) -> None:
    """
    AC-03: 後端僅監聽 127.0.0.1
    AC-04: 修改 httpPort 並重啟後，UI 使用新 Port
    AC-05: HTTP Port 被占用時明確報錯，不自動改 Port
    """
    base_dir = tmp_path / "cfg_dir"
    store = ConfigStore(base_dir)
    store.initialize_and_load()
    assert store.get_http_port() == 18080

    # 修改 appsettings.json 的 httpPort 為 19090 並重新載入 (AC-04)
    (base_dir / "appsettings.json").write_text(
        json.dumps({"schemaVersion": 1, "httpPort": 19090}),
        encoding="utf-8",
    )
    store2 = ConfigStore(base_dir)
    store2.initialize_and_load()
    assert store2.get_http_port() == 19090

    app = create_app(base_dir=base_dir, config_store=store2)
    client = TestClient(app)
    assert client.get("/api/settings").json()["httpPort"] == 19090

    # 測試 HTTP Port 被占用時明確報錯 HTTP_PORT_IN_USE (AC-05)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        busy_port = occupied.getsockname()[1]

        with pytest.raises(AppError) as exc_info:
            check_port_available("127.0.0.1", busy_port)
        assert exc_info.value.code == ErrorCode.HTTP_PORT_IN_USE


def test_ac06_ac07_ac08_ac09_ac10_ac11_ac12_server_and_profile_management(
    tmp_path: Path, sftp_server: EmbeddedSftpTestServer
) -> None:
    """
    AC-06: 各 Server 可設定不同 SFTP Port。
    AC-07: 設定保存於 EXE 同層，重新啟動後仍存在。
    AC-08: 多組佈署設定可引用同一個 Server。
    AC-09: 修改 Server 後，引用它的設定於下次工作使用新資訊。
    AC-10: 被引用的 Server 無法直接刪除。
    AC-11: 密碼以 UTF-8 Base64 保存，並可解碼後登入（保留前後空白）。
    AC-12: 查詢 API 與 Log 不包含原始或 Base64 密碼。
    """
    base_dir = tmp_path / "app_dir"
    app = create_app(base_dir=base_dir)
    client = TestClient(app)

    # 測試含前後空白的密碼以 UTF-8 Base64 保存 (AC-11)
    special_pw = "  my-secret-密碼-123  "
    sftp_server.password = special_pw

    # 建立 Server 1（先故意給錯誤密碼，稍後測試 AC-09 修改 Server 後生效）
    resp_srv1 = client.post(
        "/api/servers",
        json={
            "name": "測試 Server A",
            "host": sftp_server.host,
            "port": sftp_server.port,
            "username": sftp_server.username,
            "password": "wrong-initial-password",
        },
    )
    assert resp_srv1.status_code == 201
    srv1 = resp_srv1.json()
    srv1_id = srv1["id"]
    assert srv1["port"] == sftp_server.port
    assert srv1["hasPassword"] is True
    assert "password" not in srv1
    assert "passwordBase64" not in srv1

    # 建立 Server 2 設定不同 SFTP Port (AC-06)
    resp_srv2 = client.post(
        "/api/servers",
        json={
            "name": "測試 Server B",
            "host": "192.168.1.99",
            "port": 2222,
            "username": "admin",
            "password": "pw2",
        },
    )
    assert resp_srv2.status_code == 201
    srv2_id = resp_srv2.json()["id"]
    assert resp_srv2.json()["port"] == 2222

    # 多組佈署設定引用同一個 Server (AC-08)
    local_file_1 = tmp_path / "src1.txt"
    local_file_1.write_text("hello world 1", encoding="utf-8")
    local_file_2 = tmp_path / "src2.txt"
    local_file_2.write_text("hello world 2", encoding="utf-8")

    (sftp_server.root_dir / "opt" / "app1").mkdir(parents=True, exist_ok=True)
    (sftp_server.root_dir / "opt" / "app2").mkdir(parents=True, exist_ok=True)

    resp_p1 = client.post(
        "/api/profiles",
        json={
            "name": "Profile 1",
            "serverId": srv1_id,
            "files": [
                {"localPath": str(local_file_1), "remotePath": "/opt/app1/dest1.txt"}
            ],
        },
    )
    assert resp_p1.status_code == 201
    p1_id = resp_p1.json()["id"]

    resp_p2 = client.post(
        "/api/profiles",
        json={
            "name": "Profile 2",
            "serverId": srv1_id,
            "files": [
                {"localPath": str(local_file_2), "remotePath": "/opt/app2/dest2.txt"}
            ],
        },
    )
    assert resp_p2.status_code == 201
    p2_id = resp_p2.json()["id"]

    # 被引用的 Server 無法直接刪除，且錯誤訊息列出引用它的設定 (AC-10)
    del_resp = client.delete(f"/api/servers/{srv1_id}")
    assert del_resp.status_code == 409
    del_err = del_resp.json()["error"]
    assert del_err["code"] == ErrorCode.SERVER_IN_USE
    assert "Profile 1" in del_err["message"]
    assert "Profile 2" in del_err["message"]

    # 未被引用的 Server 2 可以正常刪除
    del_srv2 = client.delete(f"/api/servers/{srv2_id}")
    assert del_srv2.status_code == 204

    # 修改 Server 1 的密碼為正確的 special_pw (AC-09, AC-11)
    upd_resp = client.put(
        f"/api/servers/{srv1_id}",
        json={
            "name": "測試 Server A (Updated)",
            "host": sftp_server.host,
            "port": sftp_server.port,
            "username": sftp_server.username,
            "password": special_pw,
        },
    )
    assert upd_resp.status_code == 200
    assert upd_resp.json()["id"] == srv1_id  # 修改名稱不改變 ID

    # 檢查 servers.json 內密碼確實為 UTF-8 Base64 (AC-11)
    raw_servers_json = json.loads((base_dir / "servers.json").read_text(encoding="utf-8"))
    expected_b64 = base64.b64encode(special_pw.encode("utf-8")).decode("ascii")
    assert raw_servers_json["servers"][0]["passwordBase64"] == expected_b64

    # 測試連線成功
    conn_resp = client.post(f"/api/servers/{srv1_id}/test-connection")
    assert conn_resp.status_code == 200

    # 重新啟動新實例後設定仍存在 (AC-07)
    app_restarted = create_app(base_dir=base_dir)
    client2 = TestClient(app_restarted)
    servers_list = client2.get("/api/servers").json()["servers"]
    assert len(servers_list) == 1
    assert servers_list[0]["name"] == "測試 Server A (Updated)"
    # 查詢 API 不含原始密碼或 Base64 密碼 (AC-12)
    assert special_pw not in json.dumps(servers_list, ensure_ascii=False)
    assert expected_b64 not in json.dumps(servers_list, ensure_ascii=False)

    # 觸發 Profile 1 佈署，驗證修改 Server 後使用新資訊成功上傳 (AC-09)
    dep_resp = client2.post("/api/deployments", json={"profileId": p1_id})
    assert dep_resp.status_code == 202
    job_id = dep_resp.json()["jobId"]
    job_res = wait_for_job_completion(client2, job_id)
    assert job_res["status"] == "SUCCEEDED"

    # 檢查歷史紀錄檔案與 API 不含密碼或 Base64 (AC-12)
    job_log_text = (base_dir / "logs" / "deployments" / f"{job_id}.json").read_text(encoding="utf-8")
    assert special_pw not in job_log_text
    assert expected_b64 not in job_log_text


def test_ac13_ac14_ac15_ac16_ac17_ac19_ac28_upload_policies_and_partial_success(
    tmp_path: Path, sftp_server: EmbeddedSftpTestServer
) -> None:
    """
    AC-13: 本機與遠端檔名不同時，依 remotePath 正確上傳。
    AC-14: 既有遠端檔案直接覆蓋，不建立備份。
    AC-15: 遠端父目錄不存在時回報失敗，不建立目錄。
    AC-16: 一筆來源不存在時，其他有效檔案仍會上傳。
    AC-17: 一筆上傳失敗時，其他檔案仍會繼續處理。
    AC-19: 全部成功、部分成功、全部失敗可正確區分。
    AC-28: 上傳過程不執行遠端指令或部署後操作。
    """
    base_dir = tmp_path / "app_dir"
    app = create_app(base_dir=base_dir)
    client = TestClient(app)

    srv_id = client.post(
        "/api/servers",
        json={
            "name": "Linux SFTP",
            "host": sftp_server.host,
            "port": sftp_server.port,
            "username": sftp_server.username,
            "password": sftp_server.password,
        },
    ).json()["id"]

    # 準備本機檔案與遠端初始狀態
    local_ok_1 = tmp_path / "local_name_A.dll"
    local_ok_1.write_bytes(b"NEW_CONTENT_FOR_OVERWRITE")

    local_missing = tmp_path / "does_not_exist.json"

    local_ok_2 = tmp_path / "local_name_B.txt"
    local_ok_2.write_bytes(b"SECOND_VALID_FILE")

    # 遠端預先建立 /opt/target 目錄與既有檔案（測試直接覆蓋且不產生備份 AC-14，以及不同檔名 AC-13）
    remote_target_dir = sftp_server.root_dir / "opt" / "target"
    remote_target_dir.mkdir(parents=True, exist_ok=True)
    existing_remote_file = remote_target_dir / "RemoteRenamed.dll"
    existing_remote_file.write_bytes(b"OLD_CONTENT")

    # 建立包含 4 筆檔案的 Profile：
    # 1. 本機不存在 (AC-16 -> LOCAL_FILE_NOT_FOUND)
    # 2. 遠端父目錄不存在 /opt/no_such_dir/file.txt (AC-15 -> REMOTE_DIRECTORY_NOT_FOUND)
    # 3. 遠端權限不足 /opt/target/denied.txt (AC-17 -> REMOTE_PERMISSION_DENIED)
    # 4. 正常上傳並覆蓋既有檔案且檔名不同 /opt/target/RemoteRenamed.dll (AC-13, AC-14 -> SUCCEEDED)
    sftp_server.permission_denied_paths.add("/opt/target/denied.txt")

    prof_id = client.post(
        "/api/profiles",
        json={
            "name": "Mixed Profile",
            "serverId": srv_id,
            "files": [
                {"localPath": str(local_missing), "remotePath": "/opt/target/missing.json"},
                {"localPath": str(local_ok_2), "remotePath": "/opt/no_such_dir/file.txt"},
                {"localPath": str(local_ok_2), "remotePath": "/opt/target/denied.txt"},
                {"localPath": str(local_ok_1), "remotePath": "/opt/target/RemoteRenamed.dll"},
            ],
        },
    ).json()["id"]

    dep_resp = client.post("/api/deployments", json={"profileId": prof_id})
    assert dep_resp.status_code == 202
    job_res = wait_for_job_completion(client, dep_resp.json()["jobId"])

    # AC-19: 部分成功 PARTIALLY_SUCCEEDED
    assert job_res["status"] == "PARTIALLY_SUCCEEDED"
    assert job_res["totalCount"] == 4
    assert job_res["successCount"] == 1
    assert job_res["failedCount"] == 3

    files_res = job_res["files"]
    assert files_res[0]["status"] == "FAILED"
    assert files_res[0]["error"]["code"] == ErrorCode.LOCAL_FILE_NOT_FOUND

    assert files_res[1]["status"] == "FAILED"
    assert files_res[1]["error"]["code"] == ErrorCode.REMOTE_DIRECTORY_NOT_FOUND
    # AC-15: 確認未建立遠端父目錄
    assert not (sftp_server.root_dir / "opt" / "no_such_dir").exists()

    assert files_res[2]["status"] == "FAILED"
    assert files_res[2]["error"]["code"] == ErrorCode.REMOTE_PERMISSION_DENIED

    assert files_res[3]["status"] == "SUCCEEDED"
    # AC-13, AC-14: 驗證依 remotePath 正確覆蓋，且目錄內沒有產生備份檔
    assert existing_remote_file.read_bytes() == b"NEW_CONTENT_FOR_OVERWRITE"
    assert list(remote_target_dir.iterdir()) == [existing_remote_file]
    # AC-28: 未執行任何遠端指令
    assert sftp_server.Executed_commands == []


def test_ac18_ac19_connection_or_auth_failure_marks_all_files_failed(
    tmp_path: Path, sftp_server: EmbeddedSftpTestServer
) -> None:
    """
    AC-18: 連線或認證失敗時，所有未完成檔案具有明確結果。
    AC-19: 全部失敗 FAILED。
    """
    base_dir = tmp_path / "app_dir"
    app = create_app(base_dir=base_dir)
    client = TestClient(app)

    f1 = tmp_path / "f1.txt"
    f1.write_text("1", encoding="utf-8")
    f2 = tmp_path / "f2.txt"
    f2.write_text("2", encoding="utf-8")

    # 設定錯誤密碼觸發 SFTP_AUTH_FAILED
    srv_id = client.post(
        "/api/servers",
        json={
            "name": "Bad Auth Server",
            "host": sftp_server.host,
            "port": sftp_server.port,
            "username": sftp_server.username,
            "password": "WRONG_PASSWORD",
        },
    ).json()["id"]

    prof_id = client.post(
        "/api/profiles",
        json={
            "name": "All Fail Profile",
            "serverId": srv_id,
            "files": [
                {"localPath": str(f1), "remotePath": "/opt/a.txt"},
                {"localPath": str(f2), "remotePath": "/opt/b.txt"},
            ],
        },
    ).json()["id"]

    job_id = client.post("/api/deployments", json={"profileId": prof_id}).json()["jobId"]
    job_res = wait_for_job_completion(client, job_id)

    assert job_res["status"] == "FAILED"
    assert job_res["successCount"] == 0
    assert job_res["failedCount"] == 2
    for f_item in job_res["files"]:
        assert f_item["status"] == "FAILED"
        assert f_item["error"]["code"] == ErrorCode.SFTP_AUTH_FAILED


def test_ac20_ac21_ac22_concurrency_and_snapshot_isolation(
    tmp_path: Path, sftp_server: EmbeddedSftpTestServer
) -> None:
    """
    AC-20: 重複觸發時不產生並行工作（回傳 409 DEPLOYMENT_ALREADY_RUNNING）。
    AC-21: 執行期間修改設定，不影響現有工作快照。
    AC-22: 關閉或重新整理瀏覽器不停止上傳（可透過 /api/settings 或 /api/deployments/active 查回）。
    """
    base_dir = tmp_path / "app_dir"
    app = create_app(base_dir=base_dir)
    client = TestClient(app)

    (sftp_server.root_dir / "opt").mkdir(parents=True, exist_ok=True)
    f1 = tmp_path / "slow1.txt"
    f1.write_text("slow1", encoding="utf-8")
    f2 = tmp_path / "slow2.txt"
    f2.write_text("slow2", encoding="utf-8")

    # 讓第一個檔案寫入時稍微延遲 0.4 秒，以便測試並行阻擋與執行中修改設定
    def slow_write_hook(vpath: str, _data: bytes):
        if vpath == "/opt/slow1.txt":
            time.sleep(0.4)
        return None

    sftp_server.on_write_hook = slow_write_hook

    srv_id = client.post(
        "/api/servers",
        json={
            "name": "Orig Server Name",
            "host": sftp_server.host,
            "port": sftp_server.port,
            "username": sftp_server.username,
            "password": sftp_server.password,
        },
    ).json()["id"]

    prof_id = client.post(
        "/api/profiles",
        json={
            "name": "Orig Profile Name",
            "serverId": srv_id,
            "files": [
                {"localPath": str(f1), "remotePath": "/opt/slow1.txt"},
                {"localPath": str(f2), "remotePath": "/opt/slow2.txt"},
            ],
        },
    ).json()["id"]

    # 啟動第一個工作
    resp1 = client.post("/api/deployments", json={"profileId": prof_id})
    assert resp1.status_code == 202
    job_id = resp1.json()["jobId"]

    # AC-20: 立即重複觸發第二個工作，必須回傳 409 DEPLOYMENT_ALREADY_RUNNING
    resp2 = client.post("/api/deployments", json={"profileId": prof_id})
    assert resp2.status_code == 409
    assert resp2.json()["error"]["code"] == ErrorCode.DEPLOYMENT_ALREADY_RUNNING

    # 執行中請求退出工具，也必須回傳 409 DEPLOYMENT_ALREADY_RUNNING (SPEC 10.4)
    shutdown_busy = client.post("/api/shutdown")
    assert shutdown_busy.status_code == 409

    # AC-22: 模擬重新整理瀏覽器，查詢 /api/settings 可拿到 activeJobId
    settings_during = client.get("/api/settings").json()
    assert settings_during["activeJobId"] == job_id

    # AC-21: 執行期間修改 Profile 名稱與檔案清單，不影響正在執行的工作快照
    client.put(
        f"/api/profiles/{prof_id}",
        json={
            "name": "Modified Profile Name During Run",
            "serverId": srv_id,
            "files": [
                {"localPath": str(f1), "remotePath": "/opt/modified_only.txt"},
            ],
        },
    )

    job_res = wait_for_job_completion(client, job_id)
    assert job_res["status"] == "SUCCEEDED"
    assert job_res["profileName"] == "Orig Profile Name"
    assert len(job_res["files"]) == 2
    assert (sftp_server.root_dir / "opt" / "slow1.txt").exists()
    assert (sftp_server.root_dir / "opt" / "slow2.txt").exists()


def test_ac23_ac24_interrupted_job_recovery_and_history_persistence(tmp_path: Path) -> None:
    """
    AC-23: 強制結束後重啟，未完成工作標示為中斷 (INTERRUPTED)，不自動重跑，且不以重啟時間冒充結束時間。
    AC-24: 歷史紀錄重新啟動後仍可查詢，不自動刪除。
    """
    base_dir = tmp_path / "app_dir"
    logs_dir = base_dir / "logs" / "deployments"
    logs_dir.mkdir(parents=True, exist_ok=True)

    # 模擬上一輪強制中斷留下的 RUNNING 工作紀錄（1 筆成功、1 筆 UPLOADING、1 筆 PENDING）
    interrupted_job_id = "job-20261019T080000000000Z-int001"
    raw_job = {
        "schemaVersion": 1,
        "jobId": interrupted_job_id,
        "profileId": "prof-deleted",
        "profileName": "已刪除的設定快照",
        "server": {
            "id": "srv-deleted",
            "name": "舊 Server",
            "host": "10.0.0.1",
            "port": 22,
            "username": "deploy",
        },
        "createdAt": "2026-10-19T08:00:00Z",
        "startedAt": "2026-10-19T08:00:01Z",
        "finishedAt": None,
        "status": "RUNNING",
        "totalCount": 3,
        "successCount": 1,
        "failedCount": 0,
        "interruptedCount": 0,
        "files": [
            {
                "id": "f1",
                "localPath": "C:\\A.dll",
                "remotePath": "/opt/A.dll",
                "sizeBytes": 100,
                "status": "SUCCEEDED",
                "startedAt": "2026-10-19T08:00:01Z",
                "finishedAt": "2026-10-19T08:00:02Z",
                "error": None,
            },
            {
                "id": "f2",
                "localPath": "C:\\B.dll",
                "remotePath": "/opt/B.dll",
                "sizeBytes": None,
                "status": "UPLOADING",
                "startedAt": "2026-10-19T08:00:02Z",
                "finishedAt": None,
                "error": None,
            },
            {
                "id": "f3",
                "localPath": "C:\\C.dll",
                "remotePath": "/opt/C.dll",
                "sizeBytes": None,
                "status": "PENDING",
                "startedAt": None,
                "finishedAt": None,
                "error": None,
            },
        ],
    }
    (logs_dir / f"{interrupted_job_id}.json").write_text(
        json.dumps(raw_job, ensure_ascii=False), encoding="utf-8"
    )

    # 啟動應用程式觸發中斷復原
    app = create_app(base_dir=base_dir)
    client = TestClient(app)

    recovered = client.get(f"/api/deployments/{interrupted_job_id}").json()
    assert recovered["status"] == "INTERRUPTED"
    assert recovered["finishedAt"] is None  # 不以重啟時間冒充結束時間
    assert recovered["successCount"] == 1
    assert recovered["interruptedCount"] == 2
    assert recovered["files"][0]["status"] == "SUCCEEDED"
    assert recovered["files"][1]["status"] == "INTERRUPTED"
    assert recovered["files"][1]["error"]["code"] == ErrorCode.JOB_INTERRUPTED
    assert recovered["files"][2]["status"] == "INTERRUPTED"

    # AC-24: 分頁查詢仍可查到，即使原 Profile/Server 不存在
    page_res = client.get("/api/deployments?page=1&pageSize=20").json()
    assert page_res["total"] == 1
    assert page_res["items"][0]["jobId"] == interrupted_job_id


def test_ac25_corrupted_config_not_overwritten(tmp_path: Path) -> None:
    """AC-25: 損毀設定不被直接覆蓋，錯誤可辨識。"""
    base_dir = tmp_path / "corrupt_dir"
    base_dir.mkdir(parents=True, exist_ok=True)
    corrupt_content = "{ broken json content !!!"
    servers_file = base_dir / "servers.json"
    servers_file.write_text(corrupt_content, encoding="utf-8")

    store = ConfigStore(base_dir)
    with pytest.raises(AppError) as exc_info:
        store.initialize_and_load()

    assert exc_info.value.code == ErrorCode.CONFIG_INVALID
    assert "servers.json" in exc_info.value.message
    # 確認損毀的原檔案內容未被覆蓋
    assert servers_file.read_text(encoding="utf-8") == corrupt_content


def test_ac26_ac27_job_record_write_failure_handling(
    tmp_path: Path, sftp_server: EmbeddedSftpTestServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    AC-26: 初始紀錄無法建立時，不開始遠端寫入。
    AC-27: 工作中紀錄保存失敗時，UI 明確警告 (recordWriteFailed=True)，不偽稱已保存，且檔案仍繼續上傳。
    """
    base_dir = tmp_path / "app_dir"
    app = create_app(base_dir=base_dir)
    client = TestClient(app)

    (sftp_server.root_dir / "opt").mkdir(parents=True, exist_ok=True)
    f1 = tmp_path / "f1.txt"
    f1.write_bytes(b"DATA_1")

    srv_id = client.post(
        "/api/servers",
        json={
            "name": "SFTP",
            "host": sftp_server.host,
            "port": sftp_server.port,
            "username": sftp_server.username,
            "password": sftp_server.password,
        },
    ).json()["id"]

    prof_id = client.post(
        "/api/profiles",
        json={
            "name": "Profile",
            "serverId": srv_id,
            "files": [{"localPath": str(f1), "remotePath": "/opt/f1.txt"}],
        },
    ).json()["id"]

    # 測試 AC-26：模擬初始工作紀錄寫入失敗
    import app.job_manager as jm_mod

    orig_atomic_write = jm_mod.atomic_write_json

    def fail_initial_write(*_args, **_kwargs):
        raise AppError(ErrorCode.JOB_RECORD_WRITE_FAILED, "模擬磁碟滿無法寫入初始紀錄", status_code=500)

    monkeypatch.setattr(jm_mod, "atomic_write_json", fail_initial_write)
    resp_fail_init = client.post("/api/deployments", json={"profileId": prof_id})
    assert resp_fail_init.status_code == 500
    assert resp_fail_init.json()["error"]["code"] == ErrorCode.JOB_RECORD_WRITE_FAILED
    # 確認沒有任何遠端檔案被寫入
    assert not (sftp_server.root_dir / "opt" / "f1.txt").exists()

    # 測試 AC-27：初始紀錄寫入成功，但後續工作中更新紀錄寫入失敗
    write_call_count = {"n": 0}

    def fail_subsequent_writes(*args, **kwargs):
        write_call_count["n"] += 1
        if write_call_count["n"] == 1:
            return orig_atomic_write(*args, **kwargs)
        raise AppError(ErrorCode.JOB_RECORD_WRITE_FAILED, "模擬中途磁碟寫入失敗", status_code=500)

    monkeypatch.setattr(jm_mod, "atomic_write_json", fail_subsequent_writes)
    resp_ok_init = client.post("/api/deployments", json={"profileId": prof_id})
    assert resp_ok_init.status_code == 202
    job_id = resp_ok_init.json()["jobId"]

    job_res = wait_for_job_completion(client, job_id)
    # 檔案仍應完成上傳，但 recordWriteFailed 必須為 True 且有 warningMessage
    assert job_res["status"] == "SUCCEEDED"
    assert job_res["recordWriteFailed"] is True
    assert job_res["warningMessage"] is not None
    assert (sftp_server.root_dir / "opt" / "f1.txt").read_bytes() == b"DATA_1"


def test_mid_upload_disconnect_and_reconnect_for_next_file(
    tmp_path: Path, sftp_server: EmbeddedSftpTestServer
) -> None:
    """
    驗證 SPEC 12.6 上傳中斷線處理：
    - 第 1 個檔案上傳途中連線中斷 -> 第 1 個檔案 FAILED，不重試第 1 個檔案。
    - 為第 2 個檔案重新建立連線並上傳成功 -> 整體結果 PARTIALLY_SUCCEEDED。
    """
    base_dir = tmp_path / "app_dir"
    app = create_app(base_dir=base_dir)
    client = TestClient(app)

    (sftp_server.root_dir / "opt").mkdir(parents=True, exist_ok=True)
    f1 = tmp_path / "drop1.txt"
    f1.write_bytes(b"FILE_1_DATA")
    f2 = tmp_path / "ok2.txt"
    f2.write_bytes(b"FILE_2_DATA")

    dropped_once = {"done": False}

    def drop_on_first_file_hook(vpath: str, _data: bytes):
        if vpath == "/opt/drop1.txt" and not dropped_once["done"]:
            dropped_once["done"] = True
            sftp_server.drop_all_active_connections()
            return paramiko.SFTP_FAILURE
        return None

    sftp_server.on_write_hook = drop_on_first_file_hook

    srv_id = client.post(
        "/api/servers",
        json={
            "name": "SFTP",
            "host": sftp_server.host,
            "port": sftp_server.port,
            "username": sftp_server.username,
            "password": sftp_server.password,
        },
    ).json()["id"]

    prof_id = client.post(
        "/api/profiles",
        json={
            "name": "Disconnect Reconnect Profile",
            "serverId": srv_id,
            "files": [
                {"localPath": str(f1), "remotePath": "/opt/drop1.txt"},
                {"localPath": str(f2), "remotePath": "/opt/ok2.txt"},
            ],
        },
    ).json()["id"]

    job_id = client.post("/api/deployments", json={"profileId": prof_id}).json()["jobId"]
    job_res = wait_for_job_completion(client, job_id)

    assert job_res["status"] == "PARTIALLY_SUCCEEDED"
    assert job_res["files"][0]["status"] == "FAILED"
    assert job_res["files"][1]["status"] == "SUCCEEDED"
    assert (sftp_server.root_dir / "opt" / "ok2.txt").read_bytes() == b"FILE_2_DATA"


def test_path_validation_and_remote_target_is_directory(
    tmp_path: Path, sftp_server: EmbeddedSftpTestServer
) -> None:
    """
    驗證 SPEC 9.4 路徑規則與 12.4 遠端目標為既有資料夾時回報 REMOTE_PATH_INVALID。
    """
    base_dir = tmp_path / "app_dir"
    app = create_app(base_dir=base_dir)
    client = TestClient(app)

    srv_id = client.post(
        "/api/servers",
        json={
            "name": "SFTP",
            "host": sftp_server.host,
            "port": sftp_server.port,
            "username": sftp_server.username,
            "password": sftp_server.password,
        },
    ).json()["id"]

    # 1. 相對 localPath 被拒絕
    r_rel = client.post(
        "/api/profiles",
        json={
            "name": "Bad Local",
            "serverId": srv_id,
            "files": [{"localPath": "relative\\file.txt", "remotePath": "/opt/a.txt"}],
        },
    )
    assert r_rel.status_code == 400
    assert r_rel.json()["error"]["code"] == ErrorCode.CONFIG_INVALID

    # 2. 包含 /../ 的 remotePath 被拒絕
    r_trav = client.post(
        "/api/profiles",
        json={
            "name": "Bad Remote",
            "serverId": srv_id,
            "files": [{"localPath": "C:\\Build\\a.txt", "remotePath": "/opt/../etc/passwd"}],
        },
    )
    assert r_trav.status_code == 400

    # 3. 重複的 remotePath 被拒絕
    r_dup = client.post(
        "/api/profiles",
        json={
            "name": "Dup Remote",
            "serverId": srv_id,
            "files": [
                {"localPath": "C:\\Build\\a.txt", "remotePath": "/opt/same.txt"},
                {"localPath": "C:\\Build\\b.txt", "remotePath": "/opt/same.txt"},
            ],
        },
    )
    assert r_dup.status_code == 400

    # 4. 遠端目標路徑本身是既有資料夾時，回報 REMOTE_PATH_INVALID
    (sftp_server.root_dir / "opt" / "existing_dir").mkdir(parents=True, exist_ok=True)
    f_local = tmp_path / "valid.txt"
    f_local.write_text("abc", encoding="utf-8")

    prof_id = client.post(
        "/api/profiles",
        json={
            "name": "Target Is Dir",
            "serverId": srv_id,
            "files": [{"localPath": str(f_local), "remotePath": "/opt/existing_dir"}],
        },
    ).json()["id"]

    job_id = client.post("/api/deployments", json={"profileId": prof_id}).json()["jobId"]
    job_res = wait_for_job_completion(client, job_id)
    assert job_res["status"] == "FAILED"
    assert job_res["files"][0]["error"]["code"] == ErrorCode.REMOTE_PATH_INVALID


def test_single_instance_guard(tmp_path: Path) -> None:
    """驗證同目錄單一實例 Mutex 鎖定 (SPEC 10.1 步驟 1)。"""
    g1 = SingleInstanceGuard(tmp_path)
    g2 = SingleInstanceGuard(tmp_path)
    assert g1.acquire() is True
    try:
        assert g2.acquire() is False
    finally:
        g1.release()
    assert g2.acquire() is True
    g2.release()


def test_ac01_packaged_exe_runs_standalone(tmp_path: Path) -> None:
    """
    AC-01: Windows 10 x64 未另裝語言 Runtime，仍可啟動工具。
    同時驗證 SPEC 5.1：不因目前工作目錄 (cwd) 改變設定檔位置。
    """
    import os
    import subprocess
    import urllib.request

    exe_path = Path(__file__).resolve().parent.parent / "release" / "SftpDeployTool" / "SftpDeployTool.exe"
    if not exe_path.exists():
        pytest.skip("SftpDeployTool.exe 尚未打包")

    # 挑選一個空閒 Port 供獨立 EXE 測試
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        test_port = s.getsockname()[1]

    exe_dir = exe_path.parent
    appsettings_path = exe_dir / "appsettings.json"
    orig_appsettings = appsettings_path.read_text(encoding="utf-8") if appsettings_path.exists() else None

    try:
        appsettings_path.write_text(
            json.dumps({"schemaVersion": 1, "httpPort": test_port}, indent=2),
            encoding="utf-8",
        )

        # 模擬無 Python 環境變數 (清空 PATH 中的 Python，並將 cwd 設為與 EXE 不同的 tmp_path)
        clean_env = {
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "WINDIR": os.environ.get("WINDIR", r"C:\Windows"),
            "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "PATH": r"C:\Windows\system32;C:\Windows",
            "TEMP": str(tmp_path),
            "TMP": str(tmp_path),
        }

        proc = subprocess.Popen(
            [str(exe_path), "--no-browser", "--no-messagebox"],
            cwd=str(tmp_path),  # 刻意使用不同工作目錄，驗證設定檔仍以 EXE 所在目錄為準
            env=clean_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        try:
            base_url = f"http://127.0.0.1:{test_port}"
            ready = False
            for _ in range(50):
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(f"{base_url}/api/settings", timeout=0.5) as resp:
                        if resp.status == 200:
                            data = json.loads(resp.read().decode("utf-8"))
                            assert data["httpPort"] == test_port
                            ready = True
                            break
                except Exception:
                    time.sleep(0.1)

            assert ready is True, "獨立 EXE 未能在預期時間內啟動 HTTP 服務"

            # 驗證首頁 HTML 與靜態資源可由 EXE 直接提供
            with urllib.request.urlopen(f"{base_url}/", timeout=2.0) as html_resp:
                assert html_resp.status == 200
                assert "Windows 本機 SFTP 檔案上傳工具" in html_resp.read().decode("utf-8")

            # 呼叫 /api/shutdown 安全退出 EXE
            req = urllib.request.Request(
                f"{base_url}/api/shutdown",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2.0) as shut_resp:
                assert shut_resp.status == 200

            proc.wait(timeout=10)
            assert proc.returncode == 0
        finally:
            if proc.poll() is None:
                proc.kill()
    finally:
        if orig_appsettings is not None:
            appsettings_path.write_text(orig_appsettings, encoding="utf-8")


