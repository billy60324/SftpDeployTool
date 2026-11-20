# Windows 本機 SFTP 檔案上傳工具 (SftpDeployTool) 操作與風險說明

本工具為適用於 **Windows 10 x64** 的本機手動觸發 SFTP 檔案上傳與 WebLogic AP 重啟工具，已內嵌執行環境與離線前端介面，**無須在目標電腦安裝 Python、Node.js 或其他語言執行環境**，亦不依賴任何外部網路或 CDN。

---

## 1. 交付包目錄結構

```text
SftpDeployTool/
├─ SftpDeployTool.exe      # 主執行檔（雙擊啟動）
├─ _internal/              # 內嵌執行環境、相依套件與離線前端資源（請勿移動或刪除）
├─ appsettings.json        # 本機 HTTP 服務 Port 設定
├─ servers.json            # 遠端 Linux SFTP Server 與選填 WebLogic Admin 清單設定
├─ deployments.json        # 佈署設定（檔案對應清單與選填 WebLogic Redeploy 設定）
├─ README.md               # 本操作與限制風險說明文件
└─ logs/
   └─ deployments/         # 每次佈署工作的歷史紀錄 JSON 檔 (<jobId>.json)
```

> **重要提醒**：移動或分發本工具時，請將整個 `SftpDeployTool` 資料夾（包含 `_internal/` 子目錄）一併複製。設定檔與紀錄檔固定讀寫於 `SftpDeployTool.exe` 所在之同層目錄。

---

## 2. 操作說明

### 2.1 啟動與退出
- **啟動工具**：直接雙擊執行 `SftpDeployTool.exe`。
  - 程式啟動後會檢查設定檔、自動啟動本機 `http://127.0.0.1:18080` 服務，並自動開啟預設瀏覽器（Edge / Chrome）進入管理介面。
  - 若缺少預設設定檔，首次啟動時會自動建立空白設定檔與 `logs/deployments` 目錄。
  - **向下相容**：支援直接讀取舊版 `v1.0.0`（`schemaVersion: 1`）與新版 `v1.1.0`（`schemaVersion: 2`）的設定檔與歷史紀錄。
- **關閉瀏覽器不等於停止工具**：關閉瀏覽器分頁後，背景上傳工作仍會繼續執行；重新開啟 `http://127.0.0.1:18080` 或重新整理頁面，會自動恢復顯示執行中的工作進度。
- **安全退出工具**：
  - 請點擊網頁右上角的 **「退出工具」** 按鈕（或在黑色的 Console 視窗按下 `Ctrl + C`）。
  - 若目前仍有佈署工作正在執行中，系統會提示您等待工作完成後再退出。

### 2.2 修改本機 HTTP Port (`appsettings.json`)
預設本機監聽位址為 `127.0.0.1:18080`。若該 Port 與本機其他程式衝突：
1. 先退出 `SftpDeployTool.exe`。
2. 以文字編輯器開啟同目錄下的 `appsettings.json`：
   ```json
   {
     "schemaVersion": 2,
     "httpPort": 18080
   }
   ```
3. 將 `httpPort` 修改為 `1`～`65535` 之間未被占用的整數後存檔（UTF-8 編碼）。
4. 重新啟動 `SftpDeployTool.exe`。

### 2.3 設定檔匯出與匯入
1. **匯出設定**：點擊頂部導覽列的 **「匯出設定」** 按鈕，即可將所有 Server（含 Base64 編碼密碼）與佈署設定下載為單一 `.json` 備份檔。
2. **匯入設定**：點擊頂部導覽列的 **「匯入設定」** 按鈕，選擇備份檔並挑選匯入策略：
   - **合併模式（推薦）**：保留現有設定；相同 ID 自動更新，新 ID 自動新增。
   - **整批覆蓋現有設定**：以匯入檔完整替換本機所有 Server 與佈署設定（原檔案會自動備份為 `.bak`）。

### 2.4 Server 設定管理（含 WebLogic Admin 連線設定）
1. 切換至 **「3. Server 設定管理」** 頁籤。
2. 輸入顯示名稱、遠端 Linux SFTP Host（IP 或 Hostname，不含協定與斜線）、SFTP Port（預設 `22`）、登入帳號與密碼。
3. **WebLogic Admin 連線設定（選填）**：
   - 若該 Server 需搭配「上傳後自動重啟 WebLogic AP」，請填寫 `Admin Server URL`（例如 `http://fa5edapb:7001`）、`REST 版本`（預設 `12.2.1.3.0`）、`WebLogic 帳號` 與 `WebLogic 密碼`。
4. 保存後可點擊清單上的 **「測試 SFTP」** 或 **「測試 WebLogic」**，驗證連線與帳密是否正確。
5. 修改既有 Server 時，若密碼欄留空，系統將保留原有密碼；若輸入新密碼，將替換原有密碼。
6. 當某台 Server 正被任何「佈署設定」引用時，系統會禁止刪除該 Server，並提示引用它的設定名稱。

### 2.5 佈署設定管理 (Deployment Profiles)
1. 切換至 **「2. 佈署設定管理」** 頁籤。
2. 輸入佈署設定名稱，並選擇一台目標 Server（支援多組佈署設定共用同一台 Server）。
3. 設定一筆或多筆檔案對應（File Mappings）：
   - **本機檔案絕對路徑 (`localPath`)**：必須為 Windows 絕對路徑（例如 `C:\Build\App.ear`），不支援相對路徑、資料夾或萬用字元。
   - **遠端目標絕對路徑 (`remotePath`)**：必須為以 `/` 開頭且包含目標檔名的 Linux 絕對路徑（例如 `/opt/app/App.ear`），同一組設定內不可重複。
   - 可使用 **「↑ 上移」／「↓ 下移」** 按鈕調整檔案上傳的執行順序。
4. **WebLogic AP 自動重新啟動（選填）**：
   - 勾選 **「SFTP 全部上傳成功後，自動觸發重新啟動 WebLogic AP (Redeploy)」**。
   - 填寫 `AP 名稱`（`appDeployments` 名稱，如 `PMCForcastSystemEAR_7015`）與選填之 `Targets`（逗號分隔，留空則沿用現有 targets）。
   - 亦可在「進階設定」調整等待逾時秒數、Redeploy API 路徑或自訂 Request Body JSON。
5. 點擊 **「保存佈署設定」**。

### 2.6 觸發佈署、查看進度與歷史查詢
1. 在 **「1. 觸發佈署與進度」** 頁籤，選擇要執行的佈署設定並點擊 **「準備佈署」**。
2. 詳閱彈出的 **確認畫面**（核對目標 Server、檔案總數、每一筆本機／遠端路徑，以及是否啟用 WebLogic AP 自動重啟），點擊 **「確認並開始上傳」**。
3. 右側面板將每秒自動更新進度，顯示目前處理檔案、完成數／總數、各檔案結果，以及 WebLogic Redeploy 的即時終端機日誌。
   - **防護機制**：唯有當**所有檔案皆透過 SFTP 上傳成功**時才會觸發 WebLogic Redeploy；若任一檔案上傳失敗，系統會自動略過（`SKIPPED`）WebLogic 重啟。
4. 切換至 **「4. 歷史佈署紀錄」** 頁籤，可分頁查詢所有過往工作紀錄（時間皆採用台灣時區 `yyyy-mm-dd hh24:mi:ss` 格式），點擊 **「查看明細」** 可檢視當時的 Server 快照、各檔案結果與 WebLogic 執行日誌。

---

## 3. 限制與風險說明

使用本工具前請務必了解以下設計限制與風險：

1. **Base64 僅為編碼而非加密**：
   - `servers.json`、匯出的設定檔及 `.bak` 備份檔中的 `passwordBase64` 採 UTF-8 Base64 編碼保存，任何可讀取該設定檔的人皆可還原出原始密碼。請勿將包含真實帳密的設定檔提交至公開版本庫。
2. **直接覆蓋且不建立遠端備份**：
   - 遠端目標檔案若已存在，上傳時將直接截斷並覆蓋原檔，不會在遠端建立備份。
   - 若上傳過程中網路中斷、遠端空間不足或強制關閉 `SftpDeployTool.exe`，遠端可能留下空檔案或不完整的檔案內容。
3. **不提供整批交易式回復 (No Rollback)**：
   - 檔案採逐檔依序上傳；若其中一筆檔案失敗，工具會繼續處理後續檔案，且**已上傳成功的檔案不會因後續檔案失敗而自動回復**。
4. **不自動建立遠端目錄**：
   - 若遠端目標路徑的父目錄不存在，該筆檔案會直接標記為失敗 (`REMOTE_DIRECTORY_NOT_FOUND`)，工具不會自動建立遠端目錄。
5. **歷史紀錄不自動清除**：
   - `logs/deployments/<jobId>.json` 採永久保留設計，不會自動刪除，請使用者定期自行管理磁碟空間。

---

## 4. 開發環境執行、測試與打包

若您從原始碼儲存庫取得本專案，可依下列步驟在開發環境執行、驗證或重新建置免安裝交付包（建議使用 **Python 3.11+**）：

### 4.1 安裝相依套件
```powershell
pip install -r requirements.txt
```

### 4.2 以開發模式啟動服務
```powershell
python main.py
```

### 4.3 執行自動化驗收測試
```powershell
pytest -v
```

### 4.4 建置 Windows x64 免安裝交付包
執行打包腳本後，將於 `release/SftpDeployTool/` 產出完整獨立執行目錄，並同時打包 `release/SftpDeployTool-win64.zip`：
```powershell
python build_release.py
```

### 4.5 版本規格與開發計畫文件
各版本之系統規格書（`SPEC.md`）與實作計畫（`IMPLEMENTATION_PLAN.md`）依版本號歸檔於 [`docs/`](./docs/) 目錄下（例如 [`docs/v1.0.0/`](./docs/v1.0.0/) 與 [`docs/v1.1.0/`](./docs/v1.1.0/)）。
