/* 实例管理逻辑：加载实例表格、启动/停止/重启/终止、批量操作、改名、重装、串行控制台、
   换 IP、IPv6 附加/删除、升降配、尝试修复、实例详情弹窗 */
(function () {
  "use strict";
  var RBot = window.RBot;
  var instances = [];
  var selected = {}; // 选中实例的 id 集合（对象当集合用）
  var currentProfile = null; // 当前账号（profile），由 fillProfileSelect 恢复
  var terminateTarget = null; // 待终止的实例
  var renameTarget = null; // 待改名的实例
  var reinstallTarget = null; // 待重装的实例
  var changeIpTarget = null; // 待换 IP 的实例
  var resizeTarget = null; // 待升降配的实例
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
      actions += '<button type="button" class="btn btn-sm" data-act="change-ip">换 IP</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="add-ipv6">加 IPv6</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="delete-ipv6">删 IPv6</button>';
      if (inst.shape === "VM.Standard.A1.Flex") {
        actions += '<button type="button" class="btn btn-sm" data-act="resize">升降配</button>';
      }
      actions += '<button type="button" class="btn btn-sm" data-act="repair">修复</button>';
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
          '<td><button type="button" class="name-btn" data-act="detail" title="查看详情">' + esc(inst.name) + "</button>" + created + "</td>" +
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

  // 加载实例列表（走前端缓存；force=true 时跳过前端缓存并带 refresh=1
  // 绕过服务端缓存）。后端并行取公网 IP + 30 秒服务端缓存，首次约 3-5 秒。
  async function loadInstances(force) {
    try {
      var url = RBot.withProfile("/api/instances", currentProfile);
      if (force) url += (url.indexOf("?") === -1 ? "?" : "&") + "refresh=1";
      var data = await RBot.cachedApi("inst:" + (currentProfile || "DEFAULT"), url, !!force);
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

  // 实例操作：start / stop / restart，成功后清前端缓存并刷新列表
  async function instanceAction(id, action, label) {
    try {
      await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(id) + "/" + action, currentProfile), { method: "POST" });
      RBot.toast(label + "指令已发送", "success");
      RBot.cacheClear("inst:");
      await loadInstances();
    } catch (err) {
      RBot.toast(label + "失败：" + err.message, "error");
    }
  }

  // 实例详情弹窗：点实例名打开，显示形状/规格/内外网 IP/IPv6/可用域/启动盘/创建时间
  async function showDetail(instId) {
    try {
      var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(instId), currentProfile));
      var inst = res.instance || {};
      var esc = RBot.escapeHtml;
      var cfg = inst["shape-config"] || {};
      document.getElementById("detail-name").textContent = inst["display-name"] || "—";
      document.getElementById("detail-shape").textContent = inst.shape || "—";
      document.getElementById("detail-spec").textContent =
        (cfg.ocpus != null ? cfg.ocpus + " OCPU / " + cfg["memory-in-gbs"] + " GB" : "—");
      document.getElementById("detail-state").innerHTML = stateBadge(inst["lifecycle-state"]);
      document.getElementById("detail-public-ip").textContent = res.public_ip || "—";
      document.getElementById("detail-private-ip").textContent = res.private_ip || "—";
      var ipv6 = res.ipv6_addresses;
      document.getElementById("detail-ipv6").textContent =
        (ipv6 && ipv6.length ? ipv6.join(", ") : "无");
      document.getElementById("detail-ad").textContent = inst["availability-domain"] || "—";
      document.getElementById("detail-boot-volume").textContent =
        (res.boot_volume_size_gbs != null ? res.boot_volume_size_gbs + " GB" : "—");
      document.getElementById("detail-created").textContent =
        inst["time-created"] ? RBot.formatTime(inst["time-created"]) : "—";
      RBot.openModal("detail-modal");
    } catch (err) {
      RBot.toast("加载实例详情失败：" + err.message, "error");
    }
  }

  // 附加 IPv6：确认后直接执行，成功后回显新地址
  async function addIpv6(inst) {
    var name = norm(inst).name;
    var ok = await RBot.confirmDialog(
      "附加 IPv6", "确认给实例 " + RBot.escapeHtml(name) + " 分配一个 IPv6 地址吗？",
      "确认附加", false);
    if (!ok) return;
    try {
      var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/ipv6", currentProfile), { method: "POST" });
      RBot.toast("已分配 IPv6：" + (res.ipv6 || "—"), "success");
      RBot.cacheClear("inst:");
      await loadInstances();
    } catch (err) {
      RBot.toast("附加 IPv6 失败：" + err.message, "error");
    }
  }

  // 删除全部 IPv6：二次确认；无 IPv6 时后端返回 ok + 提示
  async function deleteIpv6(inst) {
    var name = norm(inst).name;
    var ok = await RBot.confirmDialog(
      "删除 IPv6",
      "确认删除实例 " + RBot.escapeHtml(name) + " 的全部 IPv6 地址吗？<br>删除后这些地址立即不可达。",
      "确认删除", true);
    if (!ok) return;
    try {
      var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/ipv6", currentProfile), { method: "DELETE" });
      var deleted = res.deleted || [];
      if (deleted.length === 0) {
        RBot.toast(res.message || "该实例没有 IPv6", "info");
      } else {
        RBot.toast("已删除 " + deleted.length + " 个 IPv6 地址", "success");
      }
      RBot.cacheClear("inst:");
      await loadInstances();
    } catch (err) {
      RBot.toast("删除 IPv6 失败：" + err.message, "error");
    }
  }

  // 尝试修复：确认后调接口，在弹窗里展示修复报告
  async function doRepair(inst) {
    var name = norm(inst).name;
    var ok = await RBot.confirmDialog(
      "尝试修复",
      "确认尝试修复实例 " + RBot.escapeHtml(name) + " 吗？<br>只检查实例状态、主 VNIC 与公网 IP，公网 IP 缺失时重新附加一个临时公网 IP，不做其他改动。",
      "确认修复", false);
    if (!ok) return;
    try {
      var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/repair", currentProfile), { method: "POST" });
      var report = res.report || {};
      document.getElementById("repair-name").textContent = name;
      document.getElementById("repair-report").innerHTML =
        (report.checks || []).map(function (c) {
          var mark = c.status === "fixed" ? "🔧" : "✅";
          return "<li>" + mark + " " + RBot.escapeHtml(c.name) + "："
            + RBot.escapeHtml(c.detail || "") + "</li>";
        }).join("");
      document.getElementById("repair-summary").textContent =
        report.repaired
          ? "已修复：新公网 IP " + (report.new_ip || "—")
          : "检查通过，无需修复。当前公网 IP：" + (report.public_ip || "—");
      RBot.openModal("repair-modal");
      RBot.cacheClear("inst:");
      await loadInstances();
    } catch (err) {
      RBot.toast("修复失败：" + err.message, "error");
    }
  }

  // 批量操作：调 POST /api/instances/batch，汇报每台结果
  async function batchAction(action, label, confirm) {
    var ids = selectedIds();
    if (ids.length === 0) return;
    try {
      var res = await RBot.api(RBot.withProfile("/api/instances/batch", currentProfile), {
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
      RBot.cacheClear("inst:");
      await loadInstances();
    } catch (err) {
      RBot.toast(label + "失败：" + err.message, "error");
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    // 先填 profile 下拉（恢复上次选择），再按当前账号加载列表；
    // profiles 接口失败时仍按默认账号加载，不让页面空白。
    var profileSelect = document.getElementById("profile-select");
    RBot.fillProfileSelect(profileSelect).then(function (profile) {
      currentProfile = profile;
      loadInstances();
    }).catch(function () {
      loadInstances();
    });
    profileSelect.addEventListener("change", function (e) {
      currentProfile = e.target.value;
      RBot.setProfile(currentProfile);
      selected = {};
      loadInstances();
    });
    document.getElementById("btn-refresh-instances").addEventListener("click", function () { loadInstances(true); });

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
      else if (act === "detail") showDetail(instId);
      else if (act === "change-ip") {
        changeIpTarget = inst;
        document.getElementById("change-ip-name").textContent = norm(inst).name;
        RBot.openModal("change-ip-modal");
      } else if (act === "add-ipv6") addIpv6(inst);
      else if (act === "delete-ipv6") deleteIpv6(inst);
      else if (act === "resize") {
        resizeTarget = inst;
        var n = norm(inst);
        document.getElementById("resize-name").textContent = n.name;
        document.getElementById("resize-current").textContent =
          (n.ocpus != null ? n.ocpus + " 核 / " + n.memory_gb + " GB" : "未知");
        document.getElementById("resize-ocpus").value = n.ocpus != null ? n.ocpus : 2;
        document.getElementById("resize-memory").value = n.memory_gb != null ? n.memory_gb : 12;
        RBot.openModal("resize-modal");
      } else if (act === "repair") doRepair(inst);
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
            var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(instId) + "/console", currentProfile));
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
        await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/terminate", currentProfile), {
          method: "POST",
          body: { confirm: true },
        });
        RBot.toast("实例已终止，引导卷已永久删除", "success");
        RBot.closeModal("terminate-modal");
        RBot.cacheClear("inst:");
        await loadInstances();
      } catch (err) {
        RBot.toast("终止失败：" + err.message, "error");
      }
    });

    // 确认换 IP：带 confirm:true，成功后回显旧 IP → 新 IP
    document.getElementById("btn-confirm-change-ip").addEventListener("click", async function () {
      if (!changeIpTarget) return;
      var inst = changeIpTarget;
      try {
        var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/change-ip", currentProfile), {
          method: "POST",
          body: { confirm: true },
        });
        RBot.toast("换 IP 成功：" + (res.old_ip || "—") + " → " + (res.new_ip || "—"), "success");
        RBot.closeModal("change-ip-modal");
        RBot.cacheClear("inst:");
        await loadInstances();
      } catch (err) {
        RBot.toast("换 IP 失败：" + err.message, "error");
      }
    });

    // 确认升/降配：前端先按免费额度上限校验，后端再校验一次
    document.getElementById("resize-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!resizeTarget) return;
      var ocpus = parseInt(document.getElementById("resize-ocpus").value, 10);
      var memory = parseInt(document.getElementById("resize-memory").value, 10);
      if (!ocpus || ocpus < 1 || ocpus > 4) {
        RBot.toast("OCPU 必须在 1–4 之间（免费额度上限 4 核）", "error");
        return;
      }
      if (!memory || memory < 1 || memory > 24) {
        RBot.toast("内存必须在 1–24GB 之间（免费额度上限 24GB）", "error");
        return;
      }
      try {
        await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(resizeTarget.id) + "/resize", currentProfile), {
          method: "POST",
          body: { ocpus: ocpus, memory_gb: memory, confirm: true },
        });
        RBot.toast("改规格已提交（" + ocpus + " 核 / " + memory + "GB），实例可能会重启", "success");
        RBot.closeModal("resize-modal");
        RBot.cacheClear("inst:");
        await loadInstances();
      } catch (err) {
        RBot.toast("升/降配失败：" + err.message, "error");
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
        await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(renameTarget.id) + "/rename", currentProfile), {
          method: "POST",
          body: { name: newName },
        });
        RBot.toast("实例已改名为 " + newName, "success");
        RBot.closeModal("rename-modal");
        RBot.cacheClear("inst:");
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
        await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(reinstallTarget.id) + "/reinstall", currentProfile), {
          method: "POST",
          body: { confirm: true, confirm_name: confirmName },
        });
        RBot.toast("重装已提交，系统盘正在重建", "success");
        RBot.closeModal("reinstall-modal");
        RBot.cacheClear("inst:");
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
