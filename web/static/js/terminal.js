// SSH terminal frontend: multi-tab xterm.js + WebSocket. SSH 终端前端：多标签 xterm.js + WebSocket。
// Backend protocol, see web/ssh.py: input/resize/ping upstream, output (base64)/closed downstream. 后端协议见 web/ssh.py：input/resize/ping 上行，output（base64）/closed 下行。
// Reconnect after a drop: first replay output via the snapshot API, then reattach the WebSocket. 断线重连：先调 snapshot 接口回放输出，再重新接上 WebSocket。

'use strict';

// ---------- 配置 ----------
const MAX_RECONNECT = 3;  // Max automatic WebSocket reconnect attempts after a drop. WebSocket 断线自动重连次数。
const RECONNECT_DELAYS = [1000, 2000, 3000];  // Wait before each reconnect (milliseconds). 每次重连的等待（毫秒）。
const STATS_INTERVAL_MS = 5000;  // /api/system poll interval. /api/system 轮询间隔。

// ---------- 小工具 ----------
function $(id) { return document.getElementById(id); }

function showToast(text, ms = 3000) {
  const toast = $('toast');
  toast.textContent = text;
  toast.classList.remove('hidden');
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => toast.classList.add('hidden'), ms);
}

/** base64 -> Uint8Array（终端输出可能含二进制/控制字符，后端用 base64 包一层）。 */
function b64ToBytes(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function wsUrl(sessionId) {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/ws/ssh/${sessionId}`;
}

// ---------- 多行粘贴确认弹窗（返回 Promise<boolean>） ----------
function confirmPaste(lineCount) {
  return new Promise((resolve) => {
    const modal = $('paste-modal');
    $('paste-modal-text').textContent =
      `粘贴内容含 ${lineCount} 行，确认执行？`;
    modal.classList.remove('hidden');
    const done = (ok) => {
      modal.classList.add('hidden');
      $('paste-confirm').onclick = null;
      $('paste-cancel').onclick = null;
      resolve(ok);
    };
    $('paste-confirm').onclick = () => done(true);
    $('paste-cancel').onclick = () => done(false);
  });
}

// ---------- 终端管理器：每标签一个 xterm 实例 + 一条 WebSocket ----------
class TerminalManager {
  constructor() {
    this.tabs = new Map();  // sessionId -> {term, fit, ws, label, el, tabEl, reconnects, closed}. sessionId -> {term, fit, ws, label, el, tabEl, reconnects, closed}。
    this.activeId = null;
    this.hosts = [];  // Saved hosts read from /api/hosts. /api/hosts 读到的已保存主机。
    this.statsTimer = null;
  }

  // ----- 标签页 -----
  openTab(sessionId, label) {
    const container = $('terminal-container');
    const pane = document.createElement('div');
    pane.className = 'terminal-pane';
    pane.id = `pane-${sessionId}`;
    container.appendChild(pane);

    const term = new Terminal({
      cursorBlink: true,
      fontSize: 14,
      fontFamily: '"JetBrains Mono", "Fira Code", Menlo, Consolas, monospace',
      theme: { background: '#05060d', foreground: '#d6dae8' },
      allowProposedApi: true,
    });
    const fit = new FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open(pane);
    fit.fit();

    const tab = {
      term, fit, ws: null, label, el: pane,
      tabEl: this._createTabElement(sessionId, label),
      reconnects: 0, closed: false,
    };
    this.tabs.set(sessionId, tab);
    this.switchTab(sessionId);

    // Keyboard input: single chars are sent directly; multi-line content (paste) goes through paste protection. 键盘输入：单字符直接发；多行内容（粘贴）走粘贴保护。
    // Note: xterm.js paste arrives via onData as one whole block, unlike per-key typing, so checking "contains multiple lines" in onData is enough to catch multi-line pastes. 说明：xterm.js 的粘贴经 onData 以整块文本到达，区别于逐字按键，因此在 onData 里按“是否含多行”判断即可拦截多行粘贴。
    term.onData(async (data) => {
      if (tab.closed || !tab.ws || tab.ws.readyState !== WebSocket.OPEN) return;
      const lines = data.split(/\r\n|\r|\n/).filter((l) => l.length > 0);
      if (lines.length > 1) {
        const ok = await confirmPaste(lines.length);
        if (tab.closed || !tab.ws || tab.ws.readyState !== WebSocket.OPEN) return;
        if (ok) {
          // Confirm: convert newlines to Enter so the remote side executes line by line. 确认执行：换行转回车，让远端逐行执行。
          tab.ws.send(JSON.stringify({
            type: 'input', data: data.replace(/\r\n|\n/g, '\r'),
          }));
        } else {
          // Cancel: paste only the first line of text, without Enter, without executing. 取消：只粘贴第一行文本，不带回车，不执行。
          tab.ws.send(JSON.stringify({ type: 'input', data: lines[0] }));
        }
      } else {
        tab.ws.send(JSON.stringify({ type: 'input', data }));
      }
    });

    term.onResize(({ cols, rows }) => {
      if (tab.ws && tab.ws.readyState === WebSocket.OPEN) {
        tab.ws.send(JSON.stringify({ type: 'resize', cols, rows }));
      }
    });

    this._connectWS(sessionId);
    return tab;
  }

  _createTabElement(sessionId, label) {
    const tabEl = document.createElement('div');
    tabEl.className = 'tab';
    tabEl.innerHTML =
      `<span class="tab-dot"></span><span class="tab-label"></span>` +
      `<button class="tab-close" title="关闭会话">×</button>`;
    tabEl.querySelector('.tab-label').textContent = label;
    tabEl.addEventListener('click', (e) => {
      if (e.target.classList.contains('tab-close')) {
        this.closeTab(sessionId);
      } else {
        this.switchTab(sessionId);
      }
    });
    $('tab-list').appendChild(tabEl);
    return tabEl;
  }

  switchTab(sessionId) {
    this.activeId = sessionId;
    for (const [id, tab] of this.tabs) {
      const active = id === sessionId;
      tab.el.classList.toggle('hidden', !active);
      tab.tabEl.classList.toggle('active', active);
      if (active) {
        tab.fit.fit();
        tab.term.focus();
      }
    }
    $('terminal-empty').classList.add('hidden');
    this._refreshStats();
    // When the SFTP panel is open, switching tabs refreshes the file list for the new session. SFTP 面板开着时，切标签要按新会话刷新文件列表。
    if (sftpPanel && sftpPanel.isOpen()) sftpPanel.load();
  }

  async closeTab(sessionId) {
    const tab = this.tabs.get(sessionId);
    if (!tab) return;
    tab.closed = true;
    if (tab.ws) tab.ws.close();
    tab.term.dispose();
    tab.el.remove();
    tab.tabEl.remove();
    this.tabs.delete(sessionId);
    try {
      await fetch(`/api/ssh/sessions/${sessionId}`, { method: 'DELETE' });
    } catch (e) { /* 后端会话由空闲清理兜底 */ }
    if (this.activeId === sessionId) {
      const next = this.tabs.keys().next();
      if (!next.done) this.switchTab(next.value);
      else {
        this.activeId = null;
        $('terminal-empty').classList.remove('hidden');
        $('sys-stats').classList.add('hidden');
        if (sftpPanel && sftpPanel.isOpen()) sftpPanel.close();
      }
    }
  }

  // ----- WebSocket -----
  _connectWS(sessionId) {
    const tab = this.tabs.get(sessionId);
    if (!tab || tab.closed) return;
    const ws = new WebSocket(wsUrl(sessionId));
    tab.ws = ws;

    ws.onopen = () => {
      tab.reconnects = 0;
      tab.tabEl.classList.remove('reconnecting', 'disconnected');
      ws.send(JSON.stringify({
        type: 'resize', cols: tab.term.cols, rows: tab.term.rows,
      }));
    };

    ws.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }
      if (msg.type === 'output' && msg.data) {
        tab.term.write(b64ToBytes(msg.data));
      } else if (msg.type === 'closed') {
        tab.term.write(`\r\n\x1b[1;31m[SSH 会话已断开：${msg.reason || ''}]\x1b[0m\r\n`);
        tab.tabEl.classList.add('disconnected');
      }
      // pong: heartbeat reply, nothing to do. pong：心跳回应，无需处理。
    };

    ws.onclose = () => {
      if (tab.closed) return;
      tab.tabEl.classList.add('reconnecting');
      this._reconnect(sessionId);
    };

    ws.onerror = () => { /* 出错后 onclose 会触发重连 */ };
  }

  /** 断线自动重连：先调 snapshot 回放输出，再重新接上 WebSocket。 */
  async _reconnect(sessionId) {
    const tab = this.tabs.get(sessionId);
    if (!tab || tab.closed) return;
    if (tab.reconnects >= MAX_RECONNECT) {
      tab.tabEl.classList.remove('reconnecting');
      tab.tabEl.classList.add('disconnected');
      tab.term.write('\r\n\x1b[1;31m[重连失败，请关闭本窗口后重新连接]\x1b[0m\r\n');
      return;
    }
    const delay = RECONNECT_DELAYS[tab.reconnects] || 3000;
    tab.reconnects++;
    await new Promise((r) => setTimeout(r, delay));
    if (tab.closed) return;
    try {
      const resp = await fetch(`/api/ssh/sessions/${sessionId}/snapshot`);
      if (resp.ok) {
        const snap = await resp.json();
        tab.term.clear();
        if (snap.output) tab.term.write(snap.output);
      }
    } catch (e) { /* 快照失败也继续重连 WS */ }
    if (!tab.closed) this._connectWS(sessionId);
  }

  /** 向当前终端发送文本（收藏命令用）：execute=true 时末尾加回车直接执行，
      否则只填入不执行。无活动会话或未连接时返回 false。 */
  sendText(text, execute = false) {
    if (!this.activeId) {
      showToast('请先连接一个 SSH 会话');
      return false;
    }
    const tab = this.tabs.get(this.activeId);
    if (!tab || !tab.ws || tab.ws.readyState !== WebSocket.OPEN) {
      showToast('终端未连接');
      return false;
    }
    tab.ws.send(JSON.stringify({
      type: 'input', data: execute ? text + '\r' : text,
    }));
    tab.term.focus();
    return true;
  }

  // ----- 新建连接 -----
  async loadHosts() {
    try {
      const resp = await fetch('/api/hosts');
      if (!resp.ok) return;
      const data = await resp.json();
      this.hosts = Array.isArray(data) ? data : (data.hosts || []);
      const select = $('conn-host-select');
      select.innerHTML = '<option value="">手动输入…</option>';
      for (const h of this.hosts) {
        const opt = document.createElement('option');
        opt.value = h.id;
        opt.textContent = `${h.name || h.host} (${h.host})`;
        select.appendChild(opt);
      }
    } catch (e) { /* /api/hosts 未就绪时只用手动输入 */ }
  }

  async createConnection(form) {
    const body = {};
    if (form.hostId) body.host_id = form.hostId;
    body.host = form.host;
    body.port = form.port;
    body.username = form.username;
    if (form.password) body.password = form.password;
    if (form.keyPath) body.key_path = form.keyPath;
    const resp = await fetch('/api/ssh/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || '连接失败');
    this.openTab(data.session_id, `${data.username}@${data.host}`);
    return data;
  }

  // ----- 资源状态轮询（/api/system，每 5 秒） -----
  startStatsPolling() {
    this._refreshStats();
    this.statsTimer = setInterval(() => this._refreshStats(), STATS_INTERVAL_MS);
  }

  async _refreshStats() {
    const badge = $('sys-stats');
    if (!this.activeId) { badge.classList.add('hidden'); return; }
    try {
      const resp = await fetch('/api/system');
      if (!resp.ok) throw new Error('no stats');
      const s = await resp.json();
      // Compatible with two response formats: {cpu_percent, mem_percent, disk_percent} or {cpu: {percent}, memory: {...}, disk: {...}}. 兼容两种返回格式：{cpu_percent, mem_percent, disk_percent} 或 {cpu: {percent}, memory: {...}, disk: {...}}。
      const cpu = s.cpu_percent ?? s.cpu?.percent ?? s.cpu;
      const mem = s.mem_percent ?? s.memory?.percent ?? s.mem;
      const disk = s.disk_percent ?? s.disk?.percent ?? s.disk;
      $('stat-cpu').textContent = `CPU ${fmtPct(cpu)}`;
      $('stat-mem').textContent = `MEM ${fmtPct(mem)}`;
      $('stat-disk').textContent = `DISK ${fmtPct(disk)}`;
      badge.classList.remove('hidden');
    } catch (e) {
      badge.classList.add('hidden');  // Hide when /api/system is not ready, without erroring. /api/system 未就绪时隐藏，不报错。
    }
  }
}

function fmtPct(v) {
  if (v === undefined || v === null) return '--';
  if (typeof v === 'number') return `${v.toFixed(1)}%`;
  return String(v);
}

// ---------- SFTP file panel ----------
// Backend API, see web/sftp.py; paths are always validated by the backend (confined to the home directory). 后端接口见 web/sftp.py；路径一律由后端校验（限定在家目录内）。
const SFTP_MAX_EDIT = 1024 * 1024;  // Same as the backend MAX_EDIT_BYTES. 与后端 MAX_EDIT_BYTES 一致。
const CHUNKED_UPLOAD_THRESHOLD = 1024 * 1024;  // Files over 1 MB use chunked upload. 超过 1MB 的文件走分块上传。
// For the frontend double-click check (a subset of the backend allowlist; the backend is the final check, so a miss only falls back to download). 前端双击判断用（后端白名单的子集；后端是最终校验，漏了也只是走下载）。
const SFTP_TEXT_EXTS = new Set([
  'txt', 'md', 'log', 'json', 'yaml', 'yml', 'toml', 'ini', 'cfg', 'conf',
  'sh', 'py', 'js', 'ts', 'css', 'html', 'xml', 'csv', 'sql',
  'c', 'h', 'cpp', 'go', 'rs', 'java', 'rb', 'php', 'pl', 'lua',
]);

function fmtSize(bytes) {
  if (bytes === undefined || bytes === null) return '--';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function fmtTime(mtime) {
  if (!mtime) return '--';
  return new Date(mtime * 1000).toLocaleString('zh-CN', { hour12: false });
}

function sftpJoin(dir, name) {
  if (!dir || dir === '/') return `/${name}`;
  return dir.endsWith('/') ? dir + name : `${dir}/${name}`;
}

class SFTPPanel {
  constructor(mgr) {
    this.mgr = mgr;
    this.currentPath = null;
    this.home = null;
    this.entries = [];
    this.editingPath = null;
    this._bindUI();
  }

  isOpen() { return $('sftp-panel').classList.contains('open'); }

  open() {
    if (!this.mgr.activeId) {
      showToast('请先连接一个 SSH 会话');
      return;
    }
    $('sftp-panel').classList.add('open');
    this.load();
  }

  close() { $('sftp-panel').classList.remove('open'); }

  toggle() { this.isOpen() ? this.close() : this.open(); }

  /** 调 SFTP 接口：统一处理 {detail} / {ok:false, error} 两种错误信封。 */
  async _api(url, options = {}) {
    const resp = await fetch(url, options);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.error || `请求失败（${resp.status}）`);
    }
    return data;
  }

  _sessionLabel() {
    const tab = this.mgr.activeId && this.mgr.tabs.get(this.mgr.activeId);
    return tab ? tab.label : '';
  }

  async load(path) {
    const sid = this.mgr.activeId;
    if (!sid) {
      showToast('请先连接一个 SSH 会话');
      this.close();
      return;
    }
    $('sftp-session').textContent = this._sessionLabel();
    const listEl = $('sftp-list');
    listEl.innerHTML = '<div class="sftp-empty">加载中…</div>';
    try {
      const url = `/api/ssh/sessions/${sid}/sftp/list` +
        (path ? `?path=${encodeURIComponent(path)}` : '');
      const data = await this._api(url);
      this.currentPath = data.path;
      this.home = data.home || data.path;
      this.entries = data.entries || [];
      this._renderBreadcrumb();
      this._renderList();
      $('sftp-status').textContent =
        `${this.entries.length} 项 · ${this.currentPath}`;
    } catch (err) {
      listEl.innerHTML = '';
      const div = document.createElement('div');
      div.className = 'sftp-empty sftp-error';
      div.textContent = err.message;
      listEl.appendChild(div);
      $('sftp-status').textContent = '';
    }
  }

  _renderBreadcrumb() {
    const bar = $('sftp-breadcrumb');
    bar.innerHTML = '';
    const path = this.currentPath || this.home || '/';
    // The home directory is the root: it shows as "家目录" and each level below is clickable. 家目录为根：显示“家目录”，其下各级可点。
    const homeBtn = document.createElement('button');
    homeBtn.className = 'sftp-crumb';
    homeBtn.textContent = '家目录';
    homeBtn.addEventListener('click', () => this.load(this.home));
    bar.appendChild(homeBtn);
    if (path !== this.home && this.home && path.startsWith(this.home)) {
      const rest = path.slice(this.home.length).split('/').filter(Boolean);
      let acc = this.home;
      for (const part of rest) {
        const sep = document.createElement('span');
        sep.className = 'sftp-crumb-sep';
        sep.textContent = '/';
        bar.appendChild(sep);
        acc = sftpJoin(acc, part);
        const btn = document.createElement('button');
        btn.className = 'sftp-crumb';
        btn.textContent = part;
        const target = acc;
        btn.addEventListener('click', () => this.load(target));
        bar.appendChild(btn);
      }
    }
    $('sftp-up').disabled = !this.home || path === this.home;
  }

  _renderList() {
    const listEl = $('sftp-list');
    listEl.innerHTML = '';
    if (!this.entries.length) {
      const div = document.createElement('div');
      div.className = 'sftp-empty';
      div.textContent = '空目录';
      listEl.appendChild(div);
      return;
    }
    for (const entry of this.entries) {
      const row = document.createElement('div');
      row.className = 'sftp-row' + (entry.is_dir ? ' sftp-row-dir' : '');
      if (entry.outside_home) row.classList.add('sftp-row-outside');
      if (entry.broken) row.classList.add('sftp-row-broken');

      const nameEl = document.createElement('span');
      nameEl.className = 'sftp-name';
      nameEl.textContent = entry.name + (entry.is_dir ? '/' : '') +
        (entry.is_symlink ? ' →' : '') + (entry.broken ? '（断链）' : '');
      nameEl.title = entry.permissions || '';
      row.appendChild(nameEl);

      const sizeEl = document.createElement('span');
      sizeEl.className = 'sftp-size';
      sizeEl.textContent = entry.is_dir ? '--' : fmtSize(entry.size);
      row.appendChild(sizeEl);

      const timeEl = document.createElement('span');
      timeEl.className = 'sftp-time';
      timeEl.textContent = fmtTime(entry.mtime);
      row.appendChild(timeEl);

      const actions = document.createElement('span');
      actions.className = 'sftp-actions';
      const fullPath = sftpJoin(this.currentPath, entry.name);
      if (!entry.is_dir && !entry.outside_home && !entry.broken) {
        const dl = document.createElement('button');
        dl.className = 'sftp-action';
        dl.textContent = '下载';
        dl.addEventListener('click', (e) => { e.stopPropagation(); this.download(fullPath); });
        actions.appendChild(dl);
      }
      if (!entry.outside_home) {
        const rn = document.createElement('button');
        rn.className = 'sftp-action';
        rn.textContent = '改名';
        rn.addEventListener('click', (e) => { e.stopPropagation(); this.rename(entry.name); });
        actions.appendChild(rn);
      }
      const del = document.createElement('button');
      del.className = 'sftp-action sftp-action-danger';
      del.textContent = '删除';
      del.addEventListener('click', (e) => { e.stopPropagation(); this.deleteEntry(entry); });
      actions.appendChild(del);
      row.appendChild(actions);

      if (entry.outside_home) {
        row.title = '符号链接指向家目录之外，无法访问';
      } else if (entry.is_dir && !entry.broken) {
        row.title = '双击进入目录';
        row.addEventListener('dblclick', () => this.load(fullPath));
      } else if (!entry.is_dir && !entry.broken) {
        row.title = '双击打开（文本文件在线编辑，其他文件下载）';
        row.addEventListener('dblclick', () => this.openFile(entry, fullPath));
      }
      listEl.appendChild(row);
    }
  }

  download(path) {
    const sid = this.mgr.activeId;
    if (!sid) return;
    const a = document.createElement('a');
    a.href = `/api/ssh/sessions/${sid}/sftp/download?path=${encodeURIComponent(path)}`;
    a.download = '';
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  openFile(entry, fullPath) {
    const ext = (entry.name.split('.').pop() || '').toLowerCase();
    const isText = !entry.name.includes('.') || SFTP_TEXT_EXTS.has(ext);
    if (isText && entry.size <= SFTP_MAX_EDIT) this.openEditor(fullPath);
    else this.download(fullPath);
  }

  async uploadFiles(files) {
    const sid = this.mgr.activeId;
    if (!sid || !files.length) return;
    for (const file of files) {
      // Large files use chunked upload (with a progress bar, cancellable); small files use the original single upload. 大文件走分块上传（带进度条，可取消）；小文件走原单次上传。
      if (file.size > CHUNKED_UPLOAD_THRESHOLD) {
        await this._uploadChunked(sid, file);
      } else {
        $('sftp-status').textContent = `正在上传 ${file.name}…`;
        try {
          const form = new FormData();
          form.append('file', file);
          const url = `/api/ssh/sessions/${sid}/sftp/upload` +
            `?path=${encodeURIComponent(this.currentPath || '')}`;
          await this._api(url, { method: 'POST', body: form });
          showToast(`已上传 ${file.name}`);
        } catch (err) {
          showToast(`上传 ${file.name} 失败：${err.message}`);
        }
      }
    }
    this.load(this.currentPath);
  }

  /** 分块上传（阶段 5 工作包 E）：init -> chunk 逐块 -> complete，
      进度条实时更新；点取消则发 cancel（带 confirm=true）删临时分块。 */
  async _uploadChunked(sid, file) {
    const progressEl = $('sftp-upload-progress');
    const nameEl = $('sftp-upload-name');
    const fillEl = $('sftp-upload-fill');
    const textEl = $('sftp-upload-text');
    const cancelBtn = $('sftp-upload-cancel');
    const base = `/api/ssh/sessions/${sid}/sftp/upload`;
    const showProgress = (received, total) => {
      const pct = total > 0 ? Math.min(100, Math.round((received / total) * 100)) : 0;
      fillEl.style.width = `${pct}%`;
      textEl.textContent = `${fmtSize(received)} / ${fmtSize(total)}（${pct}%）`;
      $('sftp-status').textContent = `正在上传 ${file.name}… ${pct}%`;
    };
    let uploadId = null;
    let cancelled = false;
    const onCancel = () => { cancelled = true; };
    cancelBtn.addEventListener('click', onCancel, { once: true });
    try {
      progressEl.hidden = false;
      nameEl.textContent = file.name;
      showProgress(0, file.size);
      const init = await this._api(`${base}/init`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          filename: file.name,
          size: file.size,
          path: this.currentPath || '',
        }),
      });
      uploadId = init.uploadId;
      const chunkSize = init.chunkSize || (1024 * 1024);
      const totalChunks = init.totalChunks || Math.ceil(file.size / chunkSize);
      for (let i = 0; i < totalChunks; i++) {
        if (cancelled) throw new Error('__cancelled__');
        const start = i * chunkSize;
        const blob = file.slice(start, Math.min(file.size, start + chunkSize));
        const form = new FormData();
        form.append('file', blob, file.name);
        const progress = await this._api(
          `${base}/chunk?uploadId=${encodeURIComponent(uploadId)}&chunkIndex=${i}`,
          { method: 'POST', body: form });
        showProgress(progress.receivedBytes || 0, progress.totalBytes || file.size);
      }
      if (cancelled) throw new Error('__cancelled__');
      await this._api(`${base}/complete`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ uploadId }),
      });
      showToast(`已上传 ${file.name}`);
    } catch (err) {
      if (err.message === '__cancelled__' && uploadId) {
        // User cancelled: call cancel to delete the temp chunks (needs confirm=true). 用户取消：调 cancel 删临时分块（需 confirm=true）。
        try {
          await this._api(`${base}/cancel`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ uploadId, confirm: true }),
          });
        } catch (e) { /* 取消失败时临时分块由后端超期清理兜底 */ }
        showToast(`已取消上传 ${file.name}`);
      } else {
        showToast(`上传 ${file.name} 失败：${err.message}`);
      }
    } finally {
      cancelBtn.removeEventListener('click', onCancel);
      progressEl.hidden = true;
      fillEl.style.width = '0%';
    }
  }

  async deleteEntry(entry) {
    const sid = this.mgr.activeId;
    if (!sid) return;
    const fullPath = sftpJoin(this.currentPath, entry.name);
    const tip = entry.is_dir ? '（仅空目录可删除）' : '';
    if (!window.confirm(`确定删除 ${entry.name}？${tip}`)) return;
    try {
      await this._api(
        `/api/ssh/sessions/${sid}/sftp?path=${encodeURIComponent(fullPath)}`,
        { method: 'DELETE' });
      showToast(`已删除 ${entry.name}`);
      this.load(this.currentPath);
    } catch (err) {
      showToast(`删除失败：${err.message}`);
    }
  }

  async rename(oldName) {
    const sid = this.mgr.activeId;
    if (!sid) return;
    const newName = window.prompt('新名称：', oldName);
    if (!newName || newName === oldName) return;
    if (newName.includes('/') || newName === '.' || newName === '..') {
      showToast('名称不能含 /');
      return;
    }
    try {
      await this._api(`/api/ssh/sessions/${sid}/sftp/rename`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          src: sftpJoin(this.currentPath, oldName),
          dst: sftpJoin(this.currentPath, newName),
        }),
      });
      showToast('已重命名');
      this.load(this.currentPath);
    } catch (err) {
      showToast(`重命名失败：${err.message}`);
    }
  }

  async mkdir() {
    const sid = this.mgr.activeId;
    if (!sid) return;
    const name = window.prompt('新目录名：');
    if (!name) return;
    try {
      await this._api(`/api/ssh/sessions/${sid}/sftp/mkdir`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: this.currentPath, name }),
      });
      showToast(`已新建目录 ${name}`);
      this.load(this.currentPath);
    } catch (err) {
      showToast(`新建目录失败：${err.message}`);
    }
  }

  async openEditor(path) {
    const sid = this.mgr.activeId;
    if (!sid) return;
    try {
      const data = await this._api(
        `/api/ssh/sessions/${sid}/sftp/file?path=${encodeURIComponent(path)}`);
      this.editingPath = data.path;
      $('sftp-editor-title').textContent = `编辑 ${data.path}`;
      $('sftp-editor-text').value = data.content;
      $('sftp-editor-error').classList.add('hidden');
      $('sftp-editor-modal').classList.remove('hidden');
      $('sftp-editor-text').focus();
    } catch (err) {
      showToast(`打开失败：${err.message}`);
    }
  }

  async saveEditor() {
    const sid = this.mgr.activeId;
    if (!sid || !this.editingPath) return;
    const errEl = $('sftp-editor-error');
    errEl.classList.add('hidden');
    try {
      await this._api(`/api/ssh/sessions/${sid}/sftp/file`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          path: this.editingPath,
          content: $('sftp-editor-text').value,
        }),
      });
      $('sftp-editor-modal').classList.add('hidden');
      showToast('已保存');
      this.load(this.currentPath);
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove('hidden');
    }
  }

  _bindUI() {
    $('sftp-close').addEventListener('click', () => this.close());
    $('sftp-refresh').addEventListener('click', () => this.load(this.currentPath));
    $('sftp-up').addEventListener('click', () => {
      if (!this.currentPath || !this.home || this.currentPath === this.home) return;
      const parent = this.currentPath.replace(/\/[^/]+\/?$/, '') || '/';
      this.load(parent);
    });
    $('sftp-mkdir').addEventListener('click', () => this.mkdir());
    $('sftp-upload-btn').addEventListener('click', () => $('sftp-file-input').click());
    $('sftp-file-input').addEventListener('change', (e) => {
      this.uploadFiles(e.target.files);
      e.target.value = '';  // Allow re-selecting the same file to upload it again. 允许重复选同一文件再次上传。
    });
    const panel = $('sftp-panel');
    panel.addEventListener('dragover', (e) => {
      e.preventDefault();
      panel.classList.add('sftp-dragover');
    });
    panel.addEventListener('dragleave', () => panel.classList.remove('sftp-dragover'));
    panel.addEventListener('drop', (e) => {
      e.preventDefault();
      panel.classList.remove('sftp-dragover');
      this.uploadFiles(e.dataTransfer.files);
    });
    $('sftp-editor-save').addEventListener('click', () => this.saveEditor());
  }
}

// ---------- Favorites panel ----------
// Backend API: GET/POST/DELETE /api/term-favorites. 后端接口：GET/POST/DELETE /api/term-favorites。
class FavoritesPanel {
  constructor(mgr) {
    this.mgr = mgr;
    this.items = [];
    this._bindUI();
  }

  isOpen() { return $('fav-panel').classList.contains('open'); }

  open() {
    $('fav-panel').classList.add('open');
    this.load();
  }

  close() { $('fav-panel').classList.remove('open'); }

  toggle() { this.isOpen() ? this.close() : this.open(); }

  async _api(url, options = {}) {
    const resp = await fetch(url, options);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.error || `请求失败（${resp.status}）`);
    }
    return data;
  }

  async load() {
    const listEl = $('fav-list');
    try {
      const data = await this._api('/api/term-favorites');
      this.items = data.items || [];
      this._render();
    } catch (err) {
      listEl.innerHTML = '<div class="sftp-empty sftp-error">加载失败：' +
        err.message.replace(/</g, '&lt;') + '</div>';
    }
  }

  _render() {
    const listEl = $('fav-list');
    if (this.items.length === 0) {
      listEl.innerHTML = '<div class="sftp-empty">还没有收藏命令<br>在下方输入名称与命令保存。</div>';
      return;
    }
    listEl.innerHTML = '';
    for (const item of this.items) {
      const row = document.createElement('div');
      row.className = 'fav-item';
      const nameEl = document.createElement('div');
      nameEl.className = 'fav-item-name';
      nameEl.textContent = item.name;
      const cmdEl = document.createElement('div');
      cmdEl.className = 'fav-item-cmd';
      cmdEl.textContent = item.command;
      cmdEl.title = '点击填入当前终端';
      cmdEl.addEventListener('click', () => {
        if (this.mgr.sendText(item.command, false)) showToast(`已填入：${item.name}`);
      });
      const actions = document.createElement('div');
      actions.className = 'fav-item-actions';
      const runBtn = document.createElement('button');
      runBtn.className = 'sftp-action';
      runBtn.textContent = '▶ 执行';
      runBtn.title = '在当前终端直接执行';
      runBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        this.mgr.sendText(item.command, true);
      });
      const delBtn = document.createElement('button');
      delBtn.className = 'sftp-action sftp-action-danger';
      delBtn.textContent = '删除';
      delBtn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!window.confirm(`确定删除收藏「${item.name}」吗？`)) return;
        try {
          await this._api(`/api/term-favorites/${encodeURIComponent(item.id)}`,
            { method: 'DELETE' });
          showToast('已删除收藏');
          this.load();
        } catch (err) {
          showToast(`删除失败：${err.message}`);
        }
      });
      actions.appendChild(runBtn);
      actions.appendChild(delBtn);
      row.appendChild(nameEl);
      row.appendChild(cmdEl);
      row.appendChild(actions);
      listEl.appendChild(row);
    }
  }

  async save() {
    const name = $('fav-name').value.trim();
    const command = $('fav-command').value.trim();
    if (!name || !command) {
      showToast('名称与命令都不能为空');
      return;
    }
    try {
      await this._api('/api/term-favorites', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, command }),
      });
      $('fav-name').value = '';
      $('fav-command').value = '';
      showToast('已保存收藏');
      this.load();
    } catch (err) {
      showToast(`保存失败：${err.message}`);
    }
  }

  _bindUI() {
    $('fav-close').addEventListener('click', () => this.close());
    $('fav-save').addEventListener('click', () => this.save());
  }
}

// ---------- Batch command execution ----------
// Backend API: POST /api/batch/exec, GET /api/batch/{id}, POST /api/batch/{id}/cancel. 后端接口：POST /api/batch/exec、GET /api/batch/{id}、POST /api/batch/{id}/cancel。
const BatchExec = {
  hosts: [],
  batchId: null,
  pollTimer: null,

  async _api(url, options = {}) {
    const resp = await fetch(url, options);
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.detail || data.error || `请求失败（${resp.status}）`);
    }
    return data;
  },

  async open() {
    $('batch-modal').classList.remove('hidden');
    $('batch-results').innerHTML = '';
    $('batch-cancel').hidden = true;
    this.batchId = null;
    const listEl = $('batch-host-list');
    listEl.innerHTML = '<div class="sftp-empty">加载主机中…</div>';
    try {
      const data = await this._api('/api/hosts');
      this.hosts = data.hosts || [];
      if (this.hosts.length === 0) {
        listEl.innerHTML = '<div class="sftp-empty">还没有保存的主机，请先在概览页添加。</div>';
        return;
      }
      listEl.innerHTML = '';
      for (const h of this.hosts) {
        const label = document.createElement('label');
        label.className = 'batch-host-item';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.value = h.id;
        checkbox.className = 'batch-host-checkbox';
        const text = document.createElement('span');
        text.textContent = `${h.name || h.host}（${h.host}，${h.auth_type === 'key' ? '密钥' : '密码'}）`;
        label.appendChild(checkbox);
        label.appendChild(text);
        listEl.appendChild(label);
      }
    } catch (err) {
      listEl.innerHTML = '<div class="sftp-empty sftp-error">加载主机失败：' +
        err.message.replace(/</g, '&lt;') + '</div>';
    }
  },

  _selectedHostIds() {
    return Array.from(document.querySelectorAll('.batch-host-checkbox:checked'))
      .map((el) => el.value);
  },

  async run() {
    const hostIds = this._selectedHostIds();
    const command = $('batch-command').value.trim();
    if (hostIds.length === 0) {
      showToast('请至少选一台主机');
      return;
    }
    if (!command) {
      showToast('命令不能为空');
      return;
    }
    const names = hostIds.map((id) => {
      const h = this.hosts.find((x) => String(x.id) === id);
      return h ? (h.name || h.host) : id;
    }).join('、');
    if (!window.confirm(`确认在以下主机执行命令吗？\n${names}\n\n命令：${command}`)) return;
    try {
      const res = await this._api('/api/batch/exec', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ host_ids: hostIds, command, confirm: true }),
      });
      this.batchId = res.batchId;
      $('batch-cancel').hidden = false;
      $('batch-results').innerHTML = '<div class="sftp-empty">执行中…</div>';
      this._poll();
    } catch (err) {
      showToast(`批量执行失败：${err.message}`);
    }
  },

  async cancel() {
    if (!this.batchId) return;
    if (!window.confirm('确定取消吗？正在执行的那台会跑完，其余主机不再执行。')) return;
    try {
      await this._api(`/api/batch/${encodeURIComponent(this.batchId)}/cancel`,
        { method: 'POST' });
      showToast('已请求取消');
    } catch (err) {
      showToast(`取消失败：${err.message}`);
    }
  },

  _poll() {
    if (this.pollTimer) clearTimeout(this.pollTimer);
    if (!this.batchId) return;
    this._api(`/api/batch/${encodeURIComponent(this.batchId)}`).then((res) => {
      this._render(res.batch);
      if (res.batch && res.batch.status === 'running') {
        this.pollTimer = setTimeout(() => this._poll(), 1500);
      } else {
        $('batch-cancel').hidden = true;
      }
    }).catch(() => {
      this.pollTimer = setTimeout(() => this._poll(), 3000);
    });
  },

  _render(batch) {
    const el = $('batch-results');
    if (!batch) { el.innerHTML = ''; return; }
    const esc = (s) => String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    let html = `<div class="batch-status">状态：${esc(batch.status)}` +
      `（${Object.keys(batch.results || {}).length}/${(batch.host_ids || []).length} 台有结果）</div>`;
    for (const hostId of (batch.host_ids || [])) {
      const r = (batch.results || {})[hostId];
      if (!r) {
        html += `<div class="batch-result"><b>${esc(hostId)}</b>：等待执行…</div>`;
        continue;
      }
      html += `<div class="batch-result"><b>${esc(r.hostName || r.hostId)}</b> ` +
        `<span class="batch-result-status">${esc(r.status)}` +
        (r.exitCode != null ? `（退出码 ${r.exitCode}）` : '') + '</span>';
      if (r.error) html += `<pre class="batch-output batch-error">${esc(r.error)}</pre>`;
      if (r.stdout) html += `<pre class="batch-output">${esc(r.stdout)}</pre>`;
      if (r.stderr) html += `<pre class="batch-output batch-error">${esc(r.stderr)}</pre>`;
      html += '</div>';
    }
    el.innerHTML = html;
  },
};

// Module-level singleton: the TerminalManager notifies the panel to refresh when tabs are switched/closed. 模块级单例：TerminalManager 切标签/关标签时通知面板刷新。
let sftpPanel = null;
let favPanel = null;

// ---------- 实例卡片 SSH 一键直连 ----------
// Instance card SSH one-click login: POST /api/ssh/connect-instance auto-logs in as root with the fixed password; on failure, fall back to the manual connect form (host prefilled when known). 实例卡片 SSH 一键直连：调 /api/ssh/connect-instance 用固定密码自动登录 root；失败回落手动连接表单（知道主机就预填）。
async function autoConnectInstance(mgr, { instanceId, profile, region, host }) {
  try {
    const resp = await fetch('/api/ssh/connect-instance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        instance_id: instanceId, profile, region,
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(data.detail || '直连失败');
    mgr.openTab(data.session_id, `${data.username}@${data.host}`);
  } catch (err) {
    showToast(`自动登录失败：${err.message}，请手动连接`);
    if (host) $('conn-host').value = host;
    if (!$('conn-port').value) $('conn-port').value = '14422';
    $('connect-error').classList.add('hidden');
    $('connect-modal').classList.remove('hidden');
    $('conn-password').focus();
  }
}

// ---------- 页面初始化 ----------
document.addEventListener('DOMContentLoaded', () => {
  const mgr = new TerminalManager();
  window.terminalManager = mgr;
  mgr.startStatsPolling();

  window.addEventListener('resize', () => {
    if (mgr.activeId) {
      const tab = mgr.tabs.get(mgr.activeId);
      if (tab) tab.fit.fit();
    }
  });

  const openConnectModal = () => {
    mgr.loadHosts();
    $('connect-error').classList.add('hidden');
    $('connect-modal').classList.remove('hidden');
    $('conn-host').focus();
  };
  $('btn-new-tab').addEventListener('click', openConnectModal);
  $('btn-empty-new').addEventListener('click', openConnectModal);
  document.querySelectorAll('[data-close]').forEach((btn) => {
    btn.addEventListener('click', () => $(btn.dataset.close).classList.add('hidden'));
  });

  // A saved host was selected -> auto-fill host/port/username. 选了已保存主机 -> 自动填 host/port/username。
  $('conn-host-select').addEventListener('change', (e) => {
    const host = mgr.hosts.find((h) => String(h.id) === e.target.value);
    if (!host) return;
    $('conn-host').value = host.host || '';
    $('conn-port').value = host.port || 22;
    $('conn-username').value = host.username || 'root';
    if (host.key_path) $('conn-key-path').value = host.key_path;
  });

  $('connect-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const errEl = $('connect-error');
    errEl.classList.add('hidden');
    try {
      await mgr.createConnection({
        hostId: $('conn-host-select').value || null,
        host: $('conn-host').value.trim(),
        port: parseInt($('conn-port').value, 10) || 22,
        username: $('conn-username').value.trim(),
        password: $('conn-password').value || null,
        keyPath: $('conn-key-path').value.trim() || null,
      });
      $('connect-modal').classList.add('hidden');
      $('conn-password').value = '';  // Passwords are never left in the form. 密码不留在表单里。
    } catch (err) {
      errEl.textContent = err.message;
      errEl.classList.remove('hidden');
    }
  });

  // ---------- SFTP file panel: slides out from the right ----------
  sftpPanel = new SFTPPanel(mgr);
  window.sftpPanel = sftpPanel;
  $('btn-sftp').addEventListener('click', () => sftpPanel.toggle());

  // ---------- Favorites panel: slides out from the left ----------
  favPanel = new FavoritesPanel(mgr);
  window.favPanel = favPanel;
  $('btn-favorites').addEventListener('click', () => favPanel.toggle());

  // ---------- Batch command execution ----------
  $('btn-batch').addEventListener('click', () => BatchExec.open());
  $('batch-run').addEventListener('click', () => BatchExec.run());
  $('batch-cancel').addEventListener('click', () => BatchExec.cancel());

  $('btn-fullscreen').addEventListener('click', () => {
    $('terminal-page').classList.toggle('fullscreen');
    if (mgr.activeId) {
      const tab = mgr.tabs.get(mgr.activeId);
      if (tab) setTimeout(() => tab.fit.fit(), 50);
    }
  });

  $('btn-close-tab').addEventListener('click', () => {
    if (mgr.activeId) mgr.closeTab(mgr.activeId);
  });

  // Instance card SSH one-click login: /terminal?instance=<id>&profile=&region=&host=
  // 实例卡片 SSH 一键直连：带 instance 参数打开时自动调后端直连，成功直接开标签；失败回落到手动连接表单（预填主机）。
  const _params = new URLSearchParams(location.search);
  const _instanceId = _params.get('instance');
  if (_instanceId) {
    autoConnectInstance(mgr, {
      instanceId: _instanceId,
      profile: _params.get('profile') || null,
      region: _params.get('region') || null,
      host: _params.get('host') || null,
    });
  }
});
