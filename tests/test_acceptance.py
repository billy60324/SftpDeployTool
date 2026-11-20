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
    last_data: Any = None
    while time.time() < deadline:
        resp = client.get(f"/api/deployments/{job_id}")
        assert resp.status_code == 200
        last_data = resp.json()
        if last_data["status"] not in ("QUEUED", "RUNNING"):
            return last_data
        time.sleep(0.05)
    raise TimeoutError(f"Job {job_id} did not finish within {timeout}s; last_data={last_data}")


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


# ==================== v1.1.0 新增驗收測試 (AC-29 ~ AC-37) ====================


def test_ac29_ac30_ui_traditional_chinese_and_table_styles(tmp_path: Path) -> None:
    """
    AC-29: UI 頁籤與所有介面文字均為繁體中文（無殘留簡體「与」字）。
    AC-30: Server 清單的「帳號 / 密碼狀態」欄位與狀態膠囊標籤不會因寬度不足而異常折行。
    """
    app = create_app(base_dir=tmp_path / "app_dir")
    client = TestClient(app)

    html_text = client.get("/").text
    assert "1. 觸發佈署與進度" in html_text
    assert "執行時間與錯誤資訊" in html_text
    assert "与" not in html_text

    css_text = client.get("/static/styles.css").text
    assert "white-space: nowrap;" in css_text

    js_text = client.get("/static/app.js").text
    assert "与" not in js_text


def test_ac31_taiwan_timezone_and_timestamp_format(
    tmp_path: Path, sftp_server: EmbeddedSftpTestServer
) -> None:
    """
    AC-31: 新產生的工作紀錄與日誌時間皆為台灣時區 (UTC+8)，且格式為 yyyy-mm-dd hh24:mi:ss。
    """
    import re
    from datetime import datetime, timedelta, timezone

    base_dir = tmp_path / "app_dir"
    app = create_app(base_dir=base_dir)
    client = TestClient(app)

    (sftp_server.root_dir / "opt").mkdir(parents=True, exist_ok=True)
    src_file = tmp_path / "tz_test.txt"
    src_file.write_text("timezone test", encoding="utf-8")

    srv_id = client.post(
        "/api/servers",
        json={
            "name": "TZ Server",
            "host": sftp_server.host,
            "port": sftp_server.port,
            "username": sftp_server.username,
            "password": sftp_server.password,
        },
    ).json()["id"]

    prof_id = client.post(
        "/api/profiles",
        json={
            "name": "TZ Profile",
            "serverId": srv_id,
            "files": [{"localPath": str(src_file), "remotePath": "/opt/tz_test.txt"}],
        },
    ).json()["id"]

    tw_tz = timezone(timedelta(hours=8))
    before_tw = datetime.now(tw_tz).replace(microsecond=0)

    dep_resp = client.post("/api/deployments", json={"profileId": prof_id})
    assert dep_resp.status_code == 202
    job_id = dep_resp.json()["jobId"]
    job_res = wait_for_job_completion(client, job_id)

    after_tw = datetime.now(tw_tz).replace(microsecond=0) + timedelta(seconds=2)

    ts_pattern = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
    for field in ("createdAt", "startedAt", "finishedAt"):
        val = job_res[field]
        assert isinstance(val, str) and ts_pattern.match(val), f"{field}={val} 不符合 yyyy-mm-dd hh24:mi:ss 格式"
        parsed_dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S").replace(tzinfo=tw_tz)
        assert before_tw <= parsed_dt <= after_tw

    f_item = job_res["files"][0]
    assert ts_pattern.match(f_item["startedAt"])
    assert ts_pattern.match(f_item["finishedAt"])


def test_ac32_config_export_and_import_overwrite_and_merge(tmp_path: Path) -> None:
    """
    AC-32: 可匯出單一整合設定 JSON 檔，並支援以「整批覆蓋 (overwrite)」或「合併 (merge)」模式匯入還原。
    """
    dir_a = tmp_path / "instance_a"
    client_a = TestClient(create_app(base_dir=dir_a))

    srv1 = client_a.post(
        "/api/servers",
        json={
            "name": "Server 1",
            "host": "10.0.0.1",
            "port": 22,
            "username": "user1",
            "password": "pw1",
            "weblogic": {
                "adminUrl": "http://wl1:7001",
                "username": "system",
                "password": "wlpw1",
                "restVersion": "12.2.1.3.0",
            },
        },
    ).json()

    prof1 = client_a.post(
        "/api/profiles",
        json={
            "name": "Profile 1",
            "serverId": srv1["id"],
            "files": [{"localPath": "C:\\Build\\a.ear", "remotePath": "/opt/a.ear"}],
            "weblogicRedeploy": {
                "enabled": True,
                "appName": "App1_EAR",
                "targets": "AdminServer",
            },
        },
    ).json()

    # 匯出設定檔
    exp_resp = client_a.get("/api/config/export")
    assert exp_resp.status_code == 200
    exported_data = exp_resp.json()
    assert exported_data["exportType"] == "SftpDeployToolConfig"
    assert exported_data["schemaVersion"] == 2
    assert len(exported_data["servers"]) == 1
    assert len(exported_data["deploymentProfiles"]) == 1
    assert exported_data["servers"][0]["passwordBase64"] != ""
    assert exported_data["servers"][0]["weblogic"]["passwordBase64"] != ""

    # 建立第二個環境 dir_b，先新增一筆既有 Server B 與 Profile B
    dir_b = tmp_path / "instance_b"
    client_b = TestClient(create_app(base_dir=dir_b))
    srv_b = client_b.post(
        "/api/servers",
        json={
            "name": "Existing Server B",
            "host": "10.0.0.2",
            "port": 2222,
            "username": "userb",
            "password": "pwb",
        },
    ).json()
    prof_b = client_b.post(
        "/api/profiles",
        json={
            "name": "Existing Profile B",
            "serverId": srv_b["id"],
            "files": [{"localPath": "C:\\Build\\b.dll", "remotePath": "/opt/b.dll"}],
        },
    ).json()

    # 1. 測試 merge 合併模式：保留 Server B / Profile B，並加入匯入的 Server 1 / Profile 1
    imp_merge = client_b.post(
        "/api/config/import",
        json={"mode": "merge", "data": exported_data},
    )
    assert imp_merge.status_code == 200
    assert imp_merge.json()["serverCount"] == 2
    assert imp_merge.json()["profileCount"] == 2

    # 2. 測試 overwrite 整批覆蓋模式：只留下匯入的 Server 1 / Profile 1
    imp_overwrite = client_b.post(
        "/api/config/import",
        json={"mode": "overwrite", "data": exported_data},
    )
    assert imp_overwrite.status_code == 200
    assert imp_overwrite.json()["serverCount"] == 1
    assert imp_overwrite.json()["profileCount"] == 1
    final_profiles = client_b.get("/api/profiles").json()["deploymentProfiles"]
    assert final_profiles[0]["id"] == prof1["id"]
    assert final_profiles[0]["weblogicRedeploy"]["enabled"] is True
    assert final_profiles[0]["weblogicRedeploy"]["appName"] == "App1_EAR"


def test_ac33_ac34_ac35_ac36_weblogic_connection_and_redeploy_workflow(
    tmp_path: Path, sftp_server: EmbeddedSftpTestServer
) -> None:
    """
    AC-33: Server 可配置 WebLogic Admin 連線資訊並執行 WebLogic REST 連線測試。
    AC-34: 佈署設定啟用 WebLogic Redeploy 時，當所有檔案 SFTP 上傳成功後自動呼叫 WebLogic Redeploy API 並輪詢結果、記錄日誌。
    AC-35: 佈署設定啟用 WebLogic Redeploy 時，若任一檔案 SFTP 上傳失敗，自動略過 (SKIPPED) WebLogic Redeploy，不重啟 AP。
    AC-36: 若 SFTP 檔案全數成功但 WebLogic Redeploy 失敗，整體工作狀態標記為 PARTIALLY_SUCCEEDED 並保留失敗原因與日誌。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    wl_state = {
        "auth_header": "Basic " + base64.b64encode("system:iamtesting123".encode("utf-8")).decode("ascii"),
        "redeploy_calls": [],
        "poll_count": 0,
        "force_redeploy_fail": False,
    }

    class MockWebLogicHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.headers.get("Authorization") != wl_state["auth_header"]:
                self._send_json(401, {"error": "Unauthorized"})
                return
            if "/deploymentProgressObjects/job-1" in self.path:
                wl_state["poll_count"] += 1
                if wl_state["poll_count"] < 2:
                    self._send_json(200, {"state": "STATE_RUNNING", "progress": "processing"})
                elif wl_state["force_redeploy_fail"]:
                    self._send_json(
                        200,
                        {"state": "STATE_FAILED", "progress": "failed", "messages": ["Deploy error"]},
                    )
                else:
                    self._send_json(200, {"state": "STATE_COMPLETED", "progress": "success"})
                return
            if "/domainRuntime" in self.path:
                self._send_json(200, {"name": "TestDomain"})
                return
            self._send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            if self.headers.get("Authorization") != wl_state["auth_header"]:
                self._send_json(401, {"error": "Unauthorized"})
                return
            if self.headers.get("X-Requested-By") != "weblogic-redeploy-tool":
                self._send_json(400, {"error": "Missing X-Requested-By"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
            wl_state["redeploy_calls"].append({"path": self.path, "body": json.loads(raw_body)})
            wl_state["poll_count"] = 0
            self._send_json(
                202,
                {
                    "state": "STATE_RUNNING",
                    "links": [
                        {
                            "rel": "job",
                            "href": f"http://127.0.0.1:{self.server.server_port}/management/weblogic/12.2.1.3.0/domainRuntime/deploymentManager/deploymentProgressObjects/job-1",
                        }
                    ],
                },
            )

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockWebLogicHandler)
    wl_port = httpd.server_port
    wl_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    wl_thread.start()

    try:
        base_dir = tmp_path / "app_dir"
        store = ConfigStore(base_dir)
        store.initialize_and_load()

        # 縮短測試時的輪詢間隔為 0.05 秒以加速測試執行
        from app.weblogic_client import run_weblogic_redeploy

        def fast_wl_runner(**kwargs: Any):
            kwargs["poll_interval_seconds"] = 0.05
            return run_weblogic_redeploy(**kwargs)

        jobs = JobManager(store, weblogic_redeploy_runner=fast_wl_runner)
        app = create_app(base_dir=base_dir, config_store=store, job_manager=jobs)
        client = TestClient(app)

        # AC-33: 建立含 WebLogic 設定的 Server 並測試 WebLogic 連線
        srv_resp = client.post(
            "/api/servers",
            json={
                "name": "SFTP + WebLogic Server",
                "host": sftp_server.host,
                "port": sftp_server.port,
                "username": sftp_server.username,
                "password": sftp_server.password,
                "weblogic": {
                    "adminUrl": f"http://127.0.0.1:{wl_port}/",
                    "username": "system",
                    "password": "iamtesting123",
                    "restVersion": "12.2.1.3.0",
                },
            },
        )
        assert srv_resp.status_code == 201
        srv_id = srv_resp.json()["id"]
        assert srv_resp.json()["weblogic"]["configured"] is True

        wl_test_resp = client.post(f"/api/servers/{srv_id}/test-weblogic")
        assert wl_test_resp.status_code == 200
        assert "TestDomain" in wl_test_resp.json()["message"]

        # 準備本機 EAR 檔案與遠端目錄
        (sftp_server.root_dir / "opt" / "wl").mkdir(parents=True, exist_ok=True)
        ear_file = tmp_path / "PMCForcastSystemEAR.ear"
        ear_file.write_bytes(b"EAR_BINARY_CONTENT")

        prof_resp = client.post(
            "/api/profiles",
            json={
                "name": "Deploy EAR & Redeploy WL",
                "serverId": srv_id,
                "files": [
                    {
                        "localPath": str(ear_file),
                        "remotePath": "/opt/wl/PMCForcastSystemEAR.ear",
                    }
                ],
                "weblogicRedeploy": {
                    "enabled": True,
                    "appName": "PMCForcastSystemEAR_7015",
                    "targets": "AdminServer,Cluster1",
                    "timeoutSeconds": 60,
                },
            },
        )
        assert prof_resp.status_code == 201
        prof_id = prof_resp.json()["id"]

        # AC-34: SFTP 全部成功 -> 自動觸發 WebLogic Redeploy 並輪詢完成 -> 整體 SUCCEEDED
        job1_id = client.post("/api/deployments", json={"profileId": prof_id}).json()["jobId"]
        job1_res = wait_for_job_completion(client, job1_id)
        assert job1_res["status"] == "SUCCEEDED"
        assert job1_res["weblogicRedeploy"]["status"] == "SUCCEEDED"
        assert len(wl_state["redeploy_calls"]) == 1
        assert (
            wl_state["redeploy_calls"][0]["path"]
            == "/management/weblogic/12.2.1.3.0/edit/appDeployments/PMCForcastSystemEAR_7015/redeploy"
        )
        assert wl_state["redeploy_calls"][0]["body"] == {"targets": ["AdminServer", "Cluster1"]}
        assert any("Redeploy 完成" in lg["message"] for lg in job1_res["weblogicRedeploy"]["logs"])

        # AC-36: SFTP 檔案成功，但 WebLogic Redeploy 失敗 -> 整體 PARTIALLY_SUCCEEDED
        wl_state["force_redeploy_fail"] = True
        job2_id = client.post("/api/deployments", json={"profileId": prof_id}).json()["jobId"]
        job2_res = wait_for_job_completion(client, job2_id)
        assert job2_res["status"] == "PARTIALLY_SUCCEEDED"
        assert job2_res["successCount"] == 1
        assert job2_res["weblogicRedeploy"]["status"] == "FAILED"
        assert job2_res["weblogicRedeploy"]["error"]["code"] == ErrorCode.WEBLOGIC_REDEPLOY_FAILED

        # AC-35: 任一檔案 SFTP 上傳失敗 -> 自動略過 (SKIPPED) WebLogic Redeploy，不發送 POST
        wl_state["force_redeploy_fail"] = False
        prev_call_count = len(wl_state["redeploy_calls"])
        missing_file = tmp_path / "missing.ear"
        prof_fail_sftp = client.post(
            "/api/profiles",
            json={
                "name": "Fail SFTP Skip WL",
                "serverId": srv_id,
                "files": [
                    {"localPath": str(ear_file), "remotePath": "/opt/wl/ok.ear"},
                    {"localPath": str(missing_file), "remotePath": "/opt/wl/missing.ear"},
                ],
                "weblogicRedeploy": {
                    "enabled": True,
                    "appName": "PMCForcastSystemEAR_7015",
                },
            },
        ).json()["id"]

        job3_id = client.post("/api/deployments", json={"profileId": prof_fail_sftp}).json()["jobId"]
        job3_res = wait_for_job_completion(client, job3_id)
        assert job3_res["status"] == "PARTIALLY_SUCCEEDED"
        assert job3_res["weblogicRedeploy"]["status"] == "SKIPPED"
        assert len(wl_state["redeploy_calls"]) == prev_call_count  # 確認未呼叫 WebLogic API
    finally:
        httpd.shutdown()


def test_ac37_backward_compatibility_with_v1_configs_and_logs(tmp_path: Path) -> None:
    """
    AC-37: 新版程式可直接載入 v1.0.0 (schemaVersion: 1) 的 appsettings.json、servers.json、deployments.json 與舊版歷史紀錄檔而不報錯。
    """
    base_dir = tmp_path / "v1_legacy_dir"
    logs_dir = base_dir / "logs" / "deployments"
    logs_dir.mkdir(parents=True, exist_ok=True)

    (base_dir / "appsettings.json").write_text(
        json.dumps({"schemaVersion": 1, "httpPort": 18088}),
        encoding="utf-8",
    )
    pw_b64 = base64.b64encode(b"legacy-pass").decode("ascii")
    (base_dir / "servers.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "servers": [
                    {
                        "id": "srv-legacy-01",
                        "name": "舊版 Server",
                        "host": "192.168.1.10",
                        "port": 22,
                        "username": "root",
                        "passwordBase64": pw_b64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (base_dir / "deployments.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "deploymentProfiles": [
                    {
                        "id": "prof-legacy-01",
                        "name": "舊版 Profile",
                        "serverId": "srv-legacy-01",
                        "files": [
                            {
                                "id": "f-01",
                                "localPath": "C:\\Build\\legacy.dll",
                                "remotePath": "/opt/legacy.dll",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (logs_dir / "job-20261019T083000Z-legacy.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "jobId": "job-20261019T083000Z-legacy",
                "profileId": "prof-legacy-01",
                "profileName": "舊版 Profile",
                "server": {
                    "id": "srv-legacy-01",
                    "name": "舊版 Server",
                    "host": "192.168.1.10",
                    "port": 22,
                    "username": "root",
                },
                "createdAt": "2026-10-19T08:30:00Z",
                "startedAt": "2026-10-19T08:30:01Z",
                "finishedAt": "2026-10-19T08:30:05Z",
                "status": "SUCCEEDED",
                "totalCount": 1,
                "successCount": 1,
                "failedCount": 0,
                "interruptedCount": 0,
                "files": [
                    {
                        "id": "f-01",
                        "localPath": "C:\\Build\\legacy.dll",
                        "remotePath": "/opt/legacy.dll",
                        "sizeBytes": 512,
                        "status": "SUCCEEDED",
                        "startedAt": "2026-10-19T08:30:01Z",
                        "finishedAt": "2026-10-19T08:30:05Z",
                        "error": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    app = create_app(base_dir=base_dir)
    client = TestClient(app)

    servers = client.get("/api/servers").json()["servers"]
    assert len(servers) == 1
    assert servers[0]["id"] == "srv-legacy-01"
    assert servers[0]["weblogic"]["configured"] is False

    profiles = client.get("/api/profiles").json()["deploymentProfiles"]
    assert len(profiles) == 1
    assert profiles[0]["id"] == "prof-legacy-01"
    assert profiles[0]["weblogicRedeploy"]["enabled"] is False

    legacy_job = client.get("/api/deployments/job-20261019T083000Z-legacy").json()
    assert legacy_job["status"] == "SUCCEEDED"
    assert legacy_job["weblogicRedeploy"]["enabled"] is False



