/* 任务管理逻辑：任务列表、点行展开日志、取消运行中的任务
 *
 * 任务字段兼容多种键名（后端字段尚未最终定稿）：
 * 状态 status/state、类型 type/kind、开始时间 started_at/created_at、
 * 进度消息 message/progress/last_message、日志 logs/log_lines。 */
(function () {
  "use strict";
  var MBot = window.MBot;
  var tasks = [];
  var expandedId = null;
  var pollTimer = null;

  function taskStatus(t) {
    return (t.status || t.state || "UNKNOWN").toUpperCase();
  }
  function taskMessage(t) {
    if (t.message) return t.message;
    if (t.progress && typeof t.progress === "string") return t.progress;
    if (t.last_message) return t.last_message;
    if (Array.isArray(t.logs) && t.logs.length) return t.logs[t.logs.length - 1];
    return "—";
  }
  function isRunning(t) {
    var s = taskStatus(t);
    return s === "RUNNING" || s === "PENDING" || s === "IN_PROGRESS";
  }

  function statusBadge(t) {
    var s = taskStatus(t);
    var label = window.MBotI18n ? window.MBotI18n.taskStatusLabel(s) : s;
    if (isRunning(t)) return '<span class="badge badge-blue">' + MBot.escapeHtml(label) + "</span>";
    if (s === "SUCCESS" || s === "COMPLETED" || s === "DONE") return '<span class="badge badge-green">' + MBot.escapeHtml(label) + "</span>";
    if (s === "FAILED" || s === "ERROR") return '<span class="badge badge-red">' + MBot.escapeHtml(label) + "</span>";
    if (s === "CANCELLED" || s === "CANCELED") return '<span class="badge badge-gray">' + MBot.escapeHtml(label) + "</span>";
    return '<span class="badge badge-gray">' + MBot.escapeHtml(label) + "</span>";
  }

  function render() {
    var tbody = document.getElementById("task-tbody");
    var empty = document.getElementById("task-empty");
    empty.hidden = tasks.length !== 0;
    if (tasks.length === 0) {
      tbody.innerHTML = "";
      return;
    }
    tbody.innerHTML = tasks.map(function (t) {
      var esc = MBot.escapeHtml;
      var id = String(t.id);
      var expanded = expandedId === id;
      var cancelBtn = isRunning(t)
        ? '<button type="button" class="btn btn-sm btn-danger" data-act="cancel">取消</button>'
        : "";
      var deleteBtn = isRunning(t)
        ? ""
        : '<button type="button" class="btn btn-sm btn-danger" data-act="delete">删除</button>';
      // Backend task fields: label (display text, e.g. "抢机：xxx" or shape + instance name) / kind (type; falls back to this when an old task has no label) / detail (description) / status (pending/running/done/failed/cancelled) / created_at (second-level timestamp). 后端任务字段：label（显示说明，如“抢机：xxx”或规格·实例名）/ kind（类型，旧任务无 label 时退回它）/ detail（说明）/ status（pending/running/done/failed/cancelled）/ created_at（秒级时间戳）。
      var sub = t.detail || t.display_name || "";
      var row =
        '<tr class="task-row' + (expanded ? " expanded" : "") + '" data-task-id="' + esc(id) + '">' +
          "<td><b>" + esc(t.label || t.type || t.kind || "—") + "</b>" +
            (sub ? "<br><span style=\"color: var(--text-muted); font-size: 12px;\">" + esc(sub) + "</span>" : "") + "</td>" +
          "<td>" + statusBadge(t) + "</td>" +
          "<td>" + esc(MBot.formatTime(t.started_at || t.created_at)) + "</td>" +
          "<td>" + esc(taskMessage(t)) + "</td>" +
          '<td><div class="row-actions">' + cancelBtn + deleteBtn + "</div></td>" +
        "</tr>";
      if (expanded) {
        row += '<tr class="task-detail-row"><td colspan="5">' +
          '<pre class="task-logs" id="task-logs-' + esc(id) + '">加载日志中…</pre></td></tr>';
      }
      return row;
    }).join("");
    if (expandedId) loadLogs(expandedId);
  }

  async function loadTasks() {
    try {
      var data = await MBot.api("/api/tasks");
      tasks = data.tasks || [];
      render();
      // Auto-poll only while a task is running; stop polling otherwise. 有运行中的任务时自动轮询，否则停掉轮询。
      var anyRunning = tasks.some(isRunning);
      if (anyRunning && !pollTimer) {
        pollTimer = setInterval(loadTasks, 5000);
      } else if (!anyRunning && pollTimer) {
        clearInterval(pollTimer);
        pollTimer = null;
      }
    } catch (err) {
      document.getElementById("task-tbody").innerHTML =
        '<tr><td colspan="5" style="text-align: center; color: var(--danger);">加载失败：' +
        MBot.escapeHtml(err.message) + "</td></tr>";
    }
  }

  async function loadLogs(id) {
    var pre = document.getElementById("task-logs-" + id);
    if (!pre) return;
    try {
      var data = await MBot.api("/api/tasks/" + encodeURIComponent(id));
      var task = data.task || data;
      var logs = task.logs || task.log_lines || [];
      if (typeof logs === "string") {
        pre.textContent = logs;
      } else if (Array.isArray(logs) && logs.length) {
        pre.textContent = logs.map(function (line) {
          return typeof line === "string" ? line : JSON.stringify(line);
        }).join("\n");
      } else if (task.message) {
        pre.textContent = task.message;
      } else {
        pre.textContent = "暂无日志";
      }
    } catch (err) {
      pre.textContent = "日志加载失败：" + err.message;
    }
  }

  async function cancelTask(id) {
    var ok = await MBot.confirmDialog(
      "取消任务",
      "<p>确定要取消这个任务吗？正在进行的抢机/创建会立即停止。</p>",
      "确认取消", true
    );
    if (!ok) return;
    try {
      await MBot.api("/api/tasks/" + encodeURIComponent(id) + "/cancel", { method: "POST" });
      MBot.toast("任务已取消", "success");
      await loadTasks();
    } catch (err) {
      MBot.toast("取消失败：" + err.message, "error");
    }
  }

  async function deleteTask(id) {
    var ok = await MBot.confirmDialog(
      "删除任务",
      "<p>确定要删除这个任务吗？任务记录和日志都会删掉，删了就找不回来了。</p>",
      "确认删除", true
    );
    if (!ok) return;
    try {
      await MBot.api("/api/tasks/" + encodeURIComponent(id), { method: "DELETE" });
      MBot.toast("任务已删除", "success");
      if (expandedId === id) expandedId = null;
      await loadTasks();
    } catch (err) {
      MBot.toast("删除失败：" + err.message, "error");
    }
  }

  /* ---------- 抢机计划 ---------- */
  var plans = [];

  function renderPlans() {
    var tbody = document.getElementById("plan-tbody");
    if (plans.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">还没有抢机计划</td></tr>';
      return;
    }
    tbody.innerHTML = plans.map(function (p) {
      var esc = MBot.escapeHtml;
      var shape = (p.shape || "").replace("VM.Standard.", "");
      var badge = p.enabled
        ? '<span class="badge badge-green">已启用</span>'
        : '<span class="badge badge-gray">已停用</span>';
      var toggleText = p.enabled ? "停用" : "启用";
      return (
        '<tr data-plan-id="' + esc(p.id) + '">' +
          "<td><b>" + esc(p.id) + "</b></td>" +
          '<td><span class="mono">' + esc(p.cron) + "</span></td>" +
          "<td>" + esc(shape) + "（" + esc(p.ocpus) + " 核 / " + esc(p.memory_gb) + "GB）</td>" +
          "<td>" + badge + "</td>" +
          '<td><div class="row-actions">' +
            '<button type="button" class="btn btn-sm" data-act="toggle">' + toggleText + "</button>" +
            '<button type="button" class="btn btn-sm btn-danger" data-act="delete">删除</button>' +
          "</div></td>" +
        "</tr>"
      );
    }).join("");
  }

  async function loadPlans() {
    try {
      var data = await MBot.api("/api/plans");
      plans = data.plans || [];
    } catch (err) {
      document.getElementById("plan-tbody").innerHTML =
        '<tr><td colspan="5" style="text-align: center; color: var(--danger);">加载失败：' +
        MBot.escapeHtml(err.message) + "</td></tr>";
      return;
    }
    renderPlans();
  }

  async function togglePlan(id, enabled) {
    try {
      await MBot.api("/api/plans/" + encodeURIComponent(id) + "/toggle", {
        method: "POST",
        body: { enabled: enabled },
      });
      MBot.toast(enabled ? "计划已启用" : "计划已停用", "success");
      await loadPlans();
    } catch (err) {
      MBot.toast("操作失败：" + err.message, "error");
    }
  }

  async function deletePlan(id) {
    var ok = await MBot.confirmDialog(
      "删除抢机计划",
      "<p>确定要删除抢机计划 <b>" + MBot.escapeHtml(id) + "</b> 吗？之后不再按该计划定时抢机。</p>",
      "确认删除", true
    );
    if (!ok) return;
    try {
      await MBot.api("/api/plans/" + encodeURIComponent(id), { method: "DELETE" });
      MBot.toast("计划已删除", "success");
      await loadPlans();
    } catch (err) {
      MBot.toast("删除失败：" + err.message, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadTasks();
    loadPlans();
    document.getElementById("btn-refresh-tasks").addEventListener("click", function () {
      loadTasks();
      loadPlans();
    });

    document.getElementById("task-tbody").addEventListener("click", function (e) {
      var cancelBtn = e.target.closest('[data-act="cancel"]');
      if (cancelBtn) {
        e.stopPropagation();
        var row = cancelBtn.closest("[data-task-id]");
        cancelTask(row.getAttribute("data-task-id"));
        return;
      }
      var deleteBtn = e.target.closest('[data-act="delete"]');
      if (deleteBtn) {
        e.stopPropagation();
        var row = deleteBtn.closest("[data-task-id]");
        deleteTask(row.getAttribute("data-task-id"));
        return;
      }
      var rowEl = e.target.closest(".task-row");
      if (!rowEl) return;
      var id = rowEl.getAttribute("data-task-id");
      expandedId = expandedId === id ? null : id;
      render();
    });

    document.getElementById("plan-tbody").addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      var row = btn.closest("[data-plan-id]");
      var planId = row.getAttribute("data-plan-id");
      var plan = plans.find(function (p) { return String(p.id) === planId; });
      if (!plan) return;
      if (btn.getAttribute("data-act") === "toggle") {
        togglePlan(planId, !plan.enabled);
      } else if (btn.getAttribute("data-act") === "delete") {
        deletePlan(planId);
      }
    });

    document.getElementById("add-plan-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      var cron = document.getElementById("plan-cron").value.trim();
      var shape = document.getElementById("plan-shape").value;
      var ocpus = parseInt(document.getElementById("plan-ocpus").value, 10);
      var memoryGb = parseInt(document.getElementById("plan-memory").value, 10);
      if (!cron) {
        MBot.toast("请填写 cron 表达式", "error");
        return;
      }
      if (!ocpus || ocpus < 1 || ocpus > 4 || !memoryGb || memoryGb < 1 || memoryGb > 24) {
        MBot.toast("OCPU 需 1-4，内存需 1-24GB", "error");
        return;
      }
      try {
        await MBot.api("/api/plans", {
          method: "POST",
          body: { cron: cron, shape: shape, ocpus: ocpus, memory_gb: memoryGb },
        });
        MBot.toast("抢机计划已添加", "success");
        document.getElementById("add-plan-form").reset();
        document.getElementById("plan-ocpus").value = 1;
        document.getElementById("plan-memory").value = 6;
        await loadPlans();
      } catch (err) {
        MBot.toast("添加失败：" + err.message, "error");
      }
    });
  });
})();
