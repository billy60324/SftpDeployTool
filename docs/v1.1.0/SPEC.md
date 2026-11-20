# Windows 本機 SFTP 檔案上傳工具 SPEC (v1.1.0)

## 1. 文件資訊

| 項目 | 內容 |
| :-: | :-: |
| 文件版本 | v1.1.0 |
| 系統名稱 | Windows 本機 SFTP 檔案上傳工具 |
| 適用平台 | Windows 10 x64 |
| 遠端平台 | Linux（SFTP） / WebLogic Server（Management RESTful API） |
| 操作介面 | 本機瀏覽器 HTML UI |
| 傳輸與控制協定 | SFTP、HTTP/HTTPS（WebLogic REST API） |
| 設定保存格式 | JSON |
| 系統定位 | 測試與內部環境使用的手動觸發檔案上傳與 WebLogic AP 重啟工具 |

### 1.1 版本迭代摘要（v1.0.0 → v1.1.0）

本文件以 `v1.0.0` 為基礎進行擴充與優化，主要變更項目如下：

1. **正體中文介面修正**：全面修正 UI 與文件內殘留之簡體字（如「觸發佈署与進度」改為「觸發佈署與進度」、「執行時間与錯誤資訊」改為「執行時間與錯誤資訊」）。
2. **Server 設定管理表格版面優化**：調整「帳號 / 密碼狀態」等欄位寬度與狀態標籤（`.status-pill`）不換行樣式（`white-space: nowrap`），解決文字頻繁折行影響閱讀的問題。
3. **日誌與歷史紀錄時區與時間格式調整**：所有 LOG 與歷史紀錄時間改用**台灣時區（UTC+8）**，時間字串格式統一為 **`yyyy-mm-dd hh24:mi:ss`**（即 `YYYY-MM-DD HH:MM:SS`，24 小時制）。
4. **設定檔匯出與匯入功能**：支援將 `servers.json` 與 `deployments.json` 整合匯出為單一 JSON 設定備份檔（含 Base64 編碼密碼），並支援以「整批覆蓋」或「合併（同 ID 更新、新 ID 新增）」模式匯入。
5. **WebLogic AP 自動重新啟動（Redeploy）功能**：
   - 參考 `weblogic-redeploy.html` 之運作機制，於 **Server 設定**中集中設定 WebLogic Admin Server 連線資訊（Admin URL、帳號、Base64 密碼、REST 版本），並提供「測試 WebLogic 連線」功能。
   - 於 **佈署設定（Deployment Profile）**中新增「SFTP 全部上傳成功後觸發 WebLogic Redeploy」開關，以及 AP 名稱（`appDeployments` 名稱）、Targets 與進階設定（等待逾時秒數、API 路徑、自訂 Request Body JSON）。
   - 當該佈署設定的所有檔案皆透過 SFTP 上傳成功時，後端自動透過 WebLogic Management RESTful API 發送 Redeploy 請求並輪詢進度，將執行結果與詳細日誌一併記錄於工作紀錄中。
6. **設定檔與紀錄檔向下相容（Backward Compatibility）**：
   - 新版程式完全相容舊版（`schemaVersion: 1`）的 `appsettings.json`、`servers.json`、`deployments.json` 與 `logs/deployments/*.json` 歷史紀錄。
   - 載入舊版設定檔時，若缺少 `v1.1.0` 新增之欄位（如 WebLogic 相關欄位），後端自動補齊安全預設值（預設不啟用 WebLogic），無須人工修改或遷移舊檔。

-----

## 2. 目標與功能範圍

### 2.1 系統目標

提供不需額外安裝語言執行環境的 Windows EXE，讓使用者：

1. 啟動本機後端服務。
2. 透過 HTML UI 管理 Server（含 SFTP 與選填之 WebLogic Admin 連線設定）與佈署設定（含檔案對應與選填之 WebLogic AP 重啟設定）。
3. 匯出完整設定檔備份，或從備份檔匯入設定。
4. 確認佈署內容後觸發上傳（並在全部檔案上傳成功後，依設定自動觸發 WebLogic AP Redeploy）。
5. 查看各檔案上傳進度、WebLogic Redeploy 執行日誌與結果。
6. 查詢歷史佈署紀錄。

### 2.2 v1.1.0 包含功能

- 多組 Server 設定管理（含 SFTP 連線資訊與 WebLogic Admin REST 連線資訊）。
- 多組佈署設定管理（含檔案對應清單與上傳後觸發 WebLogic Redeploy 設定）。
- 多組佈署設定共用同一份 Server 資訊。
- 每組佈署設定對應一台 Server。
- 每組佈署設定包含多筆檔案對應。
- 本機 HTTP Port 設定。
- 各 Server 的 SFTP Port 設定。
- SFTP 帳號密碼登入、WebLogic Admin 帳號密碼認證（皆以 Base64 編碼保存）。
- SFTP 連線測試與 WebLogic REST API 連線測試。
- 設定檔整合匯出與匯入（支援「覆蓋現有設定」與「合併設定」兩種模式）。
- 逐檔上傳與直接覆蓋；個別檔案失敗後繼續處理其他檔案。
- 所有檔案上傳成功後，若啟用 WebLogic Redeploy，自動呼叫 WebLogic REST API 重啟指定 AP 並輪詢 Job 狀態。
- 工作進度、WebLogic 執行日誌及結果顯示。
- 歷史紀錄保存及查詢（採用台灣時區 `yyyy-mm-dd hh24:mi:ss` 格式）。
- 舊版（`schemaVersion: 1`）與新版（`schemaVersion: 2`）設定檔及歷史紀錄向下相容。

-----

## 3. 執行環境與打包要求

| 項目 | 規格 |
| :-: | :-: |
| 本機 OS | Windows 10 x64 |
| 遠端 OS / 服務 | Linux (SFTP) / Oracle WebLogic Server (RESTful Management Services) |
| 程式形式 | EXE |
| 語言 Runtime | 使用者不需額外安裝 |
| 第三方相依性 | 必要相依性須隨交付包提供或內嵌 |
| 瀏覽器 | 支援現代瀏覽器，例如 Edge、Chrome |
| HTTP 監聽位址 | 固定 127.0.0.1 |
| HTTP Port | 由 appsettings.json 設定 |
| 時區與時間格式 | 台灣時區 (`UTC+8`)，格式 `yyyy-mm-dd hh24:mi:ss` |

- 採用 Python（FastAPI + Uvicorn + Paramiko + 標準庫 `urllib.request`）實作後端，並使用 PyInstaller 打包為自包含（Standalone）Windows x64 執行檔（EXE）。
- WebLogic REST API 呼叫統一由**後端 Python 服務**發起，不從瀏覽器端直接 `fetch` WebLogic Admin Server，徹底避免瀏覽器跨來源（CORS）preflight 被拒或原生 401 Basic Auth 彈窗問題。

-----

## 4. 設定資料模型與向下相容規範

### 4.1 Schema 版本與向下相容原則

- `v1.0.0` 建立之設定檔與紀錄檔為 `schemaVersion: 1`；`v1.1.0` 新增欄位後支援 `schemaVersion: 1` 與 `schemaVersion: 2`（保存時統一寫入最新 `schemaVersion: 2`，`appsettings.json` 結構未變更仍可接受 `1` 或 `2`）。
- **向下相容讀取規則**：
  1. 當讀取 `schemaVersion: 1` 或 `2` 的 `servers.json` 時，若單筆 Server 缺少 `weblogic` 欄位（或相關子欄位），自動補齊預設未設定之結構。
  2. 當讀取 `schemaVersion: 1` 或 `2` 的 `deployments.json` 時，若單筆 Profile 缺少 `weblogicRedeploy` 欄位，自動補齊預設 `enabled: false` 之結構。
  3. 當讀取舊版 `logs/deployments/*.json` 時，若缺少 `weblogicRedeploy` 欄位，預設視為該次歷史工作未啟用 WebLogic Redeploy；若時間欄位為舊版 ISO 8601 UTC 字串（如 `2026-10-19T08:30:00Z`），前端與後端皆可正常讀取與顯示。
  4. 若 `schemaVersion` 非整數或不在支援清單 `{1, 2}` 內，仍依規定報錯 `CONFIG_INVALID` 並拒絕覆蓋損毀檔案。

-----

## 5. Server 設定（含 WebLogic 連線設定）

### 5.1 檔案：`servers.json`

```json
{
  "schemaVersion": 2,
  "servers": [
    {
      "id": "server-test-01",
      "name": "測試 Server 01",
      "host": "192.168.10.50",
      "port": 22,
      "username": "deploy",
      "passwordBase64": "dGVzdC1wYXNzd29yZA==",
      "weblogic": {
        "adminUrl": "http://fa5edapb:7001",
        "username": "system",
        "passwordBase64": "aWFtdGVzdGluZzEyMw==",
        "restVersion": "12.2.1.3.0"
      }
    }
  ]
}
```

### 5.2 Server 欄位定義

| 欄位 | 型態 | 必填 | 規則 |
| :-: | :-: | :-: | :-: |
| id | String | 是 | 唯一且固定 |
| name | String | 是 | 非空白顯示名稱 |
| host | String | 是 | SFTP Hostname 或 IP，不含協定、Port、路徑 |
| port | Integer | 是 | SFTP Port，預設 22，範圍 1–65535 |
| username | String | 是 | SFTP 非空白登入帳號 |
| passwordBase64 | String | 是 | SFTP UTF-8 密碼的有效 Base64 字串 |
| weblogic | Object | 否 | WebLogic Admin 連線設定物件（舊版設定檔無此欄時自動補預設空值） |
| weblogic.adminUrl | String | 選填 | WebLogic Admin Server URL（例如 `http://fa5edapb:7001` 或 `https://192.168.10.50:7002`），須以 `http://` 或 `https://` 開頭，保存時自動去除尾部多餘的 `/`；若該 Server 不需使用 WebLogic 可留空字串 `""` |
| weblogic.username | String | 選填 | WebLogic 登入帳號；當 `adminUrl` 有填寫時為必填 |
| weblogic.passwordBase64 | String | 選填 | WebLogic 密碼之 UTF-8 Base64 字串；當新增 WebLogic 設定（`adminUrl` 非空）時必填；若未設定則為 `""` |
| weblogic.restVersion | String | 選填 | WebLogic REST 版本，預設為 `"12.2.1.3.0"`（可填 `"12.2.1.3.0"` 或 `"latest"`） |

### 5.3 Server API 與密碼保護規則

- 查詢 Server API（`GET /api/servers`）絕不回傳 `passwordBase64` 或 `weblogic.passwordBase64` 原始值，而是回傳：
  ```json
  {
    "id": "server-test-01",
    "name": "測試 Server 01",
    "host": "192.168.10.50",
    "port": 22,
    "username": "deploy",
    "hasPassword": true,
    "weblogic": {
      "configured": true,
      "adminUrl": "http://fa5edapb:7001",
      "username": "system",
      "hasPassword": true,
      "restVersion": "12.2.1.3.0"
    },
    "referencedBy": []
  }
  ```
- 修改 Server 時，若 `weblogic.password` 未傳入（或前端留空），則保留既有 `weblogic.passwordBase64`；若傳入非空字串則更新密碼。
- **WebLogic 連線測試 API（`POST /api/servers/{id}/test-weblogic`）**：
  - 後端使用該 Server 已保存（或測試請求）之 WebLogic 連線資訊，向 `{adminUrl}/management/weblogic/{restVersion}/domainRuntime?links=none&fields=name` 發送 `GET` 請求（帶入 `Authorization: Basic ...`、`Accept: application/json`、`X-Requested-By: weblogic-redeploy-tool`）。
  - 若 HTTP 2xx 則回傳連線與帳密驗證成功（並附帶 Domain 名稱）；若 401/403/404 或連線逾時則回傳對應中文診斷訊息。

-----

## 6. 佈署設定（含 WebLogic Redeploy 設定）

### 6.1 檔案：`deployments.json`

```json
{
  "schemaVersion": 2,
  "deploymentProfiles": [
    {
      "id": "profile-test-app",
      "name": "Test App 佈署",
      "serverId": "server-test-01",
      "files": [
        {
          "id": "file-01",
          "localPath": "C:\\Build\\PMCForcastSystemEAR.ear",
          "remotePath": "/opt/weblogic/upload/PMCForcastSystemEAR.ear"
        }
      ],
      "weblogicRedeploy": {
        "enabled": true,
        "appName": "PMCForcastSystemEAR_7015",
        "targets": "",
        "timeoutSeconds": 300,
        "redeployPath": "/management/weblogic/{version}/edit/appDeployments/{app}/redeploy",
        "requestBodyJson": ""
      }
    }
  ]
}
```

### 6.2 `weblogicRedeploy` 欄位定義

| 欄位 | 型態 | 必填 | 規則與預設值 |
| :-: | :-: | :-: | :-: |
| enabled | Boolean | 是 | 是否於 SFTP 全部上傳成功後觸發 WebLogic Redeploy，預設 `false` |
| appName | String | 條件必填 | WebLogic `appDeployments` 的 AP 名稱（如 `PMCForcastSystemEAR_7015`）；當 `enabled = true` 時為必填非空白 |
| targets | String | 選填 | 逗號分隔之目標名稱（如 `AdminServer,Cluster1`）；留空表示沿用現有 targets（送出 `{}`），預設 `""` |
| timeoutSeconds | Integer | 是 | 輪詢等待逾時秒數，預設 `300`，範圍 `10`–`3600` |
| redeployPath | String | 是 | Redeploy API 路徑，預設 `"/management/weblogic/{version}/edit/appDeployments/{app}/redeploy"`（執行時自動將 `{version}` 與 `{app}` 做 URL encode 取代） |
| requestBodyJson | String | 選填 | 自訂 Request Body JSON 字串；留空時依 `targets` 自動產生（有 targets 則送 `{"targets":[...]}`，無則送 `{}`）；若有填寫則保存時必須驗證為合法 JSON 格式 |

**保存驗證規則**：
- 當 `weblogicRedeploy.enabled = true` 時：
  1. `appName` 必填。
  2. 所選定的 `serverId` 必須已在 Server 設定中配置好有效的 WebLogic 連線資訊（`adminUrl`、`username` 與密碼皆已設定），否則提示使用者先至該 Server 完成 WebLogic 設定。
  3. 若 `requestBodyJson` 非空白，必須為合法的 JSON 字串。

-----

## 7. WebLogic Redeploy 執行流程與狀態定義

### 7.1 觸發條件與執行步驟

當佈署工作的 `weblogicRedeploy.enabled == true` 時：

1. **前置條件檢查（僅在所有檔案上傳成功時觸發）**：
   - 若 SFTP 檔案上傳結果中**有任何一筆檔案失敗（`failedCount > 0`）**，則**不觸發** WebLogic Redeploy，並於工作的 `weblogicRedeploy` 結果區塊標記為 `SKIPPED`（原因：「因部分或全部檔案 SFTP 上傳失敗，已略過 WebLogic Redeploy」）。
   - 唯有當**所有檔案皆上傳成功（`successCount == totalCount`）**時，工作保持 `RUNNING` 狀態，繼續執行 WebLogic Redeploy。
2. **發送 Redeploy 請求**：
   - 後端組裝 URL：`{adminUrl}{redeployPath}`（替換 `{version}` 與 `{app}`）。
   - 組裝 Request Body：若 `requestBodyJson` 非空則使用之，否則若 `targets` 以逗號拆解後非空則送 `{"targets": [...]}`，否則送 `{}`。
   - 發送 `POST` 請求，Headers 包含：
     - `Authorization: Basic <base64(user:pwd)>`
     - `Accept: application/json`
     - `Content-Type: application/json`
     - `X-Requested-By: weblogic-redeploy-tool`
3. **解析回應與輪詢進度（Polling Job Progress）**：
   - 若 POST 回應非 2xx：記錄 HTTP 狀態碼、回應內容與對應提示（401 帳密錯誤、403 權限不足、404 路徑或 AP 名稱錯誤），標記 WebLogic Redeploy 為 `FAILED`。
   - 若 POST 回應 2xx：
     - 檢查回應 JSON 的 `state` / `progress` 是否已完成（`COMPLETED` / `SUCCESS` 為成功，`FAILED` 為失敗）。
     - 若尚未完成，從 `json.links` 尋找 `rel` 含 `job|progress` 或 `href` 含 `deploymentProgressObjects|/jobs?/` 的連結。
     - 若找到輪詢連結，每 **2 秒**發送一次 `GET` 請求查詢進度，直到狀態完成或超過 `timeoutSeconds`。
     - 若未找到輪詢連結且 POST 為 2xx，視為請求已成功送出並標記為 `SUCCEEDED`（附帶提示請至 WebLogic Console 確認）。
4. **執行日誌紀錄**：
   - Redeploy 過程中的每一步（開始、請求 Method/URL、Request Body、HTTP 狀態碼、輪詢 URL、最終結果）皆附帶台灣時間戳記寫入工作紀錄的 `weblogicRedeploy.logs` 陣列中，前端可即時顯示如同 `weblogic-redeploy.html` 的執行日誌。

### 7.2 整體工作狀態判定（含 WebLogic）

| SFTP 檔案結果 | WebLogic Redeploy 設定與結果 | 最終整體工作狀態 (`job.status`) |
| :-: | :-: | :-: |
| 全部成功 (`successCount == totalCount`) | 未啟用 (`enabled = false`) | `SUCCEEDED` |
| 全部成功 (`successCount == totalCount`) | 啟用且 Redeploy 成功 (`SUCCEEDED`) | `SUCCEEDED` |
| 全部成功 (`successCount == totalCount`) | 啟用但 Redeploy 失敗 (`FAILED`) | `PARTIALLY_SUCCEEDED`（檔案上傳成功，但 AP 重啟失敗） |
| 部分成功 (`0 < successCount < totalCount`) | 任一（啟用時自動 `SKIPPED`） | `PARTIALLY_SUCCEEDED` |
| 全部失敗 (`successCount == 0`) | 任一（啟用時自動 `SKIPPED`） | `FAILED` |

-----

## 8. 設定檔匯出與匯入規格

### 8.1 匯出功能 (`GET /api/config/export`)

- 將目前的 `servers`（包含 `passwordBase64` 與 `weblogic.passwordBase64`）與 `deploymentProfiles` 整合打包為單一 JSON 結構：
  ```json
  {
    "exportType": "SftpDeployToolConfig",
    "schemaVersion": 2,
    "exportedAt": "2026-11-14 01:00:00",
    "servers": [ ... ],
    "deploymentProfiles": [ ... ]
  }
  ```
- 前端提供「匯出設定檔」按鈕，觸發瀏覽器下載檔案，預設檔名為 `sftp-deploy-config-YYYYMMDD-HHMMSS.json`。

### 8.2 匯入功能 (`POST /api/config/import`)

- 請求格式：
  ```json
  {
    "mode": "overwrite",
    "data": { ... }
  }
  ```
- 支援兩種匯入模式 (`mode`)：
  1. **`overwrite`（整批覆蓋現有設定）**：以匯入檔內的 `servers` 與 `deploymentProfiles` 完整替換現有設定。
  2. **`merge`（合併設定）**：
     - 針對 `servers`：若匯入的 Server `id` 已存在則更新該筆 Server，若不存在則新增；現有未出現在匯入檔中的 Server 予以保留。
     - 針對 `deploymentProfiles`：若匯入的 Profile `id` 已存在則更新該筆 Profile，若不存在則新增；現有未出現在匯入檔中的 Profile 予以保留。
- **驗證與向下相容**：
  - 匯入支援 `schemaVersion: 1` 或 `2` 的整合匯出檔。
  - 匯入前完整執行 Server 格式驗證、Base64 密碼有效性驗證、Profile 路徑規則驗證以及 `serverId` 引用完整性驗證；若有任何一項不合法，整批拒絕匯入並回傳明確錯誤訊息，不破壞現有設定檔。
  - 若目前有佈署工作正在執行（`QUEUED` 或 `RUNNING`），拒絕匯入並回傳 `409 DEPLOYMENT_ALREADY_RUNNING`。
  - 寫入時同樣遵循原子寫入與 `.bak` 備份規則。

-----

## 9. 時區與時間格式規範

- 所有工作紀錄（`logs/deployments/<jobId>.json`）的時間欄位（`createdAt`、`startedAt`、`finishedAt`、各檔案之 `startedAt`/`finishedAt`、WebLogic 日誌時間）與匯出檔之 `exportedAt`，一律採用**台灣時區（UTC+8）**。
- 時間字串格式固定為 **`yyyy-mm-dd hh24:mi:ss`**（Python `strftime("%Y-%m-%d %H:%M:%S")`，例如 `2026-11-14 09:30:15`）。
- `jobId` 前綴時間戳同步改用台灣時區 `job-YYYYMMDDTHHMMSSffffff-xxxxxx`，確保檔案名稱字典序與台灣時間完全一致。

-----

## 10. 前端 UI 調整項目

1. **繁體中文修正**：
   - 頂部導覽頁籤 `1. 觸發佈署与進度` → `1. 觸發佈署與進度`。
   - 歷史明細表頭 `執行時間与錯誤資訊` → `執行時間與錯誤資訊`。
2. **Server 設定管理介面優化**：
   - 調整 Server 表格欄寬比例，將「帳號 / 密碼狀態」欄位設定足夠最小寬度（如 `min-width: 150px`），並為 `.status-pill` 加上 `white-space: nowrap;`，避免狀態標籤文字換行。
   - 於 Server 表單新增可收折或分區的「**WebLogic Admin 連線設定（選填）**」區塊（Admin Server URL、WebLogic 帳號、WebLogic 密碼、REST 版本）。
   - 於 Server 清單操作欄新增「**測試 WebLogic**」按鈕（當該 Server 有設定 WebLogic 時顯示/啟用）。
   - 於頁面頂部導覽列（或設定管理區）提供「**匯出設定**」與「**匯入設定**」按鈕與匯入彈窗（可選擇檔案並勾選「合併」或「整批覆蓋」模式）。
3. **佈署設定管理介面擴充**：
   - 於佈署設定表單中新增「**SFTP 全部上傳成功後觸發 WebLogic Redeploy**」勾選開關。
   - 勾選後展開設定欄位：
     - `AP 名稱 (appDeployments 名稱)`（必填）
     - `Targets`（選填，逗號分隔）
     - 進階設定（`<details>` 收折）：`等待逾時（秒）`（預設 300）、`Redeploy API 路徑`、`Request Body (JSON)`。
4. **佈署確認彈窗、即時進度與歷史明細擴充**：
   - 確認佈署彈窗中，若該 Profile 有啟用 WebLogic Redeploy，明確顯示將連帶重啟的 WebLogic Admin URL 與 AP 名稱警告。
   - 即時進度面板與歷史工作明細中，若該工作有啟用 WebLogic Redeploy，顯示 WebLogic Redeploy 狀態標籤與黑底終端機風格的執行日誌區塊（如同 `weblogic-redeploy.html` 的日誌視窗）。
   - 歷史佈署紀錄表格欄位「建立時間 (UTC)」改為「建立時間」。

-----

## 11. 新增與調整之 API 清單

| Method | Path | 用途 |
| :-: | :-: | :-: |
| POST | `/api/servers/{id}/test-weblogic` | 測試已保存 Server 的 WebLogic Admin REST API 連線與帳密驗證 |
| GET | `/api/config/export` | 匯出整合設定檔（包含 `servers` 與 `deploymentProfiles`） |
| POST | `/api/config/import` | 匯入整合設定檔（支援 `mode: "overwrite" \| "merge"`） |

其餘既有 API（`/api/settings`、`/api/servers`、`/api/profiles`、`/api/deployments` 等）路徑保持不變，僅於 Request/Response JSON 中擴充 `weblogic` 與 `weblogicRedeploy` 欄位，並對舊版輸入保持向下相容。

-----

## 12. v1.1.0 驗收條件

除保留 `v1.0.0` 既有驗收條件外，新增與更新以下驗收條件：

| 編號 | 驗收條件 |
| :-: | :-: |
| AC-29 | UI 頁籤與所有介面文字均為繁體中文（無殘留簡體「与」字）。 |
| AC-30 | Server 清單的「帳號 / 密碼狀態」欄位與狀態膠囊標籤不會因寬度不足而異常折行。 |
| AC-31 | 新產生的工作紀錄與日誌時間皆為台灣時區 (`UTC+8`)，且格式為 `yyyy-mm-dd hh24:mi:ss`。 |
| AC-32 | 可匯出單一整合設定 JSON 檔，並支援以「整批覆蓋」或「合併」模式匯入還原。 |
| AC-33 | Server 可配置 WebLogic Admin 連線資訊並執行 WebLogic REST 連線測試。 |
| AC-34 | 佈署設定啟用 WebLogic Redeploy 時，當所有檔案 SFTP 上傳成功後會自動呼叫 WebLogic Redeploy API 並輪詢結果、記錄日誌。 |
| AC-35 | 佈署設定啟用 WebLogic Redeploy 時，若任一檔案 SFTP 上傳失敗，自動略過 (`SKIPPED`) WebLogic Redeploy，不重啟 AP。 |
| AC-36 | 若 SFTP 檔案全數成功但 WebLogic Redeploy 失敗，整體工作狀態標記為 `PARTIALLY_SUCCEEDED` 並完整保留失敗原因與日誌。 |
| AC-37 | 新版程式可直接載入 `v1.0.0` (`schemaVersion: 1`) 的 `appsettings.json`、`servers.json`、`deployments.json` 與舊版歷史紀錄檔而不報錯。 |
