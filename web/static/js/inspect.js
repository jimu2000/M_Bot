/* 巡检计划页：计划增删 + 立即巡检 + 最近报告列表。 */
(function () {
  "use strict";
  var RBot = window.RBot;
  var schedules = [];

  function renderSchedules() {
    var tbody = document.getElementById("inspect-tbody");
    var empty = document.getElementById("inspect-empty");
    empty.hidden = schedules.length !== 0;
    if (schedules.length === 0) {
      tbody.innerHTML = "";
      return;
    }
    tbody.innerHTML = schedules.map(function (s) {
      var esc = RBot.escapeHtml;
      var targets = (s.targets || []).join("、");
      if (targets.length > 60) targets = targets.slice(0, 60) + "…";
      return (
        "<tr data-schedule-id=\"" + esc(s.id) + "\">" +
          "<td class=\"mono\">" + esc(s.id) + "</td>" +
          "<td class=\"mono\">" + esc(s.cron) + "</td>" +
          "<td class=\"mono\" title=\"" + esc((s.targets || []).join("\n")) + "\">" + esc(targets) + "</td>" +
          "<td>" + (s.enabled
            ? '<span class="badge badge-green">启用</span>'
            : '<span class="badge badge-gray">停用</span>') + "</td>" +
          '<td><div class="row-actions">' +
            '<button type="button" class="btn btn-sm btn-primary" data-act="run">立即巡检</button>' +
            '<button type="button" class="btn btn-sm btn-danger" data-act="delete">删除</button>' +
          "</div></td>" +
        "</tr>"
      );
    }).join("");
  }

  function renderReports(reports) {
    var tbody = document.getElementById("report-tbody");
    if (!reports || reports.length === 0) {
      tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">暂无巡检报告</td></tr>';
      return;
    }
    tbody.innerHTML = reports.map(function (r) {
      var esc = RBot.escapeHtml;
      var instances = r.instances || [];
      var running = instances.filter(function (i) { return i.state === "RUNNING"; }).length;
      var volumes = r.volumes || {};
      var errors = (r.errors || []).join("；");
      return (
        "<tr>" +
          "<td class=\"mono\">" + esc(r.scheduleId || "—") + "</td>" +
          "<td>" + esc(r.startedAt ? RBot.formatTime(r.startedAt) : "—") + "</td>" +
          "<td>" + instances.length + " 台（RUNNING " + running + "）</td>" +
          "<td>" + (volumes.count || 0) + " 块 / " + (volumes.totalGB || 0) + " GB</td>" +
          "<td>" + (r.ok
            ? '<span class="badge badge-green">正常</span>'
            : '<span class="badge badge-red" title="' + esc(errors) + '">有问题</span>') + "</td>" +
        "</tr>"
      );
    }).join("");
  }

  async function loadAll() {
    try {
      var data = await RBot.api("/api/inspect-schedules");
      schedules = data.schedules || [];
      renderSchedules();
    } catch (err) {
      document.getElementById("inspect-tbody").innerHTML =
        '<tr><td colspan="5" style="text-align: center; color: var(--danger);">加载失败：' +
        RBot.escapeHtml(err.message) + "</td></tr>";
    }
    try {
      var rep = await RBot.api("/api/inspect-reports");
      renderReports(rep.reports || []);
    } catch (err) {
      document.getElementById("report-tbody").innerHTML =
        '<tr><td colspan="5" style="text-align: center; color: var(--text-muted);">报告加载失败</td></tr>';
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadAll();
    document.getElementById("btn-refresh-inspect").addEventListener("click", loadAll);

    document.getElementById("inspect-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      var cron = document.getElementById("inspect-cron").value.trim();
      var targetsRaw = document.getElementById("inspect-targets").value.trim();
      if (!cron || !targetsRaw) {
        RBot.toast("cron 与目标实例都不能为空", "error");
        return;
      }
      var targets = targetsRaw.split(",").map(function (t) { return t.trim(); })
        .filter(function (t) { return t.length > 0; });
      try {
        await RBot.api("/api/inspect-schedules", {
          method: "POST",
          body: { cron: cron, targets: targets, enabled: true },
        });
        RBot.toast("巡检计划已添加", "success");
        document.getElementById("inspect-cron").value = "";
        await loadAll();
      } catch (err) {
        RBot.toast("添加失败：" + err.message, "error");
      }
    });

    document.getElementById("inspect-tbody").addEventListener("click", async function (e) {
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      var row = btn.closest("[data-schedule-id]");
      var id = row.getAttribute("data-schedule-id");
      var act = btn.getAttribute("data-act");
      if (act === "run") {
        try {
          await RBot.api("/api/inspect-schedules/" + encodeURIComponent(id) + "/run", { method: "POST" });
          RBot.toast("巡检已提交，稍后刷新看报告", "success");
          setTimeout(loadAll, 3000);
        } catch (err) {
          RBot.toast("提交巡检失败：" + err.message, "error");
        }
      } else if (act === "delete") {
        var ok = await RBot.confirmDialog(
          "删除巡检计划", "确认删除计划 <b>" + RBot.escapeHtml(id) + "</b> 吗？此后不再定时巡检。",
          "确认删除", true);
        if (!ok) return;
        try {
          await RBot.api("/api/inspect-schedules/" + encodeURIComponent(id), { method: "DELETE" });
          RBot.toast("巡检计划已删除", "success");
          await loadAll();
        } catch (err) {
          RBot.toast("删除失败：" + err.message, "error");
        }
      }
    });
  });
})();
