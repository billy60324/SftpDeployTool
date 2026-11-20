"""
WebLogic Management RESTful API 連線測試與 AP Redeploy 客戶端模組 (SPEC v1.1.0 5.3, 6, 7)。
完整對標 temp/weblogic-redeploy.html 之 REST 呼叫、Basic Auth、X-Requested-By 與 Job 進度輪詢機制。
"""

from __future__ import annotations

import base64
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from app.errors import AppError, ErrorCode
from app.models import (
    DEFAULT_WEBLOGIC_REDEPLOY_PATH,
    DEFAULT_WEBLOGIC_REST_VERSION,
    DEFAULT_WEBLOGIC_TIMEOUT_SECONDS,
)

TW_TZ = timezone(timedelta(hours=8))


def tw_now_str() -> str:
    """回傳台灣時區 (UTC+8) 的 yyyy-mm-dd hh24:mi:ss 時間字串 (SPEC v1.1.0 第 9 章)。"""
    return datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")


def _build_basic_auth_header(username: str, password: str) -> str:
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {token}"


def _hint_by_status(status: int) -> Optional[str]:
    if status == 401:
        return "驗證失敗（401）：帳號或密碼被 WebLogic 拒絕。請確認帳密正確且帳號未被鎖定。"
    if status == 403:
        return "帳號權限不足（403）：需具備 Admin 或 Deployer 角色。"
    if status == 404:
        return "找不到 API 路徑（404）：請檢查 REST 版本、AP 名稱或進階設定的路徑。"
    return None


def _http_call(
    method: str,
    url: str,
    username: str,
    password: str,
    body_text: Optional[str] = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """
    發送單次 HTTP/HTTPS 請求至 WebLogic Admin Server。
    回傳 {"ok": bool, "status": int, "reason": str, "text": str, "json": Any}。
    若發生連線錯誤或逾時，拋出 OSError / socket.timeout。
    """
    headers: dict[str, str] = {
        "Authorization": _build_basic_auth_header(username, password),
        "Accept": "application/json",
        "X-Requested-By": "weblogic-redeploy-tool",
        "Connection": "close",
    }
    data_bytes: Optional[bytes] = None
    if body_text is not None:
        headers["Content-Type"] = "application/json"
        data_bytes = body_text.encode("utf-8")

    req = urllib.request.Request(url=url, data=data_bytes, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            reason = getattr(resp, "reason", "OK") or "OK"
            raw_bytes = resp.read()
    except urllib.error.HTTPError as http_err:
        status = http_err.code
        reason = getattr(http_err, "reason", "") or ""
        try:
            raw_bytes = http_err.read()
        except Exception:
            raw_bytes = b""
        finally:
            try:
                http_err.close()
            except Exception:
                pass
    except urllib.error.URLError as url_err:
        if isinstance(url_err.reason, socket.timeout):
            raise socket.timeout(str(url_err.reason)) from None
        raise OSError(str(url_err.reason)) from None

    text = raw_bytes.decode("utf-8", errors="replace") if raw_bytes else ""
    parsed_json: Any = None
    if text:
        try:
            parsed_json = json.loads(text)
        except Exception:
            parsed_json = None

    return {
        "ok": 200 <= status < 300,
        "status": status,
        "reason": str(reason),
        "text": text,
        "json": parsed_json,
    }


def test_weblogic_server_connection(
    admin_url: str,
    username: str,
    password: str,
    rest_version: str = DEFAULT_WEBLOGIC_REST_VERSION,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """
    測試 WebLogic Admin Server REST API 連線與帳密驗證 (SPEC v1.1.0 5.3)。
    對標 weblogic-redeploy.html 的 testConnection():
    GET {adminUrl}/management/weblogic/{version}/domainRuntime?links=none&fields=name
    """
    base = (admin_url or "").strip().rstrip("/")
    if not base:
        raise AppError(
            ErrorCode.WEBLOGIC_NOT_CONFIGURED,
            "此 Server 尚未設定 WebLogic Admin Server URL。",
            status_code=400,
        )
    if not username or not password:
        raise AppError(
            ErrorCode.WEBLOGIC_NOT_CONFIGURED,
            "此 Server 尚未完整設定 WebLogic 帳號與密碼。",
            status_code=400,
        )

    ver = urllib.parse.quote((rest_version or DEFAULT_WEBLOGIC_REST_VERSION).strip(), safe="")
    url = f"{base}/management/weblogic/{ver}/domainRuntime?links=none&fields=name"

    try:
        res = _http_call("GET", url, username=username, password=password, timeout=timeout)
    except socket.timeout:
        raise AppError(
            ErrorCode.WEBLOGIC_TIMEOUT,
            f"WebLogic 連線測試逾時（超過 {int(timeout)} 秒）：{base}",
            status_code=504,
        ) from None
    except Exception as exc:
        raise AppError(
            ErrorCode.WEBLOGIC_CONNECTION_FAILED,
            f"無法連線至 WebLogic Admin Server ({base})：{exc}",
            status_code=502,
        ) from None

    if res["ok"]:
        domain_name = None
        if isinstance(res["json"], dict):
            domain_name = res["json"].get("name")
        msg = "WebLogic 連線與帳密驗證成功！"
        if domain_name:
            msg = f"WebLogic 連線與帳密驗證成功（Domain：{domain_name}）！"
        return {
            "status": "OK",
            "message": msg,
            "domainName": domain_name,
        }

    status = res["status"]
    hint = _hint_by_status(status)
    if status in (401, 403):
        raise AppError(
            ErrorCode.WEBLOGIC_AUTH_FAILED,
            hint or f"WebLogic 驗證失敗（HTTP {status}）。",
            status_code=502,
        )
    raise AppError(
        ErrorCode.WEBLOGIC_CONNECTION_FAILED,
        hint or f"WebLogic 連線測試失敗（HTTP {status} {res['reason']}）。",
        status_code=502,
    )


def _find_job_link(json_data: Any, base_url: str) -> Optional[str]:
    """從回應 JSON 的 links 陣列中找出可輪詢的 job / progress 連結 (對標 findJobLink)。"""
    if not isinstance(json_data, dict):
        return None
    links = json_data.get("links")
    if not isinstance(links, list):
        return None

    for item in links:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("rel") or "")
        href = str(item.get("href") or "")
        if not href:
            continue
        if re.search(r"job|progress", rel, re.IGNORECASE) or re.search(
            r"deploymentProgressObjects|/jobs?/", href, re.IGNORECASE
        ):
            if href.startswith("http://") or href.startswith("https://"):
                return href
            return base_url.rstrip("/") + "/" + href.lstrip("/")
    return None


def _check_job_state(json_data: Any) -> dict[str, Any]:
    """判定回應 JSON 中的 WebLogic 部署狀態 (對標 jobState)。"""
    if not isinstance(json_data, dict):
        return {"done": False}
    state = str(json_data.get("state") or "").upper()
    progress = str(json_data.get("progress") or "").lower()
    if "FAILED" in state or progress == "failed":
        return {"done": True, "ok": False, "json": json_data}
    if "COMPLETED" in state or "SUCCESS" in state or progress == "success":
        return {"done": True, "ok": True, "json": json_data}
    return {"done": False, "json": json_data}


def build_redeploy_request_url_and_body(
    admin_url: str,
    rest_version: str,
    app_name: str,
    targets: str = "",
    redeploy_path: str = DEFAULT_WEBLOGIC_REDEPLOY_PATH,
    request_body_json: str = "",
) -> tuple[str, str]:
    """組裝 Redeploy POST 請求之 URL 與 JSON Body (對標 buildRedeployRequest)。"""
    base = admin_url.strip().rstrip("/")
    ver_enc = urllib.parse.quote((rest_version or DEFAULT_WEBLOGIC_REST_VERSION).strip(), safe="")
    app_enc = urllib.parse.quote(app_name.strip(), safe="")

    path_template = (redeploy_path or DEFAULT_WEBLOGIC_REDEPLOY_PATH).strip()
    resolved_path = path_template.replace("{version}", ver_enc).replace("{app}", app_enc)
    if not resolved_path.startswith("/"):
        resolved_path = "/" + resolved_path

    url = base + resolved_path

    body_str = (request_body_json or "").strip()
    if not body_str:
        target_list = [s.strip() for s in (targets or "").split(",") if s.strip()]
        body_obj: dict[str, Any] = {"targets": target_list} if target_list else {}
        body_str = json.dumps(body_obj, ensure_ascii=False)
    else:
        # 確認合法 JSON
        json.loads(body_str)

    return url, body_str


@dataclass
class WeblogicRedeployResult:
    succeeded: bool
    status: str  # "SUCCEEDED" | "FAILED"
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    logs: list[dict[str, str]] = field(default_factory=list)


def run_weblogic_redeploy(
    admin_url: str,
    username: str,
    password: str,
    rest_version: str,
    app_name: str,
    targets: str = "",
    timeout_seconds: int = DEFAULT_WEBLOGIC_TIMEOUT_SECONDS,
    redeploy_path: str = DEFAULT_WEBLOGIC_REDEPLOY_PATH,
    request_body_json: str = "",
    poll_interval_seconds: float = 2.0,
    on_log_callback: Optional[Callable[[dict[str, str]], None]] = None,
) -> WeblogicRedeployResult:
    """
    執行 WebLogic AP Redeploy 與進度輪詢 (SPEC v1.1.0 第 7 章)。
    過程中的每筆日誌皆附帶台灣時間戳 (yyyy-mm-dd hh24:mi:ss) 並可透過 callback 即時同步至 Job 紀錄。
    """
    logs: list[dict[str, str]] = []

    def append_log(message: str, level: str = "info") -> None:
        entry = {
            "time": tw_now_str(),
            "level": level,
            "message": message,
        }
        logs.append(entry)
        if on_log_callback is not None:
            try:
                on_log_callback(entry)
            except Exception:
                pass

    append_log(f"===== 開始 WebLogic Redeploy：{app_name} =====", "info")

    try:
        req_url, req_body = build_redeploy_request_url_and_body(
            admin_url=admin_url,
            rest_version=rest_version,
            app_name=app_name,
            targets=targets,
            redeploy_path=redeploy_path,
            request_body_json=request_body_json,
        )
    except Exception as exc:
        err_msg = f"組裝 WebLogic Redeploy 請求失敗：{exc}"
        append_log(err_msg, "err")
        return WeblogicRedeployResult(
            succeeded=False,
            status="FAILED",
            error_code=ErrorCode.CONFIG_INVALID,
            error_message=err_msg,
            logs=logs,
        )

    append_log(f"POST {req_url}", "info")
    append_log(f"Body: {req_body}", "dim")

    http_timeout = float(min(max(timeout_seconds, 10), 60))

    try:
        res = _http_call(
            "POST",
            req_url,
            username=username,
            password=password,
            body_text=req_body,
            timeout=http_timeout,
        )
    except socket.timeout:
        err_msg = "連線至 WebLogic Admin Server 逾時。"
        append_log(err_msg, "err")
        return WeblogicRedeployResult(
            succeeded=False,
            status="FAILED",
            error_code=ErrorCode.WEBLOGIC_TIMEOUT,
            error_message=err_msg,
            logs=logs,
        )
    except Exception as exc:
        err_msg = f"連線至 WebLogic Admin Server 失敗：{exc}"
        append_log(err_msg, "err")
        return WeblogicRedeployResult(
            succeeded=False,
            status="FAILED",
            error_code=ErrorCode.WEBLOGIC_CONNECTION_FAILED,
            error_message=err_msg,
            logs=logs,
        )

    append_log(
        f"HTTP {res['status']} {res['reason']}",
        "ok" if res["ok"] else "err",
    )
    if res["text"]:
        formatted_body = (
            json.dumps(res["json"], ensure_ascii=False, indent=2)
            if res["json"] is not None
            else res["text"]
        )
        append_log(formatted_body, "dim" if res["ok"] else "err")

    if not res["ok"]:
        hint = _hint_by_status(res["status"])
        if hint:
            append_log(hint, "err")
        err_code = (
            ErrorCode.WEBLOGIC_AUTH_FAILED
            if res["status"] in (401, 403)
            else ErrorCode.WEBLOGIC_REDEPLOY_FAILED
        )
        err_msg = hint or f"WebLogic Redeploy 請求失敗（HTTP {res['status']} {res['reason']}）。"
        append_log("===== Redeploy 失敗 =====", "err")
        return WeblogicRedeployResult(
            succeeded=False,
            status="FAILED",
            error_code=err_code,
            error_message=err_msg,
            logs=logs,
        )

    st = _check_job_state(res["json"])
    job_url = _find_job_link(res["json"], base_url=admin_url)

    if not st["done"] and job_url:
        deadline = time.time() + max(10, int(timeout_seconds or DEFAULT_WEBLOGIC_TIMEOUT_SECONDS))
        append_log(f"輪詢進度：{job_url}", "info")
        while not st["done"] and time.time() < deadline:
            time.sleep(poll_interval_seconds)
            append_log(f"GET {job_url}", "info")
            try:
                poll_res = _http_call(
                    "GET",
                    job_url,
                    username=username,
                    password=password,
                    timeout=http_timeout,
                )
            except Exception as exc:
                err_msg = f"輪詢 WebLogic 進度連線失敗：{exc}"
                append_log(err_msg, "err")
                return WeblogicRedeployResult(
                    succeeded=False,
                    status="FAILED",
                    error_code=ErrorCode.WEBLOGIC_CONNECTION_FAILED,
                    error_message=err_msg,
                    logs=logs,
                )

            append_log(
                f"HTTP {poll_res['status']} {poll_res['reason']}",
                "ok" if poll_res["ok"] else "err",
            )
            if poll_res["text"]:
                poll_formatted = (
                    json.dumps(poll_res["json"], ensure_ascii=False, indent=2)
                    if poll_res["json"] is not None
                    else poll_res["text"]
                )
                append_log(poll_formatted, "dim" if poll_res["ok"] else "err")

            if not poll_res["ok"]:
                err_msg = f"查詢進度失敗（HTTP {poll_res['status']}），停止輪詢。"
                append_log(err_msg, "err")
                return WeblogicRedeployResult(
                    succeeded=False,
                    status="FAILED",
                    error_code=ErrorCode.WEBLOGIC_REDEPLOY_FAILED,
                    error_message=err_msg,
                    logs=logs,
                )

            st = _check_job_state(poll_res["json"])

        if not st["done"]:
            err_msg = "等待逾時，請至 WebLogic Console 確認結果。"
            append_log(err_msg, "err")
            return WeblogicRedeployResult(
                succeeded=False,
                status="FAILED",
                error_code=ErrorCode.WEBLOGIC_TIMEOUT,
                error_message=err_msg,
                logs=logs,
            )
    elif not st["done"]:
        append_log("回應中未找到可輪詢的 job 連結，請至 WebLogic Console 確認部署結果。", "info")
        append_log("===== Redeploy 已送出 =====", "ok")
        return WeblogicRedeployResult(
            succeeded=True,
            status="SUCCEEDED",
            logs=logs,
        )

    if st.get("ok"):
        append_log("===== Redeploy 完成 =====", "ok")
        return WeblogicRedeployResult(
            succeeded=True,
            status="SUCCEEDED",
            logs=logs,
        )

    msgs = ""
    if isinstance(st.get("json"), dict) and st["json"].get("messages"):
        msgs = json.dumps(st["json"]["messages"], ensure_ascii=False)
    err_msg = f"WebLogic Redeploy 失敗 {msgs}".strip()
    append_log(f"===== Redeploy 失敗 ===== {msgs}".strip(), "err")
    return WeblogicRedeployResult(
        succeeded=False,
        status="FAILED",
        error_code=ErrorCode.WEBLOGIC_REDEPLOY_FAILED,
        error_message=err_msg,
        logs=logs,
    )
