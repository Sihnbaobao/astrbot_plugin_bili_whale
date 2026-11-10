/* 养鱼插件页。只用 DOM API 和字符串拼接，不引第三方库。 */

let bridge = null;

function today() {
  const now = new Date();
  const pad = function (value) {
    return String(value).padStart(2, "0");
  };
  return (
    now.getFullYear() + "-" + pad(now.getMonth() + 1) + "-" + pad(now.getDate())
  );
}

const state = {
  status: null,
  config: null,
  diary: null,
  statusTimer: 0,
  qrTimer: 0,
  pendingDelete: "",
};

const CONFIG_LAYOUT = [
  {
    title: "作息",
    presets: [
      {
        label: "照你的数据",
        values: {
          WAKE_WINDOWS: "00:10-00:50,07:30-09:00,12:10-13:00,17:30-19:00,21:00-24:00",
          WAKES_PER_WINDOW: 1,
          SKIP_PERCENT: 10,
          MIN_GAP_MINUTES: 30,
          MAX_SCAN: 3,
          MAX_DEEP_READ: 1,
        },
      },
      {
        label: "像人",
        values: {
          WAKE_WINDOWS: "20:00-24:00",
          WAKES_PER_WINDOW: 1,
          SKIP_PERCENT: 30,
          MIN_GAP_MINUTES: 10,
          MAX_SCAN: 5,
          MAX_DEEP_READ: 3,
        },
      },
      {
        label: "通勤",
        values: {
          WAKE_WINDOWS: "08:00-09:00,18:00-20:00",
          WAKES_PER_WINDOW: 1,
          SKIP_PERCENT: 30,
          MIN_GAP_MINUTES: 10,
          MAX_SCAN: 4,
          MAX_DEEP_READ: 2,
        },
      },
      {
        label: "夜猫",
        values: {
          WAKE_WINDOWS: "23:00-02:00",
          WAKES_PER_WINDOW: 1,
          SKIP_PERCENT: 20,
          MIN_GAP_MINUTES: 10,
          MAX_SCAN: 5,
          MAX_DEEP_READ: 2,
        },
      },
    ],
    items: [
      { key: "WAKE_WINDOWS", label: "作息窗口", type: "text", hint: "允许醒来的时间段，如 20:00-24:00；可写多段，逗号分隔。窗口内随机挑时刻醒，不是整点。" },
      { key: "WAKES_PER_WINDOW", label: "每窗口醒几次", type: "number", min: 0, max: 12, hint: "同一窗口里醒几次。1 = 每天一次。" },
      { key: "SKIP_PERCENT", label: "不想看概率 %", type: "number", min: 0, max: 100, hint: "掷到就只写日记、不刷视频。0 = 每天都看。" },
      { key: "MIN_GAP_MINUTES", label: "两次间隔（分钟）", type: "number", min: 1, max: 240, hint: "同一窗口醒多次时，两次之间的最小间隔。" },
    ],
  },
  {
    title: "力度",
    items: [
      { key: "FEED_PRIORITY", label: "候选来源", type: "select", optionsKey: "feed_priority_options", hint: "关注的人优先 = 先看她号里关注的人发了什么；首页推荐优先 = 用 B站 给她排的推荐流；热门榜优先 = 全站热榜。前两种需要登录，失败会自动降级。" },
      { key: "MAX_SCAN", label: "每次最多扫几条", type: "number", min: 1, max: 20, hint: "只读标题和简介，便宜。" },
      { key: "MAX_DEEP_READ", label: "每次最多深读几条", type: "number", min: 0, max: 10, hint: "深读要下载整片并抽帧，一条约 1-2 分钟。" },
      { key: "ENABLE_FRAMES", label: "抽帧看画面", type: "checkbox", hint: "关掉后看不懂纯画面视频（教程、录屏）。需要 ffmpeg。" },
    ],
  },
  {
    title: "分享",
    items: [
      { key: "SHARE_VIA", label: "分享渠道", type: "checks", optionsKey: "share_via_options", hint: "可以同时勾多个：私信发到 B站 私信；评论区发一条公开评论并点名你；只发到聊天窗口则是 QQ 里收到。三者共用每日额度。" },
      { key: "SHARE_DAILY", label: "每天最多几条", type: "number", min: 0, max: 20, hint: "上限，不是任务。0 = 只写日记、不分享。" },
      { key: "SHARE_MIN_SCORE", label: "评分门槛", type: "number", min: 0, max: 10, hint: "她给每条片子打 0-10 分，低于门槛不发。默认 7 分。" },
      { key: "ENABLE_LIKE", label: "允许点赞", type: "checkbox", hint: "只给真正深读过的视频点，一天最多 5 条，同一条不会重复点。点赞和分享是两回事。" },
      { key: "ENABLE_MEMORY", label: "聊天时记得刷过什么", type: "checkbox", hint: "在普通对话里，把今天刷过的几条挂进上下文，你问她「刚才看了什么」她能答上来。关掉的话她只在醒来那一轮里有记忆。" },
      { key: "ENABLE_COMMENT", label: "允许她自己评论", type: "checkbox", hint: "默认关：B站 对信用低的号有隐性限制，评论只有自己看得见，别人看不到。等账号养起来再开。开启后一天最多 2 条，不 @ 人、不带链接。" },
    ],
  },
  {
    title: "预算",
    items: [
      { key: "READ_DAILY", label: "每天读上限（次）", type: "number", min: 50, max: 20000, hint: "刷列表、查详情、拉字幕弹幕都算。一轮刷 + 深读约 25-40 次。" },
      { key: "WRITE_DAILY", label: "每天写上限（次）", type: "number", min: 0, max: 200, hint: "目前就是发评论。防止号被判成刷量机器人。" },
      { key: "WRITE_COOLDOWN", label: "写冷却（秒）", type: "number", min: 0, max: 3600, hint: "发完一条评论后至少隔多久才能发下一条。" },
    ],
  },
  {
    title: "身份与推送",
    items: [
      { key: "OWNER_UMO", label: "推送会话", type: "text", pickerKey: "sessions", hint: "汇报发到哪个会话，形如 星野璃月:FriendMessage:1756758977。不用手填：在聊天里发一次 /鲸鱼 状态 会自动写进来，或者在下面从最近的会话里选。" },
      { key: "OWNER_MID", label: "你的 B站 UID", type: "text", hint: "私信通道要用。打开自己的 B站 空间，地址栏 space.bilibili.com/ 后面那串数字。她只能发给你，而且必须互相关注才发得出去。" },
      { key: "OWNER_NAME", label: "怎么称呼你", type: "text", hint: "她在日记和汇报里怎么叫你。" },
      { key: "PROVIDER_ID", label: "模型", type: "select", optionsKey: "providers", hint: "留空 = 用当前会话的模型。" },
      { key: "PERSONA_ID", label: "人格", type: "select", optionsKey: "personas", hint: "留空 = 中性设定，不叫主人、不撒娇。只影响刷B站。" },
    ],
  },
];

function el(id) {
  return document.getElementById(id);
}

function setNotice(text, kind) {
  const box = el("notice");
  if (!text) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.textContent = text;
  box.className = "notice " + (kind || "");
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

/** 给 promise 加超时，避免桥接没响应时页面永远卡在"正在连接"。 */
function withTimeout(promise, timeoutMs, message) {
  return Promise.race([
    promise,
    new Promise(function (_, reject) {
      setTimeout(function () {
        reject(new Error(message));
      }, timeoutMs);
    }),
  ]);
}

/**
 * 等页面桥接就绪。
 *
 * AstrBot 会在 body 结束标签前注入桥接脚本，所以它一定在本脚本之后；这里最多等 5 秒，
 * 既覆盖脚本顺序，也覆盖桥接加载失败的情况。直接读全局变量会拿到 undefined。
 */
async function waitForBridge(timeoutMs) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    const found = window.AstrBotPluginPage || window.AstrBotPluginView;
    if (found) return found;
    await new Promise(function (resolve) {
      setTimeout(resolve, 50);
    });
  }
  return null;
}

/* ── 渲染：账号 ── */

function renderAccount(data) {
  const account = data.account || {};
  const loggedIn = !!data.logged_in;

  const pill = el("loginPill");
  pill.textContent = loggedIn ? "已登录 " + (account.name || account.mid || "") : "未登录";
  pill.className = "pill " + (loggedIn ? "ok" : "warn");

  el("accountBox").hidden = !loggedIn;
  el("loginHint").textContent = loggedIn ? "" : data.logged_in_hint || "";
  el("loginBtn").textContent = loggedIn ? "换一个账号" : "扫码登录";

  // 登录后二维码必须消失，否则会一直挂在页面上
  if (loggedIn) {
    el("qrBox").hidden = true;
    stopQrPolling();
  }

  const face = account.face || "";
  const accountAvatar = el("accountAvatar");
  const heroAvatar = el("heroAvatar");
  const heroFish = el("heroFish");

  if (loggedIn && face) {
    accountAvatar.onerror = function () {
      this.hidden = true;
    };
    heroAvatar.onerror = function () {
      this.hidden = true;
      heroFish.hidden = false;
    };
    accountAvatar.src = face;
    heroAvatar.src = face;
    heroAvatar.hidden = false;
    heroFish.hidden = true;
  } else {
    heroAvatar.hidden = true;
    heroFish.hidden = false;
  }

  if (loggedIn) {
    el("accountName").textContent = account.name || "（未取到昵称）";
    el("accountMid").textContent = account.mid ? "UID " + account.mid : "";
  }
}

/* ── 渲染：额度 ── */

function renderQuota(quota) {
  ["reads", "writes", "shares"].forEach(function (key) {
    const meter = document.querySelector('.meter[data-key="' + key + '"]');
    if (!meter) return;
    const pair = quota[key] || [0, 0];
    const used = pair[0] || 0;
    const limit = pair[1] || 0;
    const percent = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
    meter.querySelector(".meter-val").textContent = used + " / " + limit;
    const fill = meter.querySelector(".bar i");
    fill.style.width = percent + "%";
    fill.className = percent >= 100 ? "full" : percent >= 80 ? "high" : "";
  });

  const pause = el("pauseHint");
  if (quota.pause_left > 0) {
    pause.hidden = false;
    pause.textContent = "风控熔断，约 " + Math.ceil(quota.pause_left / 60) + " 分钟后恢复";
  } else {
    pause.hidden = true;
  }
}

/* ── 渲染：历史 ── */

function renderWatch(list, total) {
  const box = el("watchList");
  const count = typeof total === "number" && total ? total : (list ? list.length : 0);
  el("watchCount").textContent = count
    ? list && list.length < count
      ? "共 " + count + " 条，显示最近 " + list.length + " 条"
      : count + " 条（七天）"
    : "";
  if (!list || !list.length) {
    box.innerHTML = '<li class="empty">还没有记录。</li>';
    return;
  }
  box.innerHTML = list
    .map(function (item) {
      const score =
        item.score === null || item.score === undefined
          ? ""
          : '<span class="score">' + item.score + "/10</span>";
      const meta = [item.bvid, (item.tags || []).join(" · "), item.at]
        .filter(Boolean)
        .map(escapeHtml)
        .join(" · ");
      const note = item.note
        ? '<p class="watch-note">' + escapeHtml(item.note) + "</p>"
        : "";
      const remove =
        '<button class="del" type="button" title="删掉这条记录" data-del-bvid="' +
        escapeHtml(item.bvid || "") +
        '">×</button>';
      const from = item.via === "link"
        ? '<span class="via">别人发的</span>'
        : "";
      return (
        '<li><div class="watch-head"><span class="title">' +
        escapeHtml(item.title || "（无标题）") +
        "</span>" +
        from +
        score +
        remove +
        '</div><div class="watch-meta">' +
        meta +
        "</div>" +
        note +
        "</li>"
      );
    })
    .join("");
}

function render(data) {
  state.status = data;
  el("subtitle").textContent = data.plan || "";
  renderAccount(data);
  renderQuota(data.quota || {});
  renderWatch(data.watched || [], data.watched_total || 0);
  renderComments(data.comments || []);

  el("planText").textContent = data.plan || "—";
  el("windowText").textContent = data.windows || "—";
  el("skipText").textContent = (data.skip_percent || 0) + "% 概率今天不想看";
  el("lastRunText").textContent = data.last_run || "还没跑过";
  el("ffmpegText").textContent = data.ffmpeg ? "已就绪" : "没有（只看文字）";
  el("umoText").textContent = data.owner_umo || "未配置";

  const wakeBtn = el("wakeBtn");
  wakeBtn.disabled = !!data.waking;
  wakeBtn.textContent = data.waking ? "正在刷…" : "叫醒一次";
}

/* ── 渲染：配置 ── */

function markDirty() {
  el("saveState").textContent = "有未保存的改动";
}

/** 把一组预设值填进表单（不直接保存，用户还要点一次保存）。 */
function applyPreset(values) {
  Object.keys(values).forEach(function (key) {
    const input = document.querySelector('[data-key="' + key + '"]');
    if (!input) return;
    if (input.type === "checkbox") {
      input.checked = !!values[key];
    } else {
      input.value = String(values[key]);
    }
  });
  markDirty();
}

function buildField(item) {
  const values = (state.config && state.config.values) || {};
  const value = values[item.key];
  const field = document.createElement("label");
  field.className = "field" + (item.type === "checkbox" ? " field-check" : "");

  function makeTip() {
    if (!item.hint) return null;
    const tip = document.createElement("span");
    tip.className = "tip";
    tip.textContent = "?";
    tip.setAttribute("tabindex", "0");
    tip.setAttribute("data-tip", item.hint);
    return tip;
  }

  if (item.type === "checkbox") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = !!value;
    input.setAttribute("data-key", item.key);
    input.addEventListener("change", markDirty);
    field.appendChild(input);

    const span = document.createElement("span");
    span.className = "field-label";
    span.textContent = item.label;
    field.appendChild(span);

    const tip = makeTip();
    if (tip) field.appendChild(tip);
    return field;
  }

  const head = document.createElement("div");
  head.className = "field-head";
  const label = document.createElement("span");
  label.className = "field-label";
  label.textContent = item.label;
  head.appendChild(label);
  const tip = makeTip();
  if (tip) head.appendChild(tip);
  field.appendChild(head);

  if (item.type === "checks") {
    const wrap = document.createElement("div");
    wrap.className = "checks";
    const picked = String(value == null ? "" : value)
      .split(",")
      .map(function (part) {
        return part.trim();
      })
      .filter(Boolean);
    const options = (state.config && state.config[item.optionsKey]) || [];
    options.forEach(function (option) {
      const row = document.createElement("label");
      row.className = "check-item";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.value = option.value;
      box.checked = picked.indexOf(option.value) >= 0;
      box.setAttribute("data-key", item.key);
      box.setAttribute("data-option", option.value);
      box.addEventListener("change", markDirty);
      row.appendChild(box);
      const span = document.createElement("span");
      span.textContent = option.label;
      row.appendChild(span);
      wrap.appendChild(row);
    });
    field.appendChild(wrap);
    return field;
  }

  let input;
  if (item.type === "select") {
    input = document.createElement("select");
    const options = (state.config && state.config[item.optionsKey]) || [];
    let matched = false;
    options.forEach(function (option) {
      const opt = document.createElement("option");
      if (item.optionsKey === "personas") {
        opt.value = option.id;
        opt.textContent = option.name;
      } else if (item.optionsKey === "providers") {
        opt.value = option.id;
        opt.textContent = option.name;
      } else {
        opt.value = option.value;
        opt.textContent = option.label;
      }
      if (String(opt.value) === String(value == null ? "" : value)) matched = true;
      input.appendChild(opt);
    });
    if (!matched && value) {
      const opt = document.createElement("option");
      opt.value = String(value);
      opt.textContent = String(value) + "（已失效）";
      input.appendChild(opt);
    }
    input.value = value == null ? "" : String(value);
  } else {
    input = document.createElement("input");
    input.type = item.type === "number" ? "number" : "text";
    if (item.min !== undefined) input.min = item.min;
    if (item.max !== undefined) input.max = item.max;
    input.value = value == null ? "" : String(value);
  }
  input.setAttribute("data-key", item.key);
  input.addEventListener("input", markDirty);
  input.addEventListener("change", markDirty);
  field.appendChild(input);

  // 会话标识很难手填（平台 id 是机器人昵称），给个下拉直接从最近会话里挑
  if (item.pickerKey) {
    const picker = document.createElement("select");
    picker.className = "session-picker";
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "从最近的会话里选…";
    picker.appendChild(placeholder);
    const sessions = (state.config && state.config[item.pickerKey]) || [];
    sessions.forEach(function (option) {
      const opt = document.createElement("option");
      opt.value = option.value;
      opt.textContent = option.label;
      picker.appendChild(opt);
    });
    if (!sessions.length) {
      placeholder.textContent = "没有可选的历史会话";
    }
    picker.addEventListener("change", function () {
      if (picker.value) {
        input.value = picker.value;
        markDirty();
      }
      picker.value = "";
    });
    field.appendChild(picker);
  }
  return field;
}

function renderConfig(config) {
  state.config = config;
  const host = el("configGroups");
  host.innerHTML = "";
  CONFIG_LAYOUT.forEach(function (group) {
    const box = document.createElement("div");
    box.className = "config-group";

    const title = document.createElement("div");
    title.className = "config-group-title";
    title.textContent = group.title;
    box.appendChild(title);

    if (group.presets) {
      const chips = document.createElement("div");
      chips.className = "presets";
      group.presets.forEach(function (preset) {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.textContent = preset.label;
        chip.addEventListener("click", function () {
          applyPreset(preset.values);
        });
        chips.appendChild(chip);
      });
      box.appendChild(chips);
    }

    const grid = document.createElement("div");
    grid.className = "config-grid";
    group.items.forEach(function (item) {
      grid.appendChild(buildField(item));
    });
    box.appendChild(grid);
    host.appendChild(box);
  });
  el("saveState").textContent = "";
}

function collectConfig() {
  const updates = {};
  const multi = {};
  document.querySelectorAll("#configGroups [data-key]").forEach(function (input) {
    const key = input.getAttribute("data-key");
    if (input.getAttribute("data-option")) {
      if (!multi[key]) multi[key] = [];
      multi[key].push(input);
      return;
    }
    if (input.type === "checkbox") {
      updates[key] = input.checked;
    } else if (input.type === "number") {
      updates[key] = input.value === "" ? 0 : Number(input.value);
    } else {
      updates[key] = input.value;
    }
  });
  Object.keys(multi).forEach(function (key) {
    updates[key] = multi[key]
      .filter(function (box) {
        return box.checked;
      })
      .map(function (box) {
        return box.value;
      })
      .join(",");
  });
  return updates;
}

/* ── 加载 ── */

async function refresh() {
  try {
    const data = await bridge.apiGet("page/status");
    render(data);
  } catch (error) {
    setNotice(error && error.message ? error.message : "状态加载失败", "error");
  }
}

async function loadConfig() {
  try {
    renderConfig(await bridge.apiGet("page/config"));
  } catch (error) {
    setNotice(error && error.message ? error.message : "配置加载失败", "error");
  }
}

async function loadDiary(day) {
  try {
    const query = day ? "?date=" + encodeURIComponent(day) : "";
    state.diary = await bridge.apiGet("page/diary" + query);
  } catch (error) {
    state.diary = null;
  }
  renderDiary();
}

function parseDiaryRounds(text) {
  const rounds = [];
  let current = null;
  String(text || "")
    .split(/\r?\n/)
    .forEach(function (line) {
      const match = line.match(/^##\s+(\d{1,2}:\d{2})\s*$/);
      if (match) {
        current = { time: match[1], lines: [] };
        rounds.push(current);
        return;
      }
      if (current) current.lines.push(line);
    });
  return rounds
    .map(function (round) {
      return { time: round.time, text: round.lines.join("\n").trim() };
    })
    .filter(function (round) {
      return round.text;
    });
}

function renderComments(list) {
  const box = el("commentList");
  const items = list || [];
  el("commentCount").textContent = items.length ? items.length + " 条（七天）" : "";
  if (!items.length) {
    box.innerHTML = '<li class="empty">还没在评论区说过话。</li>';
    return;
  }
  box.innerHTML = items
    .map(function (item) {
      return (
        '<li><div class="watch-head"><span class="title">' +
        escapeHtml(item.title || "（无标题）") +
        '</span><span class="hint">' +
        escapeHtml(item.at || "") +
        '</span></div><p class="watch-note">' +
        escapeHtml(item.text || "") +
        '</p><div class="watch-meta">' +
        escapeHtml(item.bvid || "") +
        "</div></li>"
      );
    })
    .join("");
}

function renderDiary() {
  const text = state.diary ? state.diary.text : "";
  const day = state.diary ? state.diary.date : "";
  if (el("diaryTitle")) {
    el("diaryTitle").textContent =
      !day || day === today()
        ? "今天的日记"
        : day.slice(5) + " 的日记";
  }
  const days = el("diaryDays");
  if (days) {
    days.textContent = "";
    ((state.diary && state.diary.dates) || []).slice(0, 7).forEach(function (item) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = item === day ? "day-chip active" : "day-chip";
      chip.textContent = item.slice(5);
      chip.setAttribute("data-diary-day", item);
      days.appendChild(chip);
    });
  }
  const body = el("diaryBody");
  body.textContent = "";
  const rounds = parseDiaryRounds(text);
  if (!rounds.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "这天还没写。";
    body.appendChild(empty);
    return;
  }
  rounds.forEach(function (round) {
    const wrap = document.createElement("article");
    wrap.className = "diary-round";
    const time = document.createElement("div");
    time.className = "diary-time";
    time.textContent = round.time;
    const content = document.createElement("div");
    content.className = "diary-text";
    content.textContent = round.text;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "del diary-del";
    remove.textContent = "×";
    remove.title = "删掉这一轮";
    remove.setAttribute("data-del-time", round.time);
    wrap.appendChild(time);
    wrap.appendChild(content);
    wrap.appendChild(remove);
    body.appendChild(wrap);
  });
  body.scrollTop = body.scrollHeight;
}

/* ── 动作 ── */

function stopQrPolling() {
  if (state.qrTimer) {
    clearInterval(state.qrTimer);
    state.qrTimer = 0;
  }
}

async function pollQr(key) {
  let result;
  try {
    result = await bridge.apiPost("page/login/poll", { key: key });
  } catch (error) {
    stopQrPolling();
    setNotice(error && error.message ? error.message : "查询扫码状态失败", "error");
    return;
  }

  const label = el("qrState");
  if (result.state === "waiting") {
    label.textContent = "用手机 B站 App 扫一扫";
    return;
  }
  if (result.state === "scanned") {
    label.textContent = "已扫码，请在手机上点确认";
    return;
  }
  if (result.state === "expired") {
    stopQrPolling();
    label.textContent = "二维码过期了，重新点一次扫码登录";
    return;
  }
  if (result.state === "confirmed") {
    stopQrPolling();
    el("qrBox").hidden = true;
    if (result.verified === false) {
      setNotice(
        "凭据已经写进配置了，但 B站 那边还没确认这个账号。等几秒点「刷新」，或者重新扫一次。",
        "warn"
      );
      await refresh();
      return;
    }
    setNotice(
      "已登录「" + (result.name || result.mid || "B站 账号") + "」，现在可以发评论了",
      "ok"
    );
    await refresh();
  }
}

async function startLogin() {
  setNotice("", "");
  el("loginBtn").disabled = true;
  try {
    const data = await bridge.apiPost("page/login/qr", {});
    el("qrBox").hidden = false;
    if (data.image) {
      el("qrImage").src = data.image;
      el("qrImage").hidden = false;
      el("qrLink").hidden = true;
    } else {
      el("qrImage").hidden = true;
      el("qrLink").hidden = false;
      el("qrLink").href = data.url;
      el("qrLink").textContent = "二维码画不出来，点这里用 B站 App 打开";
    }
    el("qrState").textContent = "用手机 B站 App 扫一扫（约 3 分钟内有效）";
    stopQrPolling();
    state.qrTimer = setInterval(function () {
      pollQr(data.key);
    }, 2000);
  } catch (error) {
    setNotice(error && error.message ? error.message : "生成二维码失败", "error");
  } finally {
    el("loginBtn").disabled = false;
  }
}

async function saveConfig() {
  el("saveBtn").disabled = true;
  el("saveState").textContent = "保存中…";
  try {
    const data = await bridge.apiPost("page/config/save", { updates: collectConfig() });
    if (state.config && data && data.values) {
      state.config = Object.assign({}, state.config, { values: data.values });
    }
    el("saveState").textContent = "已保存";
    setNotice("设置已保存，立刻生效。", "ok");
    await refresh();
  } catch (error) {
    el("saveState").textContent = "";
    setNotice(error && error.message ? error.message : "保存失败", "error");
  } finally {
    el("saveBtn").disabled = false;
  }
}

async function wakeNow() {
  setNotice("", "");
  el("wakeBtn").disabled = true;
  try {
    await bridge.apiPost("page/wake", {});
    setNotice("叫醒了，正在刷…（会跑几分钟，回聊天窗口等她的汇报）", "ok");
    setTimeout(refresh, 1500);
  } catch (error) {
    setNotice(error && error.message ? error.message : "叫醒失败", "error");
    el("wakeBtn").disabled = false;
  }
}

/* ── 启动 ── */

function bind() {
  el("refreshBtn").addEventListener("click", function () {
    refresh();
    loadConfig();
    loadDiary();
  });
  // 统一挂在 document 上做事件委托：列表每 15 秒重渲染一次，绑在元素上会随替换失效
  if (document.addEventListener) {
    document.addEventListener("click", async function (evt) {
      const node = evt && evt.target;
      if (!node || !node.closest) return;

      const dayChip = node.closest("[data-diary-day]");
      if (dayChip) {
        await loadDiary(dayChip.getAttribute("data-diary-day"));
        return;
      }

      const watchBtn = node.closest("[data-del-bvid]");
      if (watchBtn) {
        await handleDelete(watchBtn, "w:", watchBtn.getAttribute("data-del-bvid"), async function () {
          await bridge.apiPost("page/watch/delete", {
            bvid: watchBtn.getAttribute("data-del-bvid"),
          });
          await refresh();
        });
        return;
      }

      const diaryBtn = node.closest("[data-del-time]");
      if (diaryBtn) {
        await handleDelete(diaryBtn, "d:", diaryBtn.getAttribute("data-del-time"), async function () {
          await bridge.apiPost("page/diary/delete", {
            who: "mine",
            date: state.diary ? state.diary.date : "",
            time: diaryBtn.getAttribute("data-del-time"),
          });
          await loadDiary(state.diary ? state.diary.date : "");
        });
      }
    });
  }

  async function handleDelete(button, prefix, key, action) {
    if (!key) return;
    // 两步确认：不用 window.confirm —— 插件页在 iframe 里，弹窗会被静默拦掉
    if (state.pendingDelete !== prefix + key) {
      state.pendingDelete = prefix + key;
      button.textContent = "确定?";
      button.classList.add("armed");
      setTimeout(function () {
        if (state.pendingDelete === prefix + key) {
          state.pendingDelete = "";
          button.textContent = "×";
          button.classList.remove("armed");
        }
      }, 3000);
      return;
    }
    state.pendingDelete = "";
    try {
      await action();
    } catch (error) {
      setNotice(error && error.message ? error.message : "删除失败", "error");
    }
  }

  el("loginBtn").addEventListener("click", startLogin);
  el("wakeBtn").addEventListener("click", wakeNow);
  el("saveBtn").addEventListener("click", saveConfig);

  const tabs = document.querySelectorAll("#tabs [data-tab]");
  if (tabs && tabs.length) {
    tabs.forEach(function (tab) {
      tab.addEventListener("click", function () {
        const name = tab.getAttribute("data-tab");
        tabs.forEach(function (other) {
          other.classList.toggle("active", other === tab);
        });
        document.querySelectorAll(".panel").forEach(function (panel) {
          panel.classList.toggle("active", panel.id === "panel-" + name);
        });
      });
    });
  }

}

async function boot() {
  bridge = await waitForBridge(5000);
  if (!bridge) {
    setNotice(
      "没有检测到 AstrBot 页面桥接 —— 请从 WebUI 的「插件 → 养鱼」进入；如果就是从那里进来的，刷新一次页面。",
      "error"
    );
    return;
  }
  try {
    await withTimeout(bridge.ready(), 6000, "桥接响应超时");
  } catch (error) {
    setNotice("页面桥接响应超时，如果下面一直是空白，刷新一次页面。", "warn");
  }
  bind();
  await refresh();
  await loadConfig();
  await loadDiary();
  state.statusTimer = setInterval(refresh, 15000);
}

boot();
