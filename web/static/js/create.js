/* 创建实例逻辑：四卡表单、快捷开机预设、配额预检、提交创建
 *
 * 注意（与后端 API 的对应关系，以后端实际实现为准）：
 * - 后端 POST /api/create 要求 subnet_id / availability_domain 必填，
 *   ssh_keys_file 是可选项（为空时不传公钥，只靠 root 密码登录）；
 * - 可用域 / 子网 / SSH 公钥文件由 GET /api/create/defaults 按当前
 *   账号与工作区域自动填充（与机器人开机表单同一套部署配置）；SSH
 *   公钥文件只在服务器上真实存在时才填，没有就留空；多区订阅的
 *   账号有二级区域下拉，切换区域后重新填充；
 * - ssh_keys_file 必须是面板服务器上的公钥文件路径（后端直传 OCI CLI
 *   --ssh-authorized-keys-file），不支持粘贴公钥内容；
 * - A1 的 ocpus/memory_gb 以 --shape-config 传给 launch（E2 是固定规格，
 *   不传 shape-config），boot_volume_size_gbs / assign_public_ip /
 *   assign_ipv6 / min_delay / max_delay / user_data 全部已接入后端，
 *   由 Grabber 传给 OCI CLI（见 oci/grabber.py）。 */
(function () {
  "use strict";
  var MBot = window.MBot;

  var SHAPE_E2 = "VM.Standard.E2.1.Micro";
  var SHAPE_A1 = "VM.Standard.A1.Flex";

  function $(id) { return document.getElementById(id); }

  function showError(msg) {
    var el = $("create-error");
    el.textContent = msg;
    el.hidden = false;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  function hideError() { $("create-error").hidden = true; }

  // E2 是固定规格（1 OCPU / 1 GB），选中时锁定 OCPU/内存输入
  function onShapeChange() {
    var isE2 = $("shape").value === SHAPE_E2;
    $("ocpus").disabled = isE2;
    $("memory-gb").disabled = isE2;
    $("e2-hint").hidden = !isE2;
    if (isE2) {
      $("ocpus").value = 1;
      $("memory-gb").value = 1;
      selectImageByArch("x86_64");
    } else {
      selectImageByArch("aarch64");
    }
  }

  function selectImageByArch(arch) {
    var sel = $("image-id");
    for (var i = 0; i < sel.options.length; i++) {
      if (sel.options[i].getAttribute("data-arch") === arch) {
        sel.selectedIndex = i;
        return;
      }
    }
  }

  // 快捷开机预设：点一张卡片填满表单
  function applyPreset(card) {
    var shape = card.getAttribute("data-shape");
    $("shape").value = shape;
    onShapeChange();
    if (shape !== SHAPE_E2) {
      $("ocpus").value = card.getAttribute("data-ocpus");
      $("memory-gb").value = card.getAttribute("data-memory");
    }
    selectImageByArch(card.getAttribute("data-arch"));
    var disk = card.getAttribute("data-disk");
    if (disk) $("disk-gb").value = disk;
    document.querySelectorAll(".preset-card").forEach(function (c) {
      c.style.borderColor = "";
      c.style.boxShadow = "";
    });
    card.style.borderColor = "var(--accent)";
    card.style.boxShadow = "var(--shadow)";
    MBot.toast("已应用预设：" + card.querySelector("b").textContent, "success");
  }

  function collectPayload() {
    var shape = $("shape").value;
    var isE2 = shape === SHAPE_E2;
    var name = $("display-name").value.trim();
    if (!name) {
      // 实例名留空时自动生成：免费实例按类型 + 日期（与机器人同规则，
      // 如 AMD_2026-11-24 / ARM_2026-11-24）；其他形状沿用时间戳格式。
      var d = new Date();
      if (shape === SHAPE_E2 || shape === SHAPE_A1) {
        var prefix = shape === SHAPE_E2 ? "AMD" : "ARM";
        name = prefix + "_" + d.getFullYear() + "-" +
               String(d.getMonth() + 1).padStart(2, "0") + "-" +
               String(d.getDate()).padStart(2, "0");
      } else {
        name = "mbot-" + String(d.getMonth() + 1).padStart(2, "0") + String(d.getDate()).padStart(2, "0") +
               "-" + String(d.getHours()).padStart(2, "0") + String(d.getMinutes()).padStart(2, "0");
      }
    }
    return {
      profile: $("profile").value || null,
      region: $("region-select").value || null,
      shape: shape,
      ocpus: isE2 ? 1 : parseInt($("ocpus").value, 10),
      memory_gb: isE2 ? 1 : parseInt($("memory-gb").value, 10),
      // 自定义镜像 OCID 优先于下拉（跨区域创建时填该区域的镜像 OCID）
      image_id: $("image-id-custom").value.trim() || $("image-id").value,
      display_name: name,
      // 后端必填两项（卡 3 收集）+ 可选 SSH 公钥文件（为空时只靠 root 密码）
      subnet_id: $("subnet-id").value.trim(),
      availability_domain: $("availability-domain").value.trim(),
      ssh_keys_file: $("ssh-keys-file").value.trim(),
      // 已接入后端的扩展字段（Grabber 传给 OCI CLI）
      // 硬盘：47 或留空 = 镜像默认（发 null，后端不传该参数）；
      // 显式指定最少 50GB（显式传 47 会被 OCI 拒绝）。
      boot_volume_size_gbs: (function () {
        var disk = parseInt($("disk-gb").value, 10);
        return (isNaN(disk) || disk <= 47) ? null : disk;
      })(),
      assign_public_ip: $("public-ip").checked,
      assign_ipv6: $("ipv6").checked,
      min_delay: parseInt($("min-delay").value, 10) || 0,
      max_delay: parseInt($("max-delay").value, 10) || 0,
      target_cpu: $("target-cpu").value.trim(),
      user_data: $("startup-script").value,
      // root 登录：默认开启；密码前端预填 UUID，留空由后端生成。
      root_login: $("root-login").checked,
      root_password: $("root-login").checked
        ? $("root-password").value.trim() : "",
    };
  }

  function generateUuidPassword() {
    // UUID4 的 32 位十六进制（去掉连字符），与机器人端一致。
    if (window.crypto && window.crypto.randomUUID) {
      return window.crypto.randomUUID().replace(/-/g, "");
    }
    var bytes = new Uint8Array(16);
    window.crypto.getRandomValues(bytes);
    bytes[6] = (bytes[6] & 0x0f) | 0x40; // 版本 4
    bytes[8] = (bytes[8] & 0x3f) | 0x80; // 变体
    return Array.from(bytes, function (b) {
      return b.toString(16).padStart(2, "0");
    }).join("");
  }

  async function submitCreate(e) {
    e.preventDefault();
    hideError();
    var payload = collectPayload();
    var diskRaw = parseInt($("disk-gb").value, 10);
    if (!isNaN(diskRaw) && diskRaw > 47 && diskRaw < 50) {
      showError("硬盘显式指定最少 50GB：47 表示用镜像默认大小，48/49 不可用。");
      return;
    }
    if (!payload.ocpus || payload.ocpus < 1 || !payload.memory_gb || payload.memory_gb < 1) {
      showError("请填写有效的 OCPU 与内存。");
      return;
    }
    if (!payload.availability_domain || !payload.subnet_id) {
      showError("请填写可用域与子网 OCID（卡 3 网络与安全）。");
      return;
    }
    var btn = $("btn-create");
    btn.disabled = true;
    btn.textContent = "配额预检中…";
    try {
      // 先调配额预检（带当前账号与区域，预检该区域的免费额度）
      var check = await MBot.api(MBot.withProfile("/api/quota/can-launch", payload.profile, payload.region), {
        method: "POST",
        body: { shape: payload.shape, ocpus: payload.ocpus, memory_gb: payload.memory_gb },
      });
      if (!check.can_launch) {
        showError("配额不足，无法创建：" + (check.reason || "免费额度已用完"));
        return;
      }
      // 预检通过，提交创建
      btn.textContent = "创建中…";
      var result = await MBot.api("/api/create", { method: "POST", body: payload });
      MBot.toast("创建任务已提交", "success");
      if (result.root_password) {
        // root 密码只显示这一次（后端不保存、不再可查；抢机成功后
        // 还会在 Telegram / Bark 通知里带上）。
        await MBot.confirmDialog(
          "🔑 root 登录密码（只显示这一次）",
          "<p>请立即记下，面板不会再显示它：</p>" +
          "<p><code>" + MBot.escapeHtml(result.root_password) + "</code></p>" +
          "<p>实例创建完成后用 root + 该密码登录。抢机成功后，密码也会随 Telegram / Bark 通知推送。</p>",
          "已记下");
      }
      window.location.href = "/tasks";
    } catch (err) {
      showError("创建失败：" + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "开始创建";
    }
  }

  function onRootLoginChange() {
    var on = $("root-login").checked;
    $("root-password").disabled = !on;
    $("btn-regen-password").disabled = !on;
  }

  // 加载创建默认值（可用域 / 子网 / SSH 公钥文件）：后端按当前账号
  // 与工作区域从部署配置里取；SSH 公钥文件只在服务器上真实存在时才
  // 返回路径，没有就不填。切换账号或区域时重新加载。
  async function loadDefaults() {
    try {
      var profile = $("profile").value || null;
      var region = $("region-select").value || null;
      var data = await MBot.api(MBot.withProfile("/api/create/defaults", profile, region));
      if (data.availability_domain) $("availability-domain").value = data.availability_domain;
      if (data.subnet_id) $("subnet-id").value = data.subnet_id;
      if (data.ssh_keys_file) $("ssh-keys-file").value = data.ssh_keys_file;
    } catch (err) {
      // 默认值加载失败不阻塞表单（用户手填即可）。
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    // 填 profile 下拉（恢复上次选择），再填区域下拉（多区账号才显示），
    // 最后加载该账号 + 区域的网络默认值。失败时保持默认账号，仍按默认
    // 账号加载默认值，不阻塞表单。
    var profileSelect = $("profile");
    var regionSelect = $("region-select");
    MBot.fillProfileSelect(profileSelect).then(function (profile) {
      MBot.fillRegionSelect(regionSelect, profile);
      loadDefaults();
    }, loadDefaults);
    profileSelect.addEventListener("change", function (e) {
      MBot.setProfile(e.target.value);
      // 切账号时区域重置为新账号的主区（与机器人端一致）。
      MBot.setRegion(null);
      MBot.fillRegionSelect(regionSelect, e.target.value);
      loadDefaults();
    });
    regionSelect.addEventListener("change", function (e) {
      MBot.setRegion(e.target.value);
      loadDefaults();
    });
    $("shape").addEventListener("change", onShapeChange);
    onShapeChange();
    // root 密码默认预填一个随机 UUID；关掉 root 登录时禁用输入框。
    $("root-password").value = generateUuidPassword();
    $("root-login").addEventListener("change", onRootLoginChange);
    onRootLoginChange();
    $("btn-regen-password").addEventListener("click", function () {
      $("root-password").value = generateUuidPassword();
    });
    document.getElementById("preset-grid").addEventListener("click", function (e) {
      var card = e.target.closest(".preset-card");
      if (card) applyPreset(card);
    });
    document.getElementById("preset-grid").addEventListener("keydown", function (e) {
      if ((e.key === "Enter" || e.key === " ") && e.target.classList.contains("preset-card")) {
        e.preventDefault();
        applyPreset(e.target);
      }
    });
    $("create-form").addEventListener("submit", submitCreate);
  });
})();
