/**
 * 插件页脚本的冒烟测试（Node 原生，无第三方依赖）。
 *
 *   node pages/dashboard/test/smoke.mjs
 *
 * 页面是前端拼字符串读字段的，字段改名、元素 id 对不上、登录后忘了收起二维码，
 * 这些都只会在浏览器里才暴露。这里用最小 DOM 桩把 app.js 真跑两遍：
 *   ① 未登录  → 二维码默认收起、显示鱼图标、不报错
 *   ② 已登录  → 二维码收起、显示头像与昵称、配置表单渲染
 *
 * app.js 在无 package.json 的目录里会被 Node 当成 CJS，所以先复制成 .mjs 再导入。
 */

import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "app.js"), "utf8");
const markup = readFileSync(join(here, "..", "index.html"), "utf8");
const styles = readFileSync(join(here, "..", "style.css"), "utf8");

// 页面拆成「今天 / 设置」两栏，靠 tab 切换——结构错了整页就是空白
check(markup.includes('id="tabs"'), "页面应有 tab 栏");
check(markup.includes('data-tab="live"') && markup.includes('data-tab="settings"'), "应有今天和设置两个 tab");
check(markup.includes('id="panel-live"') && markup.includes('id="panel-settings"'), "两个面板容器都应存在");
check(markup.indexOf('id="panel-live"') < markup.indexOf('id="panel-settings"'), "今天在前、设置在后");
check(markup.indexOf("今天的日记") < markup.indexOf("panel-settings"), "日记属于今天那一栏");
check(
  styles.includes(".panel.active") && styles.includes(".live-stack"),
  "布局样式应定义面板切换与纵向堆叠"
);
check(!markup.includes("pre class=\"diary-body\""), "日记容器不该还是 pre（按轮次分块）");

check(
  source.includes('node.closest("[data-del-bvid]")'),
  "删除按钮应走事件委托（列表会重渲染，绑在元素上会失效）"
);
check(
  !/window\.confirm\s*\(/.test(source),
  "插件页在 iframe 里，不能用 window.confirm"
);

// app.js 里 el("xxx") 的每个 id 都必须在 index.html 里存在，否则是静默失败
const wantedIds = Array.from(source.matchAll(/el\("([^"]+)"\)/g), function (match) {
  return match[1];
});
const presentIds = new Set(
  Array.from(markup.matchAll(/id="([^"]+)"/g), function (match) {
    return match[1];
  })
);
const missingIds = wantedIds.filter(function (id) {
  return !presentIds.has(id);
});
check(
  missingIds.length === 0,
  "app.js 引用的元素都应在 index.html 里，缺：" + missingIds.join(", ")
);
const workDir = mkdtempSync(join(tmpdir(), "bili-whale-ui-"));
const appFile = join(workDir, "app.mjs");
writeFileSync(appFile, source, "utf8");

function makeNode(tag) {
  const node = {
    tagName: tag,
    children: [],
    attributes: {},
    className: "",
    _text: "",
    value: "",
    hidden: false,
    checked: false,
    type: "",
    src: "",
    href: "",
    style: {},
    disabled: false,
    id: "",
    _html: "",
    appendChild(child) {
      this.children.push(child);
      return child;
    },
    setAttribute(key, value) {
      this.attributes[key] = value;
    },
    getAttribute(key) {
      return Object.prototype.hasOwnProperty.call(this.attributes, key)
        ? this.attributes[key]
        : null;
    },
    addEventListener() {},
    classList: { add() {}, remove() {} },
    querySelector() {
      return makeNode("i");
    },
    querySelectorAll() {
      return [];
    },
  };
  Object.defineProperty(node, "textContent", {
    get() {
      if (node.children.length) {
        return node.children
          .map(function (child) {
            return child.textContent || "";
          })
          .join("\n");
      }
      return node._text;
    },
    set(value) {
      node._text = String(value);
      node.children.length = 0;
    },
  });
  Object.defineProperty(node, "innerHTML", {
    get() {
      return node._html;
    },
    set(value) {
      node._html = String(value);
      if (value === "") node.children.length = 0;
    },
  });
  return node;
}

const nodes = new Map();
function getEl(id) {
  if (!nodes.has(id)) {
    const node = makeNode("div");
    node.id = id;
    if (id === "notice" || id === "qrBox" || id === "accountBox") node.hidden = true;
    nodes.set(id, node);
  }
  return nodes.get(id);
}

const baseStatus = {
  logged_in: false,
  account: {},
  quota: { reads: [0, 3000], writes: [0, 30], shares: [0, 3], pause_left: 0 },
  plan: "下一次 11-06 23:01 醒来（20:00-24:00）",
  windows: "20:00-24:00",
  skip_percent: 30,
  share: {},
  logged_in_hint: "只能读",
  ffmpeg: "",
  owner_umo: "",
  last_run: "",
  waking: false,
  watched: [],
};

const loggedInStatus = JSON.parse(JSON.stringify(baseStatus));
loggedInStatus.logged_in = true;
loggedInStatus.account = {
  name: "阿鲸",
  mid: "12345",
  face: "https://i0.hdslb.com/bfs/face/sample.jpg",
};
loggedInStatus.watched = [
  { bvid: "BV1xx411c7mD", title: "猫猫", at: "2026-11-06 12:00", score: 14 },
];

const configPayload = {
  values: {
    WAKE_WINDOWS: "20:00-24:00",
    WAKES_PER_WINDOW: 1,
    SKIP_PERCENT: 30,
    MIN_GAP_MINUTES: 10,
    MAX_SCAN: 5,
    MAX_DEEP_READ: 3,
    FEED_PRIORITY: "following",
    ENABLE_FRAMES: true,
    SHARE_VIA: "dm,local",
    ENABLE_LIKE: true,
    ENABLE_MEMORY: true,
    SHARE_DAILY: 3,
    SHARE_MIN_SCORE: 13,
    READ_DAILY: 3000,
    WRITE_DAILY: 30,
    WRITE_COOLDOWN: 120,
    OWNER_UMO: "",
    OWNER_MID: "",
    OWNER_NAME: "主人",
    PROVIDER_ID: "",
    PERSONA_ID: "",
  },
  personas: [
    { id: "", name: "不使用人格（中性设定）" },
    { id: "whale", name: "whale — 话少" },
  ],
  providers: [
    { id: "", name: "跟随当前会话的聊天模型" },
    { id: "opencode/x", name: "opencode/x（deepseek-v4.1-flash）" },
  ],
  sessions: [
    { value: "星野璃月:FriendMessage:1756758977", label: "私聊 · 星野璃月:FriendMessage:1756758977" },
  ],
  share_via_options: [
    { value: "dm", label: "B站 私信" },
    { value: "comment", label: "视频评论区" },
    { value: "local", label: "只发到聊天窗口" },
  ],
  feed_priority_options: [
    { value: "following", label: "关注的人优先" },
    { value: "recommend", label: "首页推荐优先" },
    { value: "popular", label: "热门榜优先" },
  ],
};

globalThis.window = {
  AstrBotPluginPage: {
    ready: async () => ({}),
    apiGet: async (path) => {
      if (path === "page/status") return globalThis.__status;
      if (path === "page/config") return configPayload;
      return {
        date: "2026-11-06",
        mine: "## 09:10\n写给自己",
        yours: "## 09:10\n写给你\n\n## 18:52\n又写了一段",
      };
    },
    apiPost: async () => ({}),
  },
};
globalThis.document = {
  getElementById: getEl,
  createElement: makeNode,
  querySelector: () => makeNode("div"),
  querySelectorAll: () => [],
};
// 别让页面里的定时器挂住测试进程
globalThis.setInterval = () => 0;
globalThis.setTimeout = (fn) => 0;

async function run(status, label) {
  globalThis.__status = status;
  await import(pathToFileURL(appFile).href + "?case=" + label);
  await new Promise((resolve) => process.nextTick(resolve));
  return {
    noticeHidden: getEl("notice").hidden,
    qrHidden: getEl("qrBox").hidden,
    heroAvatarHidden: getEl("heroAvatar").hidden,
    heroAvatarSrc: getEl("heroAvatar").src,
    heroFishHidden: getEl("heroFish").hidden,
    accountBoxHidden: getEl("accountBox").hidden,
    accountName: getEl("accountName").textContent,
    pill: getEl("loginPill").textContent,
    configGroups: getEl("configGroups").children.length,
    diary: getEl("diaryBody").textContent,
    diaryRounds: getEl("diaryBody").children.length,
  };
}

const loggedOut = await run(baseStatus, "logged-out");
const loggedIn = await run(loggedInStatus, "logged-in");

let failed = 0;
function check(condition, message) {
  if (!condition) {
    console.log("FAIL: " + message);
    failed += 1;
  }
}

check(loggedOut.noticeHidden, "未登录时不应弹出错误提示");
check(loggedOut.qrHidden, "未登录时二维码默认收起");
check(loggedOut.heroFishHidden === false, "未登录时显示鱼图标");
check(loggedOut.accountBoxHidden, "未登录时不显示账号卡");
check(loggedIn.qrHidden, "登录后二维码必须收起");
check(loggedIn.heroAvatarHidden === false, "登录后显示头像");
check(String(loggedIn.heroAvatarSrc).includes("hdslb.com"), "登录后头像取的是 B站 地址");
check(loggedIn.accountBoxHidden === false, "登录后显示账号卡");
check(loggedIn.accountName === "阿鲸", "登录后显示昵称");
check(loggedIn.pill.includes("阿鲸"), "状态胶囊显示昵称");
check(loggedIn.configGroups === 5, "配置表单渲染 5 组，实际 " + loggedIn.configGroups);

function countInputs(node) {
  let total = node.attributes && node.attributes["data-key"] ? 1 : 0;
  (node.children || []).forEach(function (child) {
    total += countInputs(child);
  });
  return total;
}
const inputCount = countInputs(getEl("configGroups"));
// 20 个普通字段 + 分享渠道的 3 个勾选框
check(inputCount === 23, "配置表单渲染 23 个输入项，实际 " + inputCount);

// 说明文字收在「?」气泡里，不铺在页面上；但每个字段都得有一个，否则等于没解释
function countTips(node) {
  let total = node.attributes && node.attributes["data-tip"] ? 1 : 0;
  (node.children || []).forEach(function (child) {
    total += countTips(child);
  });
  return total;
}
const tipCount = countTips(getEl("configGroups"));
check(tipCount === 21, "每个字段都应带一个 ? 说明，实际 " + tipCount);

// 推送会话这类标识很难手填，必须给出「从最近会话里选」的下拉
function countClass(node, className) {
  let total = node.className === className ? 1 : 0;
  (node.children || []).forEach(function (child) {
    total += countClass(child, className);
  });
  return total;
}
check(
  countClass(getEl("configGroups"), "session-picker") === 1,
  "推送会话应当带一个会话选择下拉"
);

const chipCount = countClass(getEl("configGroups"), "chip");
check(chipCount === 4, "作息组应渲染 4 个预设，实际 " + chipCount);
check(
  loggedIn.diary.indexOf("写给你") >= 0 && loggedIn.diary.indexOf("又写了一段") >= 0,
  "日记默认展示写给用户那本，且按轮次分开"
);
check(
  loggedIn.diaryRounds === 2,
  "两轮日记应渲染成两个区块，实际 " + loggedIn.diaryRounds
);

// 显隐全靠 hidden 属性，而 .qr / .account 这类类名设了 display，
// 会盖掉浏览器默认的 [hidden] { display: none } —— 样式表里必须有一条强制规则兜住。
const css = readFileSync(join(here, "..", "style.css"), "utf8");
check(
  /\[hidden\]\s*\{[^}]*display\s*:\s*none\s*!important/.test(css),
  "样式表缺少 [hidden] { display: none !important }，二维码会盖不住"
);

console.log(failed === 0 ? "UI SMOKE OK" : "UI SMOKE FAILED: " + failed);
process.exit(failed === 0 ? 0 : 1);
