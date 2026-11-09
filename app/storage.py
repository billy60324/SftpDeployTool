"""管理 JSON 設定檔的初始化、原子寫入、備份與記憶體快取 (SPEC 5.2, 5.3, 6~10)。"""

from __future__ import annotations

import copy
import json
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from app.errors import AppError, ErrorCode
from app.models import (
    SUPPORTED_SCHEMA_VERSION,
    decode_password_base64,
    encode_password_base64,
    is_strict_int,
    validate_appsettings_data,
    validate_deployments_file_data,
    validate_host,
    validate_local_path,
    validate_port,
    validate_remote_path,
    validate_servers_file_data,
)

DEFAULT_HTTP_PORT = 18080


def atomic_write_json(
    target_path: Path,
    data: dict[str, Any],
    error_code: str = ErrorCode.CONFIG_WRITE_FAILED,
    keep_backup: bool = True,
) -> None:
    """
    依 SPEC 5.3 寫入規則保存 JSON：
    - 使用 UTF-8 編碼。
    - 寫入同目錄暫存檔 (.tmp) -> flush & fsync -> 備份舊檔為 .bak -> 原子替換原檔。
    - 寫入失敗時拋出指定錯誤碼，不默默改存他處。
    """
    parent = target_path.parent
    tmp_path = parent / f"{target_path.name}.{uuid.uuid4().hex[:8]}.tmp"
    bak_path = parent / f"{target_path.name}.bak"

    try:
        parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())

        if keep_backup and target_path.exists():
            shutil.copy2(target_path, bak_path)

        os.replace(tmp_path, target_path)
    except Exception as exc:
        # 清理可能殘留的 .tmp
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise AppError(
            error_code,
            f"寫入檔案 {target_path.name} 失敗：{exc}",
            status_code=500,
        ) from None


def read_json_file(file_path: Path) -> Any:
    """讀取並解析 UTF-8 JSON 檔案，損毀時拋出 CONFIG_INVALID 並標明檔名 (SPEC 5.2, 10.2)。"""
    try:
        raw_text = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"無法讀取設定檔 {file_path.name}（請確認檔案編碼為 UTF-8 且具讀取權限）：{exc}",
            status_code=400,
            details={"file": file_path.name},
        ) from None

    try:
        return json.loads(raw_text)
    except Exception as exc:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"設定檔 {file_path.name} JSON 格式損毀：{exc}",
            status_code=400,
            details={"file": file_path.name},
        ) from None


class ConfigStore:
    """集中管理 appsettings.json, servers.json, deployments.json 與目錄權限。"""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.appsettings_path = base_dir / "appsettings.json"
        self.servers_path = base_dir / "servers.json"
        self.deployments_path = base_dir / "deployments.json"
        self.logs_dir = base_dir / "logs" / "deployments"

        self._lock = threading.RLock()
        self._appsettings: dict[str, Any] = {
            "schemaVersion": SUPPORTED_SCHEMA_VERSION,
            "httpPort": DEFAULT_HTTP_PORT,
        }
        self._servers: list[dict[str, Any]] = []
        self._profiles: list[dict[str, Any]] = []

    def initialize_and_load(self) -> None:
        """
        依 SPEC 10.1 啟動流程步驟 2~5：
        - 確認設定及紀錄目錄可讀寫。
        - 建立缺少的設定檔與目錄（既有檔案若損毀不得直接覆蓋）。
        - 驗證既有設定格式並載入。
        """
        with self._lock:
            self._verify_directory_permissions()
            self._ensure_default_files_if_missing()
            self._load_all_from_disk()

    def _verify_directory_permissions(self) -> None:
        """檢查 base_dir 與 logs/deployments 目錄是否可讀寫。"""
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            probe_file = self.base_dir / f".perm_check_{uuid.uuid4().hex[:6]}.tmp"
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink()

            log_probe = self.logs_dir / f".perm_check_{uuid.uuid4().hex[:6]}.tmp"
            log_probe.write_text("ok", encoding="utf-8")
            log_probe.unlink()
        except Exception as exc:
            raise AppError(
                ErrorCode.CONFIG_WRITE_FAILED,
                f"工具目錄或紀錄目錄無讀寫權限：{exc}",
                status_code=500,
            ) from None

    def _ensure_default_files_if_missing(self) -> None:
        """僅在設定檔『不存在』時建立預設檔 (SPEC 5.2)。"""
        if not self.appsettings_path.exists():
            atomic_write_json(
                self.appsettings_path,
                {
                    "schemaVersion": SUPPORTED_SCHEMA_VERSION,
                    "httpPort": DEFAULT_HTTP_PORT,
                },
                keep_backup=False,
            )

        if not self.servers_path.exists():
            atomic_write_json(
                self.servers_path,
                {
                    "schemaVersion": SUPPORTED_SCHEMA_VERSION,
                    "servers": [],
                },
                keep_backup=False,
            )

        if not self.deployments_path.exists():
            atomic_write_json(
                self.deployments_path,
                {
                    "schemaVersion": SUPPORTED_SCHEMA_VERSION,
                    "deploymentProfiles": [],
                },
                keep_backup=False,
            )

    def _load_all_from_disk(self) -> None:
        """讀取並驗證三個設定檔，任一失敗皆指明檔名並拒絕啟動 (SPEC 10.2)。"""
        raw_app = read_json_file(self.appsettings_path)
        try:
            app_cfg = validate_appsettings_data(raw_app)
        except AppError as err:
            raise AppError(
                err.code,
                f"[appsettings.json] {err.message}",
                status_code=err.status_code,
                details={"file": "appsettings.json"},
            ) from None

        raw_srv = read_json_file(self.servers_path)
        try:
            srv_cfg = validate_servers_file_data(raw_srv)
        except AppError as err:
            raise AppError(
                err.code,
                f"[servers.json] {err.message}",
                status_code=err.status_code,
                details={"file": "servers.json"},
            ) from None

        valid_server_ids = {s["id"] for s in srv_cfg["servers"]}

        raw_dep = read_json_file(self.deployments_path)
        try:
            dep_cfg = validate_deployments_file_data(raw_dep, valid_server_ids)
        except AppError as err:
            raise AppError(
                err.code,
                f"[deployments.json] {err.message}",
                status_code=err.status_code,
                details={"file": "deployments.json"},
            ) from None

        self._appsettings = app_cfg
        self._servers = srv_cfg["servers"]
        self._profiles = dep_cfg["deploymentProfiles"]

    # -------------------- 查詢介面 --------------------

    def get_http_port(self) -> int:
        with self._lock:
            return int(self._appsettings["httpPort"])

    def get_settings(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._appsettings)

    def list_servers_public(self) -> list[dict[str, Any]]:
        """
        回傳不含密碼與 passwordBase64 的 Server 清單 (SPEC 8.4, 11.1, 15.3)。
        同時附帶 hasPassword: true 與 referencedByProfiles 供前端顯示引用情況。
        """
        with self._lock:
            result: list[dict[str, Any]] = []
            for s in self._servers:
                refs = [
                    {"id": p["id"], "name": p["name"]}
                    for p in self._profiles
                    if p["serverId"] == s["id"]
                ]
                result.append(
                    {
                        "id": s["id"],
                        "name": s["name"],
                        "host": s["host"],
                        "port": s["port"],
                        "username": s["username"],
                        "hasPassword": bool(s.get("passwordBase64")),
                        "referencedBy": refs,
                    }
                )
            return result

    def get_server_public(self, server_id: str) -> dict[str, Any]:
        with self._lock:
            for s in self.list_servers_public():
                if s["id"] == server_id:
                    return s
            raise AppError(
                ErrorCode.SERVER_NOT_FOUND,
                f"找不到指定的 Server：{server_id}",
                status_code=404,
            )

    def get_server_with_credentials(self, server_id: str) -> dict[str, Any]:
        """僅供後端內部執行連線測試或建立工作快照使用，包含解碼後的密碼。"""
        with self._lock:
            for s in self._servers:
                if s["id"] == server_id:
                    copied = copy.deepcopy(s)
                    copied["password"] = decode_password_base64(s["passwordBase64"])
                    return copied
            raise AppError(
                ErrorCode.SERVER_NOT_FOUND,
                f"找不到指定的 Server：{server_id}",
                status_code=404,
            )

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._profiles)

    def get_profile(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            for p in self._profiles:
                if p["id"] == profile_id:
                    return copy.deepcopy(p)
            raise AppError(
                ErrorCode.PROFILE_NOT_FOUND,
                f"找不到指定的佈署設定：{profile_id}",
                status_code=404,
            )

    # -------------------- Server 異動介面 --------------------

    def create_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        """新增 Server (SPEC 8.2, 8.4, 15.3)。"""
        if not isinstance(payload, dict):
            raise AppError(ErrorCode.CONFIG_INVALID, "請求內容必須為 JSON 物件。", status_code=400)

        with self._lock:
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise AppError(ErrorCode.CONFIG_INVALID, "Server 名稱 (name) 為必填。", status_code=400)

            host = validate_host(payload.get("host"))
            raw_port = payload.get("port", 22)
            if raw_port is None:
                raw_port = 22
            port = validate_port(raw_port, "port")

            username = payload.get("username")
            if not isinstance(username, str) or not username.strip():
                raise AppError(ErrorCode.CONFIG_INVALID, "登入帳號 (username) 為必填。", status_code=400)

            if "password" not in payload or payload.get("password") is None:
                raise AppError(ErrorCode.CONFIG_INVALID, "新增 Server 時必須提供密碼 (password)。", status_code=400)

            password_b64 = encode_password_base64(payload.get("password"))

            new_id = f"srv-{uuid.uuid4().hex[:8]}"
            existing_ids = {s["id"] for s in self._servers}
            while new_id in existing_ids:
                new_id = f"srv-{uuid.uuid4().hex[:8]}"

            new_server = {
                "id": new_id,
                "name": name.strip(),
                "host": host,
                "port": port,
                "username": username.strip(),
                "passwordBase64": password_b64,
            }

            updated_servers = copy.deepcopy(self._servers) + [new_server]
            atomic_write_json(
                self.servers_path,
                {
                    "schemaVersion": SUPPORTED_SCHEMA_VERSION,
                    "servers": updated_servers,
                },
            )
            self._servers = updated_servers
            return self.get_server_public(new_id)

    def update_server(self, server_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """
        修改 Server (SPEC 6.2, 8.4, 8.5)：
        - 修改名稱不得改變 ID。
        - 未傳入 password 欄位：保留原密碼。
        - 傳入非空 password：替換原密碼。
        - 傳入空字串或 null：回報驗證錯誤。
        """
        if not isinstance(payload, dict):
            raise AppError(ErrorCode.CONFIG_INVALID, "請求內容必須為 JSON 物件。", status_code=400)

        with self._lock:
            target_idx: Optional[int] = None
            for idx, s in enumerate(self._servers):
                if s["id"] == server_id:
                    target_idx = idx
                    break

            if target_idx is None:
                raise AppError(
                    ErrorCode.SERVER_NOT_FOUND,
                    f"找不到指定的 Server：{server_id}",
                    status_code=404,
                )

            current = self._servers[target_idx]

            # 若傳入 id 且與路徑不一致，拒絕修改 ID
            if "id" in payload and payload["id"] != server_id:
                raise AppError(
                    ErrorCode.CONFIG_INVALID,
                    "不允許修改 Server ID。",
                    status_code=400,
                )

            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise AppError(ErrorCode.CONFIG_INVALID, "Server 名稱 (name) 為必填。", status_code=400)

            host = validate_host(payload.get("host"))
            raw_port = payload.get("port", current["port"])
            if raw_port is None:
                raw_port = 22
            port = validate_port(raw_port, "port")

            username = payload.get("username")
            if not isinstance(username, str) or not username.strip():
                raise AppError(ErrorCode.CONFIG_INVALID, "登入帳號 (username) 為必填。", status_code=400)

            if "password" not in payload:
                password_b64 = current["passwordBase64"]
            else:
                pw_val = payload["password"]
                if pw_val is None or not isinstance(pw_val, str) or len(pw_val) == 0:
                    raise AppError(
                        ErrorCode.CONFIG_INVALID,
                        "密碼不得為空字串或 null（若不修改密碼請勿傳入 password 欄位）。",
                        status_code=400,
                    )
                password_b64 = encode_password_base64(pw_val)

            updated_record = {
                "id": server_id,
                "name": name.strip(),
                "host": host,
                "port": port,
                "username": username.strip(),
                "passwordBase64": password_b64,
            }

            updated_servers = copy.deepcopy(self._servers)
            updated_servers[target_idx] = updated_record

            atomic_write_json(
                self.servers_path,
                {
                    "schemaVersion": SUPPORTED_SCHEMA_VERSION,
                    "servers": updated_servers,
                },
            )
            self._servers = updated_servers
            return self.get_server_public(server_id)

    def delete_server(self, server_id: str) -> None:
        """
        刪除未被引用的 Server (SPEC 8.5, AC-10)：
        - 若被任何佈署設定引用，拒絕刪除並回傳 409 SERVER_IN_USE，訊息與 details 列出引用它的佈署設定。
        """
        with self._lock:
            target_idx: Optional[int] = None
            for idx, s in enumerate(self._servers):
                if s["id"] == server_id:
                    target_idx = idx
                    break

            if target_idx is None:
                raise AppError(
                    ErrorCode.SERVER_NOT_FOUND,
                    f"找不到指定的 Server：{server_id}",
                    status_code=404,
                )

            referencing_profiles = [
                {"id": p["id"], "name": p["name"]}
                for p in self._profiles
                if p["serverId"] == server_id
            ]
            if referencing_profiles:
                ref_names = "、".join(f"{p['name']} ({p['id']})" for p in referencing_profiles)
                raise AppError(
                    ErrorCode.SERVER_IN_USE,
                    f"此 Server 正被以下佈署設定引用，無法刪除：{ref_names}",
                    status_code=409,
                    details={"referencedBy": referencing_profiles},
                )

            updated_servers = copy.deepcopy(self._servers)
            updated_servers.pop(target_idx)

            atomic_write_json(
                self.servers_path,
                {
                    "schemaVersion": SUPPORTED_SCHEMA_VERSION,
                    "servers": updated_servers,
                },
            )
            self._servers = updated_servers

    # -------------------- 佈署設定 (Profile) 異動介面 --------------------

    def _validate_and_normalize_files_input(
        self,
        files_input: Any,
        existing_file_ids: Optional[set[str]] = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(files_input, list) or len(files_input) == 0:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "files 必須為包含至少一筆檔案對應的陣列。",
                status_code=400,
            )

        seen_ids: set[str] = set()
        seen_remote_paths: set[str] = set()
        normalized: list[dict[str, Any]] = []

        for item in files_input:
            if not isinstance(item, dict):
                raise AppError(ErrorCode.CONFIG_INVALID, "檔案對應項目必須為 JSON 物件。", status_code=400)

            raw_id = item.get("id")
            if isinstance(raw_id, str) and raw_id.strip():
                f_id = raw_id.strip()
                # 若提供 existing_file_ids，僅允許沿用該設定原有的檔案 ID 或系統產生新 ID
                if existing_file_ids is not None and f_id not in existing_file_ids:
                    f_id = f"file-{uuid.uuid4().hex[:8]}"
            else:
                f_id = f"file-{uuid.uuid4().hex[:8]}"

            while f_id in seen_ids:
                f_id = f"file-{uuid.uuid4().hex[:8]}"
            seen_ids.add(f_id)

            local_path = validate_local_path(item.get("localPath"))
            remote_path = validate_remote_path(item.get("remotePath"))

            if remote_path in seen_remote_paths:
                raise AppError(
                    ErrorCode.CONFIG_INVALID,
                    f"同一組佈署設定不得有重複的遠端目標路徑 (remotePath)：{remote_path}",
                    status_code=400,
                )
            seen_remote_paths.add(remote_path)

            normalized.append(
                {
                    "id": f_id,
                    "localPath": local_path,
                    "remotePath": remote_path,
                }
            )
        return normalized

    def create_profile(self, payload: dict[str, Any]) -> dict[str, Any]:
        """新增佈署設定 (SPEC 6.2, 9.2 ~ 9.5)。"""
        if not isinstance(payload, dict):
            raise AppError(ErrorCode.CONFIG_INVALID, "請求內容必須為 JSON 物件。", status_code=400)

        with self._lock:
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise AppError(ErrorCode.CONFIG_INVALID, "佈署設定名稱 (name) 為必填。", status_code=400)

            server_id = payload.get("serverId")
            if not isinstance(server_id, str) or not server_id.strip():
                raise AppError(ErrorCode.CONFIG_INVALID, "serverId 為必填。", status_code=400)

            valid_server_ids = {s["id"] for s in self._servers}
            if server_id not in valid_server_ids:
                raise AppError(
                    ErrorCode.CONFIG_INVALID,
                    f"引用的 Server 不存在：{server_id}",
                    status_code=400,
                )

            files = self._validate_and_normalize_files_input(
                payload.get("files"),
                existing_file_ids=set(),
            )

            new_id = f"prof-{uuid.uuid4().hex[:8]}"
            existing_ids = {p["id"] for p in self._profiles}
            while new_id in existing_ids:
                new_id = f"prof-{uuid.uuid4().hex[:8]}"

            new_profile = {
                "id": new_id,
                "name": name.strip(),
                "serverId": server_id,
                "files": files,
            }

            updated_profiles = copy.deepcopy(self._profiles) + [new_profile]
            atomic_write_json(
                self.deployments_path,
                {
                    "schemaVersion": SUPPORTED_SCHEMA_VERSION,
                    "deploymentProfiles": updated_profiles,
                },
            )
            self._profiles = updated_profiles
            return copy.deepcopy(new_profile)

    def update_profile(self, profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """修改佈署設定 (SPEC 6.2, 9.2 ~ 9.5)。"""
        if not isinstance(payload, dict):
            raise AppError(ErrorCode.CONFIG_INVALID, "請求內容必須為 JSON 物件。", status_code=400)

        with self._lock:
            target_idx: Optional[int] = None
            for idx, p in enumerate(self._profiles):
                if p["id"] == profile_id:
                    target_idx = idx
                    break

            if target_idx is None:
                raise AppError(
                    ErrorCode.PROFILE_NOT_FOUND,
                    f"找不到指定的佈署設定：{profile_id}",
                    status_code=404,
                )

            if "id" in payload and payload["id"] != profile_id:
                raise AppError(
                    ErrorCode.CONFIG_INVALID,
                    "不允許修改佈署設定 ID。",
                    status_code=400,
                )

            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise AppError(ErrorCode.CONFIG_INVALID, "佈署設定名稱 (name) 為必填。", status_code=400)

            server_id = payload.get("serverId")
            if not isinstance(server_id, str) or not server_id.strip():
                raise AppError(ErrorCode.CONFIG_INVALID, "serverId 為必填。", status_code=400)

            valid_server_ids = {s["id"] for s in self._servers}
            if server_id not in valid_server_ids:
                raise AppError(
                    ErrorCode.CONFIG_INVALID,
                    f"引用的 Server 不存在：{server_id}",
                    status_code=400,
                )

            current_file_ids = {f["id"] for f in self._profiles[target_idx]["files"]}
            files = self._validate_and_normalize_files_input(
                payload.get("files"),
                existing_file_ids=current_file_ids,
            )

            updated_profile = {
                "id": profile_id,
                "name": name.strip(),
                "serverId": server_id,
                "files": files,
            }

            updated_profiles = copy.deepcopy(self._profiles)
            updated_profiles[target_idx] = updated_profile

            atomic_write_json(
                self.deployments_path,
                {
                    "schemaVersion": SUPPORTED_SCHEMA_VERSION,
                    "deploymentProfiles": updated_profiles,
                },
            )
            self._profiles = updated_profiles
            return copy.deepcopy(updated_profile)

    def delete_profile(self, profile_id: str) -> None:
        """刪除佈署設定。"""
        with self._lock:
            target_idx: Optional[int] = None
            for idx, p in enumerate(self._profiles):
                if p["id"] == profile_id:
                    target_idx = idx
                    break

            if target_idx is None:
                raise AppError(
                    ErrorCode.PROFILE_NOT_FOUND,
                    f"找不到指定的佈署設定：{profile_id}",
                    status_code=404,
                )

            updated_profiles = copy.deepcopy(self._profiles)
            updated_profiles.pop(target_idx)

            atomic_write_json(
                self.deployments_path,
                {
                    "schemaVersion": SUPPORTED_SCHEMA_VERSION,
                    "deploymentProfiles": updated_profiles,
                },
            )
            self._profiles = updated_profiles
