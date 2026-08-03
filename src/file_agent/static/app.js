"use strict";

const byId = (id) => document.getElementById(id);

async function requestJson(url, options = {}) {
  const { allowUnauthorized = false, ...fetchOptions } = options;
  const headers = new Headers(fetchOptions.headers || {});
  if (fetchOptions.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(url, { ...fetchOptions, headers });
  if (response.status === 401 && !allowUnauthorized) {
    window.location.assign("/login");
    throw new Error("登录状态已失效");
  }
  if (!response.ok) {
    let detail = `请求失败（${response.status}）`;
    try {
      const payload = await response.json();
      detail =
        typeof payload.detail === "string"
          ? payload.detail
          : JSON.stringify(payload.detail);
    } catch {
      // Keep the deterministic fallback message.
    }
    const error = new Error(detail);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function showToast(message) {
  const toast = byId("toast");
  if (!toast) return;
  toast.textContent = message;
  toast.hidden = false;
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => {
    toast.hidden = true;
  }, 3600);
}

async function refreshTreeWithFeedback(button, refresh, notify) {
  button.disabled = true;
  button.textContent = "刷新中…";
  try {
    await refresh();
    notify("文件列表已刷新");
  } catch (error) {
    notify(error instanceof Error ? error.message : "文件列表刷新失败");
  } finally {
    button.disabled = false;
    button.textContent = "刷新";
  }
}

window.FileAgentUi = Object.freeze({ refreshTreeWithFeedback });

function initializeLogin() {
  const form = byId("login-form");
  const submit = byId("login-submit");
  const errorBox = byId("login-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    errorBox.hidden = true;
    try {
      await requestJson("/api/auth/login", {
        method: "POST",
        allowUnauthorized: true,
        body: JSON.stringify({
          username: byId("username").value,
          password: byId("password").value,
        }),
      });
      window.location.assign("/");
    } catch (error) {
      errorBox.textContent =
        error.status === 401 ? "账号或密码不正确。" : error.message;
      errorBox.hidden = false;
      submit.disabled = false;
    }
  });
}

function initializeApp() {
  const state = {
    workspaceId: window.sessionStorage.getItem("fileAgentWorkspace"),
    runId: window.sessionStorage.getItem("fileAgentRun"),
    source: null,
    running: false,
    answerBlock: null,
    answerText: "",
    answerRenderTimer: null,
    reasoningBlock: null,
    approvals: new Map(),
    previewPath: null,
    previewStart: 1,
    previewNext: null,
  };

  const setRunState = (running, label) => {
    state.running = running;
    byId("run-button").disabled = running;
    byId("task-input").disabled = running;
    byId("cancel-button").hidden = !running;
    byId("run-status").textContent = label;
    byId("run-indicator").classList.toggle("active", running);
  };

  const clearRunPanels = () => {
    if (state.answerRenderTimer !== null) {
      window.clearTimeout(state.answerRenderTimer);
      state.answerRenderTimer = null;
    }
    byId("conversation").replaceChildren();
    byId("activity-feed").replaceChildren();
    byId("conversation-empty").hidden = true;
    byId("activity-empty").hidden = true;
    state.answerBlock = null;
    state.answerText = "";
    state.reasoningBlock = null;
    state.approvals.clear();
    updateUsage({});
  };

  const addConversationBlock = (kind, title) => {
    const article = document.createElement("article");
    article.className = `message ${kind}`;
    const heading = document.createElement("div");
    heading.className = "message-label";
    heading.textContent = title;
    const content = document.createElement("div");
    content.className = "message-content";
    if (kind === "answer") content.classList.add("markdown-content");
    article.append(heading, content);
    byId("conversation").append(article);
    return content;
  };

  const renderAnswerNow = () => {
    if (state.answerRenderTimer !== null) {
      window.clearTimeout(state.answerRenderTimer);
      state.answerRenderTimer = null;
    }
    if (!state.answerBlock) return;
    window.FileAgentMarkdown.renderMarkdown(state.answerBlock, state.answerText);
  };

  const scheduleAnswerRender = () => {
    if (state.answerRenderTimer !== null) return;
    state.answerRenderTimer = window.setTimeout(renderAnswerNow, 80);
  };

  const addActivity = (kind, title, detail) => {
    byId("activity-empty").hidden = true;
    const item = document.createElement("article");
    item.className = `activity-item ${kind}`;
    const heading = document.createElement("strong");
    heading.textContent = title;
    const body = document.createElement("pre");
    body.textContent = detail;
    item.append(heading, body);
    byId("activity-feed").append(item);
    item.scrollIntoView({ block: "nearest" });
    return item;
  };

  const updateUsage = (data) => {
    byId("usage-input").textContent = String(data.input_tokens || 0);
    byId("usage-output").textContent = String(data.output_tokens || 0);
    byId("usage-reasoning").textContent = String(data.reasoning_tokens || 0);
    byId("usage-total").textContent = String(data.total_tokens || 0);
  };

  const resolveApproval = async (approvalId, approved, card) => {
    const buttons = card.querySelectorAll("button");
    buttons.forEach((button) => {
      button.disabled = true;
    });
    try {
      await requestJson(`/api/approvals/${encodeURIComponent(approvalId)}`, {
        method: "POST",
        body: JSON.stringify({ approved }),
      });
    } catch (error) {
      buttons.forEach((button) => {
        button.disabled = false;
      });
      showToast(error.message);
    }
  };

  const addApproval = (data) => {
    if (state.approvals.has(data.approval_id)) return;
    const card = document.createElement("article");
    card.className = "approval-card";
    const heading = document.createElement("strong");
    heading.textContent = `等待审批 · ${data.tool}`;
    const argumentsBox = document.createElement("pre");
    argumentsBox.textContent = JSON.stringify(data.args || {}, null, 2);
    const actions = document.createElement("div");
    actions.className = "approval-actions";
    const deny = document.createElement("button");
    deny.className = "quiet-button";
    deny.type = "button";
    deny.textContent = "拒绝";
    const approve = document.createElement("button");
    approve.className = "approve-button";
    approve.type = "button";
    approve.textContent = "批准并执行";
    deny.addEventListener("click", () =>
      resolveApproval(data.approval_id, false, card),
    );
    approve.addEventListener("click", () =>
      resolveApproval(data.approval_id, true, card),
    );
    actions.append(deny, approve);
    card.append(heading, argumentsBox, actions);
    byId("activity-feed").append(card);
    state.approvals.set(data.approval_id, card);
  };

  const markApproval = (data) => {
    const card = state.approvals.get(data.approval_id);
    if (!card) return;
    card.classList.add(data.approved ? "approved" : "denied");
    const heading = card.querySelector("strong");
    heading.textContent = `${data.approved ? "已批准" : "已拒绝"} · ${data.tool}`;
    card.querySelectorAll("button").forEach((button) => {
      button.disabled = true;
    });
  };

  const finishRun = (label) => {
    renderAnswerNow();
    setRunState(false, label);
    showToast(`运行状态：${label}`);
    if (state.source) state.source.close();
    state.source = null;
    state.runId = null;
    window.sessionStorage.removeItem("fileAgentRun");
    loadTree();
  };

  const parseEvent = (event) => {
    try {
      return JSON.parse(event.data);
    } catch {
      return {};
    }
  };

  const connectRun = (runId) => {
    if (state.source) state.source.close();
    state.runId = runId;
    window.sessionStorage.setItem("fileAgentRun", runId);
    byId("trace-link").href = `/api/runs/${encodeURIComponent(runId)}/trace`;
    byId("trace-link").hidden = false;
    setRunState(true, "正在运行");
    const source = new EventSource(
      `/api/runs/${encodeURIComponent(runId)}/events`,
    );
    state.source = source;

    source.addEventListener("run.started", () => {
      setRunState(true, "正在分析任务");
    });
    source.addEventListener("llm.started", () => {
      state.reasoningBlock = null;
      addActivity("model", "模型调用", "正在生成下一步…");
    });
    source.addEventListener("reasoning.delta", (event) => {
      const data = parseEvent(event);
      if (!state.reasoningBlock) {
        state.reasoningBlock = addConversationBlock("reasoning", "推理摘要");
      }
      state.reasoningBlock.textContent += data.delta || "";
    });
    source.addEventListener("answer.delta", (event) => {
      const data = parseEvent(event);
      if (!state.answerBlock) {
        state.answerBlock = addConversationBlock("answer", "文件助理");
      }
      state.answerText += data.delta || "";
      scheduleAnswerRender();
    });
    source.addEventListener("tool.started", (event) => {
      const data = parseEvent(event);
      addActivity(
        "tool",
        `工具 ${data.step || ""} · ${data.tool || "未知"}`,
        JSON.stringify(data.args || {}, null, 2),
      );
    });
    source.addEventListener("tool.completed", (event) => {
      const data = parseEvent(event);
      const result = data.result || {};
      addActivity(
        result.ok ? "success" : "failure",
        result.ok ? "工具完成" : "工具未执行",
        result.summary || "",
      );
      loadTree();
    });
    source.addEventListener("approval.required", (event) => {
      addApproval(parseEvent(event));
      setRunState(true, "等待写操作审批");
    });
    source.addEventListener("approval.resolved", (event) => {
      markApproval(parseEvent(event));
      setRunState(true, "继续运行");
    });
    source.addEventListener("usage.updated", (event) => {
      updateUsage(parseEvent(event));
    });
    source.addEventListener("llm.retrying", (event) => {
      const data = parseEvent(event);
      addActivity("failure", "模型连接重试", data.error || "");
    });
    source.addEventListener("run.completed", () => finishRun("已完成"));
    source.addEventListener("run.incomplete", (event) => {
      const data = parseEvent(event);
      addActivity("failure", "运行未完成", data.error || "");
      finishRun("未完成");
    });
    source.addEventListener("run.failed", (event) => {
      const data = parseEvent(event);
      addActivity("failure", "运行失败", data.error || "");
      finishRun("运行失败");
    });
    source.addEventListener("run.cancelled", () => finishRun("已取消"));
    source.onerror = () => {
      if (state.running) {
        byId("run-status").textContent = "正在重新连接";
        requestJson("/api/auth/me").catch(() => {});
      }
    };
  };

  const createWorkspace = async () => {
    const data = await requestJson("/api/workspaces", { method: "POST" });
    state.workspaceId = data.id;
    window.sessionStorage.setItem("fileAgentWorkspace", data.id);
    return data.id;
  };

  const ensureWorkspace = async () => {
    byId("workspace-state").hidden = false;
    byId("file-tree").hidden = true;
    if (!state.workspaceId) await createWorkspace();
    try {
      await loadTree();
    } catch (error) {
      if (error.status !== 410) throw error;
      window.sessionStorage.removeItem("fileAgentWorkspace");
      window.sessionStorage.removeItem("fileAgentRun");
      state.runId = null;
      await createWorkspace();
      await loadTree();
    }
    byId("workspace-state").hidden = true;
    byId("file-tree").hidden = false;
  };

  async function loadTree() {
    if (!state.workspaceId) throw new Error("工作区尚未准备完成");
    const data = await requestJson(
      `/api/workspaces/${encodeURIComponent(state.workspaceId)}/tree`,
    );
    const tree = byId("file-tree");
    const fragment = document.createDocumentFragment();
    for (const entry of data.entries || []) {
      const row = document.createElement(
        entry.type === "file" ? "button" : "div",
      );
      row.className = `tree-entry ${entry.type}`;
      const depth = Math.min(entry.path.split("/").length - 1, 8);
      row.classList.add(`depth-${depth}`);
      const marker = document.createElement("span");
      marker.className = "tree-marker";
      marker.textContent = entry.type === "file" ? "·" : "›";
      const label = document.createElement("span");
      label.textContent = entry.path;
      row.append(marker, label);
      if (entry.type === "file") {
        row.type = "button";
        row.addEventListener("click", () => openPreview(entry.path, 1));
      }
      fragment.append(row);
    }
    tree.replaceChildren(fragment);
  }

  async function openPreview(path, startLine) {
    const data = await requestJson(
      `/api/workspaces/${encodeURIComponent(state.workspaceId)}/files?` +
        new URLSearchParams({
          path,
          start_line: String(startLine),
          max_lines: "200",
        }),
    );
    state.previewPath = path;
    state.previewStart = startLine;
    state.previewNext = data.next_start_line;
    byId("preview-title").textContent = path;
    byId("preview-content").textContent = data.untrusted_content || "";
    byId("preview-range").textContent =
      data.end_line === null
        ? "没有更多内容"
        : `第 ${data.start_line}–${data.end_line} 行`;
    byId("preview-previous").disabled = startLine <= 1;
    byId("preview-next").disabled = !data.has_more || !data.next_start_line;
    const dialog = byId("preview-dialog");
    if (!dialog.open) dialog.showModal();
  }

  byId("task-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (state.running) return;
    clearRunPanels();
    setRunState(true, "正在创建运行");
    try {
      const run = await requestJson("/api/runs", {
        method: "POST",
        body: JSON.stringify({
          workspace_id: state.workspaceId,
          task: byId("task-input").value.trim(),
        }),
      });
      connectRun(run.id);
    } catch (error) {
      setRunState(false, "启动失败");
      showToast(error.message);
    }
  });

  byId("cancel-button").addEventListener("click", async () => {
    if (!state.runId) return;
    try {
      await requestJson(`/api/runs/${encodeURIComponent(state.runId)}/cancel`, {
        method: "POST",
      });
    } catch (error) {
      showToast(error.message);
    }
  });

  byId("reset-button").addEventListener("click", async () => {
    if (!window.confirm("确定重置工作区吗？文件修改和模型上下文都会清空。")) return;
    try {
      await requestJson(
        `/api/workspaces/${encodeURIComponent(state.workspaceId)}/reset`,
        { method: "POST" },
      );
      clearRunPanels();
      byId("conversation-empty").hidden = false;
      byId("trace-link").hidden = true;
      await loadTree();
      showToast("工作区和模型上下文已重置");
    } catch (error) {
      showToast(error.message);
    }
  });

  const refreshButton = byId("refresh-tree");
  refreshButton.addEventListener("click", () =>
    refreshTreeWithFeedback(refreshButton, loadTree, showToast),
  );
  byId("preview-close").addEventListener("click", () =>
    byId("preview-dialog").close(),
  );
  byId("preview-previous").addEventListener("click", () => {
    if (state.previewPath) {
      openPreview(state.previewPath, Math.max(1, state.previewStart - 200));
    }
  });
  byId("preview-next").addEventListener("click", () => {
    if (state.previewPath && state.previewNext) {
      openPreview(state.previewPath, state.previewNext);
    }
  });
  byId("logout-button").addEventListener("click", async () => {
    await requestJson("/api/auth/logout", { method: "POST" });
    window.sessionStorage.removeItem("fileAgentWorkspace");
    window.sessionStorage.removeItem("fileAgentRun");
    window.location.assign("/login");
  });

  Promise.all([requestJson("/api/auth/me"), ensureWorkspace()])
    .then(([user]) => {
      byId("current-user").textContent = user.username;
      if (state.runId) {
        clearRunPanels();
        connectRun(state.runId);
      }
    })
    .catch((error) => showToast(error.message));
}

const page = document.body.dataset.page;
if (page === "login") initializeLogin();
if (page === "app") initializeApp();
