(function () {
  const boot = JSON.parse(document.getElementById("boot").textContent);
  const queue = [];
  let running = false;
  let stopRequested = false;
  let activeXhr = null;
  let lastRender = 0;

  const els = {
    banner: document.getElementById("banner"),
    disk: document.getElementById("disk"),
    limits: document.getElementById("limits"),
    pick: document.getElementById("pick-button"),
    start: document.getElementById("start-button"),
    stop: document.getElementById("stop-button"),
    input: document.getElementById("file-input"),
    queue: document.getElementById("queue"),
    incomplete: document.getElementById("incomplete"),
    files: document.getElementById("files"),
    history: document.getElementById("history"),
    refresh: document.getElementById("refresh-button"),
  };

  els.limits.textContent =
    "Максимум " + formatBytes(boot.maxUploadBytes) + " на файл. Разрешени: " + boot.allowedExtensions.join(", ");

  els.pick.addEventListener("click", function () { els.input.click(); });
  els.input.addEventListener("change", addFiles);
  els.start.addEventListener("click", function () { runQueue(); });
  els.stop.addEventListener("click", stopUpload);
  els.refresh.addEventListener("click", function () { refresh().catch(showError); });
  window.addEventListener("beforeunload", function (event) {
    if (running) {
      event.preventDefault();
      event.returnValue = "";
    }
  });

  refresh().catch(showError);
  setInterval(function () {
    if (document.visibilityState === "visible" && !running) {
      refresh().catch(function () {});
    }
  }, 30000);

  function addFiles() {
    const files = Array.from(els.input.files || []);
    files.forEach(function (file) {
      const key = clientKey(file);
      const extension = extensionOf(file.name);
      if (!boot.allowedExtensions.includes(extension)) {
        showError("„" + file.name + "“ има неразрешено разширение.");
        return;
      }
      if (file.size > boot.maxUploadBytes) {
        showError("„" + file.name + "“ е по-голям от лимита.");
        return;
      }
      if (file.size < 1) {
        showError("„" + file.name + "“ е празен.");
        return;
      }
      if (queue.some(function (item) { return item.key === key && item.status !== "done"; })) {
        return;
      }
      queue.push({
        key: key,
        file: file,
        name: file.name,
        size: file.size,
        status: "queued",
        loaded: 0,
        speed: 0,
        eta: null,
        error: "",
        savedAs: "",
        speedStamp: 0,
        speedBytes: 0,
      });
    });
    els.input.value = "";
    updateButtons();
    renderQueue(true);
  }

  async function runQueue() {
    if (running) return;
    running = true;
    stopRequested = false;
    clearBanner();
    updateButtons();
    try {
      for (const item of queue) {
        if (stopRequested) break;
        if (item.status !== "queued") continue;
        await uploadOne(item);
      }
    } finally {
      running = false;
      activeXhr = null;
      updateButtons();
      renderQueue(true);
      refresh().catch(showError);
    }
  }

  async function uploadOne(item) {
    item.status = "uploading";
    item.error = "";
    renderQueue(true);
    try {
      const init = await api("/api/uploads", {
        method: "POST",
        json: { filename: item.name, size: item.size, client_key: item.key },
      });
      let offset = init.offset;
      const chunkSize = init.chunk_size || boot.chunkSize;
      item.loaded = offset;
      if (init.resumed) {
        item.error = "";
        showInfo("Продължаване на „" + item.name + "“ от " + formatBytes(offset) + ".");
      }
      while (offset < item.size) {
        if (stopRequested) {
          item.status = "stopped";
          item.error = "Спряно. Изберете същия файл и натиснете Качи, за да продължите.";
          return;
        }
        const status = await api("/api/uploads/" + init.upload_id);
        offset = status.offset;
        item.loaded = offset;
        if (offset >= item.size) break;
        const end = Math.min(offset + chunkSize, item.size);
        const blob = item.file.slice(offset, end);
        const result = await sendChunkWithRetry(init.upload_id, offset, blob, function (loaded) {
          noteProgress(item, offset + loaded);
        });
        offset = result.offset;
        item.loaded = offset;
      }
      const done = await api("/api/uploads/" + init.upload_id + "/complete", { method: "POST", json: {} });
      item.status = "done";
      item.savedAs = done.name;
      item.loaded = item.size;
      item.eta = 0;
    } catch (error) {
      if (item.status !== "stopped") {
        item.status = "error";
        item.error = error.message || "Качването не успя.";
      }
    }
    renderQueue(true);
  }

  async function sendChunkWithRetry(uploadId, offset, blob, onProgress) {
    let lastError = null;
    for (let attempt = 1; attempt <= 3; attempt += 1) {
      try {
        return await putChunk(uploadId, offset, blob, onProgress);
      } catch (error) {
        lastError = error;
        if (stopRequested || attempt === 3) break;
        showInfo("Опит " + (attempt + 1) + " от 3 за същото парче…");
        await sleep(attempt * 1000);
        const status = await api("/api/uploads/" + uploadId);
        if (status.offset > offset) {
          return { offset: status.offset, resync: true };
        }
      }
    }
    throw lastError || new Error("Качването на парчето не успя.");
  }

  function putChunk(uploadId, offset, blob, onProgress) {
    return new Promise(function (resolve, reject) {
      const xhr = new XMLHttpRequest();
      activeXhr = xhr;
      xhr.open("PUT", "/api/uploads/" + uploadId + "/chunk?offset=" + offset);
      xhr.setRequestHeader("X-CSRF-Token", boot.csrfToken);
      xhr.timeout = 10 * 60 * 1000;
      xhr.upload.onprogress = function (event) {
        if (event.lengthComputable) onProgress(event.loaded);
      };
      xhr.onload = function () {
        let data = {};
        try { data = JSON.parse(xhr.responseText || "{}"); } catch (error) { data = {}; }
        if (xhr.status === 401) {
          window.location.href = "/login";
          reject(new Error("Необходим е вход."));
          return;
        }
        if (xhr.status === 409 && typeof data.offset === "number") {
          resolve({ offset: data.offset, resync: true });
          return;
        }
        if (xhr.status >= 200 && xhr.status < 300 && typeof data.offset === "number") {
          resolve({ offset: data.offset, resync: false });
          return;
        }
        reject(new Error(typeof data.detail === "string" ? data.detail : "Качването на парчето не успя."));
      };
      xhr.onerror = function () {
        reject(new Error("Мрежова грешка. Проверете Tailscale и опитайте отново."));
      };
      xhr.ontimeout = function () {
        reject(new Error("Времето за изчакване изтече. Можете да продължите качването."));
      };
      xhr.onabort = function () {
        reject(new Error("Качването е спряно."));
      };
      xhr.send(blob);
    });
  }

  function stopUpload() {
    stopRequested = true;
    if (activeXhr) activeXhr.abort();
  }

  function noteProgress(item, absoluteLoaded) {
    const now = performance.now();
    item.loaded = absoluteLoaded;
    if (!item.speedStamp) {
      item.speedStamp = now;
      item.speedBytes = absoluteLoaded;
    } else {
      const dt = (now - item.speedStamp) / 1000;
      if (dt >= 0.4) {
        const instant = (absoluteLoaded - item.speedBytes) / dt;
        item.speed = item.speed ? item.speed * 0.7 + instant * 0.3 : instant;
        item.speedStamp = now;
        item.speedBytes = absoluteLoaded;
      }
    }
    if (item.speed > 0) item.eta = (item.size - absoluteLoaded) / item.speed;
    renderQueue(false);
  }

  function renderQueue(force) {
    const now = performance.now();
    if (!force && now - lastRender < 200) return;
    lastRender = now;
    els.queue.replaceChildren();
    if (!queue.length) {
      els.queue.appendChild(empty("Още няма избрани файлове."));
      return;
    }
    queue.forEach(function (item) {
      const card = document.createElement("article");
      const top = document.createElement("div");
      top.className = "queue-top";
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = item.name;
      const state = document.createElement("div");
      state.className = "meta";
      state.textContent = statusText(item);
      top.append(name, state);
      const bar = document.createElement("div");
      bar.className = "progress";
      bar.setAttribute("role", "progressbar");
      const percent = item.size ? Math.min(100, Math.round((item.loaded / item.size) * 100)) : 0;
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      bar.setAttribute("aria-valuenow", String(percent));
      const fill = document.createElement("div");
      fill.className = "progress-fill";
      fill.style.width = percent + "%";
      bar.appendChild(fill);
      const details = document.createElement("p");
      details.className = "meta";
      const parts = [percent + "%", formatBytes(item.loaded) + " / " + formatBytes(item.size)];
      if (item.status === "uploading" && item.speed > 0) {
        parts.push(formatBytes(item.speed) + "/с");
        parts.push("остават " + formatEta(item.eta));
      }
      details.textContent = parts.join(" · ");
      card.append(top, bar, details);
      if (item.error) {
        const error = document.createElement("p");
        error.className = "banner error";
        error.textContent = item.error;
        card.appendChild(error);
      }
      if (item.savedAs) {
        const saved = document.createElement("p");
        saved.className = "meta";
        saved.textContent = "Записан като " + item.savedAs;
        card.appendChild(saved);
      }
      if (item.status === "error" || item.status === "stopped") {
        const actions = document.createElement("div");
        actions.className = "queue-actions";
        const retry = document.createElement("button");
        retry.type = "button";
        retry.textContent = "Опитай отново";
        retry.addEventListener("click", function () {
          item.status = "queued";
          item.error = "";
          runQueue();
        });
        actions.appendChild(retry);
        card.appendChild(actions);
      }
      els.queue.appendChild(card);
    });
  }

  async function refresh() {
    const [overview, fileData, historyData] = await Promise.all([
      api("/api/overview", { csrf: false }),
      api("/api/files", { csrf: false }),
      api("/api/history", { csrf: false }),
    ]);
    els.disk.textContent = "Свободни " + formatBytes(overview.free_bytes) + " · заети от качванията " + formatBytes(overview.used_bytes);
    renderIncomplete(overview.incomplete || []);
    renderFiles(fileData.files || []);
    renderHistory(historyData.events || []);
  }

  function renderIncomplete(items) {
    els.incomplete.replaceChildren();
    if (!items.length) {
      els.incomplete.appendChild(empty("Няма прекъснати качвания."));
      return;
    }
    items.forEach(function (item) {
      const row = document.createElement("article");
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = item.original || "Файл";
      const meta = document.createElement("p");
      meta.className = "meta";
      const percent = item.size ? Math.round((item.offset / item.size) * 100) : 0;
      meta.textContent = percent + "% · " + formatBytes(item.offset) + " / " + formatBytes(item.size) + " · изберете същия файл, за да продължите";
      const discard = document.createElement("button");
      discard.type = "button";
      discard.className = "ghost";
      discard.textContent = "Отхвърли";
      discard.addEventListener("click", function () {
        if (!window.confirm("Да се отхвърли ли незавършеното качване?")) return;
        api("/api/uploads/" + item.upload_id + "/discard", { method: "POST", json: {} })
          .then(refresh)
          .catch(showError);
      });
      row.append(name, meta, discard);
      els.incomplete.appendChild(row);
    });
  }

  function renderFiles(files) {
    els.files.replaceChildren();
    if (!files.length) {
      els.files.appendChild(empty("Още няма записани файлове."));
      return;
    }
    files.forEach(function (file) {
      const row = document.createElement("article");
      row.className = "file-row";
      const info = document.createElement("div");
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = file.name;
      const meta = document.createElement("p");
      meta.className = "meta";
      meta.textContent = formatBytes(file.size) + " · " + formatDate(file.modified);
      info.append(name, meta);
      const actions = document.createElement("div");
      actions.className = "file-actions";
      const link = document.createElement("a");
      link.className = "button-link";
      link.href = "/download/" + encodeURIComponent(file.name);
      link.textContent = "Изтегли";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "ghost";
      remove.textContent = "Изтрий";
      remove.addEventListener("click", function () {
        if (!window.confirm("Да се изтрие ли файлът „" + file.name + "“?\n\nТова е необратимо.")) return;
        api("/api/files/delete", { method: "POST", json: { name: file.name, confirm: true } })
          .then(refresh)
          .catch(showError);
      });
      actions.append(link, remove);
      row.append(info, actions);
      els.files.appendChild(row);
    });
  }

  function renderHistory(events) {
    els.history.replaceChildren();
    if (!events.length) {
      els.history.appendChild(empty("Все още няма събития."));
      return;
    }
    events.forEach(function (event) {
      const row = document.createElement("p");
      row.className = "meta";
      const bits = [formatDate(event.time), event.message || event.action];
      if (event.name) bits.push(event.name);
      if (event.size) bits.push(formatBytes(event.size));
      row.textContent = bits.join(" · ");
      els.history.appendChild(row);
    });
  }

  function api(path, options) {
    const opts = options || {};
    const headers = new Headers(opts.headers || {});
    let body = opts.body;
    if (opts.json !== undefined) {
      headers.set("Content-Type", "application/json");
      body = JSON.stringify(opts.json);
    }
    if (opts.csrf !== false && opts.method && opts.method !== "GET") {
      headers.set("X-CSRF-Token", boot.csrfToken);
    }
    return fetch(path, {
      method: opts.method || "GET",
      headers: headers,
      body: body,
      credentials: "same-origin",
    }).then(async function (response) {
      const text = await response.text();
      let data = {};
      if (text) {
        try { data = JSON.parse(text); } catch (error) { data = { detail: text }; }
      }
      if (response.status === 401) {
        window.location.href = "/login";
        throw new Error("Необходим е вход.");
      }
      if (!response.ok) {
        throw new Error(typeof data.detail === "string" ? data.detail : "Заявката не успя.");
      }
      return data;
    });
  }

  function updateButtons() {
    const hasQueued = queue.some(function (item) { return item.status === "queued"; });
    els.start.disabled = running || !hasQueued;
    els.stop.disabled = !running;
  }

  function statusText(item) {
    if (item.status === "queued") return "Чака";
    if (item.status === "uploading") return "Качва се";
    if (item.status === "done") return "Готово";
    if (item.status === "stopped") return "Спряно";
    return "Грешка";
  }

  function showError(error) {
    els.banner.hidden = false;
    els.banner.className = "banner error";
    els.banner.textContent = error && error.message ? error.message : String(error);
  }

  function showInfo(message) {
    els.banner.hidden = false;
    els.banner.className = "banner";
    els.banner.textContent = message;
  }

  function clearBanner() {
    els.banner.hidden = true;
    els.banner.textContent = "";
  }

  function empty(text) {
    const node = document.createElement("p");
    node.className = "empty muted";
    node.textContent = text;
    return node;
  }

  function clientKey(file) {
    return file.name + "|" + file.size + "|" + file.lastModified;
  }

  function extensionOf(name) {
    const dot = name.lastIndexOf(".");
    return dot >= 0 ? name.slice(dot).toLowerCase() : "";
  }

  function formatBytes(bytes) {
    if (!Number.isFinite(bytes) || bytes < 0) return "—";
    const units = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    const digits = unit === 0 ? 0 : value >= 10 ? 1 : 2;
    return value.toFixed(digits) + " " + units[unit];
  }

  function formatEta(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "—";
    const total = Math.round(seconds);
    if (total < 60) return total + " сек";
    const minutes = Math.floor(total / 60);
    if (minutes < 60) return minutes + " мин " + (total % 60) + " сек";
    const hours = Math.floor(minutes / 60);
    return hours + " ч " + (minutes % 60) + " мин";
  }

  function formatDate(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("bg-BG");
  }

  function sleep(ms) {
    return new Promise(function (resolve) { setTimeout(resolve, ms); });
  }
})();
