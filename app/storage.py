"""管理 JSON 設定檔的初始化、原子寫入、備份、匯出匯入與記憶體快取 (SPEC v1.1.0)。"""

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
    CURRENT_SCHEMA_VERSION,
    DEFAULT_WEBLOGIC_REST_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    decode_password_base64,
    default_profile_weblogic_redeploy_config,
    default_server_weblogic_config,
    encode_password_base64,
    is_server_weblogic_configured,
    is_strict_int,
    validate_admin_url,
    validate_appsettings_data,
    validate_deployments_file_data,
    validate_host,
    validate_local_path,
    validate_port,
    validate_profile_record,
    validate_profile_weblogic_redeploy_record,
    validate_remote_path,
    validate_server_record,
    validate_servers_file_data,
)
from app.weblogic_client import tw_now_str

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
            "schemaVersion": CURRENT_SCHEMA_VERSION,
            "httpPort": DEFAULT_HTTP_PORT,
        }
        self._servers: list[dict[str, Any]] = []
        self._profiles: list[dict[str, Any]] = []

    def initialize_and_load(self) -> None:
        """
        依 SPEC 10.1 啟動流程步驟 2~5：
        - 確認設定及紀錄目錄可讀寫。
        - 建立缺少的設定檔與目錄（既有檔案若損毀不得直接覆蓋）。
        - 驗證既有設定格式並載入（向下相容 schemaVersion 1 與 2）。
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
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
                    "httpPort": DEFAULT_HTTP_PORT,
                },
                keep_backup=False,
            )

        if not self.servers_path.exists():
            atomic_write_json(
                self.servers_path,
                {
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
                    "servers": [],
                },
                keep_backup=False,
            )

        if not self.deployments_path.exists():
            atomic_write_json(
                self.deployments_path,
                {
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
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
        wl_configured_ids = {
            s["id"]
            for s in srv_cfg["servers"]
            if is_server_weblogic_configured(s.get("weblogic"))
        }

        raw_dep = read_json_file(self.deployments_path)
        try:
            dep_cfg = validate_deployments_file_data(
                raw_dep,
                valid_server_ids=valid_server_ids,
                weblogic_configured_server_ids=wl_configured_ids,
            )
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
        回傳不含密碼與 passwordBase64 的 Server 清單 (SPEC 5.3)。
        同時附帶 hasPassword、weblogic 公開資訊與 referencedBy 供前端顯示。
        """
        with self._lock:
            result: list[dict[str, Any]] = []
            for s in self._servers:
                refs = [
                    {"id": p["id"], "name": p["name"]}
                    for p in self._profiles
                    if p["serverId"] == s["id"]
                ]
                wl_raw = s.get("weblogic") or default_server_weblogic_config()
                wl_configured = is_server_weblogic_configured(wl_raw)
                result.append(
                    {
                        "id": s["id"],
                        "name": s["name"],
                        "host": s["host"],
                        "port": s["port"],
                        "username": s["username"],
                        "hasPassword": bool(s.get("passwordBase64")),
                        "weblogic": {
                            "configured": wl_configured,
                            "adminUrl": wl_raw.get("adminUrl", ""),
                            "username": wl_raw.get("username", ""),
                            "hasPassword": bool(wl_raw.get("passwordBase64")),
                            "restVersion": wl_raw.get("restVersion", DEFAULT_WEBLOGIC_REST_VERSION),
                        },
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
        """僅供後端內部執行連線測試或建立工作快照使用，包含解碼後的 SFTP 與 WebLogic 密碼。"""
        with self._lock:
            for s in self._servers:
                if s["id"] == server_id:
                    copied = copy.deepcopy(s)
                    copied["password"] = decode_password_base64(s["passwordBase64"])
                    wl = copied.get("weblogic") or default_server_weblogic_config()
                    wl_pw_b64 = wl.get("passwordBase64", "")
                    wl["password"] = decode_password_base64(wl_pw_b64) if wl_pw_b64 else ""
                    copied["weblogic"] = wl
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

    def _parse_server_weblogic_input(
        self,
        wl_input: Any,
        existing_wl: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """解析來自 UI/API 的 Server weblogic 輸入（支援原始 password 轉 Base64 或保留既有密碼）。"""
        if wl_input is None:
            return copy.deepcopy(existing_wl) if existing_wl else default_server_weblogic_config()

        if not isinstance(wl_input, dict):
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "weblogic 設定欄位必須為 JSON 物件。",
                status_code=400,
            )

        admin_url = validate_admin_url(wl_input.get("adminUrl", ""))
        username_raw = wl_input.get("username", "")
        if not isinstance(username_raw, str):
            raise AppError(ErrorCode.CONFIG_INVALID, "WebLogic 帳號必須為字串。", status_code=400)
        username = username_raw.strip()

        rest_ver_raw = wl_input.get("restVersion", DEFAULT_WEBLOGIC_REST_VERSION)
        if not isinstance(rest_ver_raw, str):
            raise AppError(ErrorCode.CONFIG_INVALID, "WebLogic REST 版本必須為字串。", status_code=400)
        rest_version = rest_ver_raw.strip() or DEFAULT_WEBLOGIC_REST_VERSION

        # 若清空 adminUrl，視為停用/清除該 Server 的 WebLogic 設定
        if not admin_url:
            return {
                "adminUrl": "",
                "username": username,
                "passwordBase64": (existing_wl.get("passwordBase64", "") if existing_wl else ""),
                "restVersion": rest_version,
            }

        if not username:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "填寫 WebLogic Admin Server URL 時，WebLogic 帳號為必填。",
                status_code=400,
            )

        if "password" in wl_input and wl_input["password"] is not None and wl_input["password"] != "":
            pw_val = wl_input["password"]
            if not isinstance(pw_val, str):
                raise AppError(ErrorCode.CONFIG_INVALID, "WebLogic 密碼必須為字串。", status_code=400)
            pw_b64 = encode_password_base64(pw_val)
        elif existing_wl and existing_wl.get("passwordBase64"):
            pw_b64 = existing_wl["passwordBase64"]
        else:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "設定 WebLogic Admin 連線時必須提供 WebLogic 密碼。",
                status_code=400,
            )

        return {
            "adminUrl": admin_url,
            "username": username,
            "passwordBase64": pw_b64,
            "restVersion": rest_version,
        }

    def create_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        """新增 Server (SPEC 5.2, 5.3)。"""
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
            weblogic_cfg = self._parse_server_weblogic_input(payload.get("weblogic"), existing_wl=None)

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
                "weblogic": weblogic_cfg,
            }

            updated_servers = copy.deepcopy(self._servers) + [new_server]
            atomic_write_json(
                self.servers_path,
                {
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
                    "servers": updated_servers,
                },
            )
            self._servers = updated_servers
            return self.get_server_public(new_id)

    def update_server(self, server_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """修改 Server (SPEC 5.2, 5.3)。"""
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

            weblogic_cfg = self._parse_server_weblogic_input(
                payload.get("weblogic"),
                existing_wl=current.get("weblogic"),
            )

            # 檢查是否有啟用 WebLogic Redeploy 的 Profile 引用此 Server，若有則不允許清除此 Server 的 WebLogic 設定
            if not is_server_weblogic_configured(weblogic_cfg):
                wl_profiles = [
                    p["name"]
                    for p in self._profiles
                    if p["serverId"] == server_id
                    and (p.get("weblogicRedeploy") or {}).get("enabled") is True
                ]
                if wl_profiles:
                    raise AppError(
                        ErrorCode.CONFIG_INVALID,
                        f"此 Server 正被啟用 WebLogic Redeploy 的佈署設定（{'、'.join(wl_profiles)}）引用，無法清空 WebLogic 連線設定。",
                        status_code=400,
                    )

            updated_record = {
                "id": server_id,
                "name": name.strip(),
                "host": host,
                "port": port,
                "username": username.strip(),
                "passwordBase64": password_b64,
                "weblogic": weblogic_cfg,
            }

            updated_servers = copy.deepcopy(self._servers)
            updated_servers[target_idx] = updated_record

            atomic_write_json(
                self.servers_path,
                {
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
                    "servers": updated_servers,
                },
            )
            self._servers = updated_servers
            return self.get_server_public(server_id)

    def delete_server(self, server_id: str) -> None:
        """刪除未被引用的 Server (SPEC 8.5, AC-10)。"""
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
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
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
        """新增佈署設定 (SPEC 6.1, 6.2)。"""
        if not isinstance(payload, dict):
            raise AppError(ErrorCode.CONFIG_INVALID, "請求內容必須為 JSON 物件。", status_code=400)

        with self._lock:
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise AppError(ErrorCode.CONFIG_INVALID, "佈署設定名稱 (name) 為必填。", status_code=400)

            server_id = payload.get("serverId")
            if not isinstance(server_id, str) or not server_id.strip():
                raise AppError(ErrorCode.CONFIG_INVALID, "serverId 為必填。", status_code=400)

            target_srv = next((s for s in self._servers if s["id"] == server_id), None)
            if target_srv is None:
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

            srv_wl_ok = is_server_weblogic_configured(target_srv.get("weblogic"))
            weblogic_redeploy = validate_profile_weblogic_redeploy_record(
                payload.get("weblogicRedeploy"),
                prof_id=new_id,
                server_weblogic_configured=srv_wl_ok,
            )

            new_profile = {
                "id": new_id,
                "name": name.strip(),
                "serverId": server_id,
                "files": files,
                "weblogicRedeploy": weblogic_redeploy,
            }

            updated_profiles = copy.deepcopy(self._profiles) + [new_profile]
            atomic_write_json(
                self.deployments_path,
                {
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
                    "deploymentProfiles": updated_profiles,
                },
            )
            self._profiles = updated_profiles
            return copy.deepcopy(new_profile)

    def update_profile(self, profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """修改佈署設定 (SPEC 6.1, 6.2)。"""
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

            target_srv = next((s for s in self._servers if s["id"] == server_id), None)
            if target_srv is None:
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

            srv_wl_ok = is_server_weblogic_configured(target_srv.get("weblogic"))
            raw_wl_redeploy = (
                payload.get("weblogicRedeploy")
                if "weblogicRedeploy" in payload
                else self._profiles[target_idx].get("weblogicRedeploy")
            )
            weblogic_redeploy = validate_profile_weblogic_redeploy_record(
                raw_wl_redeploy,
                prof_id=profile_id,
                server_weblogic_configured=srv_wl_ok,
            )

            updated_profile = {
                "id": profile_id,
                "name": name.strip(),
                "serverId": server_id,
                "files": files,
                "weblogicRedeploy": weblogic_redeploy,
            }

            updated_profiles = copy.deepcopy(self._profiles)
            updated_profiles[target_idx] = updated_profile

            atomic_write_json(
                self.deployments_path,
                {
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
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
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
                    "deploymentProfiles": updated_profiles,
                },
            )
            self._profiles = updated_profiles

    # -------------------- 設定檔匯出與匯入 (SPEC v1.1.0 第 8 章) --------------------

    def export_config(self) -> dict[str, Any]:
        """匯出整合設定檔（包含 servers 與 deploymentProfiles 及 Base64 密碼）。"""
        with self._lock:
            return {
                "exportType": "SftpDeployToolConfig",
                "schemaVersion": CURRENT_SCHEMA_VERSION,
                "exportedAt": tw_now_str(),
                "servers": copy.deepcopy(self._servers),
                "deploymentProfiles": copy.deepcopy(self._profiles),
            }

    def import_config(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        匯入整合設定檔 (SPEC v1.1.0 8.2)：
        - 支援 mode: "overwrite"（整批覆蓋）或 "merge"（合併：同 ID 更新、新 ID 新增）。
        - 支援 schemaVersion 1 與 2 之向下相容匯入。
        """
        if not isinstance(payload, dict):
            raise AppError(ErrorCode.CONFIG_INVALID, "匯入請求必須為 JSON 物件。", status_code=400)

        mode = str(payload.get("mode") or "overwrite").strip().lower()
        if mode not in ("overwrite", "merge"):
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "匯入模式 (mode) 僅支援 'overwrite'（整批覆蓋）或 'merge'（合併）。",
                status_code=400,
            )

        data = payload.get("data")
        if not isinstance(data, dict):
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "匯入內容 (data) 必須為包含 servers 與 deploymentProfiles 的 JSON 物件。",
                status_code=400,
            )

        schema_version = data.get("schemaVersion", 1)
        if not is_strict_int(schema_version) or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"匯入檔的 schemaVersion 不支援或無效（支援版本：{sorted(SUPPORTED_SCHEMA_VERSIONS)}）。",
                status_code=400,
            )

        raw_servers = data.get("servers")
        raw_profiles = data.get("deploymentProfiles")
        if not isinstance(raw_servers, list) or not isinstance(raw_profiles, list):
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                "匯入檔必須包含 servers 陣列與 deploymentProfiles 陣列。",
                status_code=400,
            )

        # 先驗證匯入檔本身的 servers 是否無重複 ID 且欄位有效
        seen_import_srv_ids: set[str] = set()
        validated_import_servers: list[dict[str, Any]] = []
        for s_item in raw_servers:
            srv = validate_server_record(s_item)
            if srv["id"] in seen_import_srv_ids:
                raise AppError(
                    ErrorCode.CONFIG_INVALID,
                    f"匯入檔中存在重複的 Server ID：{srv['id']}",
                    status_code=400,
                )
            seen_import_srv_ids.add(srv["id"])
            validated_import_servers.append(srv)

        with self._lock:
            if mode == "overwrite":
                final_servers = validated_import_servers
            else:
                # merge: 現有同 ID 更新，新 ID 新增
                merged_srv_map: dict[str, dict[str, Any]] = {
                    s["id"]: copy.deepcopy(s) for s in self._servers
                }
                srv_order: list[str] = [s["id"] for s in self._servers]
                for srv in validated_import_servers:
                    if srv["id"] not in merged_srv_map:
                        srv_order.append(srv["id"])
                    merged_srv_map[srv["id"]] = srv
                final_servers = [merged_srv_map[sid] for sid in srv_order]

            valid_srv_ids = {s["id"] for s in final_servers}
            wl_configured_srv_ids = {
                s["id"]
                for s in final_servers
                if is_server_weblogic_configured(s.get("weblogic"))
            }

            # 驗證匯入的 deploymentProfiles
            seen_import_prof_ids: set[str] = set()
            validated_import_profiles: list[dict[str, Any]] = []
            for p_item in raw_profiles:
                prof = validate_profile_record(
                    p_item,
                    valid_server_ids=valid_srv_ids,
                    weblogic_configured_server_ids=wl_configured_srv_ids,
                )
                if prof["id"] in seen_import_prof_ids:
                    raise AppError(
                        ErrorCode.CONFIG_INVALID,
                        f"匯入檔中存在重複的佈署設定 ID：{prof['id']}",
                        status_code=400,
                    )
                seen_import_prof_ids.add(prof["id"])
                validated_import_profiles.append(prof)

            if mode == "overwrite":
                final_profiles = validated_import_profiles
            else:
                merged_prof_map: dict[str, dict[str, Any]] = {
                    p["id"]: copy.deepcopy(p) for p in self._profiles
                }
                prof_order: list[str] = [p["id"] for p in self._profiles]
                for prof in validated_import_profiles:
                    if prof["id"] not in merged_prof_map:
                        prof_order.append(prof["id"])
                    merged_prof_map[prof["id"]] = prof
                final_profiles = [merged_prof_map[pid] for pid in prof_order]

                # 再次確認合併後所有 Profile（包含既有 Profile）引用的 Server 皆有效
                for prof in final_profiles:
                    validate_profile_record(
                        prof,
                        valid_server_ids=valid_srv_ids,
                        weblogic_configured_server_ids=wl_configured_srv_ids,
                    )

            # 原子寫入 servers.json 與 deployments.json
            atomic_write_json(
                self.servers_path,
                {
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
                    "servers": final_servers,
                },
            )
            atomic_write_json(
                self.deployments_path,
                {
                    "schemaVersion": CURRENT_SCHEMA_VERSION,
                    "deploymentProfiles": final_profiles,
                },
            )

            self._servers = final_servers
            self._profiles = final_profiles

            return {
                "status": "OK",
                "mode": mode,
                "serverCount": len(final_servers),
                "profileCount": len(final_profiles),
                "message": f"設定檔匯入成功（模式：{'整批覆蓋' if mode == 'overwrite' else '合併'}，目前共 {len(final_servers)} 台 Server、{len(final_profiles)} 組佈署設定）。",
            }
