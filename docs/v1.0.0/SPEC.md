# Windows 本機 SFTP 檔案上傳工具 SPEC

## 1. 文件資訊

| 項目 | 內容 |
| :-: | :-: |
| 文件版本 | v1.0 |
| 系統名稱 | Windows 本機 SFTP 檔案上傳工具 |
| 適用平台 | Windows 10 x64 |
| 遠端平台 | Linux |
| 操作介面 | 本機瀏覽器 HTML UI |
| 傳輸協定 | SFTP |
| 設定保存格式 | JSON |
| 系統定位 | 測試環境使用的手動觸發檔案上傳工具 |

### 1.1 規格說明

本文件整合已確認的需求，並補充必要的實作規則。

- 已確認需求：平台、設定關係、帳密保存方式、上傳與覆蓋政策等。
- 補充實作預設：本機服務預設 Port、單一工作限制、API 格式、逾時及工作復原規則等。
- 非第一版必要功能：於文件末尾另列，不視為本次開發範圍。

本文件中的「佈署」僅表示：

將指定的本機檔案透過 SFTP 上傳至遠端指定檔案位置，必要時覆蓋既有檔案。

不代表應用程式安裝完成或服務已載入新檔案。

-----

## 2. 目標與功能範圍

### 2.1 系統目標

提供不需額外安裝語言執行環境的 Windows EXE，讓使用者：

1. 啟動本機後端服務。
2. 透過 HTML UI 管理 Server 與佈署設定。
3. 確認佈署內容後觸發上傳。
4. 查看各檔案的執行進度與結果。
5. 查詢歷史佈署紀錄。

### 2.2 第一版包含功能

- 多組 Server 設定管理。
- 多組佈署設定管理。
- 多組佈署設定共用同一份 Server 資訊。
- 每組佈署設定對應一台 Server。
- 每組佈署設定包含多筆檔案對應。
- 本機 HTTP Port 設定。
- 各 Server 的 SFTP Port 設定。
- SFTP 帳號密碼登入。
- 密碼以 Base64 編碼保存。
- SFTP 連線測試。
- 逐檔上傳與直接覆蓋。
- 個別檔案失敗後繼續處理其他檔案。
- 工作進度及結果顯示。
- 歷史紀錄保存及查詢。

### 2.3 第一版不包含功能

- 其他電腦連入 UI。
- 多人操作與使用者權限管理。
- 瀏覽器選檔、拖曳或來源檔案上傳。
- 資料夾遞迴上傳。
- 萬用字元批次展開。
- 排程及 CI/CD 整合。
- SSH 私鑰認證。
- 遠端目錄自動建立。
- 自動重試失敗檔案。
- 遠端檔案備份。
- 失敗回復或整批交易式佈署。
- 暫存檔上傳後替換目標檔案。
- 遠端指令執行。
- 打包、解壓縮、服務重啟及健康檢查。
- 檔案內容雜湊比對。
- 自動清除歷史紀錄。

-----

## 3. 執行環境

| 項目 | 規格 |
| :-: | :-: |
| 本機 OS | Windows 10 x64 |
| 遠端 OS | Linux |
| 程式形式 | EXE |
| 語言 Runtime | 使用者不需額外安裝 |
| 第三方相依性 | 必要相依性須隨交付包提供或內嵌 |
| 瀏覽器 | 支援現代瀏覽器，例如 Edge、Chrome |
| HTTP 監聽位址 | 固定 127.0.0.1 |
| HTTP Port | 由 appsettings.json 設定 |
| SFTP Port | 由各 Server 設定決定 |
| 本機檔案存取權限 | 使用啟動 EXE 的 Windows 使用者權限 |

### 3.1 打包要求

- 不要求使用者自行安裝 Python、Node.js、Java、.NET Runtime 等語言執行環境。
- 採用 Python（FastAPI + Uvicorn + Paramiko）實作後端，並使用 PyInstaller 打包為自包含（Standalone）Windows x64 執行檔（EXE）。
- HTML、JavaScript、CSS、字型或圖示等所有前端 UI 資源須完全內嵌或隨交付包提供，採原生 JS / 內建靜態資源方式實作，無須 Node.js 執行環境。
- UI 完全不依賴任何外部 CDN 或外網資源，確保於封閉內網環境可直接離線運行。
- 不要求以系統管理員權限執行。

-----

## 4. 系統架構

```text
使用者啟動 EXE
│
▼
讀取 JSON 設定、檢查目錄權限
│
▼
啟動 127.0.0.1 HTTP 服務
│
▼
開啟瀏覽器 HTML UI
│
├─ Server 設定管理
├─ 佈署設定管理
├─ 確認並觸發佈署
└─ 查看進度與歷史紀錄
│
▼
後端背景工作
│
├─ 讀取 Windows 本機檔案
├─ 連線 Linux SFTP Server
├─ 逐檔上傳
└─ 保存結果
```

### 4.1 職責分工

#### 前端

- 顯示與編輯設定。
- 執行基本欄位驗證。
- 顯示佈署確認畫面。
- 呼叫後端 API。
- 顯示進度、結果及歷史紀錄。

#### 後端

- 驗證設定內容。
- 讀寫 JSON。
- 處理密碼 Base64 編碼及解碼。
- 存取本機來源檔案。
- 執行 SFTP。
- 管理背景工作及並行限制。
- 保存工作紀錄。
- 提供 API 與 UI 靜態資源。

來源檔案內容不經由前端傳送。

-----

## 5. 目錄結構

```text
SftpDeployTool/
├─ SftpDeployTool.exe
├─ _internal/              # PyInstaller 隨包附帶之 Runtime、相依套件與內嵌前端資源
├─ appsettings.json
├─ servers.json
├─ deployments.json
└─ logs/
   └─ deployments/
      ├─ <jobId>.json
      └─ ...
```

### 5.1 路徑規則

- 設定檔直接存放於 EXE 同層目錄。
- 紀錄存放於 EXE 同層的 logs/deployments。
- 目錄基準為 EXE 所在位置（PyInstaller 打包環境下以 `sys.executable` 所在目錄為準），不是目前工作目錄（`os.getcwd()`）或內部資源目錄（`sys._MEIPASS` / `_internal`）。
- 內嵌之 UI 靜態資源（HTML/CSS/JS）則由打包資源目錄（`sys._MEIPASS`，即 `_internal`）讀取。
- 不因捷徑的「開始位置」改變設定檔位置。

### 5.2 首次啟動

缺少設定檔時建立：

- appsettings.json：包含預設 HTTP Port。
- servers.json：空 Server 清單。
- deployments.json：空佈署設定清單。
- logs/deployments：歷史紀錄目錄。

既有檔案若格式損毀，不得視為不存在而直接覆蓋。

### 5.3 寫入規則

- JSON 使用 UTF-8。
- 設定保存採「寫入暫存檔 → 完成寫入 → 替換原檔」。
- 設定寫入失敗時，不回報保存成功。
- 不默默改存至其他資料夾。
- 建議保留上一版設定為 .bak；備份亦可能包含可還原的帳密。

-----

## 6. 設定資料模型

### 6.1 關聯方式

採用集中管理 Server 的方式：

```text
Server
1
│
└── N 個 Deployment Profile
    │
    └── N 個 File Mapping
```

- 一個佈署設定只引用一個 Server。
- 多個佈署設定可引用同一個 Server。
- 佈署設定不重複保存 Host、Port、帳號、密碼。

### 6.2 ID 規則

- Server、佈署設定、檔案對應均具有 ID。
- ID 由系統產生。
- Server ID 在 Server 清單內唯一。
- 佈署設定 ID 在佈署設定清單內唯一。
- 檔案對應 ID 在所屬佈署設定內唯一。
- 修改名稱不得改變 ID。
- 第一版不提供 UI 修改 ID 功能。

-----

## 7. 本機服務設定

### 7.1 檔案：appsettings.json

```json
{
  "schemaVersion": 1,
  "httpPort": 18080
}
```

| 欄位 | 型態 | 必填 | 說明 |
| :-: | :-: | :-: | :-: |
| schemaVersion | Integer | 是 | 目前為 1 |
| httpPort | Integer | 是 | 本機 HTTP Port，範圍 1–65535 |

### 7.2 行為

- 預設 Port 為 18080。
- 固定監聽 127.0.0.1，不提供外部監聽選項。
- UI 位址為 http://127.0.0.1:\<httpPort\>。
- 修改設定後須重新啟動 EXE。
- Port 被占用時，顯示錯誤並停止啟動。
- 不自動改用其他 Port。
- 第一版透過 JSON 修改 HTTP Port，不要求提供 UI 編輯功能。

### 7.3 Port 區分

| Port | 用途 |
| :-: | :-: |
| httpPort | 瀏覽器連線本機工具 |
| Server 的 port | 工具連線遠端 SFTP |

兩者互不影響。

-----

## 8. Server 設定

### 8.1 檔案：servers.json

```json
{
  "schemaVersion": 1,
  "servers": [
    {
      "id": "server-test-01",
      "name": "測試 Server 01",
      "host": "192.168.10.50",
      "port": 22,
      "username": "deploy",
      "passwordBase64": "dGVzdC1wYXNzd29yZA=="
    }
  ]
}
```

上例密碼為示範用的 test-password，不可作為實際環境預設密碼。

### 8.2 欄位定義

| 欄位 | 型態 | 必填 | 規則 |
| :-: | :-: | :-: | :-: |
| id | String | 是 | 唯一且固定 |
| name | String | 是 | 非空白顯示名稱 |
| host | String | 是 | Hostname 或 IP，不含協定、Port、路徑 |
| port | Integer | 是 | 預設 22，範圍 1–65535 |
| username | String | 是 | 非空白登入帳號 |
| passwordBase64 | String | 是 | UTF-8 密碼的有效 Base64 字串 |

補充規則：

- 建立 Server 時，未指定 Port 可由後端補為 22。
- 第一版不支援空密碼登入。
- 密碼不得自動去除前後空白。
- Base64 無法解碼或解碼後不是有效 UTF-8 時，視為設定錯誤。

### 8.3 密碼保存

```text
儲存：
原始密碼 → UTF-8 Bytes → Base64 → servers.json

登入：
servers.json → Base64 解碼 → UTF-8 密碼 → SFTP 認證
```

Base64 是編碼，不是加密。取得設定檔的人可以還原密碼。

依已確認需求，第一版接受此保存方式，不採用 DPAPI 或其他加密機制。

### 8.4 UI 與 API 密碼規則

- UI 輸入原始密碼，由後端負責編碼。
- 查詢 API 不回傳原始密碼或 passwordBase64。
- 查詢結果提供 hasPassword。
- 修改 Server 時：
  - 未傳入 password：保留原密碼。
  - 傳入非空密碼：替換原密碼。
  - 傳入空字串或 null：回報驗證錯誤。
- Log 不得包含原始密碼或 Base64 密碼。

### 8.5 引用與刪除

- 修改 Server 後，所有引用它的佈署設定於下次執行使用更新值。
- 正在執行的工作不受後續修改影響。
- Server 被引用時，拒絕刪除。
- 刪除拒絕訊息應列出引用它的佈署設定。

-----

## 9. 佈署設定

### 9.1 檔案：deployments.json

```json
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
          "remotePath": "/opt/test-app/App.dll"
        },
        {
          "id": "file-02",
          "localPath": "C:\\Build\\appsettings.Test.json",
          "remotePath": "/opt/test-app/appsettings.json"
        }
      ]
    }
  ]
}
```

### 9.2 佈署設定欄位

| 欄位 | 型態 | 必填 | 說明 |
| :-: | :-: | :-: | :-: |
| id | String | 是 | 唯一 ID |
| name | String | 是 | 非空白顯示名稱 |
| serverId | String | 是 | 引用已存在的 Server |
| files | Array | 是 | 至少一筆檔案對應 |

### 9.3 檔案對應欄位

| 欄位 | 型態 | 必填 | 說明 |
| :-: | :-: | :-: | :-: |
| id | String | 是 | 檔案對應 ID |
| localPath | String | 是 | Windows 本機絕對檔案路徑 |
| remotePath | String | 是 | Linux 絕對檔案路徑，包含目標檔名 |

### 9.4 路徑規則

- localPath 不支援相對路徑，必須為 Windows 絕對路徑（如 `C:\...` 或 UNC 路徑），且不得以 `\` 或 `/` 結尾。
- remotePath 必須以 `/` 開頭，且不得以 `/` 結尾。
- remotePath 禁止包含 `/../`、`/./`、反斜線 `\` 或空路徑區段（`//`），避免相對路徑穿越造成非預期覆蓋。
- 不解析萬用字元。
- 不執行環境變數替換。
- 不展開資料夾。
- 遠端路徑保留大小寫。
- 本機檔名與遠端檔名可以不同。
- 同一組設定不得有完全相同的 remotePath（依區分大小寫之字串完全比對）。
- 同一個本機檔案可對應不同遠端位置。
- 執行順序依 files 陣列順序。

第一版僅檢查目標路徑字串重複，不要求辨識遠端符號連結或不同路徑實際指向同一檔案的情況。

### 9.5 保存驗證與執行驗證

保存設定時檢查：

- 欄位型態。
- 必填欄位。
- ID 唯一性。
- Server 引用有效性。
- 路徑格式。
- 重複遠端路徑。

不因來源檔案目前不存在而拒絕保存設定。

來源檔案是否存在、是否可讀，以及遠端目錄是否存在，於實際執行時逐檔檢查。

-----

## 10. 程式啟動與生命週期

### 10.1 啟動流程

1. 確認同一工具目錄沒有其他實例執行（以 EXE 所在目錄路徑之 Hash 建立 Windows Named Mutex 或 `.lock` 鎖定檔；若已有實例執行，提示使用者並停止新實例啟動）。
2. 確認設定及紀錄目錄可讀寫。
3. 建立缺少的設定檔與目錄。
4. 驗證既有設定格式。
5. 載入設定。
6. 處理上一輪未正常結束的工作紀錄。
7. 啟動本機 HTTP 服務。
8. 開啟預設瀏覽器。

### 10.2 設定錯誤與啟動異常顯示

- appsettings.json 無效或 HTTP Port 被占用：停止啟動並顯示錯誤。
- servers.json 或 deployments.json 無效：顯示檔名與錯誤原因，停止正常啟動。
- 由於啟動失敗時 HTTP 服務尚未就緒，工具須透過 Console 視窗（並等待使用者按鍵結束，避免視窗一閃即逝）或 Windows 原生 `MessageBoxW` 對話框明確顯示中文錯誤原因與問題檔名。
- 不默默丟棄無效設定。
- 不直接覆蓋損毀檔案。

### 10.3 設定重新載入

- UI 保存成功後，立即更新後端記憶體設定。
- 使用文字編輯器直接修改 JSON 後，須重新啟動 EXE。
- 第一版不提供檔案監看或自動熱載入。

### 10.4 關閉行為與狀態恢復

- 關閉瀏覽器不會停止 EXE。
- 頁面重新整理或重新開啟時，前端可透過 `GET /api/settings`（或 `GET /api/deployments/active`）取得目前執行中的 `activeJobId`，自動恢復顯示執行中工作的進度畫面。
- 工具須提供明確的程式退出方式：於 HTML UI 提供「退出工具」按鈕（呼叫 `POST /api/shutdown`），並支援 Console 視窗 `Ctrl+C` 關閉。
- 正常退出時，若有工作執行中（`QUEUED` 或 `RUNNING`），`POST /api/shutdown` 回傳 `409 DEPLOYMENT_ALREADY_RUNNING`，於 UI 提示使用者等待工作完成後再退出。
- 第一版不提供工作取消功能。
- 強制結束 EXE 不保證遠端檔案完整。

-----

## 11. 前端功能

### 11.1 Server 管理

- 顯示 Server 清單。
- 新增、修改、刪除 Server。
- 編輯名稱、Host、Port、帳號、密碼。
- 顯示是否已設定密碼。
- 測試已保存的 Server 連線。
- 顯示被佈署設定引用的情況。

連線測試只驗證能否建立 SFTP 連線及完成認證，不保證所有目標路徑具有寫入權限。

### 11.2 佈署設定管理

- 顯示佈署設定清單。
- 新增、修改、刪除設定。
- 選擇一台 Server。
- 顯示選定 Server 的 Host、Port、帳號。
- 增加、修改、刪除檔案對應。
- 調整檔案執行順序。
- 保存設定。

### 11.3 確認佈署

按下「佈署」後，顯示確認畫面：

- 佈署設定名稱。
- Server 名稱、Host、Port、帳號。
- 檔案總數。
- 每筆 localPath 與 remotePath。
- 以下警告：

目標檔案已存在時將直接覆蓋，不建立備份。上傳中斷可能留下不完整檔案。

使用者確認後才呼叫建立工作 API。

### 11.4 進度與結果

執行中顯示：

- 工作 ID。
- 整體狀態。
- 目前處理檔案。
- 已完成數／總檔案數。
- 各檔案狀態與錯誤原因。
- 若執行期間發生工作紀錄保存失敗（`recordWriteFailed = true`），顯示明確警告訊息。

第一版以檔案數進度為必要功能，不要求顯示傳輸速率或預估剩餘時間。

### 11.5 歷史紀錄

- 分頁顯示歷史工作。
- 預設依建立時間由新到舊排序。
- 查看單次工作及各檔案結果。
- 即使原佈署設定已刪除，仍可查看歷史快照。

-----

## 12. 佈署執行規格

### 12.1 工作限制

- 同一時間只允許一個佈署工作。
- 已有工作執行時，拒絕建立第二個工作。
- 拒絕結果使用 HTTP 409。
- 第一版不提供等待佇列。
- 每個工作使用唯一 jobId。

### 12.2 建立工作

1. 驗證佈署設定存在。
2. 驗證 Server 引用存在。
3. 驗證設定結構有效。
4. 確認沒有其他工作執行。
5. 取得當下設定快照。
6. 建立初始工作紀錄。
7. 回傳 jobId。
8. 背景執行上傳。

初始紀錄若無法建立，不開始任何遠端寫入。

### 12.3 設定快照

工作使用建立當下的：

- 佈署設定名稱與 ID。
- Server 連線資訊。
- 檔案清單及順序。

敏感登入資訊僅供執行使用，不寫入工作紀錄。

設定快照不等於來源檔案內容快照；後端於處理各檔案時直接讀取來源。因此使用者應避免上傳期間修改來源檔案。

### 12.4 逐檔處理流程

```text
檢查本機來源
│
├─ 不存在／不可讀／是資料夾
│  → 該檔案 FAILED
│  → 下一筆
│
▼
檢查遠端父目錄与目標路徑型態
│
├─ 父目錄不存在
│  → 該檔案 FAILED (REMOTE_DIRECTORY_NOT_FOUND)
│  → 不建立目錄
│  → 下一筆
├─ 遠端目標路徑已存在且為資料夾（非一般檔案）
│  → 該檔案 FAILED (REMOTE_PATH_INVALID)
│  → 下一筆
│
▼
開啟遠端目標，存在則覆蓋
│
▼
傳送檔案內容並完成關閉
│
├─ 發生錯誤 → FAILED
└─ 正常完成 → SUCCEEDED
```

### 12.5 固定上傳政策

| 項目 | 行為 |
| :-: | :-: |
| 上傳方式 | 逐檔、依設定順序 |
| 既有目標 | 直接覆蓋，不備份 |
| 遠端目錄不存在 | 該檔案失敗，不建立 |
| 個別檔案失敗 | 繼續處理其他檔案 |
| 重試同一檔案 | 不自動重試 |
| 上傳後操作 | 無 |
| 成功檔案回復 | 無 |

### 12.6 連線失敗處理

- Server 無法連線或認證失敗時，所有尚未完成的檔案記錄失敗及共同原因。
- 不必為每筆檔案重複執行相同的失敗登入。
- 上傳中斷線時：
  - 當前檔案失敗。
  - 不重試當前檔案。
  - 為下一筆檔案嘗試重新建立連線一次；若重新建立連線亦失敗（例如網路或 Server 已完全中斷），則該筆與後續所有未處理檔案一併標記為連線失敗並結束工作，避免逐檔重複等待逾時。
- 每筆檔案最終都必須有明確結果。

### 12.7 成功定義

單檔成功定義為：

SFTP 寫入及關閉操作正常完成，未收到傳輸錯誤。

不代表：

- 已做內容雜湊比對。
- 遠端應用程式已載入新檔案。
- 所有檔案同時生效。

### 12.8 覆蓋與完整性限制

- 直接覆蓋可能先截斷既有檔案。
- 中途失敗可能留下空檔案或部分內容。
- 不保證原檔案保留。
- 多檔案不具交易性。
- 已成功檔案不因後續失敗而回復。

### 12.9 逾時預設

以下為補充實作預設，不要求提供 UI 編輯：

| 項目 | 預設 |
| :-: | :-: |
| 建立連線逾時 | 15 秒 |
| 認證逾時 | 30 秒 |
| 傳輸無進展逾時 | 60 秒 |

- 不設定整個工作的固定總時間上限。
- 採用固定區塊大小（例如 64KB–256KB）串流讀寫，並將 SFTP Channel socket 逾時設為 60 秒；只要單次區塊讀寫持續有進展，大檔案不會因總耗時超過 60 秒而失敗；若任一區塊卡住超過 60 秒則觸發 `SFTP_TIMEOUT`。
- 逾時須轉為明確的檔案或工作錯誤。

-----

## 13. 工作狀態

### 13.1 整體狀態

| 狀態 | 定義 |
| :-: | :-: |
| QUEUED | 工作已建立，背景處理尚未開始 |
| RUNNING | 正在執行 |
| SUCCEEDED | 所有檔案成功 |
| PARTIALLY_SUCCEEDED | 至少一筆成功，且至少一筆失敗 |
| FAILED | 所有檔案失敗 |
| INTERRUPTED | 程式中斷，工作未正常完成 |

正常完成時：

```text
successCount = totalCount
→ SUCCEEDED

0 < successCount < totalCount
→ PARTIALLY_SUCCEEDED

successCount = 0
→ FAILED
```

### 13.2 單檔狀態

| 狀態 | 定義 |
| :-: | :-: |
| PENDING | 尚未處理 |
| UPLOADING | 正在上傳 |
| SUCCEEDED | 正常完成 |
| FAILED | 檢查或傳輸失敗 |
| INTERRUPTED | 工作中斷，無法確認正常完成 |

### 13.3 中斷復原

重新啟動時，若歷史工作仍為 QUEUED 或 RUNNING：

- 整體標記為 INTERRUPTED。
- 保留已保存的成功與失敗結果。
- 尚未完成的檔案標記為 INTERRUPTED。
- 不自動續傳或重跑。
- 不推測遠端檔案目前完整與否。
- 若無法得知實際中斷時間，不以重新啟動時間冒充原工作結束時間。

-----

## 14. 歷史紀錄

### 14.1 保存位置與命名規則

```text
logs/deployments/<jobId>.json
```

- `jobId` 命名格式採用包含 UTC 時間戳之前綴（例如 `job-20261019T083000Z-a1b2c3`）。
- 分頁查詢時，後端可直接透過目錄檔名依時間倒序排列並切片，僅讀取當前分頁所需的 JSON 檔案並轉為摘要（不含 `files` 陣列），避免因歷史紀錄增加而一次載入所有完整工作檔案。

### 14.2 必要內容

| 類別 | 欄位 |
| :-: | :-: |
| 工作 | ID、狀態、建立時間、開始時間、結束時間、紀錄寫入警告狀態 |
| 佈署設定 | ID、名稱 |
| Server 快照 | ID、名稱、Host、Port、Username |
| 統計 | 總數、成功數、失敗數、中斷數 |
| 檔案 | ID、來源、目標、大小、狀態、時間、錯誤 |

時間使用 ISO 8601，建議统一保存 UTC，例如：

```text
2026-10-19T08:30:00Z
```

### 14.3 範例

```json
{
  "schemaVersion": 1,
  "jobId": "job-20261019T083000Z-001",
  "profileId": "profile-test-app",
  "profileName": "Test App 佈署",
  "server": {
    "id": "server-test-01",
    "name": "測試 Server 01",
    "host": "192.168.10.50",
    "port": 22,
    "username": "deploy"
  },
  "createdAt": "2026-10-19T08:30:00Z",
  "startedAt": "2026-10-19T08:30:01Z",
  "finishedAt": "2026-10-19T08:30:05Z",
  "status": "PARTIALLY_SUCCEEDED",
  "totalCount": 2,
  "successCount": 1,
  "failedCount": 1,
  "interruptedCount": 0,
  "recordWriteFailed": false,
  "warningMessage": null,
  "files": [
    {
      "id": "file-01",
      "localPath": "C:\\Build\\App.Test.dll",
      "remotePath": "/opt/test-app/App.dll",
      "sizeBytes": 102400,
      "status": "SUCCEEDED",
      "startedAt": "2026-10-19T08:30:01Z",
      "finishedAt": "2026-10-19T08:30:04Z",
      "error": null
    },
    {
      "id": "file-02",
      "localPath": "C:\\Build\\appsettings.Test.json",
      "remotePath": "/opt/test-app/appsettings.json",
      "sizeBytes": null,
      "status": "FAILED",
      "startedAt": "2026-10-19T08:30:04Z",
      "finishedAt": "2026-10-19T08:30:05Z",
      "error": {
        "code": "LOCAL_FILE_NOT_FOUND",
        "message": "本機來源檔案不存在。"
      }
    }
  ]
}
```

### 14.4 保存與保留規則

- 工作建立時保存初始紀錄。
- 每個檔案完成後更新紀錄。
- 工作結束後保存最終結果。
- 不自動刪除歷史紀錄。
- 進行分頁查詢，避免一次載入全部歷史。
- 原 Server 或佈署設定被刪除，不影響既有紀錄。

### 14.5 紀錄寫入失敗

- 工作尚未開始：拒絕執行。
- 工作執行中：繼續依既定政策處理檔案，但將記憶體中之工作狀態標記 `recordWriteFailed: true` 與 `warningMessage`，透過 `GET /api/deployments/{jobId}` 向 UI 顯示紀錄保存失敗警告。
- 不得把紀錄失敗混同為檔案未上傳。
- 不得回報紀錄已永久保存。
- 若程式之後中斷，未成功寫入的結果可能無法復原。

-----

## 15. API 規格

### 15.1 共通規則

- Base URL：http://127.0.0.1:\<httpPort\>/api
- Request／Response：JSON。
- 寫入請求使用 Content-Type: application/json。
- 後端必須重新驗證，不只依賴前端驗證。
- 不使用 GET 執行寫入、測試連線或佈署。
- 查詢 API 不回傳密碼及 Base64 密碼。

### 15.2 API 清單

| Method | Path | 用途 |
| :-: | :-: | :-: |
| GET | /api/settings | 查詢目前 HTTP 設定與目前執行中之 `activeJobId` |
| POST | /api/shutdown | 請求安全關閉本機服務與退出 EXE（若有工作執行中回傳 409） |
| GET | /api/servers | 查詢 Server 清單 |
| POST | /api/servers | 新增 Server |
| PUT | /api/servers/{id} | 修改 Server |
| DELETE | /api/servers/{id} | 刪除未被引用的 Server |
| POST | /api/servers/{id}/test-connection | 測試已保存的連線資訊 |
| GET | /api/profiles | 查詢佈署設定清單 |
| GET | /api/profiles/{id} | 查詢單一佈署設定 |
| POST | /api/profiles | 新增佈署設定 |
| PUT | /api/profiles/{id} | 修改佈署設定 |
| DELETE | /api/profiles/{id} | 刪除佈署設定 |
| POST | /api/deployments | 建立工作 |
| GET | /api/deployments/active | 查詢目前執行中的工作（無則回傳 `{"activeJob": null}`） |
| GET | /api/deployments?page=1&pageSize=20 | 分頁查詢工作摘要 |
| GET | /api/deployments/{jobId} | 查詢工作與各檔案結果（含 `recordWriteFailed` 狀態） |

### 15.3 Server 新增請求

```json
{
  "name": "測試 Server 01",
  "host": "192.168.10.50",
  "port": 22,
  "username": "deploy",
  "password": "example-password"
}
```

password 為寫入專用欄位，後端轉換成 passwordBase64 保存。

Server 查詢結果：

```json
{
  "id": "server-test-01",
  "name": "測試 Server 01",
  "host": "192.168.10.50",
  "port": 22,
  "username": "deploy",
  "hasPassword": true
}
```

### 15.4 建立工作

請求：

```json
{
  "profileId": "profile-test-app"
}
```

成功接受後回傳 HTTP 202：

```json
{
  "jobId": "job-example-001",
  "status": "QUEUED"
}
```

HTTP 202 只表示工作已接受，不表示上傳成功。

### 15.5 歷史分頁

- page 預設 1，最小 1。
- pageSize 預設 20，範圍 1–100。
- 清單回傳工作摘要。
- 完整檔案結果由單筆查詢取得。

回傳格式：

```json
{
  "page": 1,
  "pageSize": 20,
  "total": 0,
  "items": []
}
```

### 15.6 HTTP 狀態碼

| 狀態碼 | 使用情境 |
| :-: | :-: |
| 200 | 查詢、修改、連線測試或請求退出成功 |
| 201 | 新增設定成功 |
| 202 | 工作已接受 |
| 204 | 刪除成功 |
| 400 | 欄位、JSON 或參數驗證失敗 |
| 404 | 指定資源不存在 |
| 409 | 已有工作執行、Server 被引用等衝突 |
| 500 | 非預期錯誤或本機保存失敗 |
| 502 | 連線測試中的遠端連線或認證失敗 |
| 504 | 連線測試逾時 |

背景工作中的 SFTP 失敗記錄於工作結果，不改變先前建立工作 API 的 202。

### 15.7 錯誤格式

```json
{
  "error": {
    "code": "DEPLOYMENT_ALREADY_RUNNING",
    "message": "目前已有佈署工作執行中。",
    "details": {
      "jobId": "job-example-001"
    }
  }
}
```

錯誤訊息不得包含密碼或含密碼的設定內容。

-----

## 16. 錯誤代碼

| 代碼 | 說明 |
| :-: | :-: |
| CONFIG_INVALID | 設定格式或內容無效 |
| CONFIG_WRITE_FAILED | 設定保存失敗 |
| HTTP_PORT_IN_USE | 本機 HTTP Port 被占用 |
| SERVER_NOT_FOUND | Server 不存在 |
| SERVER_IN_USE | Server 被佈署設定引用 |
| PROFILE_NOT_FOUND | 佈署設定不存在 |
| DEPLOYMENT_ALREADY_RUNNING | 已有工作執行 |
| LOCAL_FILE_NOT_FOUND | 本機檔案不存在 |
| LOCAL_PATH_NOT_FILE | 來源不是一般檔案 |
| LOCAL_FILE_ACCESS_DENIED | 無權讀取來源 |
| LOCAL_FILE_READ_FAILED | 讀取來源失敗 |
| REMOTE_DIRECTORY_NOT_FOUND | 遠端父目錄不存在 |
| REMOTE_PERMISSION_DENIED | 遠端操作權限不足 |
| REMOTE_PATH_INVALID | 遠端路徑無法作為目標檔案 |
| SFTP_CONNECTION_FAILED | SFTP 連線失敗 |
| SFTP_AUTH_FAILED | 認證失敗 |
| SFTP_TIMEOUT | 連線、認證或傳輸逾時 |
| SFTP_UPLOAD_FAILED | 上傳失敗 |
| JOB_RECORD_WRITE_FAILED | 工作紀錄保存失敗 |
| JOB_INTERRUPTED | 工作非正常中斷 |
| INTERNAL_ERROR | 未分類的非預期錯誤 |

遠端回報權限不足或一般失敗時，不得一律標示為目錄不存在。

-----

## 17. 本機限制與範圍

### 17.1 本機存取設定

使用環境為乾淨內網，不額外實作複雜的 Session Token 或防偽驗證機制，僅維持以下基本規範：

- 僅監聽 127.0.0.1。
- 查詢 API 與 Log 不記錄、不回傳敏感密碼內容。

### 17.2 設定檔保護

- Base64 密碼視為可還原的登入資訊。
- 不建議提交包含帳密的設定檔至公開版本庫。
- 不透過靜態檔案服務公開 EXE 同層目錄。
- servers.json、備份及工作紀錄應由後端讀取，不提供任意檔案下載介面。

### 17.3 SSH Host Key

SSH Host Key 驗證屬安全建議，尚未列為已確認的第一版必要功能。

若實作加入此功能，應：

- 保存可信的 Host Key Fingerprint。
- 首次設定時要求核對。
- Fingerprint 不符時拒絕上傳。

若未實作驗證，需明確記錄其風險：工具無法可靠辨識遠端是否為預期的 Server。

-----

## 18. 非功能需求

### 18.1 可用性

- 上傳在背景執行，不阻塞 UI。
- 工作不依赖瀏覽器頁面持續開啟。
- 不以單一長時間 HTTP Request 等待整個工作結束。
- 前端執行中可每秒查詢一次工作狀態。

### 18.2 檔案處理

- 使用串流讀寫，不將整個檔案一次載入記憶體。
- 不設定來源檔案大小的固定應用層上限。
- 實際可傳輸大小受本機、SFTP Server 及檔案系統限制。

### 18.3 紀錄管理

- 永久保留，不自動清除。
- 查詢採分頁。
- 磁碟容量由使用者管理。
- 不因紀錄增加而要求一次讀入所有完整工作內容。

### 18.4 可維護性

- 設定與紀錄具有 schemaVersion。
- 不支援的版本回報錯誤，不自行猜測格式。
- 錯誤提供穩定代碼與可理解訊息。
- 實作技術可自行選擇，但不得改變本文件對外行為。

-----

## 19. 驗收條件

| 編號 | 驗收條件 |
| :-: | :-: |
| AC-01 | Windows 10 x64 未另裝語言 Runtime，仍可啟動工具。 |
| AC-02 | UI 資源可由工具本身提供，不依賴外部 CDN。 |
| AC-03 | 後端僅監聽 127.0.0.1，其他電腦無法直接連入。 |
| AC-04 | 修改 httpPort 並重啟後，UI 使用新 Port。 |
| AC-05 | HTTP Port 被占用時明確報錯，不自動改 Port。 |
| AC-06 | 各 Server 可設定不同 SFTP Port。 |
| AC-07 | 設定保存於 EXE 同層，重新啟動後仍存在。 |
| AC-08 | 多組佈署設定可引用同一個 Server。 |
| AC-09 | 修改 Server 後，引用它的設定於下次工作使用新資訊。 |
| AC-10 | 被引用的 Server 無法直接刪除。 |
| AC-11 | 密碼以 UTF-8 Base64 保存，並可解碼後登入。 |
| AC-12 | 查詢 API 與 Log 不包含原始或 Base64 密碼。 |
| AC-13 | 本機與遠端檔名不同時，依 remotePath 正確上傳。 |
| AC-14 | 既有遠端檔案直接覆蓋，不建立備份。 |
| AC-15 | 遠端父目錄不存在時回報失敗，不建立目錄。 |
| AC-16 | 一筆來源不存在時，其他有效檔案仍會上傳。 |
| AC-17 | 一筆上傳失敗時，其他檔案仍會繼續處理。 |
| AC-18 | 連線或認證失敗時，所有未完成檔案具有明確結果。 |
| AC-19 | 全部成功、部分成功、全部失敗可正確區分。 |
| AC-20 | 重複觸發時不產生並行工作。 |
| AC-21 | 執行期間修改設定，不影響現有工作快照。 |
| AC-22 | 關閉或重新整理瀏覽器不停止上傳。 |
| AC-23 | 強制結束後重啟，未完成工作標示為中斷，不自動重跑。 |
| AC-24 | 歷史紀錄重新啟動後仍可查詢，不自動刪除。 |
| AC-25 | 損毀設定不被直接覆蓋，錯誤可辨識。 |
| AC-26 | 初始紀錄無法建立時，不開始遠端寫入。 |
| AC-27 | 工作中紀錄保存失敗時，UI 明確警告，不偽稱已保存。 |
| AC-28 | 上傳過程不執行遠端指令或部署後操作。 |

-----

## 20. 交付項目

1. Windows 10 x64 可執行交付包。
2. EXE 及必要內嵌或隨包相依性。
3. HTML UI。
4. 初始或範例 JSON：
   - appsettings.json
   - servers.json
   - deployments.json
5. 操作說明：
   - 啟動與退出。
   - HTTP Port 修改。
   - Server 設定。
   - 佈署設定。
   - 結果與歷史查詢。
6. 限制與風險說明：
   - Base64 非加密。
   - 直接覆蓋可能留下不完整檔案。
   - 不提供整批回復。
   - 紀錄不自動清除。
7. 依本文件驗收條件執行的測試結果。

-----

## 21. 後續擴充候選

以下不屬於第一版必要交付：

- SSH Host Key Fingerprint 管理。
- 更安全的密碼保存方式。
- 暫存檔上傳後替換目標。
- 遠端檔案備份與回復。
- 檔案雜湊驗證。
- 原生本機選檔對話框。
- 工作取消。
- 失敗檔案手動重跑。
- 匯入／匯出設定。
- 排程及 CI/CD 整合。