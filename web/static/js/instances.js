/* 实例管理逻辑：加载实例表格、启动/停止/重启/终止、批量操作、改名、重装、串行控制台 */
(function () {
  "use strict";
  var RBot = window.RBot;
  var instances = [];
  var selected = {}; // 选中实例的 id 集合（对象当集合用）
  var terminateTarget = null; // 待终止的实例
  var renameTarget = null; // 待改名的实例
  var reinstallTarget = null; // 待重装的实例
  var consoleCommand = ""; // 当前串行控制台连接命令（供复制）

  var BATCH_LABEL = {
    start: "批量开机",
    stop: "批量关机",
    restart: "批量重启",
    terminate: "批量删除",
  };

  // 字段归一化：后端返回 R-Bot 形状（camelCase）
  // {id, name, publicIp, shape, state, ocpus, memoryInGBs, region, timeCreated}，
  // 过渡期同时双写旧字段，这里优先认新字段，旧字段作兜底。
  // 注意：列表只含 RUNNING 实例（与 R-Bot 一致）。
  function norm(inst) {
    return {
      id: inst.id,
      name: inst.name || inst["display-name"] || inst.display_name || "—",
      shape: inst.shape || "—",
      state: inst.state || inst["lifecycle-state"] || "",
      ocpus: inst.ocpus != null ? inst.ocpus
        : (inst["shape-config"] && inst["shape-config"].ocpus),
      memory_gb: inst.memoryInGBs != null ? inst.memoryInGBs
        : (inst.memory_gb != null ? inst.memory_gb
        : (inst["shape-config"] && inst["shape-config"]["memory-in-gbs"])),
      public_ip: inst.publicIp || inst["public-ip"] || inst.public_ip || null,
      created: inst.timeCreated || inst["time-created"] || inst.time_created || null,
    };
  }

  function stateBadge(state) {
    var s = (state || "").toUpperCase();
    if (s === "RUNNING") return '<span class="badge badge-green">RUNNING</span>';
    if (s === "STOPPED") return '<span class="badge badge-gray">STOPPED</span>';
    if (s === "TERMINATED") return '<span class="badge badge-red">TERMINATED</span>';
    return '<span class="badge badge-amber">' + RBot.escapeHtml(state || "—") + "</span>";
  }

  function selectedIds() {
    return Object.keys(selected);
  }

  function selectedInstances() {
    return instances.filter(function (raw) {
      return Object.prototype.hasOwnProperty.call(selected, String(raw.id));
    });
  }

  function updateBatchBar() {
    var count = selectedIds().length;
    document.getElementById("batch-count").textContent = "已选 " + count + " 台";
    var buttons = document.querySelectorAll("[data-batch]");
    buttons.forEach(function (btn) { btn.disabled = count === 0; });
    var selectAll = document.getElementById("select-all");
    if (selectAll) {
      selectAll.checked = instances.length > 0 && count === instances.length;
    }
  }

  function render() {
    var tbody = document.getElementById("instance-tbody");
    var empty = document.getElementById("instance-empty");
    empty.hidden = instances.length !== 0;
    if (instances.length === 0) {
      tbody.innerHTML = "";
      updateBatchBar();
      return;
    }
    tbody.innerHTML = instances.map(function (raw) {
      var esc = RBot.escapeHtml;
      var inst = norm(raw);
      var state = (inst.state || "").toUpperCase();
      var running = state === "RUNNING";
      var stopped = state === "STOPPED";
      var ip = inst.public_ip || "—";
      var ipCell = inst.public_ip
        ? '<span class="mono">' + esc(ip) + '</span> ' +
          '<button type="button" class="copy-btn" data-copy="' + esc(ip) + '" title="复制 IP">⧉</button>'
        : "—";
      var actions = "";
      if (stopped) actions += '<button type="button" class="btn btn-sm btn-primary" data-act="start">启动</button>';
      if (running) {
        actions += '<button type="button" class="btn btn-sm" data-act="stop">停止</button>';
        actions += '<button type="button" class="btn btn-sm" data-act="restart">重启</button>';
      }
      actions += '<button type="button" class="btn btn-sm" data-act="rename">改名</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="reinstall">重装</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="console">控制台</button>';
      actions += '<button type="button" class="btn btn-sm btn-danger" data-act="terminate">终止</button>';
      // OCPU/内存：后端列表接口目前不返回 shape-config，缺失时显示 "—"
      var created = inst.created
        ? '<br><span style="color: var(--text-faint); font-size: 12px;">' + esc(RBot.formatTime(inst.created)) + "</span>"
        : "";
      var checked = Object.prototype.hasOwnProperty.call(selected, String(inst.id))
        ? " checked" : "";
      return (
        "<tr data-instance-id=\"" + esc(inst.id) + "\">" +
          '<td><input type="checkbox" class="row-select" data-select="' + esc(inst.id) + '"' + checked + ' aria-label="选择实例"></td>' +
          "<td><b>" + esc(inst.name) + "</b>" + created + "</td>" +
          "<td class=\"mono\">" + esc(inst.shape) + "</td>" +
          "<td>" + stateBadge(inst.state) + "</td>" +
          "<td>" + esc(inst.ocpus != null ? inst.ocpus : "—") + "</td>" +
          "<td>" + esc(inst.memory_gb != null ? inst.memory_gb + " GB" : "—") + "</td>" +
          "<td>" + ipCell + "</td>" +
          '<td><div class="row-actions">' + actions + "</div></td>" +
        "</tr>"
      );
    }).join("");
    updateBatchBar();
  }

  async function loadInstances() {
    try {
      var data = await RBot.api("/api/instances");
      instances = data.instances || [];
      // 刷新后清理已不存在实例的选中状态
      var alive = {};
      instances.forEach(function (raw) { alive[String(raw.id)] = true; });
      Object.keys(selected).forEach(function (id) {
        if (!alive[id]) delete selected[id];
      });
      render();
    } catch (err) {
      document.getElementById("instance-tbody").innerHTML =
        '<tr><td colspan="8" style="text-align: center; color: var(--danger);">加载失败：' +
        RBot.escapeHtml(err.message) + "</td></tr>";
    }
  }

  // 实例操作：start / stop / restart，成功后刷新列表并 toast
  async function instanceAction(id, action, label) {
    try {
      await RBot.api("/api/instances/" + encodeURIComponent(id) + "/" + action, { method: "POST" });
      RBot.toast(label + "指令已发送", "success");
      await loadInstances();
    } catch (err) {
      RBot.toast(label + "失败：" + err.message, "error");
    }
  }

  // 批量操作：调 POST /api/instances/batch，汇报每台结果
  async function batchAction(action, label, confirm) {
    var ids = selectedIds();
    if (ids.length === 0) return;
    try {
      var res = await RBot.api("/api/instances/batch", {
        method: "POST",
        body: { action: action, instance_ids: ids, confirm: !!confirm },
      });
      var ok = (res.succeeded || []).length;
      var bad = (res.failed || []).length;
      if (bad === 0) {
        RBot.toast(label + "完成：成功 " + ok + " 台", "success");
      } else {
        var detail = (res.failed || []).map(function (f) {
          return f.id.slice(-4) + "：" + f.error;
        }).join("；");
        RBot.toast(label + "：成功 " + ok + " 台，失败 " + bad + " 台（" + detail + "）", "error");
      }
      selected = {};
      await loadInstances();
    } catch (err) {
      RBot.toast(label + "失败：" + err.message, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadInstances();
    document.getElementById("btn-refresh-instances").addEventListener("click", loadInstances);

    // 全选 / 取消全选
    document.getElementById("select-all").addEventListener("change", function (e) {
      if (e.target.checked) {
        instances.forEach(function (raw) { selected[String(raw.id)] = true; });
      } else {
        selected = {};
      }
      render();
    });

    // 批量操作按钮
    document.querySelectorAll("[data-batch]").forEach(function (btn) {
      btn.addEventListener("click", async function () {
        var action = btn.getAttribute("data-batch");
        var label = BATCH_LABEL[action] || action;
        var targets = selectedInstances();
        if (targets.length === 0) return;
        if (action === "terminate") {
          // 批量删除用专用二次确认弹窗（列出待删实例名）
          document.getElementById("batch-terminate-count").textContent = targets.length;
          document.getElementById("batch-terminate-names").innerHTML =
            targets.map(function (raw) {
              return "<li>" + RBot.escapeHtml(norm(raw).name) + "</li>";
            }).join("");
          RBot.openModal("batch-terminate-modal");
          return;
        }
        var names = targets.map(function (raw) { return norm(raw).name; }).join("、");
        var ok = await RBot.confirmDialog(
          label, "确认" + label + "以下实例吗？<br>" + RBot.escapeHtml(names),
          "确认" + label, false);
        if (ok) await batchAction(action, label, false);
      });
    });

    document.getElementById("instance-tbody").addEventListener("click", function (e) {
      var checkbox = e.target.closest("[data-select]");
      if (checkbox) {
        var id = checkbox.getAttribute("data-select");
        if (checkbox.checked) selected[id] = true;
        else delete selected[id];
        updateBatchBar();
        return;
      }
      var copyBtn = e.target.closest("[data-copy]");
      if (copyBtn) {
        RBot.copyText(copyBtn.getAttribute("data-copy"), "IP");
        return;
      }
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      var row = btn.closest("[data-instance-id]");
      var instId = row.getAttribute("data-instance-id");
      var inst = instances.find(function (x) { return String(x.id) === instId; });
      if (!inst) return;
      var act = btn.getAttribute("data-act");
      if (act === "start") instanceAction(instId, "start", "启动");
      else if (act === "stop") instanceAction(instId, "stop", "停止");
      else if (act === "restart") instanceAction(instId, "restart", "重启");
      else if (act === "terminate") {
        terminateTarget = inst;
        document.getElementById("terminate-name").textContent = norm(inst).name;
        RBot.openModal("terminate-modal");
      } else if (act === "rename") {
        renameTarget = inst;
        document.getElementById("rename-current").textContent = norm(inst).name;
        document.getElementById("rename-name").value = norm(inst).name;
        RBot.openModal("rename-modal");
      } else if (act === "reinstall") {
        reinstallTarget = inst;
        document.getElementById("reinstall-name").textContent = norm(inst).name;
        document.getElementById("reinstall-confirm-name").value = "";
        RBot.openModal("reinstall-modal");
      } else if (act === "console") {
        (async function () {
          try {
            var res = await RBot.api("/api/instances/" + encodeURIComponent(instId) + "/console");
            var conn = res.console || {};
            consoleCommand = conn["connection-string"] || "";
            document.getElementById("console-name").textContent = norm(inst).name;
            document.getElementById("console-state").textContent =
              conn["lifecycle-state"] ? "（" + conn["lifecycle-state"] + "）" : "";
            document.getElementById("console-command").textContent =
              consoleCommand || "未返回连接命令";
            document.getElementById("console-fingerprint").textContent =
              conn.fingerprint ? "指纹：" + conn.fingerprint : "";
            RBot.openModal("console-modal");
          } catch (err) {
            RBot.toast("获取串行控制台失败：" + err.message, "error");
          }
        })();
      }
    });

    // 确认终止：带 confirm:true，后端会永久删除引导卷
    document.getElementById("btn-confirm-terminate").addEventListener("click", async function () {
      if (!terminateTarget) return;
      var inst = terminateTarget;
      try {
        await RBot.api("/api/instances/" + encodeURIComponent(inst.id) + "/terminate", {
          method: "POST",
          body: { confirm: true },
        });
        RBot.toast("实例已终止，引导卷已永久删除", "success");
        RBot.closeModal("terminate-modal");
        await loadInstances();
      } catch (err) {
        RBot.toast("终止失败：" + err.message, "error");
      }
    });

    // 确认批量删除：带 confirm:true
    document.getElementById("btn-confirm-batch-terminate").addEventListener("click", async function () {
      RBot.closeModal("batch-terminate-modal");
      await batchAction("terminate", "批量删除", true);
    });

    // 确认改名
    document.getElementById("rename-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!renameTarget) return;
      var newName = document.getElementById("rename-name").value.trim();
      if (!newName) {
        RBot.toast("新实例名不能为空", "error");
        return;
      }
      try {
        await RBot.api("/api/instances/" + encodeURIComponent(renameTarget.id) + "/rename", {
          method: "POST",
          body: { name: newName },
        });
        RBot.toast("实例已改名为 " + newName, "success");
        RBot.closeModal("rename-modal");
        await loadInstances();
      } catch (err) {
        RBot.toast("改名失败：" + err.message, "error");
      }
    });

    // 确认重装：输入名必须与实例名完全一致
    document.getElementById("reinstall-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!reinstallTarget) return;
      var confirmName = document.getElementById("reinstall-confirm-name").value;
      if (confirmName !== norm(reinstallTarget).name) {
        RBot.toast("输入的实例名与实际不符，请完整输入实例名", "error");
        return;
      }
      try {
        await RBot.api("/api/instances/" + encodeURIComponent(reinstallTarget.id) + "/reinstall", {
          method: "POST",
          body: { confirm: true, confirm_name: confirmName },
        });
        RBot.toast("重装已提交，系统盘正在重建", "success");
        RBot.closeModal("reinstall-modal");
        await loadInstances();
      } catch (err) {
        RBot.toast("重装失败：" + err.message, "error");
      }
    });

    // 复制串行控制台连接命令
    document.getElementById("btn-copy-console").addEventListener("click", function () {
      if (consoleCommand) RBot.copyText(consoleCommand, "连接命令");
    });
  });
})();
