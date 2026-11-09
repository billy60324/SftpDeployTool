"use strict";

(function () {
  // 全域狀態
  const state = {
    servers: [],
    profiles: [],
    activeJobId: null,
    viewingJobId: null,
    pollTimer: null,
    pendingConfirmProfileId: null,
    historyPage: 1,
    historyPageSize: 20,
    historyTotal: 0,
    fileDrafts: [], // 佈署設定表單內的檔案對應陣列 [{ id, localPath, remotePath }]
  };

  // -------------------- 工具函式 --------------------

  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function formatBytes(bytes) {
    if (bytes === null || bytes === undefined) return "-";
    const num = Number(bytes);
    if (isNaN(num)) return "-";
    if (num < 1024) return num + " B";
    if (num < 1024 * 1024) return (num / 1024).toFixed(1) + " KB";
    return (num / (1024 * 1024)).toFixed(2) + " MB";
  }

  let toastTimeout = null;
  function showToast(message, isError = false) {
    const el = document.getElementById("global-toast");
    if (!el) return;
    el.textContent = message;
    el.className = "toast " + (isError ? "toast-error" : "toast-success");
    if (toastTimeout) clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => {
      el.classList.add("hidden");
    }, 4500);
  }

  function getStatusBadgeHtml(status) {
    const map = {
      QUEUED: { cls: "status-running", label: "QUEUED (排隊中)" },
      RUNNING: { cls: "status-running", label: "RUNNING (執行中)" },
      UPLOADING: { cls: "status-running", label: "UPLOADING (上傳中)" },
      PENDING: { cls: "status-neutral", label: "PENDING (待處理)" },
      SUCCEEDED: { cls: "status-succeeded", label: "SUCCEEDED (成功)" },
      PARTIALLY_SUCCEEDED: { cls: "status-partial", label: "PARTIALLY_SUCCEEDED (部分成功)" },
      FAILED: { cls: "status-failed", label: "FAILED (失敗)" },
      INTERRUPTED: { cls: "status-interrupted", label: "INTERRUPTED (已中斷)" },
    };
    const item = map[status] || { cls: "status-neutral", label: status || "-" };
    return `<span class="status-pill ${item.cls}">${escapeHtml(item.label)}</span>`;
  }

  async function apiRequest(method, url, body = null) {
    const opts = {
      method: method,
      headers: {},
    };
    if (body !== null) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const resp = await fetch(url, opts);
    if (resp.status === 204) {
      return null;
    }
    let data = null;
    try {
      data = await resp.json();
    } catch (_e) {
      data = null;
    }
    if (!resp.ok) {
      const errMsg =
        (data && data.error && data.error.message) ||
        `HTTP ${resp.status} 請求失敗`;
      const errCode = (data && data.error && data.error.code) || "ERROR";
      const err = new Error(`[${errCode}] ${errMsg}`);
      err.code = errCode;
      err.status = resp.status;
      err.details = data && data.error ? data.error.details : null;
      throw err;
    }
    return data;
  }

  // -------------------- 頁籤切換 --------------------

  function switchTab(tabId) {
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.getAttribute("data-tab") === tabId);
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.id === tabId);
    });
    if (tabId === "tab-history") {
      loadHistoryPage(state.historyPage);
    }
  }

  // -------------------- 載入初始資料與設定 --------------------

  async function loadAllData() {
    try {
      const [settings, srvData, profData] = await Promise.all([
        apiRequest("GET", "/api/settings"),
        apiRequest("GET", "/api/servers"),
        apiRequest("GET", "/api/profiles"),
      ]);

      document.getElementById("header-http-port").textContent = settings.httpPort || "-";
      state.servers = srvData.servers || [];
      state.profiles = profData.deploymentProfiles || [];

      renderServersTable();
      renderProfileServerSelect();
      renderProfilesTable();
      renderDeployQuickList();

      // 若頁面重整時有正在執行的工作，自動恢復追蹤 (SPEC 10.4)
      if (settings.activeJobId) {
        startPollingJob(settings.activeJobId);
      } else if (!state.viewingJobId) {
        // 若尚無檢視中工作，嘗試載入最新一筆歷史工作顯示於右側面板
        const hist = await apiRequest("GET", "/api/deployments?page=1&pageSize=1");
        if (hist && hist.items && hist.items.length > 0) {
          await fetchAndRenderJobProgress(hist.items[0].jobId);
        }
      }
    } catch (err) {
      showToast("載入設定資料失敗：" + err.message, true);
    }
  }

  // -------------------- Server 管理 (SPEC 11.1) --------------------

  function renderServersTable() {
    const tbody = document.getElementById("servers-tbody");
    if (!tbody) return;

    if (state.servers.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="text-muted" style="text-align:center;">目前尚無 Server 設定，請由右側表單新增。</td></tr>`;
      return;
    }

    tbody.innerHTML = state.servers
      .map((srv) => {
        const refs = srv.referencedBy || [];
        const refsHtml =
          refs.length > 0
            ? refs.map((r) => `<span class="status-pill status-neutral">${escapeHtml(r.name)}</span>`).join(" ")
            : `<span class="text-muted">未被引用</span>`;
        const pwHtml = srv.hasPassword
          ? `<span class="status-pill status-succeeded">已設定密碼</span>`
          : `<span class="status-pill status-failed">未設定密碼</span>`;

        return `
          <tr>
            <td>
              <strong>${escapeHtml(srv.name)}</strong><br/>
              <code>${escapeHtml(srv.id)}</code>
            </td>
            <td><code>${escapeHtml(srv.host)}:${escapeHtml(srv.port)}</code></td>
            <td>
              <div><code>${escapeHtml(srv.username)}</code></div>
              <div style="margin-top:4px;">${pwHtml}</div>
            </td>
            <td>${refsHtml}</td>
            <td>
              <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button type="button" class="btn btn-sm btn-secondary" data-action="test-server" data-id="${escapeHtml(srv.id)}">測試連線</button>
                <button type="button" class="btn btn-sm btn-secondary" data-action="edit-server" data-id="${escapeHtml(srv.id)}">編輯</button>
                <button type="button" class="btn btn-sm btn-danger" data-action="delete-server" data-id="${escapeHtml(srv.id)}">刪除</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function resetServerForm() {
    document.getElementById("server-id-input").value = "";
    document.getElementById("server-name-input").value = "";
    document.getElementById("server-host-input").value = "";
    document.getElementById("server-port-input").value = "22";
    document.getElementById("server-username-input").value = "";
    document.getElementById("server-password-input").value = "";
    document.getElementById("server-form-title").textContent = "新增 Server";
    document.getElementById("server-password-req").classList.remove("hidden");
    document.getElementById("server-password-input").placeholder = "輸入 SFTP 登入密碼";
    document.getElementById("server-password-hint").textContent =
      "密碼將以 UTF-8 Base64 編碼保存於本機 servers.json（注意：前後空白將完整保留，不會自動去除）。";
  }

  function editServerForm(serverId) {
    const srv = state.servers.find((s) => s.id === serverId);
    if (!srv) return;
    document.getElementById("server-id-input").value = srv.id;
    document.getElementById("server-name-input").value = srv.name;
    document.getElementById("server-host-input").value = srv.host;
    document.getElementById("server-port-input").value = srv.port;
    document.getElementById("server-username-input").value = srv.username;
    document.getElementById("server-password-input").value = "";
    document.getElementById("server-form-title").textContent = `修改 Server (${srv.id})`;
    document.getElementById("server-password-req").classList.add("hidden");
    document.getElementById("server-password-input").placeholder = "留空表示保留原密碼；輸入新值則替換密碼";
    document.getElementById("server-password-hint").textContent =
      "修改時若不變更密碼，請保持密碼欄空白；若輸入新密碼將替換原密碼。";
  }

  async function handleServerFormSubmit(e) {
    e.preventDefault();
    const id = document.getElementById("server-id-input").value.trim();
    const name = document.getElementById("server-name-input").value.trim();
    const host = document.getElementById("server-host-input").value.trim();
    const portStr = document.getElementById("server-port-input").value.trim();
    const username = document.getElementById("server-username-input").value.trim();
    // 注意：依 SPEC 8.2 密碼不得自動去除前後空白
    const passwordRaw = document.getElementById("server-password-input").value;

    const port = portStr ? Number(portStr) : 22;
    const payload = {
      name,
      host,
      port,
      username,
    };

    if (!id) {
      // 新增時必填 password
      payload.password = passwordRaw;
    } else if (passwordRaw.length > 0) {
      // 修改時僅在有輸入新密碼時傳入 password
      payload.password = passwordRaw;
    }

    try {
      if (!id) {
        await apiRequest("POST", "/api/servers", payload);
        showToast("新增 Server 成功！");
      } else {
        await apiRequest("PUT", `/api/servers/${encodeURIComponent(id)}`, payload);
        showToast("修改 Server 設定成功！");
      }
      resetServerForm();
      await loadAllData();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function handleTestServerConnection(serverId, btnEl) {
    const origText = btnEl.textContent;
    btnEl.disabled = true;
    btnEl.textContent = "測試中...";
    try {
      const res = await apiRequest(
        "POST",
        `/api/servers/${encodeURIComponent(serverId)}/test-connection`
      );
      showToast(res.message || "SFTP 連線測試成功！");
    } catch (err) {
      showToast("連線測試失敗：" + err.message, true);
    } finally {
      btnEl.disabled = false;
      btnEl.textContent = origText;
    }
  }

  async function handleDeleteServer(serverId) {
    const srv = state.servers.find((s) => s.id === serverId);
    if (!srv) return;
    if (!window.confirm(`確定要刪除 Server「${srv.name} (${srv.id})」嗎？`)) {
      return;
    }
    try {
      await apiRequest("DELETE", `/api/servers/${encodeURIComponent(serverId)}`);
      showToast("已刪除 Server。");
      if (document.getElementById("server-id-input").value === serverId) {
        resetServerForm();
      }
      await loadAllData();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  // -------------------- 佈署設定管理 (SPEC 11.2) --------------------

  function renderProfileServerSelect() {
    const sel = document.getElementById("profile-server-select");
    if (!sel) return;
    const currentVal = sel.value;
    sel.innerHTML =
      `<option value="">-- 請選擇目標 Server --</option>` +
      state.servers
        .map(
          (s) =>
            `<option value="${escapeHtml(s.id)}">${escapeHtml(s.name)} (${escapeHtml(s.host)}:${escapeHtml(s.port)})</option>`
        )
        .join("");
    if (currentVal && state.servers.some((s) => s.id === currentVal)) {
      sel.value = currentVal;
    }
    updateSelectedServerPreview();
  }

  function updateSelectedServerPreview() {
    const sel = document.getElementById("profile-server-select");
    const preview = document.getElementById("selected-server-preview");
    if (!sel || !preview) return;
    const srv = state.servers.find((s) => s.id === sel.value);
    if (!srv) {
      preview.classList.add("hidden");
      preview.innerHTML = "";
      return;
    }
    preview.classList.remove("hidden");
    preview.innerHTML = `
      <strong>選定 Server 資訊：</strong>
      Host: <code>${escapeHtml(srv.host)}</code> ·
      Port: <code>${escapeHtml(srv.port)}</code> ·
      帳號: <code>${escapeHtml(srv.username)}</code>
    `;
  }

  function syncDraftsFromInputs() {
    const container = document.getElementById("file-mappings-container");
    if (!container) return;
    const rows = container.querySelectorAll(".file-mapping-row");
    rows.forEach((row, idx) => {
      if (state.fileDrafts[idx]) {
        state.fileDrafts[idx].localPath = row.querySelector(".input-local-path").value;
        state.fileDrafts[idx].remotePath = row.querySelector(".input-remote-path").value;
      }
    });
  }

  function renderFileMappingsDrafts() {
    const container = document.getElementById("file-mappings-container");
    if (!container) return;

    if (state.fileDrafts.length === 0) {
      state.fileDrafts.push({ id: "", localPath: "", remotePath: "" });
    }

    container.innerHTML = state.fileDrafts
      .map((item, idx) => {
        const isFirst = idx === 0;
        const isLast = idx === state.fileDrafts.length - 1;
        return `
          <div class="file-mapping-row" data-index="${idx}">
            <div class="file-mapping-header">
              <span><strong>#${idx + 1}</strong> ${item.id ? `<code>${escapeHtml(item.id)}</code>` : "(新檔案對應)"}</span>
              <div style="display:flex; gap:4px;">
                <button type="button" class="btn btn-sm btn-secondary" data-file-action="up" data-index="${idx}" ${isFirst ? "disabled" : ""} title="上移順序">&uarr; 上移</button>
                <button type="button" class="btn btn-sm btn-secondary" data-file-action="down" data-index="${idx}" ${isLast ? "disabled" : ""} title="下移順序">&darr; 下移</button>
                <button type="button" class="btn btn-sm btn-danger" data-file-action="remove" data-index="${idx}" ${state.fileDrafts.length <= 1 ? "disabled" : ""} title="刪除此筆">刪除</button>
              </div>
            </div>
            <div class="file-mapping-inputs">
              <div>
                <label style="font-size:12px;">本機檔案絕對路徑 (localPath)</label>
                <input type="text" class="input-local-path" value="${escapeHtml(item.localPath)}" placeholder="例如：C:\\Build\\App.dll" />
              </div>
              <div>
                <label style="font-size:12px;">遠端目標絕對路徑 (remotePath)</label>
                <input type="text" class="input-remote-path" value="${escapeHtml(item.remotePath)}" placeholder="例如：/opt/app/App.dll" />
              </div>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function resetProfileForm() {
    document.getElementById("profile-id-input").value = "";
    document.getElementById("profile-name-input").value = "";
    document.getElementById("profile-server-select").value = "";
    document.getElementById("profile-form-title").textContent = "新增佈署設定";
    state.fileDrafts = [{ id: "", localPath: "", remotePath: "" }];
    updateSelectedServerPreview();
    renderFileMappingsDrafts();
  }

  function editProfileForm(profileId) {
    const prof = state.profiles.find((p) => p.id === profileId);
    if (!prof) return;
    document.getElementById("profile-id-input").value = prof.id;
    document.getElementById("profile-name-input").value = prof.name;
    document.getElementById("profile-server-select").value = prof.serverId;
    document.getElementById("profile-form-title").textContent = `修改佈署設定 (${prof.id})`;
    state.fileDrafts = (prof.files || []).map((f) => ({
      id: f.id || "",
      localPath: f.localPath || "",
      remotePath: f.remotePath || "",
    }));
    updateSelectedServerPreview();
    renderFileMappingsDrafts();
  }

  async function handleProfileFormSubmit(e) {
    e.preventDefault();
    syncDraftsFromInputs();

    const id = document.getElementById("profile-id-input").value.trim();
    const name = document.getElementById("profile-name-input").value.trim();
    const serverId = document.getElementById("profile-server-select").value.trim();

    const files = state.fileDrafts.map((f) => {
      const obj = {
        localPath: (f.localPath || "").trim(),
        remotePath: (f.remotePath || "").trim(),
      };
      if (f.id) obj.id = f.id;
      return obj;
    });

    const payload = {
      name,
      serverId,
      files,
    };

    try {
      if (!id) {
        await apiRequest("POST", "/api/profiles", payload);
        showToast("新增佈署設定成功！");
      } else {
        await apiRequest("PUT", `/api/profiles/${encodeURIComponent(id)}`, payload);
        showToast("修改佈署設定成功！");
      }
      resetProfileForm();
      await loadAllData();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  async function handleDeleteProfile(profileId) {
    const prof = state.profiles.find((p) => p.id === profileId);
    if (!prof) return;
    if (!window.confirm(`確定要刪除佈署設定「${prof.name} (${prof.id})」嗎？`)) {
      return;
    }
    try {
      await apiRequest("DELETE", `/api/profiles/${encodeURIComponent(profileId)}`);
      showToast("已刪除佈署設定。");
      if (document.getElementById("profile-id-input").value === profileId) {
        resetProfileForm();
      }
      await loadAllData();
    } catch (err) {
      showToast(err.message, true);
    }
  }

  function renderProfilesTable() {
    const tbody = document.getElementById("profiles-tbody");
    if (!tbody) return;

    if (state.profiles.length === 0) {
      tbody.innerHTML = `<tr><td colspan="4" class="text-muted" style="text-align:center;">目前尚無佈署設定，請先新增 Server 後建立佈署設定。</td></tr>`;
      return;
    }

    tbody.innerHTML = state.profiles
      .map((prof) => {
        const srv = state.servers.find((s) => s.id === prof.serverId);
        const srvHtml = srv
          ? `<strong>${escapeHtml(srv.name)}</strong><br/><code>${escapeHtml(srv.host)}:${escapeHtml(srv.port)}</code>`
          : `<span class="text-danger">未知 Server (${escapeHtml(prof.serverId)})</span>`;

        return `
          <tr>
            <td>
              <strong>${escapeHtml(prof.name)}</strong><br/>
              <code>${escapeHtml(prof.id)}</code>
            </td>
            <td>${srvHtml}</td>
            <td><strong>${(prof.files || []).length}</strong> 筆</td>
            <td>
              <div style="display:flex; gap:6px; flex-wrap:wrap;">
                <button type="button" class="btn btn-sm btn-primary" data-action="prepare-deploy" data-id="${escapeHtml(prof.id)}">佈署</button>
                <button type="button" class="btn btn-sm btn-secondary" data-action="edit-profile" data-id="${escapeHtml(prof.id)}">編輯</button>
                <button type="button" class="btn btn-sm btn-danger" data-action="delete-profile" data-id="${escapeHtml(prof.id)}">刪除</button>
              </div>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  function renderDeployQuickList() {
    const container = document.getElementById("deploy-profiles-quicklist");
    if (!container) return;

    if (state.profiles.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <p>尚無可用的佈署設定。</p>
          <button type="button" class="btn btn-secondary" onclick="document.querySelector('[data-tab=\\'tab-profiles\\']').click()">前往新增佈署設定</button>
        </div>
      `;
      return;
    }

    container.innerHTML = state.profiles
      .map((prof) => {
        const srv = state.servers.find((s) => s.id === prof.serverId);
        const srvText = srv
          ? `${srv.name} (${srv.host}:${srv.port} · ${srv.username})`
          : `未知 Server (${prof.serverId})`;
        const isRunning = Boolean(state.activeJobId);

        return `
          <div class="profile-quick-card">
            <div>
              <h3>${escapeHtml(prof.name)}</h3>
              <div class="text-muted" style="font-size:12px;">
                目標 Server：<code>${escapeHtml(srvText)}</code> · 共 <strong>${(prof.files || []).length}</strong> 筆檔案對應
              </div>
            </div>
            <div>
              <button
                type="button"
                class="btn btn-primary"
                data-action="prepare-deploy"
                data-id="${escapeHtml(prof.id)}"
                ${isRunning ? "disabled title='目前已有佈署工作執行中'" : ""}
              >
                準備佈署
              </button>
            </div>
          </div>
        `;
      })
      .join("");
  }

  // -------------------- 確認佈署與觸發工作 (SPEC 11.3, 11.4) --------------------

  function openConfirmDeployModal(profileId) {
    const prof = state.profiles.find((p) => p.id === profileId);
    if (!prof) return;
    const srv = state.servers.find((s) => s.id === prof.serverId);
    if (!srv) {
      showToast("此佈署設定引用的 Server 不存在！", true);
      return;
    }

    state.pendingConfirmProfileId = prof.id;
    document.getElementById("confirm-profile-name").textContent = prof.name;
    document.getElementById("confirm-server-name").textContent = srv.name;
    document.getElementById("confirm-server-hostport").textContent = `${srv.host}:${srv.port}`;
    document.getElementById("confirm-server-user").textContent = srv.username;
    document.getElementById("confirm-file-count").textContent = String((prof.files || []).length);

    const tbody = document.getElementById("confirm-files-tbody");
    tbody.innerHTML = (prof.files || [])
      .map(
        (f, i) => `
        <tr>
          <td>#${i + 1}</td>
          <td><code>${escapeHtml(f.localPath)}</code></td>
          <td><code>${escapeHtml(f.remotePath)}</code></td>
        </tr>
      `
      )
      .join("");

    document.getElementById("confirm-deploy-modal").classList.remove("hidden");
  }

  function closeConfirmDeployModal() {
    state.pendingConfirmProfileId = null;
    document.getElementById("confirm-deploy-modal").classList.add("hidden");
  }

  async function handleConfirmAndStartDeploy() {
    const profileId = state.pendingConfirmProfileId;
    if (!profileId) return;

    const btn = document.getElementById("btn-confirm-deploy");
    btn.disabled = true;
    try {
      const res = await apiRequest("POST", "/api/deployments", { profileId });
      closeConfirmDeployModal();
      switchTab("tab-deploy");
      showToast(`已建立佈署工作：${res.jobId}`);
      startPollingJob(res.jobId);
    } catch (err) {
      showToast("無法啟動佈署：" + err.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  function startPollingJob(jobId) {
    state.activeJobId = jobId;
    state.viewingJobId = jobId;
    document.getElementById("active-job-indicator").classList.remove("hidden");
    renderDeployQuickList();

    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }

    fetchAndRenderJobProgress(jobId);
    state.pollTimer = setInterval(() => {
      fetchAndRenderJobProgress(jobId);
    }, 1000);
  }

  function stopPollingJob() {
    state.activeJobId = null;
    document.getElementById("active-job-indicator").classList.add("hidden");
    renderDeployQuickList();
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  async function fetchAndRenderJobProgress(jobId) {
    try {
      const job = await apiRequest("GET", `/api/deployments/${encodeURIComponent(jobId)}`);
      renderJobProgressPanel(job);
      if (job.status !== "QUEUED" && job.status !== "RUNNING") {
        if (state.activeJobId === jobId) {
          stopPollingJob();
        }
      }
    } catch (err) {
      if (state.activeJobId === jobId) {
        stopPollingJob();
      }
      showToast("查詢工作狀態失敗：" + err.message, true);
    }
  }

  function renderJobProgressPanel(job) {
    if (!job) return;
    document.getElementById("job-progress-empty").classList.add("hidden");
    document.getElementById("job-progress-content").classList.remove("hidden");

    const badgeContainer = document.getElementById("job-panel-status-badge");
    badgeContainer.outerHTML = `<span id="job-panel-status-badge">${getStatusBadgeHtml(job.status)}</span>`;

    // 紀錄寫入失敗警告 (SPEC 14.5 & AC-27)
    const warnEl = document.getElementById("job-record-warning");
    const warnTextEl = document.getElementById("job-record-warning-text");
    if (job.recordWriteFailed) {
      warnEl.classList.remove("hidden");
      warnTextEl.textContent =
        job.warningMessage || "工作紀錄保存失敗，目前結果僅存在記憶體中，未永久保存。";
    } else {
      warnEl.classList.add("hidden");
      warnTextEl.textContent = "";
    }

    const srv = job.server || {};
    const files = job.files || [];
    const total = job.totalCount || files.length || 0;
    const doneCount =
      (job.successCount || 0) + (job.failedCount || 0) + (job.interruptedCount || 0);
    const uploadingFile = files.find((f) => f.status === "UPLOADING");

    document.getElementById("job-meta-id").textContent = job.jobId || "-";
    document.getElementById("job-meta-profile").textContent = `${job.profileName || "-"} (${job.profileId || "-"})`;
    document.getElementById("job-meta-server").textContent = `${srv.name || "-"} (${srv.host || "-"}:${srv.port || "-"} · ${srv.username || "-"})`;
    document.getElementById("job-meta-created").textContent = job.createdAt || "-";
    document.getElementById("job-meta-current-file").textContent = uploadingFile
      ? uploadingFile.localPath
      : job.status === "RUNNING" || job.status === "QUEUED"
      ? "準備中..."
      : "已結束";
    document.getElementById("job-meta-progress-text").textContent = `${doneCount} / ${total}`;
    document.getElementById("job-meta-counts-detail").textContent =
      `（成功 ${job.successCount || 0} · 失敗 ${job.failedCount || 0} · 中斷 ${job.interruptedCount || 0}）`;

    const pct = total > 0 ? Math.min(100, Math.round((doneCount / total) * 100)) : 0;
    document.getElementById("job-progress-bar").style.width = `${pct}%`;

    const tbody = document.getElementById("job-files-tbody");
    tbody.innerHTML = files
      .map((f, idx) => {
        const errHtml = f.error
          ? `<div class="text-danger"><strong>[${escapeHtml(f.error.code)}]</strong> ${escapeHtml(f.error.message)}</div>`
          : "";
        const timeHtml = f.finishedAt
          ? `<div class="text-muted" style="font-size:11px;">完成於 ${escapeHtml(f.finishedAt)}</div>`
          : f.startedAt
          ? `<div class="text-muted" style="font-size:11px;">開始於 ${escapeHtml(f.startedAt)}</div>`
          : "";

        return `
          <tr>
            <td>#${idx + 1}</td>
            <td><code>${escapeHtml(f.localPath)}</code></td>
            <td><code>${escapeHtml(f.remotePath)}</code></td>
            <td>${escapeHtml(formatBytes(f.sizeBytes))}</td>
            <td>${getStatusBadgeHtml(f.status)}</td>
            <td>${errHtml}${timeHtml || "-"}</td>
          </tr>
        `;
      })
      .join("");
  }

  // -------------------- 歷史紀錄查詢 (SPEC 11.5) --------------------

  async function loadHistoryPage(page) {
    try {
      const p = Math.max(1, page);
      const data = await apiRequest(
        "GET",
        `/api/deployments?page=${p}&pageSize=${state.historyPageSize}`
      );
      state.historyPage = data.page;
      state.historyTotal = data.total;
      renderHistoryTable(data.items || []);
    } catch (err) {
      showToast("載入歷史紀錄失敗：" + err.message, true);
    }
  }

  function renderHistoryTable(items) {
    const totalPages = Math.max(1, Math.ceil(state.historyTotal / state.historyPageSize));
    document.getElementById("history-page-info").textContent =
      `第 ${state.historyPage} 頁 / 共 ${totalPages} 頁（總計 ${state.historyTotal} 筆）`;
    document.getElementById("btn-history-prev").disabled = state.historyPage <= 1;
    document.getElementById("btn-history-next").disabled = state.historyPage >= totalPages;

    const tbody = document.getElementById("history-tbody");
    if (items.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" class="text-muted" style="text-align:center;">尚無歷史佈署紀錄。</td></tr>`;
      return;
    }

    tbody.innerHTML = items
      .map((item) => {
        const srv = item.server || {};
        return `
          <tr>
            <td><code>${escapeHtml(item.jobId)}</code></td>
            <td>
              <strong>${escapeHtml(item.profileName)}</strong><br/>
              <code>${escapeHtml(item.profileId)}</code>
            </td>
            <td>
              ${escapeHtml(srv.name || "-")}<br/>
              <code>${escapeHtml(srv.host || "-")}:${escapeHtml(srv.port || "-")}</code>
            </td>
            <td>${escapeHtml(item.createdAt || "-")}</td>
            <td>${getStatusBadgeHtml(item.status)}</td>
            <td>
              總數 <strong>${item.totalCount}</strong> /
              成功 <strong style="color:var(--success)">${item.successCount}</strong> /
              失敗 <strong style="color:var(--danger)">${item.failedCount}</strong> /
              中斷 <strong>${item.interruptedCount}</strong>
            </td>
            <td>
              <button type="button" class="btn btn-sm btn-secondary" data-action="view-history-job" data-id="${escapeHtml(item.jobId)}">
                查看明細
              </button>
            </td>
          </tr>
        `;
      })
      .join("");
  }

  async function viewHistoryJobDetail(jobId) {
    try {
      const job = await apiRequest("GET", `/api/deployments/${encodeURIComponent(jobId)}`);
      const card = document.getElementById("history-detail-card");
      card.classList.remove("hidden");
      document.getElementById("history-detail-jobid").textContent = job.jobId;

      const srv = job.server || {};
      document.getElementById("history-detail-meta").innerHTML = `
        <div><span class="meta-label">佈署設定快照：</span><strong>${escapeHtml(job.profileName)}</strong> (<code>${escapeHtml(job.profileId)}</code>)</div>
        <div><span class="meta-label">Server 快照：</span><strong>${escapeHtml(srv.name)}</strong> (<code>${escapeHtml(srv.host)}:${escapeHtml(srv.port)}</code> · 帳號 <code>${escapeHtml(srv.username)}</code>)</div>
        <div><span class="meta-label">整體狀態：</span>${getStatusBadgeHtml(job.status)}</div>
        <div><span class="meta-label">建立／開始／結束：</span>${escapeHtml(job.createdAt || "-")} / ${escapeHtml(job.startedAt || "-")} / ${escapeHtml(job.finishedAt || "未正常結束")}</div>
      `;

      const tbody = document.getElementById("history-detail-files-tbody");
      tbody.innerHTML = (job.files || [])
        .map((f, idx) => {
          const errHtml = f.error
            ? `<div class="text-danger"><strong>[${escapeHtml(f.error.code)}]</strong> ${escapeHtml(f.error.message)}</div>`
            : "";
          return `
            <tr>
              <td>#${idx + 1}</td>
              <td><code>${escapeHtml(f.id)}</code></td>
              <td><code>${escapeHtml(f.localPath)}</code></td>
              <td><code>${escapeHtml(f.remotePath)}</code></td>
              <td>${escapeHtml(formatBytes(f.sizeBytes))}</td>
              <td>${getStatusBadgeHtml(f.status)}</td>
              <td>
                <div class="text-muted" style="font-size:11px;">
                  ${escapeHtml(f.startedAt || "-")} ~ ${escapeHtml(f.finishedAt || "-")}
                </div>
                ${errHtml}
              </td>
            </tr>
          `;
        })
        .join("");
      card.scrollIntoView({ behavior: "smooth" });
    } catch (err) {
      showToast("讀取歷史工作明細失敗：" + err.message, true);
    }
  }

  // -------------------- 安全退出工具 (SPEC 10.4) --------------------

  async function handleShutdownApp() {
    if (!window.confirm("確定要關閉本機 SFTP 檔案上傳工具服務嗎？")) {
      return;
    }
    try {
      await apiRequest("POST", "/api/shutdown");
      document.body.innerHTML = `
        <div style="max-width:520px; margin:120px auto; text-align:center; background:#fff; padding:32px; border-radius:10px; border:1px solid #e2e8f0;">
          <h2>SFTP 檔案上傳工具已安全關閉</h2>
          <p class="text-muted">本機後端服務已停止運行，您現在可以關閉此瀏覽器分頁。</p>
        </div>
      `;
    } catch (err) {
      showToast(err.message, true);
    }
  }

  // -------------------- 事件綁定 --------------------

  document.addEventListener("DOMContentLoaded", () => {
    // 頁籤切換
    document.querySelectorAll(".tab-btn").forEach((btn) => {
      btn.addEventListener("click", () => switchTab(btn.getAttribute("data-tab")));
    });

    // 頂部退出按鈕
    document.getElementById("btn-shutdown").addEventListener("click", handleShutdownApp);

    // Tab 1: 重新整理快速列表
    document.getElementById("btn-refresh-deploy-list").addEventListener("click", loadAllData);

    // Tab 2: 佈署設定表單與檔案對應操作
    document.getElementById("btn-new-profile").addEventListener("click", resetProfileForm);
    document.getElementById("btn-reset-profile-form").addEventListener("click", resetProfileForm);
    document.getElementById("profile-server-select").addEventListener("change", updateSelectedServerPreview);
    document.getElementById("profile-form").addEventListener("submit", handleProfileFormSubmit);

    document.getElementById("btn-add-file-row").addEventListener("click", () => {
      syncDraftsFromInputs();
      state.fileDrafts.push({ id: "", localPath: "", remotePath: "" });
      renderFileMappingsDrafts();
    });

    document.getElementById("file-mappings-container").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-file-action]");
      if (!btn) return;
      syncDraftsFromInputs();
      const action = btn.getAttribute("data-file-action");
      const idx = Number(btn.getAttribute("data-index"));
      if (action === "up" && idx > 0) {
        const tmp = state.fileDrafts[idx - 1];
        state.fileDrafts[idx - 1] = state.fileDrafts[idx];
        state.fileDrafts[idx] = tmp;
        renderFileMappingsDrafts();
      } else if (action === "down" && idx < state.fileDrafts.length - 1) {
        const tmp = state.fileDrafts[idx + 1];
        state.fileDrafts[idx + 1] = state.fileDrafts[idx];
        state.fileDrafts[idx] = tmp;
        renderFileMappingsDrafts();
      } else if (action === "remove" && state.fileDrafts.length > 1) {
        state.fileDrafts.splice(idx, 1);
        renderFileMappingsDrafts();
      }
    });

    // Tab 3: Server 表單
    document.getElementById("btn-new-server").addEventListener("click", resetServerForm);
    document.getElementById("btn-reset-server-form").addEventListener("click", resetServerForm);
    document.getElementById("server-form").addEventListener("submit", handleServerFormSubmit);

    // Tab 4: 歷史紀錄分頁
    document.getElementById("btn-history-prev").addEventListener("click", () => {
      if (state.historyPage > 1) loadHistoryPage(state.historyPage - 1);
    });
    document.getElementById("btn-history-next").addEventListener("click", () => {
      loadHistoryPage(state.historyPage + 1);
    });
    document.getElementById("btn-history-refresh").addEventListener("click", () => {
      loadHistoryPage(state.historyPage);
    });
    document.getElementById("btn-close-history-detail").addEventListener("click", () => {
      document.getElementById("history-detail-card").classList.add("hidden");
    });

    // 確認佈署 Modal
    document.getElementById("btn-close-confirm-modal").addEventListener("click", closeConfirmDeployModal);
    document.getElementById("btn-cancel-deploy").addEventListener("click", closeConfirmDeployModal);
    document.getElementById("btn-confirm-deploy").addEventListener("click", handleConfirmAndStartDeploy);

    // 委派表格與卡片動作按鈕
    document.body.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-action]");
      if (!btn) return;
      const action = btn.getAttribute("data-action");
      const id = btn.getAttribute("data-id");

      if (action === "test-server") {
        handleTestServerConnection(id, btn);
      } else if (action === "edit-server") {
        editServerForm(id);
      } else if (action === "delete-server") {
        handleDeleteServer(id);
      } else if (action === "edit-profile") {
        editProfileForm(id);
      } else if (action === "delete-profile") {
        handleDeleteProfile(id);
      } else if (action === "prepare-deploy") {
        openConfirmDeployModal(id);
      } else if (action === "view-history-job") {
        viewHistoryJobDetail(id);
      }
    });

    // 初始化表單與載入資料
    resetServerForm();
    resetProfileForm();
    loadAllData();
  });
})();
