"use strict";
const $ = id => document.getElementById(id);
let entries = [], settings = null, busy = false, cancelled = false, activeXHR = null, toastTimer;
function toast(message, error = false) {
  $("toast").textContent = message;
  $("toast").classList.toggle("error", error);
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 6000);
}
function bytes(number) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let index = 0;
  while (number >= 1024 && index < units.length - 1) { number /= 1024; index++; }
  return `${number.toFixed(index ? 1 : 0)} ${units[index]}`;
}
function loginView(show) {
  $("loading").hidden = true;
  $("login-panel").hidden = !show;
  $("workspace").hidden = show;
  $("logout").hidden = show;
}
async function api(path, method = "GET", body) {
  const headers = {"X-File-Relay": "1"};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), credentials: "same-origin"});
  const data = await response.json();
  if (!response.ok) {
    if (response.status === 401) loginView(true);
    throw new Error(data.error || `请求失败 (${response.status})`);
  }
  return data;
}
function render() {
  const list = $("file-list");
  list.replaceChildren();
  $("empty").hidden = entries.length !== 0;
  for (const item of entries) {
    const row = document.createElement("div"); row.className = "file-row";
    const icon = document.createElement("span"); icon.className = "file-icon"; icon.textContent = item.kind === "directory" ? "▤" : "↥";
    const info = document.createElement("div"); info.className = "file-info";
    const name = document.createElement("div"); name.className = "file-name"; name.textContent = item.name;
    const meta = document.createElement("div"); meta.className = "file-meta";
    meta.textContent = `${bytes(item.size)} · ${new Date(item.created * 1000).toLocaleString("zh-CN", {month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit"})}`;
    info.append(name, meta);
    const controls = document.createElement("div"); controls.className = "file-controls";
    const link = document.createElement("a"); link.className = "download-link"; link.href = `/api/download/${item.id}`; link.download = item.name; link.textContent = "下载 ↓";
    const remove = document.createElement("button"); remove.className = "delete-button"; remove.textContent = "删除"; remove.setAttribute("aria-label", `删除中转副本 ${item.name}`);
    remove.addEventListener("click", async () => {
      if (!confirm(`删除“${item.name}”？`)) return;
      try { await api(`/api/files/${item.id}`, "DELETE"); await refresh(); toast("已删除中转副本"); } catch (error) { toast(error.message, true); }
    });
    controls.append(link, remove); row.append(icon, info, controls); list.append(row);
  }
}
async function refresh() {
  const [info, files] = await Promise.all([api("/api/info"), api("/api/files")]);
  settings = info; entries = files;
  $("file-count").textContent = info.file_count;
  $("used-space").textContent = `${bytes(info.used_bytes)} / ${bytes(info.total_quota_bytes)}`;
  $("retention").textContent = info.retention_hours % 24 === 0 ? `${info.retention_hours / 24} 天` : `${info.retention_hours} 小时`;
  $("upload-limit").textContent = `单文件最大 ${bytes(info.max_file_bytes)}`;
  loginView(false); render();
}
$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button"); button.disabled = true;
  try { await api("/api/login", "POST", {token: $("token").value.trim()}); $("token").value = ""; await refresh(); }
  catch (error) { toast(error.message, true); }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => {
  try { await api("/api/logout", "POST"); entries = []; settings = null; loginView(true); } catch (error) { toast(error.message, true); }
});
$("refresh").addEventListener("click", () => refresh().catch(error => toast(error.message, true)));
$("cleanup").addEventListener("click", async () => {
  try {
    const preview = await api("/api/cleanup", "POST");
    if (!preview.count) return toast("没有需要清理的过期文件");
    if (!confirm(`清理 ${preview.count} 个过期中转副本，释放 ${bytes(preview.bytes)}？`)) return;
    const result = await api("/api/cleanup?dry_run=0", "POST"); await refresh(); toast(`已清理 ${result.count} 个过期副本`);
  } catch (error) { toast(error.message, true); }
});
function upload(blob, name, kind) {
  return new Promise((resolve, reject) => {
    if (cancelled) return reject(new Error("传输已取消"));
    const xhr = new XMLHttpRequest(); activeXHR = xhr;
    const query = new URLSearchParams({name, kind});
    xhr.open("POST", `/api/files?${query}`); xhr.timeout = 3600000;
    xhr.setRequestHeader("X-File-Relay", "1"); xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) {
        $("progress").value = event.loaded / event.total * 100;
        $("transfer-label").textContent = `${name} · ${bytes(event.loaded)} / ${bytes(event.total)}`;
      }
    };
    xhr.onload = () => {
      activeXHR = null;
      let result; try { result = JSON.parse(xhr.responseText); } catch { return reject(new Error("服务器返回了无效响应")); }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result);
      else { if (xhr.status === 401) loginView(true); reject(new Error(result.error || "上传失败")); }
    };
    xhr.onerror = () => { activeXHR = null; reject(new Error("连接中断，请检查服务状态")); };
    xhr.ontimeout = () => { activeXHR = null; reject(new Error("上传超过 1 小时时限")); };
    xhr.onabort = () => { activeXHR = null; reject(new Error("传输已取消")); };
    xhr.send(blob);
  });
}
async function sendFiles(files, directory = false) {
  if (busy || !files.length || !settings) return;
  busy = true; cancelled = false; $("transfer").hidden = false; $("progress").value = 0;
  for (const id of ["pick-files", "pick-folder"]) $(id).disabled = true;
  try {
    if (directory) {
      const root = files[0].webkitRelativePath.split("/")[0];
      const archive = await zipDirectory(files);
      if (archive.size > settings.max_file_bytes) throw new Error("目录归档超过单文件大小限制");
      await upload(archive, `${root}.zip`, "directory");
    } else {
      for (const file of files) {
        if (cancelled) throw new Error("传输已取消");
        if (file.size > settings.max_file_bytes) throw new Error(`${file.name} 超过单文件大小限制`);
        await upload(file, file.name, "file");
      }
    }
    toast("上传完成");
  } catch (error) { toast(error.message, true); }
  finally {
    busy = false; $("transfer").hidden = true; $("files").value = ""; $("folder").value = "";
    for (const id of ["pick-files", "pick-folder"]) $(id).disabled = false;
    refresh().catch(error => toast(error.message, true));
  }
}
$("pick-files").addEventListener("click", () => $("files").click());
$("pick-folder").addEventListener("click", () => $("folder").click());
$("files").addEventListener("change", event => sendFiles([...event.target.files]));
$("folder").addEventListener("change", event => sendFiles([...event.target.files], true));
$("cancel-upload").addEventListener("click", () => { cancelled = true; if (activeXHR) activeXHR.abort(); });
$("drop-zone").addEventListener("click", () => { if (!busy) $("files").click(); });
$("drop-zone").addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); if (!busy) $("files").click(); } });
for (const name of ["dragenter", "dragover"]) $("drop-zone").addEventListener(name, event => { event.preventDefault(); $("drop-zone").classList.add("dragover"); });
for (const name of ["dragleave", "drop"]) $("drop-zone").addEventListener(name, event => { event.preventDefault(); $("drop-zone").classList.remove("dragover"); });
$("drop-zone").addEventListener("drop", event => {
  const items = [...event.dataTransfer.items];
  if (items.some(item => item.webkitGetAsEntry?.()?.isDirectory)) return toast("上传文件夹请使用“选择文件夹”按钮", true);
  sendFiles([...event.dataTransfer.files]);
});

// Small ZIP writer using Blob references: files are streamed once for CRC32,
// then composed without loading the complete directory into JavaScript memory.
const crcTable = new Uint32Array(256);
for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1; crcTable[n] = c; }
async function zipDirectory(files) {
  if (files.length > 65535) throw new Error("网页目录上传最多支持 65535 个文件，请改用 CLI");
  const encoder = new TextEncoder(), local = [], central = [];
  let offset = 0, centralSize = 0;
  if (files.reduce((sum, file) => sum + file.size, 0) > settings.max_file_bytes) throw new Error("网页目录归档不压缩，目录总大小超过单文件上限，请改用 CLI");
  for (let index = 0; index < files.length; index++) {
    if (cancelled) throw new Error("传输已取消");
    const file = files[index], relative = file.webkitRelativePath.split("/").slice(1).join("/");
    if (!relative || relative.split("/").some(part => !part || part === "." || part === "..") || relative.includes("\\")) throw new Error("文件夹包含不支持的路径");
    const name = encoder.encode(relative);
    if (name.length > 65535 || file.size >= 0xffffffff) throw new Error("目录需要 ZIP64，请改用 CLI");
    $("transfer-label").textContent = `准备目录 ${index + 1}/${files.length}：${relative}`;
    $("progress").value = index / files.length * 100;
    let crc = 0xffffffff;
    const reader = file.stream().getReader();
    try {
      while (true) {
        if (cancelled) throw new Error("传输已取消");
        const {value, done} = await reader.read(); if (done) break;
        for (const byte of value) crc = crcTable[(crc ^ byte) & 255] ^ (crc >>> 8);
      }
    } finally { await reader.cancel(); reader.releaseLock(); }
    crc = (crc ^ 0xffffffff) >>> 0;
    const date = new Date(file.lastModified), year = Math.min(2107, Math.max(1980, date.getFullYear()));
    const dosTime = (date.getHours() << 11) | (date.getMinutes() << 5) | (date.getSeconds() >> 1);
    const dosDate = ((year - 1980) << 9) | ((date.getMonth() + 1) << 5) | date.getDate();
    const header = new Uint8Array(30), h = new DataView(header.buffer);
    h.setUint32(0, 0x04034b50, true); h.setUint16(4, 20, true); h.setUint16(6, 0x800, true);
    h.setUint16(10, dosTime, true); h.setUint16(12, dosDate, true); h.setUint32(14, crc, true);
    h.setUint32(18, file.size, true); h.setUint32(22, file.size, true); h.setUint16(26, name.length, true);
    local.push(header, name, file);
    const directory = new Uint8Array(46), d = new DataView(directory.buffer);
    d.setUint32(0, 0x02014b50, true); d.setUint16(4, 20, true); d.setUint16(6, 20, true); d.setUint16(8, 0x800, true);
    d.setUint16(12, dosTime, true); d.setUint16(14, dosDate, true); d.setUint32(16, crc, true);
    d.setUint32(20, file.size, true); d.setUint32(24, file.size, true); d.setUint16(28, name.length, true); d.setUint32(42, offset, true);
    central.push(directory, name); centralSize += directory.length + name.length; offset += header.length + name.length + file.size;
    if (offset + centralSize + 22 > Math.min(settings.max_file_bytes, 0xffffffff)) throw new Error("目录归档超过大小限制，请改用 CLI");
  }
  const end = new Uint8Array(22), e = new DataView(end.buffer);
  e.setUint32(0, 0x06054b50, true); e.setUint16(8, files.length, true); e.setUint16(10, files.length, true);
  e.setUint32(12, centralSize, true); e.setUint32(16, offset, true);
  return new Blob([...local, ...central, end], {type: "application/zip"});
}
refresh().catch(error => { loginView(true); if (error.message !== "请先登录") toast(error.message, true); });
setInterval(() => { if (!document.hidden && !$("workspace").hidden && !busy) refresh().catch(() => {}); }, 10000);
