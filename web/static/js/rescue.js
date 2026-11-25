/* 救机页逻辑：前置检查 / 安全恢复 / 无盘救机 / 孤儿盘 / Netboot 向导 / 控制台连接 */
(function () {
  "use strict";
  var MBot = window.MBot;

  var instances = [];
  var backups = [];
  var currentProfile = null;
  var currentRegion = null;

  function qs(id) { return document.getElementById(id); }

  function withProfile(url) {
    return MBot.withProfile(url, currentProfile, currentRegion);
  }

  function fillInstanceSelects() {
    ["preflight-instance", "restore-instance", "diskless-instance",
     "netboot-instance"].forEach(function (id) {
      var sel = qs(id);
      if (!sel) return;
      sel.innerHTML = instances.map(function (inst, i) {
        return "<option value=\"" + i + "\">" +
          MBot.escapeHtml(inst.name || inst.id) + "（" +
          MBot.escapeHtml(inst.state || "?") + "）</option>";
      }).join("");
    });
  }

  function fillBackupSelect() {
    var sel = qs("restore-backup");
    if (!sel) return;
    sel.innerHTML = backups.map(function (b, i) {
      return "<option value=\"" + i + "\">" +
        MBot.escapeHtml(b.name || b.id) + "（" +
        MBot.escapeHtml(b.state || "?") + "，" + (b.size || "?") + " GB）</option>";
    }).join("");
  }

  function loadInstances() {
    return MBot.api(withProfile("/api/instances")).then(function (r) {
      instances = (r.instances || []).map(function (inst) {
        return {
          id: inst.instanceId || inst.id,
          name: inst.displayName || inst["display-name"] || inst.id,
          state: inst.lifecycleState || inst["lifecycle-state"] || "",
        };
      });
      fillInstanceSelects();
    });
  }

  function loadBackups() {
    return MBot.api(withProfile("/api/boot-backups")).then(function (r) {
      backups = (r.backups || []).map(function (b) {
        return {
          id: b.backupId || b.id,
          name: b.displayName || b["display-name"] || b.id,
          size: b.sizeInGBs != null ? b.sizeInGBs : b["size-in-gbs"],
          state: b.lifecycleState || b["lifecycle-state"] || "",
        };
      });
      fillBackupSelect();
    });
  }

  function loadOrphans() {
    var tbody = qs("orphan-tbody");
    return MBot.api(withProfile("/api/rescue/orphans")).then(function (r) {
      var list = r.orphans || [];
      qs("orphan-total").textContent =
        "共 " + (r.count || 0) + " 块孤儿盘，占用 " + (r.total_gb || 0) + " GB 额度。";
      if (!list.length) {
        tbody.innerHTML = "<tr><td colspan=\"4\" style=\"text-align: center; color: var(--text-muted);\">没有孤儿盘</td></tr>";
        return;
      }
      tbody.innerHTML = list.map(function (o) {
        return "<tr><td><b>" + MBot.escapeHtml(o.displayName || o.id) + "</b></td>" +
          "<td>" + MBot.escapeHtml(o.sizeInGBs) + " GB</td>" +
          "<td>" + MBot.escapeHtml(o.lifecycleState || "") + "</td>" +
          "<td><button type=\"button\" class=\"btn btn-sm btn-danger\" data-orphan=\"" +
          MBot.escapeHtml(o.volumeId || o.id) + "\">删除腾额度</button></td></tr>";
      }).join("");
    });
  }

  function loadConsoles() {
    return MBot.api(withProfile("/api/rescue/console")).then(function (r) {
      var list = r.connections || [];
      qs("console-list").textContent = list.length
        ? list.map(function (c) {
            return (c["instance-id"] || c.instanceId || "?") + " · " +
              (c["lifecycle-state"] || c.lifecycleState || "?") + "\n" +
              (c["connection-string"] || c.connectionString || "");
          }).join("\n\n")
        : "当前没有串行控制台连接。";
    });
  }

  function loadAll() {
    return Promise.all([loadInstances(), loadBackups(), loadOrphans(), loadConsoles()])
      .catch(function (err) { MBot.toast("加载失败：" + err.message); });
  }

  function selectedInstance(selectId) {
    var sel = qs(selectId);
    if (!sel || !instances.length) return null;
    return instances[parseInt(sel.value, 10)] || null;
  }

  function init() {
    var profileSelect = qs("profile-select");
    var regionSelect = qs("region-select");
    MBot.fillProfileSelect(profileSelect).then(function (profile) {
      currentProfile = profile;
      currentRegion = MBot.fillRegionSelect(regionSelect, currentProfile);
      loadAll();
    }).catch(function () {
      loadAll();
    });
    profileSelect.addEventListener("change", function (e) {
      currentProfile = e.target.value;
      MBot.setProfile(currentProfile);
      MBot.setRegion(null);
      currentRegion = MBot.fillRegionSelect(regionSelect, currentProfile);
      loadAll();
    });
    regionSelect.addEventListener("change", function (e) {
      currentRegion = e.target.value;
      MBot.setRegion(currentRegion);
      loadAll();
    });

    qs("btn-refresh-rescue").addEventListener("click", loadAll);

    qs("btn-preflight").addEventListener("click", function () {
      var inst = selectedInstance("preflight-instance");
      var action = qs("preflight-action").value;
      if (!inst) return;
      qs("preflight-result").textContent = "检查中…";
      MBot.api(withProfile("/api/rescue/preflight?instance_id=" +
        encodeURIComponent(inst.id) + "&action=" + encodeURIComponent(action))).then(function (r) {
        var lines = ["结论：" + (r.can_do ? "可以做" : "不能做")];
        (r.blockers || []).forEach(function (b) { lines.push("⛔ " + b); });
        (r.warnings || []).forEach(function (w) { lines.push("⚠️ " + w); });
        var f = r.facts || {};
        lines.push("实例状态：" + (f.instance_state || "?") +
          "，启动盘：" + (f.has_boot_volume ? "已挂载" : "未挂载") +
          "，剩余额度：" + (f.remaining_gb != null ? f.remaining_gb + " GB" : "?"));
        qs("preflight-result").textContent = lines.join("\n");
      }).catch(function (err) {
        qs("preflight-result").textContent = "检查失败：" + err.message;
      });
    });

    qs("btn-safe-restore").addEventListener("click", function () {
      var inst = selectedInstance("restore-instance");
      var sel = qs("restore-backup");
      var backup = sel && backups.length ? backups[parseInt(sel.value, 10)] : null;
      if (!inst || !backup) return;
      if (!window.confirm("安全恢复会先建新盘、原生换盘、再删旧盘，实例会停机一次。确定继续？")) return;
      qs("restore-result").textContent = "恢复中（约 3 分钟），请勿关闭页面…";
      MBot.api(withProfile("/api/rescue/safe-restore"), {
        method: "POST",
        body: JSON.stringify({
          instance_id: inst.id, backup_id: backup.id, confirm: true,
        }),
      }).then(function (r) {
        var lines = ["✅ 安全恢复完成，新盘：" + (r.new_boot_volume_id || "?")];
        lines.push("旧盘已删除：" + (r.old_volume_deleted ? "是" : "否（见警告）"));
        (r.warnings || []).forEach(function (w) { lines.push("⚠️ " + w); });
        qs("restore-result").textContent = lines.join("\n");
        loadAll();
      }).catch(function (err) {
        qs("restore-result").textContent = "恢复失败：" + err.message;
      });
    });

    qs("btn-diskless-plan").addEventListener("click", function () {
      var inst = selectedInstance("diskless-instance");
      if (!inst) return;
      qs("diskless-result").textContent = "查询中…";
      MBot.api(withProfile("/api/rescue/diskless/" + encodeURIComponent(inst.id))).then(function (r) {
        var lines = [r.message, "", r.cost_note || ""];
        (r.backups || []).forEach(function (b, i) {
          lines.push((i + 1) + ". " + (b["display-name"] || b.id) +
            "（" + (b["size-in-gbs"] || "?") + " GB）");
        });
        if (!r.can_rebuild) lines.push("当前无法从备份新建实例。");
        qs("diskless-result").textContent = lines.join("\n");
      }).catch(function (err) {
        qs("diskless-result").textContent = "查询失败：" + err.message;
      });
    });

    qs("btn-netboot").addEventListener("click", function () {
      var inst = selectedInstance("netboot-instance");
      if (!inst) return;
      qs("netboot-result").textContent = "加载中…";
      MBot.api(withProfile("/api/rescue/netboot-guide?instance_id=" +
        encodeURIComponent(inst.id)))
        .then(function (r) {
          var lines = ["适用场景：" + r.applies_when, "", "先决条件："];
          (r.prerequisites || []).forEach(function (s) { lines.push("• " + s); });
          lines.push("", "控制台步骤：");
          (r.console_steps || []).forEach(function (s, i) {
            lines.push((i + 1) + ". " + s.text + (s.manual ? "（手动）" : ""));
          });
          lines.push("", "EFI Shell 命令：");
          (r.efi_commands || []).forEach(function (c) { lines.push("  " + c); });
          lines.push("", "TFTP 节点：" + r.tftp_server);
          lines.push("", "netboot.xyz 菜单指引：");
          (r.menu_steps || []).forEach(function (s, i) {
            lines.push((i + 1) + ". " + s.text + "（手动）");
          });
          lines.push("", "DD 步骤：");
          (r.dd_steps || []).forEach(function (s, i) {
            lines.push((i + 1) + ". " + s);
          });
          qs("netboot-result").textContent = lines.join("\n");
        }).catch(function (err) {
          qs("netboot-result").textContent = "加载失败：" + err.message;
        });
    });

    qs("btn-netboot-console").addEventListener("click", function () {
      var inst = selectedInstance("netboot-instance");
      if (!inst) return;
      if (!window.confirm("为实例创建串行控制台连接？需要服务器上有 RSA 公钥。")) return;
      MBot.api(withProfile("/api/rescue/console"), {
        method: "POST",
        body: JSON.stringify({ instance_id: inst.id, confirm: true }),
      }).then(function (r) {
        qs("netboot-result").textContent =
          "控制台连接已创建（" + (r.connection["lifecycle-state"] || "?") + "）：\n" +
          (r.connection["connection-string"] || "");
        loadConsoles();
      }).catch(function (err) {
        qs("netboot-result").textContent = "创建失败：" + err.message;
      });
    });

    qs("orphan-tbody").addEventListener("click", function (ev) {
      var btn = ev.target.closest("[data-orphan]");
      if (!btn) return;
      var volumeId = btn.getAttribute("data-orphan");
      if (!window.confirm("删除这块孤儿盘腾额度？删了不可恢复。")) return;
      MBot.api(withProfile("/api/rescue/orphans/" + encodeURIComponent(volumeId) +
        "/delete"), {
        method: "POST",
        body: JSON.stringify({ confirm: true }),
      }).then(function () {
        MBot.toast("孤儿盘已删除");
        loadOrphans();
      }).catch(function (err) { MBot.toast("删除失败：" + err.message); });
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
