"""定義設定檔模型、驗證規則、向下相容轉換與 API Request/Response 結構 (SPEC v1.1.0)。"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional

from app.errors import AppError, ErrorCode

SUPPORTED_SCHEMA_VERSION = 1
CURRENT_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = {1, 2}

DEFAULT_WEBLOGIC_REST_VERSION = "12.2.1.3.0"
DEFAULT_WEBLOGIC_REDEPLOY_PATH = "/management/weblogic/{version}/edit/appDeployments/{app}/redeploy"
DEFAULT_WEBLOGIC_TIMEOUT_SECONDS = 300

_DRIVE_ABS_RE = re.compile(r"^[a-zA-Z]:[\\/](?:[^\\/]+[\\/])*[^\\/]+$")
_UNC_ABS_RE = re.compile(r"^\\\\[^\\/?*\"<>|]+\\[^\\/?*\"<>|]+(?:\\[^\\/?*\"<>|]+)+$")


def is_strict_int(val: Any) -> bool:
    """檢查是否為純整數（排除 Python 中 isinstance(True, int) 為 True 的情況）。"""
    return isinstance(val, int) and not isinstance(val, bool)


def default_server_weblogic_config() -> dict[str, Any]:
    """回傳預設空白的 Server WebLogic 設定結構（供舊版向下相容與預設初始化使用）。"""
    return {
        "adminUrl": "",
        "username": "",
        "passwordBase64": "",
        "restVersion": DEFAULT_WEBLOGIC_REST_VERSION,
    }


def default_profile_weblogic_redeploy_config() -> dict[str, Any]:
    """回傳預設未啟用的 Profile WebLogic Redeploy 設定結構（供舊版向下相容與預設初始化使用）。"""
    return {
        "enabled": False,
        "appName": "",
        "targets": "",
        "timeoutSeconds": DEFAULT_WEBLOGIC_TIMEOUT_SECONDS,
        "redeployPath": DEFAULT_WEBLOGIC_REDEPLOY_PATH,
        "requestBodyJson": "",
    }


def is_server_weblogic_configured(wl: Optional[dict[str, Any]]) -> bool:
    """檢查該 Server 的 WebLogic 連線設定是否已完整配置（URL、帳號、密碼皆存在）。"""
    if not isinstance(wl, dict):
        return False
    return bool(
        str(wl.get("adminUrl") or "").strip()
        and str(wl.get("username") or "").strip()
        and str(wl.get("passwordBase64") or "").strip()
    )


def encode_password_base64(password: str) -> str:
    """
    將原始密碼轉為 UTF-8 Base64 字串 (SPEC 8.2, 8.3)。
    - 不自動去除前後空白。
    - 不支援空密碼。
    """
    if not isinstance(password, str) or len(password) == 0:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "密碼不得為空字串或未提供。",
            status_code=400,
        )
    raw_bytes = password.encode("utf-8")
    return base64.b64encode(raw_bytes).decode("ascii")


def decode_password_base64(password_b64: Any) -> str:
    """
    驗證並解碼 passwordBase64 為 UTF-8 原始密碼 (SPEC 8.2, 8.3)。
    - Base64 無法解碼或解碼後非有效 UTF-8 或為空字串時，視為設定錯誤。
    """
    if not isinstance(password_b64, str) or not password_b64.strip():
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "passwordBase64 必須為非空白的有效 Base64 字串。",
            status_code=400,
        )
    try:
        # validate=True 確保不接受非 Base64 字元，同時檢查長度為 4 的倍數
        padded = password_b64.strip()
        if len(padded) % 4 != 0:
            raise ValueError("Invalid base64 length")
        decoded_bytes = base64.b64decode(padded, validate=True)
        # 重新編碼比對，防止不合法的 padding
        if base64.b64encode(decoded_bytes).decode("ascii") != padded:
            raise ValueError("Non-canonical base64")
        password = decoded_bytes.decode("utf-8")
    except Exception:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "passwordBase64 無法解碼或不是有效的 UTF-8 編碼內容。",
            status_code=400,
        ) from None

    if len(password) == 0:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "第一版不支援空密碼登入。",
            status_code=400,
        )
    return password


def validate_host(host: Any) -> str:
    """驗證 Server Hostname 或 IP，不含協定、Port、路徑 (SPEC 8.2)。"""
    if not isinstance(host, str) or not host.strip():
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "Server host 為必填且不得為空白。",
            status_code=400,
        )
    h = host.strip()
    if "://" in h or "/" in h or "\\" in h or ":" in h or any(c.isspace() for c in h):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "Server host 僅可填寫 Hostname 或 IPv4 位址，不得包含協定、Port、斜線或空白。",
            status_code=400,
        )
    return h


def validate_port(port: Any, field_name: str = "port") -> int:
    """驗證 Port 範圍 1–65535 (SPEC 7.1, 8.2)。"""
    if not is_strict_int(port) or port < 1 or port > 65535:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"{field_name} 必須為 1 到 65535 之間的整數。",
            status_code=400,
        )
    return port


def validate_admin_url(url: Any) -> str:
    """驗證 WebLogic Admin Server URL，須以 http:// 或 https:// 開頭並移除結尾斜線。"""
    if url is None:
        return ""
    if not isinstance(url, str):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "WebLogic Admin Server URL 必須為字串。",
            status_code=400,
        )
    cleaned = url.strip().rstrip("/")
    if not cleaned:
        return ""
    if not (cleaned.startswith("http://") or cleaned.startswith("https://")):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"WebLogic Admin Server URL 必須以 http:// 或 https:// 開頭：{url}",
            status_code=400,
        )
    if any(c.isspace() for c in cleaned):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "WebLogic Admin Server URL 不得包含空白字元。",
            status_code=400,
        )
    return cleaned


def validate_server_weblogic_record(raw_wl: Any, srv_id: str = "") -> dict[str, Any]:
    """
    驗證並正規化單筆 Server 的 weblogic 設定區塊 (SPEC v1.1.0 5.2)。
    若為 None 或未提供，自動補齊預設空結構（向下相容 v1.0.0）。
    """
    if raw_wl is None:
        return default_server_weblogic_config()
    if not isinstance(raw_wl, dict):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"Server ({srv_id}) 的 weblogic 欄位必須為 JSON 物件。",
            status_code=400,
        )

    admin_url = validate_admin_url(raw_wl.get("adminUrl", ""))
    username_raw = raw_wl.get("username", "")
    if not isinstance(username_raw, str):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"Server ({srv_id}) 的 weblogic.username 必須為字串。",
            status_code=400,
        )
    username = username_raw.strip()

    rest_ver_raw = raw_wl.get("restVersion", DEFAULT_WEBLOGIC_REST_VERSION)
    if not isinstance(rest_ver_raw, str):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"Server ({srv_id}) 的 weblogic.restVersion 必須為字串。",
            status_code=400,
        )
    rest_version = rest_ver_raw.strip() or DEFAULT_WEBLOGIC_REST_VERSION

    pw_b64_raw = raw_wl.get("passwordBase64", "")
    if not isinstance(pw_b64_raw, str):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"Server ({srv_id}) 的 weblogic.passwordBase64 必須為字串。",
            status_code=400,
        )
    pw_b64 = pw_b64_raw.strip()

    if admin_url:
        if not username:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"Server ({srv_id}) 已填寫 WebLogic Admin Server URL 時，WebLogic 帳號為必填。",
                status_code=400,
            )
        if not pw_b64:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"Server ({srv_id}) 已填寫 WebLogic Admin Server URL 時，WebLogic 密碼為必填。",
                status_code=400,
            )
        decode_password_base64(pw_b64)
    else:
        # 若未啟用/未填寫 adminUrl，但有殘留 passwordBase64 則仍驗證或允許清空
        if pw_b64:
            decode_password_base64(pw_b64)

    return {
        "adminUrl": admin_url,
        "username": username,
        "passwordBase64": pw_b64,
        "restVersion": rest_version,
    }


def validate_profile_weblogic_redeploy_record(
    raw_wl: Any,
    prof_id: str = "",
    server_weblogic_configured: bool = True,
) -> dict[str, Any]:
    """
    驗證並正規化單筆 Profile 的 weblogicRedeploy 設定區塊 (SPEC v1.1.0 6.2)。
    若為 None 或未提供，自動補齊預設 enabled=False 結構（向下相容 v1.0.0）。
    """
    if raw_wl is None:
        return default_profile_weblogic_redeploy_config()
    if not isinstance(raw_wl, dict):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"佈署設定 ({prof_id}) 的 weblogicRedeploy 欄位必須為 JSON 物件。",
            status_code=400,
        )

    enabled = raw_wl.get("enabled", False)
    if not isinstance(enabled, bool):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"佈署設定 ({prof_id}) 的 weblogicRedeploy.enabled 必須為布林值。",
            status_code=400,
        )

    app_name_raw = raw_wl.get("appName", "")
    if not isinstance(app_name_raw, str):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"佈署設定 ({prof_id}) 的 weblogicRedeploy.appName 必須為字串。",
            status_code=400,
        )
    app_name = app_name_raw.strip()

    targets_raw = raw_wl.get("targets", "")
    if not isinstance(targets_raw, str):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"佈署設定 ({prof_id}) 的 weblogicRedeploy.targets 必須為字串。",
            status_code=400,
        )
    targets = targets_raw.strip()

    timeout_raw = raw_wl.get("timeoutSeconds", DEFAULT_WEBLOGIC_TIMEOUT_SECONDS)
    if timeout_raw is None:
        timeout_raw = DEFAULT_WEBLOGIC_TIMEOUT_SECONDS
    if not is_strict_int(timeout_raw) or timeout_raw < 10 or timeout_raw > 3600:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"佈署設定 ({prof_id}) 的 WebLogic 等待逾時秒數 (timeoutSeconds) 必須為 10 到 3600 之間的整數。",
            status_code=400,
        )

    redeploy_path_raw = raw_wl.get("redeployPath", DEFAULT_WEBLOGIC_REDEPLOY_PATH)
    if not isinstance(redeploy_path_raw, str):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"佈署設定 ({prof_id}) 的 weblogicRedeploy.redeployPath 必須為字串。",
            status_code=400,
        )
    redeploy_path = redeploy_path_raw.strip() or DEFAULT_WEBLOGIC_REDEPLOY_PATH

    body_raw = raw_wl.get("requestBodyJson", "")
    if not isinstance(body_raw, str):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"佈署設定 ({prof_id}) 的 weblogicRedeploy.requestBodyJson 必須為字串。",
            status_code=400,
        )
    request_body_json = body_raw.strip()

    if request_body_json:
        try:
            json.loads(request_body_json)
        except Exception as exc:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"佈署設定 ({prof_id}) 的 WebLogic Request Body 不是合法的 JSON 格式：{exc}",
                status_code=400,
            ) from None

    if enabled:
        if not app_name:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"佈署設定 ({prof_id}) 啟用 WebLogic Redeploy 時，AP 名稱 (appName) 為必填。",
                status_code=400,
            )
        if not server_weblogic_configured:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"佈署設定 ({prof_id}) 啟用了 WebLogic Redeploy，但所選的目標 Server 尚未設定 WebLogic Admin 連線資訊（URL、帳號、密碼）。",
                status_code=400,
            )

    return {
        "enabled": enabled,
        "appName": app_name,
        "targets": targets,
        "timeoutSeconds": timeout_raw,
        "redeployPath": redeploy_path,
        "requestBodyJson": request_body_json,
    }


def validate_local_path(local_path: Any) -> str:
    """
    驗證 Windows 本機絕對檔案路徑 (SPEC 9.3, 9.4)：
    - 不支援相對路徑。
    - 不得以 \\ 或 / 結尾。
    - 必須為磁碟機絕對路徑 (如 C:\\dir\\file.txt) 或 UNC 路徑 (\\\\server\\share\\file.txt)。
    """
    if not isinstance(local_path, str) or not local_path.strip():
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "localPath 為必填且不得為空白。",
            status_code=400,
        )
    if local_path.endswith("\\") or local_path.endswith("/"):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"localPath 不得以斜線結尾（必須包含目標檔案名稱）：{local_path}",
            status_code=400,
        )
    if "\x00" in local_path:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "localPath 包含無效字元。",
            status_code=400,
        )
    if not (_DRIVE_ABS_RE.match(local_path) or _UNC_ABS_RE.match(local_path)):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"localPath 必須為 Windows 本機絕對檔案路徑（不支援相對路徑）：{local_path}",
            status_code=400,
        )
    # 檢查路徑區段是否包含 . 或 .. 相對路徑導覽
    normalized_sep = local_path.replace("/", "\\")
    parts = [p for p in normalized_sep.split("\\") if p]
    if any(p in (".", "..") for p in parts):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"localPath 不支援相對路徑區段 (. 或 ..)：{local_path}",
            status_code=400,
        )
    return local_path


def validate_remote_path(remote_path: Any) -> str:
    """
    驗證 Linux 絕對檔案路徑 (SPEC 9.3, 9.4)：
    - 必須以 / 開頭，且不得以 / 結尾。
    - 禁止包含 /../、/./、反斜線 \\ 或空路徑區段 (//)。
    """
    if not isinstance(remote_path, str) or not remote_path.strip():
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "remotePath 為必填且不得為空白。",
            status_code=400,
        )
    if not remote_path.startswith("/"):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"remotePath 必須以 / 開頭：{remote_path}",
            status_code=400,
        )
    if remote_path.endswith("/"):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"remotePath 不得以 / 結尾（必須包含目標檔名）：{remote_path}",
            status_code=400,
        )
    if "\\" in remote_path or "//" in remote_path or "\x00" in remote_path:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"remotePath 不得包含反斜線 \\ 或連續斜線 //：{remote_path}",
            status_code=400,
        )
    segments = remote_path.split("/")[1:]
    if not segments or any(seg in ("", ".", "..") for seg in segments):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"remotePath 不得包含相對路徑區段 (. 或 ..)：{remote_path}",
            status_code=400,
        )
    return remote_path


def validate_appsettings_data(data: Any) -> dict[str, Any]:
    """驗證 appsettings.json 結構，向下相容 schemaVersion 1 與 2 (SPEC 4.1, 7.1)。"""
    if not isinstance(data, dict):
        raise AppError(ErrorCode.CONFIG_INVALID, "appsettings.json 必須為 JSON 物件。")
    schema_version = data.get("schemaVersion")
    if not is_strict_int(schema_version) or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"appsettings.json 的 schemaVersion 不支援或無效（支援版本：{sorted(SUPPORTED_SCHEMA_VERSIONS)}）。",
        )
    http_port = validate_port(data.get("httpPort"), "httpPort")
    return {
        "schemaVersion": schema_version,
        "httpPort": http_port,
    }


def validate_server_record(raw: Any) -> dict[str, Any]:
    """驗證單筆 Server 設定紀錄，並自動向下相容補齊 weblogic 欄位 (SPEC 5.2)。"""
    if not isinstance(raw, dict):
        raise AppError(ErrorCode.CONFIG_INVALID, "Server 項目必須為 JSON 物件。")

    srv_id = raw.get("id")
    if not isinstance(srv_id, str) or not srv_id.strip():
        raise AppError(ErrorCode.CONFIG_INVALID, "Server id 為必填字串。")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AppError(ErrorCode.CONFIG_INVALID, f"Server ({srv_id}) 的 name 為必填非空白字串。")

    host = validate_host(raw.get("host"))
    port = validate_port(raw.get("port"), "port")

    username = raw.get("username")
    if not isinstance(username, str) or not username.strip():
        raise AppError(ErrorCode.CONFIG_INVALID, f"Server ({srv_id}) 的 username 為必填非空白字串。")

    password_b64 = raw.get("passwordBase64")
    # 驗證 Base64 可正常解碼為非空 UTF-8 字串
    decode_password_base64(password_b64)

    weblogic_cfg = validate_server_weblogic_record(raw.get("weblogic"), srv_id=srv_id.strip())

    return {
        "id": srv_id.strip(),
        "name": name.strip(),
        "host": host,
        "port": port,
        "username": username.strip(),
        "passwordBase64": password_b64.strip(),
        "weblogic": weblogic_cfg,
    }


def validate_servers_file_data(data: Any) -> dict[str, Any]:
    """驗證 servers.json 結構與 ID 唯一性，支援 schemaVersion 1 與 2 (SPEC 4.1, 5.1)。"""
    if not isinstance(data, dict):
        raise AppError(ErrorCode.CONFIG_INVALID, "servers.json 必須為 JSON 物件。")
    schema_version = data.get("schemaVersion")
    if not is_strict_int(schema_version) or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"servers.json 的 schemaVersion 不支援或無效（支援版本：{sorted(SUPPORTED_SCHEMA_VERSIONS)}）。",
        )
    servers_raw = data.get("servers")
    if not isinstance(servers_raw, list):
        raise AppError(ErrorCode.CONFIG_INVALID, "servers.json 的 servers 欄位必須為陣列。")

    seen_ids: set[str] = set()
    validated_servers: list[dict[str, Any]] = []
    for item in servers_raw:
        srv = validate_server_record(item)
        if srv["id"] in seen_ids:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"servers.json 中存在重複的 Server ID：{srv['id']}",
            )
        seen_ids.add(srv["id"])
        validated_servers.append(srv)

    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "servers": validated_servers,
    }


def validate_profile_record(
    raw: Any,
    valid_server_ids: set[str],
    weblogic_configured_server_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """驗證單筆 Deployment Profile 設定紀錄，並自動向下相容補齊 weblogicRedeploy 欄位 (SPEC 6.1, 6.2)。"""
    if not isinstance(raw, dict):
        raise AppError(ErrorCode.CONFIG_INVALID, "佈署設定項目必須為 JSON 物件。")

    prof_id = raw.get("id")
    if not isinstance(prof_id, str) or not prof_id.strip():
        raise AppError(ErrorCode.CONFIG_INVALID, "佈署設定 id 為必填字串。")

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise AppError(ErrorCode.CONFIG_INVALID, f"佈署設定 ({prof_id}) 的 name 為必填非空白字串。")

    server_id = raw.get("serverId")
    if not isinstance(server_id, str) or not server_id.strip():
        raise AppError(ErrorCode.CONFIG_INVALID, f"佈署設定 ({prof_id}) 的 serverId 為必填字串。")
    if server_id not in valid_server_ids:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"佈署設定 ({prof_id}) 引用了不存在的 Server ID：{server_id}",
        )

    files_raw = raw.get("files")
    if not isinstance(files_raw, list) or len(files_raw) == 0:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"佈署設定 ({prof_id}) 的 files 必須為包含至少一筆檔案對應的陣列。",
        )

    seen_file_ids: set[str] = set()
    seen_remote_paths: set[str] = set()
    validated_files: list[dict[str, Any]] = []

    for f_item in files_raw:
        if not isinstance(f_item, dict):
            raise AppError(ErrorCode.CONFIG_INVALID, f"佈署設定 ({prof_id}) 中的檔案對應必須為物件。")
        f_id = f_item.get("id")
        if not isinstance(f_id, str) or not f_id.strip():
            raise AppError(ErrorCode.CONFIG_INVALID, f"佈署設定 ({prof_id}) 中的檔案對應缺少有效 id。")
        f_id = f_id.strip()
        if f_id in seen_file_ids:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"佈署設定 ({prof_id}) 中存在重複的檔案對應 ID：{f_id}",
            )
        seen_file_ids.add(f_id)

        local_path = validate_local_path(f_item.get("localPath"))
        remote_path = validate_remote_path(f_item.get("remotePath"))

        if remote_path in seen_remote_paths:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"佈署設定 ({prof_id}) 中包含重複的遠端目標路徑 remotePath：{remote_path}",
            )
        seen_remote_paths.add(remote_path)

        validated_files.append(
            {
                "id": f_id,
                "localPath": local_path,
                "remotePath": remote_path,
            }
        )

    srv_wl_ok = (
        True
        if weblogic_configured_server_ids is None
        else (server_id in weblogic_configured_server_ids)
    )
    weblogic_redeploy = validate_profile_weblogic_redeploy_record(
        raw.get("weblogicRedeploy"),
        prof_id=prof_id.strip(),
        server_weblogic_configured=srv_wl_ok,
    )

    return {
        "id": prof_id.strip(),
        "name": name.strip(),
        "serverId": server_id,
        "files": validated_files,
        "weblogicRedeploy": weblogic_redeploy,
    }


def validate_deployments_file_data(
    data: Any,
    valid_server_ids: set[str],
    weblogic_configured_server_ids: Optional[set[str]] = None,
) -> dict[str, Any]:
    """驗證 deployments.json 結構與引用完整性，支援 schemaVersion 1 與 2 (SPEC 4.1, 6.1)。"""
    if not isinstance(data, dict):
        raise AppError(ErrorCode.CONFIG_INVALID, "deployments.json 必須為 JSON 物件。")
    schema_version = data.get("schemaVersion")
    if not is_strict_int(schema_version) or schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            f"deployments.json 的 schemaVersion 不支援或無效（支援版本：{sorted(SUPPORTED_SCHEMA_VERSIONS)}）。",
        )
    profiles_raw = data.get("deploymentProfiles")
    if not isinstance(profiles_raw, list):
        raise AppError(
            ErrorCode.CONFIG_INVALID,
            "deployments.json 的 deploymentProfiles 欄位必須為陣列。",
        )

    seen_ids: set[str] = set()
    validated_profiles: list[dict[str, Any]] = []
    for item in profiles_raw:
        prof = validate_profile_record(
            item,
            valid_server_ids=valid_server_ids,
            weblogic_configured_server_ids=weblogic_configured_server_ids,
        )
        if prof["id"] in seen_ids:
            raise AppError(
                ErrorCode.CONFIG_INVALID,
                f"deployments.json 中存在重複的佈署設定 ID：{prof['id']}",
            )
        seen_ids.add(prof["id"])
        validated_profiles.append(prof)

    return {
        "schemaVersion": CURRENT_SCHEMA_VERSION,
        "deploymentProfiles": validated_profiles,
    }
