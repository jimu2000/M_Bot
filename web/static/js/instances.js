/* 实例管理逻辑：R-Bot 风格卡片布局（每台实例一张卡片）、启动/停止/重启/
   终止（含保留启动盘）、批量操作、改名、重装、串行控制台、IP 管理弹窗
   （换 IP / 附加 IPv6 / 删除 IPv6）、启动盘弹窗（详情 + 扩容）、
   升降配、尝试修复、实例详情弹窗 */
(function () {
  "use strict";
  var RBot = window.RBot;
  var instances = [];
  var volumesByInstance = {}; // 启动盘：实例 id -> /api/volumes 条目（卡片显示用）
  var selected = {}; // 选中实例的 id 集合（对象当集合用）
  var currentProfile = null; // 当前账号（profile），由 fillProfileSelect 恢复
  var currentRegion = null; // 当前工作区域，由 fillRegionSelect 恢复（多区账号才有二级下拉）
  var loading = true; // 加载中时卡片区显示占位，不闪旧数据
  var terminateTarget = null; // 待终止的实例
  var terminatePreserve = false; // 终止时是否保留引导卷
  var renameTarget = null; // 待改名的实例
  var reinstallTarget = null; // 待重装的实例
  var changeIpTarget = null; // 待换 IP 的实例
  var ipTarget = null; // IP 管理弹窗当前实例
  var bootVolumeTarget = null; // 启动盘弹窗当前实例 {instance, volumeId, size}
  var resizeTarget = null; // 待升降配的实例
  var detailTarget = null; // 详情弹窗当前实例 {id, name, state}
  var bootswapTarget = null; // 待恢复引导卷的实例
  var bootswapSelected = null; // 已选备份的 OCID
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

  // 启动盘格子：大小 · VPU + 状态徽标 + 已附加徽标；无数据时显示 "—"
  function bootVolumeCell(inst) {
    var esc = RBot.escapeHtml;
    var vol = volumesByInstance[String(inst.id)];
    if (!vol) return "—";
    var size = vol.sizeInGBs != null ? vol.sizeInGBs + " GB" : "—";
    var vpu = vol.vpusPerGB != null ? " · VPU " + vol.vpusPerGB : "";
    var state = (vol.lifecycleState || vol["lifecycle-state"] || "").toUpperCase();
    var stateHtml = "";
    if (state === "AVAILABLE") {
      stateHtml = '<span class="badge badge-green">AVAILABLE</span>';
    } else if (state) {
      stateHtml = '<span class="badge badge-amber">' + esc(state) + "</span>";
    }
    var attachedHtml = vol.attached
      ? '<span class="badge badge-blue">已附加</span>' : "";
    return '<span class="tag-chip">' + esc(size + vpu) + "</span> "
      + stateHtml + " " + attachedHtml;
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
    var grid = document.getElementById("instance-grid");
    var empty = document.getElementById("instance-empty");
    if (loading) {
      grid.innerHTML = '<p class="instance-loading">加载中…</p>';
      empty.hidden = true;
      updateBatchBar();
      return;
    }
    empty.hidden = instances.length !== 0;
    if (instances.length === 0) {
      grid.innerHTML = "";
      updateBatchBar();
      return;
    }
    grid.innerHTML = instances.map(function (raw) {
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
      var spec = (inst.ocpus != null && inst.memory_gb != null)
        ? inst.ocpus + " OCPU / " + inst.memory_gb + " GB" : "—";
      var created = inst.created ? esc(RBot.formatTime(inst.created)) : "—";
      // 按钮矩阵（R-Bot 顺序）：关机/启动、重启、串行控制台、重命名、
      // 升降级、修复、重置镜像、IP 管理、启动盘、删除、删除（保留启动盘）。
      // 启停/重启按状态显示；升降级只对 A1.Flex 显示。
      var actions = "";
      if (stopped) actions += '<button type="button" class="btn btn-sm btn-primary" data-act="start">启动</button>';
      if (running) {
        actions += '<button type="button" class="btn btn-sm" data-act="stop">关机</button>';
        actions += '<button type="button" class="btn btn-sm" data-act="restart">重启</button>';
      }
      actions += '<button type="button" class="btn btn-sm" data-act="console">串行控制台</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="rename">重命名</button>';
      if (inst.shape === "VM.Standard.A1.Flex") {
        actions += '<button type="button" class="btn btn-sm" data-act="resize">升降级</button>';
      }
      actions += '<button type="button" class="btn btn-sm" data-act="repair">修复</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="reinstall">重置镜像</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="ip-manage">IP 管理</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="boot-volume">启动盘</button>';
      actions += '<button type="button" class="btn btn-sm btn-danger" data-act="terminate">删除</button>';
      actions += '<button type="button" class="btn btn-sm" data-act="terminate-keep">删除（保留启动盘）</button>';
      var checked = Object.prototype.hasOwnProperty.call(selected, String(inst.id))
        ? " checked" : "";
      return (
        '<article class="card instance-card" data-instance-id="' + esc(inst.id) + '">' +
          '<div class="instance-card-head">' +
            '<input type="checkbox" class="row-select" data-select="' + esc(inst.id) + '"' + checked + ' aria-label="选择实例">' +
            '<button type="button" class="name-btn instance-card-name" data-act="detail" title="查看详情">' + esc(inst.name) + "</button>" +
            stateBadge(inst.state) +
          "</div>" +
          '<div class="instance-meta">' +
            '<div class="instance-meta-row"><span class="instance-meta-label">公网 IP</span><span class="instance-meta-value">' + ipCell + "</span></div>" +
            '<div class="instance-meta-row"><span class="instance-meta-label">Shape</span><span class="instance-meta-value mono">' + esc(inst.shape) + "</span></div>" +
            '<div class="instance-meta-row"><span class="instance-meta-label">规格</span><span class="instance-meta-value">' + esc(spec) + "</span></div>" +
            '<div class="instance-meta-row"><span class="instance-meta-label">启动盘</span><span class="instance-meta-value">' + bootVolumeCell(inst) + "</span></div>" +
            '<div class="instance-meta-row"><span class="instance-meta-label">创建时间</span><span class="instance-meta-value">' + created + "</span></div>" +
          "</div>" +
          '<button type="button" class="btn btn-primary instance-ssh" data-act="ssh">⌨️ SSH</button>' +
          '<div class="instance-actions">' + actions + "</div>" +
        "</article>"
      );
    }).join("");
    updateBatchBar();
  }

  // 加载实例列表（走前端缓存；force=true 时跳过前端缓存并带 refresh=1
  // 绕过服务端缓存）。后端并行取公网 IP + 30 秒服务端缓存，首次约 3-5 秒。
  // 同时并行拉启动盘列表（复用 volumes 页的 "vol:list:" 缓存键），按
  // instanceId 拼到卡片上；启动盘拉取失败时降级（卡片显示"—"），
  // 不影响实例列表。切账号/区域期间旧请求的结果直接丢弃，不覆盖新数据。
  async function loadInstances(force) {
    var profileAtStart = currentProfile;
    var regionAtStart = currentRegion;
    if (force) {
      loading = true;
      render();
    }
    try {
      var url = RBot.withProfile("/api/instances", currentProfile, currentRegion);
      if (force) url += (url.indexOf("?") === -1 ? "?" : "&") + "refresh=1";
      var data = await RBot.cachedApi("inst:" + (currentProfile || "DEFAULT") + ":" + (currentRegion || ""), url, !!force);
      if (profileAtStart !== currentProfile || regionAtStart !== currentRegion) return;
      instances = data.instances || [];
      try {
        var volUrl = RBot.withProfile("/api/volumes", currentProfile, currentRegion);
        var volData = await RBot.cachedApi("vol:list:" + (currentProfile || "DEFAULT") + ":" + (currentRegion || ""), volUrl, !!force);
        if (profileAtStart !== currentProfile || regionAtStart !== currentRegion) return;
        volumesByInstance = {};
        (volData.volumes || []).forEach(function (v) {
          var iid = v.instanceId || v["instance-id"];
          if (iid && !volumesByInstance[String(iid)]) {
            volumesByInstance[String(iid)] = v;
          }
        });
      } catch (volErr) {
        volumesByInstance = {};
      }
      // 刷新后清理已不存在实例的选中状态
      var alive = {};
      instances.forEach(function (raw) { alive[String(raw.id)] = true; });
      Object.keys(selected).forEach(function (id) {
        if (!alive[id]) delete selected[id];
      });
      loading = false;
      render();
    } catch (err) {
      if (profileAtStart !== currentProfile || regionAtStart !== currentRegion) return;
      loading = false;
      document.getElementById("instance-grid").innerHTML =
        '<p class="instance-error">加载失败：' +
        RBot.escapeHtml(err.message) + "</p>";
      document.getElementById("instance-empty").hidden = true;
    }
  }

  // 实例操作：start / stop / restart，成功后清前端缓存并刷新列表
  async function instanceAction(id, action, label) {
    try {
      await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(id) + "/" + action, currentProfile, currentRegion), { method: "POST" });
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
      var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(instId), currentProfile, currentRegion));
      var inst = res.instance || {};
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
      var bootText = "—";
      if (res.boot_volume_size_gbs != null) {
        bootText = res.boot_volume_size_gbs + " GB";
        if (res.boot_volume_vpus_per_gb != null) {
          bootText += " · VPU " + res.boot_volume_vpus_per_gb;
        }
      }
      document.getElementById("detail-boot-volume").textContent = bootText;
      document.getElementById("detail-created").textContent =
        inst["time-created"] ? RBot.formatTime(inst["time-created"]) : "—";
      detailTarget = {
        id: instId,
        name: inst["display-name"] || "—",
        state: inst["lifecycle-state"] || "",
      };
      // 引导卷恢复只对运行中 / 已停止的实例可用
      document.getElementById("detail-bootswap-btn").hidden =
        !(detailTarget.state === "RUNNING" || detailTarget.state === "STOPPED");
      RBot.openModal("detail-modal");
    } catch (err) {
      RBot.toast("加载实例详情失败：" + err.message, "error");
    }
  }

  // 引导卷恢复弹窗：列该实例启动盘的可用备份，选一个确认后起后台任务
  async function showBootswap() {
    if (!detailTarget) return;
    bootswapTarget = detailTarget;
    bootswapSelected = null;
    document.getElementById("bootswap-name").textContent = bootswapTarget.name;
    document.getElementById("bootswap-confirm-btn").disabled = true;
    var listEl = document.getElementById("bootswap-list");
    listEl.textContent = "加载中…";
    RBot.closeModal("detail-modal");
    RBot.openModal("bootswap-modal");
    try {
      var res = await RBot.api(RBot.withProfile(
        "/api/instances/" + encodeURIComponent(bootswapTarget.id) + "/boot-backups",
        currentProfile, currentRegion));
      var backups = res.backups || [];
      if (!backups.length) {
        listEl.textContent = "这台实例的启动盘还没有可用备份（AVAILABLE）。请先在启动盘页面创建备份，几分钟后可用。";
        return;
      }
      listEl.innerHTML = "";
      backups.forEach(function (b) {
        var label = document.createElement("label");
        label.className = "radio-row";
        var radio = document.createElement("input");
        radio.type = "radio";
        radio.name = "bootswap-backup";
        radio.value = b.id;
        radio.addEventListener("change", function () {
          bootswapSelected = b.id;
          document.getElementById("bootswap-confirm-btn").disabled = false;
        });
        var text = document.createElement("span");
        var created = b["time-created"] ? RBot.formatTime(b["time-created"]) : "—";
        text.textContent = (b["display-name"] || "—") + "（" +
          (b["size-in-gbs"] != null ? b["size-in-gbs"] + "GB" : "—") +
          "，" + created + "）";
        label.appendChild(radio);
        label.appendChild(text);
        listEl.appendChild(label);
      });
    } catch (err) {
      listEl.textContent = "加载备份失败：" + err.message;
    }
  }

  // IP 管理弹窗：显示当前公网 IPv4 / 内网 IP / IPv6，三个操作按钮
  // 复用已有的换 IP 弹窗与 addIpv6/deleteIpv6 流程
  async function showIpManage(inst) {
    ipTarget = inst;
    document.getElementById("ip-name").textContent = norm(inst).name;
    document.getElementById("ip-public").textContent = norm(inst).public_ip || "—";
    document.getElementById("ip-private").textContent = "加载中…";
    document.getElementById("ip-ipv6").textContent = "加载中…";
    RBot.openModal("ip-modal");
    try {
      var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id), currentProfile, currentRegion));
      if (ipTarget !== inst) return; // 弹窗期间切了实例，丢弃旧结果
      document.getElementById("ip-public").textContent = res.public_ip || "—";
      document.getElementById("ip-private").textContent = res.private_ip || "—";
      var ipv6 = res.ipv6_addresses;
      document.getElementById("ip-ipv6").textContent =
        (ipv6 && ipv6.length ? ipv6.join(", ") : "无");
    } catch (err) {
      if (ipTarget !== inst) return;
      document.getElementById("ip-private").textContent = "—";
      document.getElementById("ip-ipv6").textContent = "加载失败：" + err.message;
    }
  }

  // 启动盘弹窗：详情优先用详情接口的启动盘字段（现查，最新），
  // 查不到时退回卡片拼好的启动盘数据；都没有时报错不打开。
  // 扩容复用 POST /api/volumes/{id}/resize。
  async function showBootVolume(inst) {
    var n = norm(inst);
    var vol = null;
    try {
      var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id), currentProfile, currentRegion));
      if (res.boot_volume_id) {
        vol = {
          volumeId: res.boot_volume_id,
          sizeInGBs: res.boot_volume_size_gbs,
          vpusPerGB: res.boot_volume_vpus_per_gb,
          lifecycleState: res.boot_volume_state,
          attachmentState: res.boot_volume_attachment_state,
          attached: res.boot_volume_attachment_state === "ATTACHED"
            || res.boot_volume_attachment_state === "ATTACHING",
        };
      }
    } catch (err) {
      // 详情失败时退回列表里拼好的启动盘数据
    }
    if (!vol) {
      var joined = volumesByInstance[String(inst.id)];
      if (joined && (joined.volumeId || joined.id)) {
        vol = {
          volumeId: joined.volumeId || joined.id,
          sizeInGBs: joined.sizeInGBs != null ? joined.sizeInGBs : joined["size-in-gbs"],
          vpusPerGB: joined.vpusPerGB != null ? joined.vpusPerGB : joined["vpus-per-gb"],
          lifecycleState: joined.lifecycleState || joined["lifecycle-state"],
          attachmentState: joined.attachmentState,
          attached: !!joined.attached,
        };
      }
    }
    if (!vol || !vol.volumeId) {
      RBot.toast("未找到该实例的启动盘", "error");
      return;
    }
    bootVolumeTarget = { instance: inst, volumeId: vol.volumeId, size: vol.sizeInGBs };
    document.getElementById("boot-volume-instance").textContent = n.name;
    document.getElementById("boot-volume-size").textContent =
      vol.sizeInGBs != null ? vol.sizeInGBs + " GB" : "—";
    document.getElementById("boot-volume-vpus").textContent =
      vol.vpusPerGB != null ? vol.vpusPerGB : "—";
    document.getElementById("boot-volume-state").textContent =
      vol.lifecycleState || "—";
    document.getElementById("boot-volume-attached").textContent =
      vol.attached ? "已附加" : (vol.attachmentState || "—");
    document.getElementById("boot-volume-ocid").textContent =
      "…" + String(vol.volumeId).slice(-12);
    document.getElementById("boot-volume-ocid").title = vol.volumeId;
    document.getElementById("boot-volume-new-size").value =
      vol.sizeInGBs != null ? vol.sizeInGBs + 10 : "";
    RBot.openModal("boot-volume-modal");
  }

  // 附加 IPv6：确认后直接执行，成功后回显新地址
  async function addIpv6(inst) {
    var name = norm(inst).name;
    var ok = await RBot.confirmDialog(
      "附加 IPv6", "确认给实例 " + RBot.escapeHtml(name) + " 分配一个 IPv6 地址吗？",
      "确认附加", false);
    if (!ok) return;
    try {
      var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/ipv6", currentProfile, currentRegion), { method: "POST" });
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
      var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/ipv6", currentProfile, currentRegion), { method: "DELETE" });
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
      var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/repair", currentProfile, currentRegion), { method: "POST" });
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

  // 终止确认弹窗：删除与删除（保留启动盘）共用，按模式切换标题/警告/按钮文案
  function openTerminateModal(inst, preserve) {
    terminateTarget = inst;
    terminatePreserve = !!preserve;
    document.getElementById("terminate-name").textContent = norm(inst).name;
    if (preserve) {
      document.getElementById("terminate-title").textContent = "终止实例（保留启动盘）";
      document.getElementById("terminate-warning").textContent =
        "实例将终止，引导卷会保留下来（占用 200GB 免费额度），之后可在启动盘页面查看或删除；实例上的数据仍保存在该引导卷里。";
      document.getElementById("btn-confirm-terminate").textContent = "确认终止（保留启动盘）";
    } else {
      document.getElementById("terminate-title").textContent = "终止实例";
      document.getElementById("terminate-warning").textContent =
        "此操作不可撤销，将永久删除引导卷，实例上的数据会全部丢失。";
      document.getElementById("btn-confirm-terminate").textContent = "确认终止";
    }
    RBot.openModal("terminate-modal");
  }

  // 批量操作：调 POST /api/instances/batch，汇报每台结果
  async function batchAction(action, label, confirm) {
    var ids = selectedIds();
    if (ids.length === 0) return;
    try {
      var res = await RBot.api(RBot.withProfile("/api/instances/batch", currentProfile, currentRegion), {
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
    // 先填 profile 下拉（恢复上次选择），再填区域下拉（多区账号才显示），
    // 最后按当前账号 + 区域加载列表；profiles 接口失败时仍按默认账号加载，
    // 不让页面空白。
    var profileSelect = document.getElementById("profile-select");
    var regionSelect = document.getElementById("region-select");
    RBot.fillProfileSelect(profileSelect).then(function (profile) {
      currentProfile = profile;
      currentRegion = RBot.fillRegionSelect(regionSelect, currentProfile);
      loadInstances();
    }).catch(function () {
      loadInstances();
    });
    profileSelect.addEventListener("change", function (e) {
      currentProfile = e.target.value;
      RBot.setProfile(currentProfile);
      // 切账号时区域重置为新账号的主区（与机器人端一致）。
      RBot.setRegion(null);
      currentRegion = RBot.fillRegionSelect(regionSelect, currentProfile);
      selected = {};
      // 切账号时先清空列表并显示加载占位，再加载新账号数据，
      // 避免旧账号卡片闪一帧（v1.3.0 留下的小瑕疵）。
      instances = [];
      volumesByInstance = {};
      loading = true;
      render();
      loadInstances();
    });
    regionSelect.addEventListener("change", function (e) {
      currentRegion = e.target.value;
      RBot.setRegion(currentRegion);
      selected = {};
      instances = [];
      volumesByInstance = {};
      loading = true;
      render();
      loadInstances();
    });
    document.getElementById("btn-refresh-instances").addEventListener("click", function () { loadInstances(true); });

    // 全选 / 取消全选（批量栏里的复选框）
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

    // 卡片区事件委托：勾选 / 复制 IP / 各操作按钮
    document.getElementById("instance-grid").addEventListener("click", function (e) {
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
      var card = btn.closest("[data-instance-id]");
      var instId = card.getAttribute("data-instance-id");
      var inst = instances.find(function (x) { return String(x.id) === instId; });
      if (!inst) return;
      var act = btn.getAttribute("data-act");
      if (act === "start") instanceAction(instId, "start", "启动");
      else if (act === "stop") instanceAction(instId, "stop", "停止");
      else if (act === "restart") instanceAction(instId, "restart", "重启");
      else if (act === "detail") showDetail(instId);
      else if (act === "ssh") {
        // 终端页目前不接受 ?host=/?instance= 参数，直接跳终端页
        window.location.href = "/terminal";
      } else if (act === "ip-manage") showIpManage(inst);
      else if (act === "boot-volume") showBootVolume(inst);
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
      else if (act === "terminate") openTerminateModal(inst, false);
      else if (act === "terminate-keep") openTerminateModal(inst, true);
      else if (act === "rename") {
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
            var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(instId) + "/console", currentProfile, currentRegion));
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

    // 确认终止：带 confirm:true；保留模式多传 preserve_boot_volume:true
    document.getElementById("btn-confirm-terminate").addEventListener("click", async function () {
      if (!terminateTarget) return;
      var inst = terminateTarget;
      var preserve = terminatePreserve;
      try {
        await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/terminate", currentProfile, currentRegion), {
          method: "POST",
          body: { confirm: true, preserve_boot_volume: preserve },
        });
        RBot.toast(preserve
          ? "实例已终止，引导卷已保留（可在启动盘页面删除）"
          : "实例已终止，引导卷已永久删除", "success");
        RBot.closeModal("terminate-modal");
        RBot.cacheClear("inst:");
        if (preserve) RBot.cacheClear("vol:");
        await loadInstances();
      } catch (err) {
        RBot.toast("终止失败：" + err.message, "error");
      }
    });

    // 详情弹窗里的“引导卷恢复”按钮：打开备份选择弹窗
    document.getElementById("detail-bootswap-btn").addEventListener("click", function () {
      showBootswap();
    });

    // 引导卷恢复确认：起后台任务，跳任务页看进度
    document.getElementById("bootswap-confirm-btn").addEventListener("click", async function () {
      if (!bootswapTarget || !bootswapSelected) return;
      var btn = document.getElementById("bootswap-confirm-btn");
      btn.disabled = true;
      try {
        await RBot.api(RBot.withProfile(
          "/api/instances/" + encodeURIComponent(bootswapTarget.id) + "/restore-boot-volume",
          currentProfile, currentRegion), {
          method: "POST",
          body: { backup_id: bootswapSelected, confirm: true },
        });
        RBot.toast("引导卷恢复已开始，进度请在任务页查看", "success");
        RBot.closeModal("bootswap-modal");
        window.location.href = "/tasks";
      } catch (err) {
        RBot.toast("恢复失败：" + err.message, "error");
        btn.disabled = false;
      }
    });

    // IP 管理弹窗里的三个操作：换 IP 复用换 IP 弹窗，IPv6 复用已有流程
    document.getElementById("btn-ip-change").addEventListener("click", function () {
      if (!ipTarget) return;
      changeIpTarget = ipTarget;
      document.getElementById("change-ip-name").textContent = norm(ipTarget).name;
      RBot.closeModal("ip-modal");
      RBot.openModal("change-ip-modal");
    });
    document.getElementById("btn-ip-add-ipv6").addEventListener("click", function () {
      if (!ipTarget) return;
      var inst = ipTarget;
      RBot.closeModal("ip-modal");
      addIpv6(inst);
    });
    document.getElementById("btn-ip-delete-ipv6").addEventListener("click", function () {
      if (!ipTarget) return;
      var inst = ipTarget;
      RBot.closeModal("ip-modal");
      deleteIpv6(inst);
    });

    // 启动盘弹窗里的"备份启动卷"：配额检查 + 满了提示删最早，全在共享流程里
    document.getElementById("btn-boot-volume-backup").addEventListener("click", async function () {
      if (!bootVolumeTarget || !bootVolumeTarget.volumeId) return;
      var created = await RBot.backupBootVolume(bootVolumeTarget.volumeId, currentProfile);
      if (created) RBot.closeModal("boot-volume-modal");
    });

    // 启动盘扩容：前端先校验新容量大于当前容量，后端再校验一次
    document.getElementById("boot-volume-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!bootVolumeTarget) return;
      var size = parseInt(document.getElementById("boot-volume-new-size").value, 10);
      if (!size || size <= 0) {
        RBot.toast("新容量必须是正整数（GB）", "error");
        return;
      }
      if (bootVolumeTarget.size != null && size <= bootVolumeTarget.size) {
        RBot.toast("新容量必须大于当前容量 " + bootVolumeTarget.size + "GB", "error");
        return;
      }
      var ok = await RBot.confirmDialog(
        "确认扩容",
        "确认把实例 " + RBot.escapeHtml(norm(bootVolumeTarget.instance).name)
        + " 的启动盘扩到 " + size + "GB 吗？<br>扩容后无法缩回，扩出的部分会计入 200GB 免费额度。",
        "确认扩容", false);
      if (!ok) return;
      try {
        await RBot.api(RBot.withProfile("/api/volumes/" + encodeURIComponent(bootVolumeTarget.volumeId) + "/resize", currentProfile, currentRegion), {
          method: "POST",
          body: { size_in_gbs: size },
        });
        RBot.toast("扩容已提交，启动盘正在扩大到 " + size + "GB", "success");
        RBot.closeModal("boot-volume-modal");
        RBot.cacheClear("vol:");
        RBot.cacheClear("inst:");
        await loadInstances();
      } catch (err) {
        RBot.toast("扩容失败：" + err.message, "error");
      }
    });

    // 确认换 IP：带 confirm:true，成功后回显旧 IP → 新 IP
    document.getElementById("btn-confirm-change-ip").addEventListener("click", async function () {
      if (!changeIpTarget) return;
      var inst = changeIpTarget;
      try {
        var res = await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(inst.id) + "/change-ip", currentProfile, currentRegion), {
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
        await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(resizeTarget.id) + "/resize", currentProfile, currentRegion), {
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
        await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(renameTarget.id) + "/rename", currentProfile, currentRegion), {
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
        await RBot.api(RBot.withProfile("/api/instances/" + encodeURIComponent(reinstallTarget.id) + "/reinstall", currentProfile, currentRegion), {
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
