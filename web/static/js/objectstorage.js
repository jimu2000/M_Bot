/* 对象存储逻辑：Bucket 列表/创建/删除、对象列表/上传/删除 */
(function () {
  "use strict";
  var MBot = window.MBot;
  var buckets = [];
  var objects = [];
  var currentBucket = null;
  var deleteBucketTarget = null;
  var deleteObjectTarget = null;

  function formatSize(bytes) {
    if (bytes == null || isNaN(bytes)) return "—";
    if (bytes >= 1024 * 1024 * 1024) return (bytes / (1024 * 1024 * 1024)).toFixed(2) + " GB";
    if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(2) + " MB";
    if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " KB";
    return bytes + " B";
  }

  function showBucketsView() {
    document.getElementById("buckets-view").hidden = false;
    document.getElementById("objects-view").hidden = true;
    currentBucket = null;
  }

  function showObjectsView(bucketName) {
    currentBucket = bucketName;
    document.getElementById("objects-bucket-name").textContent = bucketName;
    document.getElementById("buckets-view").hidden = true;
    document.getElementById("objects-view").hidden = false;
  }

  function renderBuckets() {
    var tbody = document.getElementById("bucket-tbody");
    var empty = document.getElementById("bucket-empty");
    empty.hidden = buckets.length !== 0;
    if (buckets.length === 0) {
      tbody.innerHTML = "";
      return;
    }
    tbody.innerHTML = buckets.map(function (b) {
      var esc = MBot.escapeHtml;
      var access = b["public-access-type"] || b.public_access_type || "—";
      var tier = b["storage-tier"] || b.storage_tier || "—";
      return (
        '<tr data-bucket-name="' + esc(b.name) + '">' +
          "<td><b>" + esc(b.name) + "</b></td>" +
          "<td>" + esc(MBot.formatTime(b["time-created"] || b.time_created)) + "</td>" +
          "<td>" + esc(access) + "</td>" +
          "<td>" + esc(tier) + "</td>" +
          '<td><div class="row-actions">' +
            '<button type="button" class="btn btn-sm" data-act="open">打开</button>' +
            '<button type="button" class="btn btn-sm btn-danger" data-act="delete">删除</button>' +
          "</div></td>" +
        "</tr>"
      );
    }).join("");
  }

  function renderObjects() {
    var tbody = document.getElementById("object-tbody");
    if (objects.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align: center; color: var(--text-muted);">Bucket 为空</td></tr>';
      return;
    }
    tbody.innerHTML = objects.map(function (o) {
      var esc = MBot.escapeHtml;
      return (
        '<tr data-object-name="' + esc(o.name) + '">' +
          "<td><b>" + esc(o.name) + "</b></td>" +
          "<td>" + esc(formatSize(o.size)) + "</td>" +
          "<td>" + esc(MBot.formatTime(o["time-created"] || o.time_created)) + "</td>" +
          '<td><div class="row-actions">' +
            '<button type="button" class="btn btn-sm btn-danger" data-act="delete">删除</button>' +
          "</div></td>" +
        "</tr>"
      );
    }).join("");
  }

  async function loadBuckets() {
    try {
      var data = await MBot.api("/api/objectstorage/buckets");
      buckets = data.buckets || [];
    } catch (err) {
      document.getElementById("bucket-tbody").innerHTML =
        '<tr><td colspan="5" style="text-align: center; color: var(--danger);">加载失败：' +
        MBot.escapeHtml(err.message) + "</td></tr>";
      return;
    }
    renderBuckets();
  }

  async function loadObjects(bucketName) {
    try {
      var data = await MBot.api("/api/objectstorage/buckets/" + encodeURIComponent(bucketName) + "/objects");
      objects = data.objects || [];
    } catch (err) {
      document.getElementById("object-tbody").innerHTML =
        '<tr><td colspan="4" style="text-align: center; color: var(--danger);">加载失败：' +
        MBot.escapeHtml(err.message) + "</td></tr>";
      return;
    }
    renderObjects();
  }

  // 上传用 multipart（MBot.api 只发 JSON），这里直接用 fetch。
  async function uploadFile(bucketName, file, objectName) {
    var formData = new FormData();
    formData.append("file", file);
    if (objectName) formData.append("name", objectName);
    var res = await fetch("/api/objectstorage/buckets/" + encodeURIComponent(bucketName) + "/objects", {
      method: "POST",
      credentials: "same-origin",
      body: formData,
    });
    var data = null;
    try { data = await res.json(); } catch (e) { /* 忽略解析失败 */ }
    if (res.status === 401) {
      window.location.href = "/login";
      throw new Error("未登录");
    }
    if (!res.ok || (data && data.ok === false)) {
      throw new Error((data && data.error) || ("上传失败（HTTP " + res.status + "）"));
    }
    return data;
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadBuckets();
    document.getElementById("btn-refresh-buckets").addEventListener("click", function () {
      if (currentBucket) {
        loadObjects(currentBucket);
      } else {
        loadBuckets();
      }
    });
    document.getElementById("btn-new-bucket").addEventListener("click", function () {
      MBot.openModal("create-bucket-modal");
    });
    document.getElementById("btn-back-buckets").addEventListener("click", function () {
      showBucketsView();
      loadBuckets();
    });

    // Bucket 表操作按钮
    document.getElementById("bucket-tbody").addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      var row = btn.closest("[data-bucket-name]");
      var name = row.getAttribute("data-bucket-name");
      if (btn.getAttribute("data-act") === "open") {
        showObjectsView(name);
        loadObjects(name);
      } else if (btn.getAttribute("data-act") === "delete") {
        deleteBucketTarget = name;
        document.getElementById("delete-bucket-name").textContent = name;
        document.getElementById("delete-bucket-empty").checked = false;
        MBot.openModal("delete-bucket-modal");
      }
    });

    // 对象表操作按钮
    document.getElementById("object-tbody").addEventListener("click", function (e) {
      var btn = e.target.closest("[data-act]");
      if (!btn) return;
      var row = btn.closest("[data-object-name]");
      deleteObjectTarget = row.getAttribute("data-object-name");
      document.getElementById("delete-object-name").textContent = deleteObjectTarget;
      MBot.openModal("delete-object-modal");
    });

    // 确认创建 Bucket
    document.getElementById("create-bucket-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      var name = document.getElementById("bucket-name").value.trim();
      if (!name) return;
      try {
        await MBot.api("/api/objectstorage/buckets", {
          method: "POST",
          body: { name: name },
        });
        MBot.toast("Bucket 已创建", "success");
        MBot.closeModal("create-bucket-modal");
        document.getElementById("create-bucket-form").reset();
        await loadBuckets();
      } catch (err) {
        MBot.toast("创建失败：" + err.message, "error");
      }
    });

    // 确认删除 Bucket
    document.getElementById("btn-confirm-delete-bucket").addEventListener("click", async function () {
      if (!deleteBucketTarget) return;
      var empty = document.getElementById("delete-bucket-empty").checked;
      var url = "/api/objectstorage/buckets/" + encodeURIComponent(deleteBucketTarget) +
        (empty ? "?empty=true" : "");
      try {
        await MBot.api(url, { method: "DELETE" });
        MBot.toast("Bucket 已删除", "success");
        MBot.closeModal("delete-bucket-modal");
        await loadBuckets();
      } catch (err) {
        MBot.toast("删除失败：" + err.message, "error");
      }
    });

    // 确认删除对象
    document.getElementById("btn-confirm-delete-object").addEventListener("click", async function () {
      if (!currentBucket || !deleteObjectTarget) return;
      try {
        await MBot.api(
          "/api/objectstorage/buckets/" + encodeURIComponent(currentBucket) +
          "/objects/" + encodeURIComponent(deleteObjectTarget),
          { method: "DELETE" });
        MBot.toast("对象已删除", "success");
        MBot.closeModal("delete-object-modal");
        await loadObjects(currentBucket);
      } catch (err) {
        MBot.toast("删除失败：" + err.message, "error");
      }
    });

    // 上传对象
    document.getElementById("upload-form").addEventListener("submit", async function (e) {
      e.preventDefault();
      if (!currentBucket) return;
      var fileInput = document.getElementById("upload-file");
      var file = fileInput.files[0];
      if (!file) {
        MBot.toast("请先选择文件", "error");
        return;
      }
      var objectName = document.getElementById("upload-name").value.trim();
      var btn = document.getElementById("btn-upload");
      btn.disabled = true;
      btn.textContent = "上传中…";
      try {
        await uploadFile(currentBucket, file, objectName || null);
        MBot.toast("上传成功", "success");
        document.getElementById("upload-form").reset();
        await loadObjects(currentBucket);
      } catch (err) {
        MBot.toast("上传失败：" + err.message, "error");
      } finally {
        btn.disabled = false;
        btn.textContent = "上传";
      }
    });
  });
})();
