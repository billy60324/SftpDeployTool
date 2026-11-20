# Windows 本機 SFTP 檔案上傳工具 (SftpDeployTool) v1.1.0 實作計畫

## 1. 目標說明

依據 [docs/v1.1.0/SPEC.md](./SPEC.md)，在保留 `v1.0.0` 所有穩定機制的基礎上，完成 `v1.1.0` 六大功能改進與擴充：

1. **UI 繁體中文化修正**：修正「觸發佈署與進度」、「執行時間與錯誤資訊」等文字。
2. **Server 管理列表欄位寬度與不換行優化**：優化「帳號 / 密碼狀態」欄寬與 `.status-pill` 樣式。
3. **台灣時區 (`UTC+8`) 與 `yyyy-mm-dd hh24:mi:ss` 時間格式**：統一後端產生的時間戳與前端顯示。
4. **設定檔匯出與匯入功能**：提供單一 JSON 整合匯出與支援 `overwrite` / `merge` 模式的匯入 API 及 UI。
5. **SFTP 上傳成功後自動觸發 WebLogic AP Redeploy**：整合 `weblogic-redeploy.html` 的 WebLogic REST API 測試連線、触发 Redeploy 與 Job 輪詢機制。
6. **Config JSON 向下相容**：支援無縫讀取 `schemaVersion: 1` 的舊版設定檔與歷史紀錄。

---

## 2. 模組變更設計

### 2.1 錯誤代碼與資料模型 (`app/errors.py`, `app/models.py`)
- **`app/errors.py`**：
  - 新增 WebLogic 相關錯誤代碼：`WEBLOGIC_CONNECTION_FAILED`、`WEBLOGIC_AUTH_FAILED`、`WEBLOGIC_REDEPLOY_FAILED`、`WEBLOGIC_TIMEOUT`、`WEBLOGIC_NOT_CONFIGURED`。
- **`app/models.py`**：
  - 將 `SUPPORTED_SCHEMA_VERSIONS = {1, 2}`、最新寫入版本設為 `CURRENT_SCHEMA_VERSION = 2`。
  - 擴充 `validate_server_record()`：支援選填的 `weblogic` 物件（`adminUrl`、`username`、`passwordBase64`、`restVersion`），當舊版 `schemaVersion: 1` 缺少該欄位時自動補齊預設空結構。
  - 擴充 `validate_profile_record()`：支援選填的 `weblogicRedeploy` 物件（`enabled`、`appName`、`targets`、`timeoutSeconds`、`redeployPath`、`requestBodyJson`），當 `enabled=True` 時驗證 `appName` 必填、目標 Server 已設定 WebLogic 連線資訊、且自訂 `requestBodyJson`（若有填寫）為合法 JSON。

### 2.2 WebLogic REST 客戶端 (`app/weblogic_client.py` - 新增模組)
- 封裝 `weblogic-redeploy.html` 中的 REST 呼叫與輪詢邏輯：
  - `test_weblogic_connection(admin_url, username, password, rest_version)`：呼叫 `GET {adminUrl}/management/weblogic/{version}/domainRuntime?links=none&fields=name`，帶入 `Authorization: Basic ...`、`Accept: application/json`、`X-Requested-By: weblogic-redeploy-tool`，回傳連線結果與診斷提示（401/403/404）。
  - `run_weblogic_redeploy(...)`：
    - 組裝 Redeploy URL 與 Request Body（支援自訂 JSON 或依 `targets` 自動生成 `{"targets": [...]}` / `{}`）。
    - 發送 `POST` 請求並記錄每一步台灣時間戳日誌（`[{"time": "...", "level": "info|ok|err|dim", "message": "..."}]`）。
    - 解析回應中之 `state` / `progress` 及 `links` 內的轮詢 URL（`job|progress` 或 `deploymentProgressObjects|/jobs?/`），每 2 秒輪詢一次直到完成或超過 `timeoutSeconds`。

### 2.3 設定管理與匯出匯入 (`app/storage.py`)
- 更新 `ConfigStore`：
  - 保存 `servers.json` 與 `deployments.json` 時寫入 `schemaVersion: 2`，讀取時相容 `1` 與 `2`。
  - `list_servers_public()` 回傳 `weblogic` 公開資訊（含 `configured`、`adminUrl`、`username`、`hasPassword`、`restVersion`，不洩漏 `passwordBase64`）。
  - `create_server()` / `update_server()` 支援寫入或保留 WebLogic 密碼與設定。
  - 新增 `export_config()`：匯出包含 `servers` 與 `deploymentProfiles` 的整合 JSON。
  - 新增 `import_config(payload, mode)`：支援 `overwrite` 與 `merge` 模式，驗證通過後原子寫入 `servers.json` 與 `deployments.json` 並更新記憶體快取。

### 2.4 背景工作與台灣時區格式 (`app/job_manager.py`)
- 新增 `tw_now_str()`：使用 `timezone(timedelta(hours=8))` 產生 `YYYY-MM-DD HH:MM:SS` 格式字串。
- 更新 `generate_job_id()`：使用台灣時區時間戳前綴。
- 更新 `_run_deployment_worker()`：
  - 當 SFTP 階段結束後，檢查 `profile["weblogicRedeploy"]["enabled"]`：
    - 若未啟用：維持既有邏輯。
    - 若已啟用但 `successCount < totalCount`：將 `job["weblogicRedeploy"]["status"]` 設為 `"SKIPPED"`，不執行重啟。
    - 若已啟用且 `successCount == totalCount`：將 `job["weblogicRedeploy"]["status"]` 設為 `"RUNNING"`，呼叫 `weblogic_client.run_weblogic_redeploy()`，並透過 callback 即時更新 `job["weblogicRedeploy"]["logs"]` 並寫入工作紀錄檔，讓前端輪詢時可即時看到進度日誌。
    - 若 WebLogic Redeploy 成功，整體工作狀態為 `SUCCEEDED`；若失敗，整體工作狀態為 `PARTIALLY_SUCCEEDED`。

### 2.5 API 端點與前端介面 (`app/api.py`, `app/static/*`)
- **`app/api.py`**：
  - 新增 `POST /api/servers/{server_id}/test-weblogic`
  - 新增 `GET /api/config/export`
  - 新增 `POST /api/config/import`
- **`app/static/index.html`, `styles.css`, `app.js`**：
  - 修正簡體字為繁體中文。
  - 調整 Server 表格欄寬與 `.status-pill { white-space: nowrap; }`。
  - 新增頂部「匯出設定」與「匯入設定」按鈕及匯入對話框（Modal）。
  - Server 表單新增 WebLogic 設定區塊；Server 列表新增「測試 WebLogic」按鈕與 WebLogic 狀態顯示。
  - 佈署設定表單新增 WebLogic Redeploy 設定區塊（含進階設定 `<details>`）。
  - 確認佈署 Modal、即時進度面板與歷史明細新增 WebLogic Redeploy 狀態與黑底終端機日誌檢視區。

---

## 3. 驗證與測試計畫

1. **自動化測試 (`tests/test_acceptance.py`)**：
   - 驗證舊版 `v1.0.0` 全部 28 項驗收測試持續通過。
   - 新增 `v1.1.0` 驗收測試（`AC-29` ～ `AC-37`）：包含台灣時區時間格式檢查、`schemaVersion: 1` 舊設定檔相容載入、匯出與匯入（`overwrite` / `merge`）、模擬 WebLogic REST Server 測試連線與 Redeploy 輪詢成功/失敗/因 SFTP 失敗自動略過等情境。
2. **打包驗證 (`python build_release.py`)**：
   - 重新打包獨立執行檔 `release/SftpDeployTool/SftpDeployTool.exe` 並確認運作正常。
