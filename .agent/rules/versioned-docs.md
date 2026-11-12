---
trigger: always_on
description: 專案規格文件 (SPEC) 與開發計畫依版本目錄歸檔及 Git Tag 規範
---

# 專案文件與版本管理規範 (Versioned Documentation & Release Rule)

1. **根目錄 Markdown 原則**：
   - 專案根目錄僅保留 `README.md` 作為主要入口說明文件。
   - 系統規格書（`SPEC.md`）、實作計畫（`IMPLEMENTATION_PLAN.md`）及其他設計文件，一律不得放置於專案根目錄。

2. **分版本存放開發文件 (`docs/<version>/`)**：
   - 所有版本的規格書與開發計畫皆須依語意化版本號（例如 `v1.0.0`、`v1.1.0`）存放於 `docs/<version>/` 子目錄下：
     - `docs/<version>/SPEC.md`
     - `docs/<version>/IMPLEMENTATION_PLAN.md`
   - 每當使用者提出新功能開發、規格變更或新版本迭代需求時，必須先在對應的新版本目錄 `docs/<新版本號>/` 建立或更新 `SPEC.md` 與 `IMPLEMENTATION_PLAN.md`。

3. **版本發佈與 Git Tag 規範**：
   - 當某版本的開發、測試與文件整理完成並準備發佈時，應將 `docs/<version>/` 一併納入版控提交，並在該 Commit 打上對應的 Git Tag（如 `v1.0.0`）推送至遠端儲存庫。
