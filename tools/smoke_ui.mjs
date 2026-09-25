// 前端 UI 冒烟：用系统 Edge 无头 + CDP 实测「阶段流程图 → 点击节点 → 阶段详情」这条交互链。
//
// 为什么不用 playwright：本机 playwright 自带的 Chromium 因沙箱权限起不来（见 CONTEXT.md 环境备忘），
// 而系统 Edge 可以 `--headless=new --remote-debugging-port` 驱动；Node 25 自带 WebSocket，无需第三方依赖。
//
// 用法（先起操作台）：
//   python -m pipeline.server --port 8787 --no-browser
//   node tools/smoke_ui.mjs
// 可选环境变量：
//   CONSOLE_URL  默认 http://127.0.0.1:8787/
//   RUN_ID       指定要验证的运行；不传则自动挑「有阶段产物」的最新一次
//   EDGE_BIN     指定 Edge 可执行文件
//   SHOT         截图输出路径（默认可视化复核用，不传则不截图）
//   PICK_STAGE   点开哪个阶段节点，默认 dev
//
// 环境不满足（没装 Edge / 服务没起 / 没有可验证的运行）时打印 SKIP 并以 0 退出 ——
// UI 冒烟是「可选增强」，不该让整体回归变红。
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const BASE = process.env.CONSOLE_URL || "http://127.0.0.1:8787/";
const PICK_STAGE = process.env.PICK_STAGE || "dev";
const PORT = Number(process.env.CDP_PORT || 9333);
const EDGE_CANDIDATES = [
  process.env.EDGE_BIN,
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe",
  "/usr/bin/microsoft-edge",
  "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
].filter(Boolean);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const results = [];
function check(ok, label, detail = "") {
  results.push(!!ok);
  console.log(`  [${ok ? "OK  " : "FAIL"}] ${label}${detail ? "  " + detail : ""}`);
}
function skip(reason) {
  console.log(`  [SKIP] ${reason}`);
}

async function fetchJson(path, init) {
  const res = await fetch(new URL(path, BASE), init);
  return res.ok ? res.json() : null;
}

/**
 * 决定验证哪个运行。
 *
 * 优先**自己造一个 mock 运行**再删掉：这样日志一定带阶段标记（旧运行没有标记，
 * 「切片带边界标题」这类断言会误报）。若已有运行在进行中（单驻留，接口返回 409），
 * 或造运行失败，则退回「挑一个已有运行」，并把标记相关断言自动放宽。
 */
async function ensureRun() {
  if (process.env.RUN_ID) return { id: process.env.RUN_ID, mine: false };
  const created = await fetchJson("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      requirement: "UI 冒烟：给客户列表页增加导出 Excel 按钮",
      review_every: 1,
      mock: true,
    }),
  });
  if (created && created.run_id) {
    const runId = created.run_id;
    console.log(`  自建 mock 运行: ${runId}（跑完即删）`);
    for (let i = 0; i < 200; i++) {
      const d = await fetchJson(`/api/runs/${runId}`);
      const st = (d && d.state && d.state.status) || "";
      const after = (d && d.state && d.state.paused_after) || "";
      if (st === "failed") break;
      if (st === "done" || (st === "paused" && after === "human_review")) {
        return { id: runId, mine: true };
      }
      if (st === "paused") {
        // PM 未决项等闸门会先停一下；mock 运行直接放行继续跑
        await fetchJson(`/api/runs/${runId}/resume`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        });
      }
      await sleep(600);
    }
    return { id: runId, mine: true }; // 没跑到理想停点也先用它验证
  }
  const rows = (await fetchJson("/api/runs") || {}).runs || [];
  const pick = rows.find((r) => (r.stages || 0) > 0);
  return pick ? { id: pick.run_id, mine: false } : null;
}

let ws = null;
let seq = 0;
const pending = new Map();
const problems = [];

function send(method, params = {}) {
  const id = ++seq;
  ws.send(JSON.stringify({ id, method, params }));
  return new Promise((res, rej) => pending.set(id, { res, rej }));
}
async function evalJs(expression) {
  const r = await send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true });
  if (r.exceptionDetails) {
    throw new Error("页面 JS 异常: " +
      (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  }
  return r.result.value;
}

async function findPageTarget() {
  for (let i = 0; i < 60; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === "page" && t.webSocketDebuggerUrl);
      if (page) return page;
    } catch { /* Edge 还没起来 */ }
    await sleep(250);
  }
  throw new Error("Edge CDP 未就绪");
}

async function run() {
  if (!(await fetch(new URL("/api/flow", BASE)).then(() => true).catch(() => false))) {
    skip(`操作台不可达（${BASE}）—— 先起 python -m pipeline.server --port 8787 --no-browser`);
    return 0;
  }
  const edgeBin = EDGE_CANDIDATES.find((p) => existsSync(p));
  if (!edgeBin) {
    skip("未找到 Edge 可执行文件（可用 EDGE_BIN 指定）");
    return 0;
  }
  const picked = await ensureRun();
  if (!picked) {
    skip("没有可验证的运行（自建 mock 运行失败，且已有运行里没有带阶段产物的）");
    return 0;
  }
  const runId = picked.id;
  console.log(`  Edge: ${edgeBin}`);
  console.log(`  运行: ${runId}　目标 URL: ${BASE}`);
  // 旧运行没有阶段标记：相关断言要放宽，否则是「拿旧数据判新功能」，误报
  const probe = await fetchJson(`/api/runs/${runId}/log?stage=${PICK_STAGE}&lines=5`);
  const hasMarkers = !!(probe && probe.has_markers);
  if (!hasMarkers) console.log("  注意：该运行没有阶段标记（旧运行），标记相关断言将放宽");

  const profile = mkdtempSync(join(tmpdir(), "edge-cdp-"));
  const edge = spawn(edgeBin, [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${profile}`,
    "--remote-allow-origins=*",
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--window-size=1680,1200",
    "about:blank",
  ], { stdio: "ignore" });

  try {
    const page = await findPageTarget();
    ws = new WebSocket(page.webSocketDebuggerUrl);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && pending.has(msg.id)) {
        const p = pending.get(msg.id);
        pending.delete(msg.id);
        msg.error ? p.rej(new Error(JSON.stringify(msg.error))) : p.res(msg.result);
        return;
      }
      if (msg.method === "Runtime.exceptionThrown") {
        problems.push("未捕获异常: " + (msg.params.exceptionDetails.exception?.description ||
          msg.params.exceptionDetails.text));
      }
      if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
        problems.push("console.error: " +
          msg.params.args.map((a) => a.value ?? a.description ?? "").join(" "));
      }
    };
    await send("Runtime.enable");
    await send("Page.enable");
    await send("Page.navigate", { url: BASE });
    await sleep(1900);

    const flow = await (await fetch(new URL("/api/flow", BASE))).json();
    const wantNodes = flow.nodes.length;
    // 前端把「同一对节点的多条条件边」合并成一条，期望值按去重后算
    const pairs = new Set();
    Object.entries(flow.linear).forEach(([a, b]) => pairs.add(`${a}|${b}`));
    Object.entries(flow.conditional).forEach(([a, t]) =>
      Object.values(t).forEach((b) => pairs.add(`${a}|${b}`)));

    await evalJs(`openRun(${JSON.stringify(runId)})`);
    await sleep(1600);

    // 流程图渲染在「运行详情」视图里（每个运行各一张图）
    check(await evalJs("!!document.querySelector('svg.pipe')"), "流程图 SVG 已渲染");
    const nodeCount = await evalJs("document.querySelectorAll('.gnode').length");
    check(nodeCount === wantNodes, `节点数与流定义一致（${wantNodes}）`, String(nodeCount));
    const edgeCount = await evalJs("document.querySelectorAll('.gedge').length");
    check(edgeCount === pairs.size, `边数与流定义一致（${pairs.size}）`, String(edgeCount));
    check(await evalJs("document.querySelectorAll('.gedge.e-loop').length >= 3"),
      "回流 / 条件边用虚线区分");
    const labels = await evalJs("[...document.querySelectorAll('.gedge-lb')].map(t=>t.textContent).join(',')");
    check(!/[a-z_]{4,}/.test(labels), "边标签已全部中文化", labels.slice(0, 70));
    check(await evalJs("$('d-graph-hint').hidden === false && $('d-graph-detail').hidden === true"),
      "未选中节点时只显示提示、不显示详情");
    check(await evalJs("[...document.querySelectorAll('.gnode')].every(g=>!!g.querySelector('title'))"),
      "每个节点都有悬停说明（title）");

    const raw = await evalJs(
      "[...document.querySelectorAll('.gnode')].map(g=>{const c=[...g.classList].find(x=>/^g-(done|run|gate|attn|wait|skip)$/.test(x));return g.dataset.stage+'='+(c||'?')}).join(',')"
    );
    const statuses = {};
    raw.split(",").forEach((kv) => { const [k, v] = kv.split("="); statuses[k] = v; });
    check((raw.match(/g-done/g) || []).length >= 4, "已完成阶段被标绿",
      Object.entries(statuses).map(([k, v]) => `${k}${v}`).join(" ").slice(0, 90));
    check(statuses["done"] !== "g-done", "未到达的终止节点不会被误标为已完成", statuses["done"]);

    // 点击节点 → 详情面板
    await evalJs(`document.querySelector('.gnode[data-stage="${PICK_STAGE}"]').dispatchEvent(new MouseEvent('click',{bubbles:true}))`);
    await sleep(900);
    check(await evalJs("!$('d-graph-detail').hidden && $('d-graph-hint').hidden"), "点击节点后详情面板展开");
    check(await evalJs(`!!document.querySelector('.gnode[data-stage="${PICK_STAGE}"]').classList.contains('g-sel')`),
      "被点节点高亮（g-sel）");
    const panel = await evalJs("$('d-graph-detail').innerText");
    check(/产物文件/.test(panel) && /执行日志/.test(panel) && /状态/.test(panel),
      "面板含 状态信息 / 产物文件 / 执行日志 三块");
    check(/模型|耗时|token/.test(panel), "面板展示该阶段的状态信息（模型 / 耗时 / token）",
      panel.split("\n").slice(0, 8).join(" / ").slice(0, 90));

    const logText = await evalJs("($('gp-log')||{}).textContent || ''");
    check(logText.length > 20 && !/^加载中/.test(logText), `执行日志已按阶段填入（${logText.length} 字）`,
      logText.split("\n")[0].slice(0, 40));
    if (hasMarkers) check(/== STAGE /.test(logText), "日志切片带阶段边界标题");
    else skip("旧运行无阶段标记，跳过「切片带边界标题」断言");

    await evalJs(`document.querySelector('.gnode[data-stage="pm"]').dispatchEvent(new MouseEvent('click',{bubbles:true}))`);
    await sleep(700);
    const panel2 = await evalJs("$('d-graph-detail').innerText");
    check(/产品经理/.test(panel2) && panel2 !== panel, "切换节点后面板内容随之更新");
    const log2 = await evalJs("($('gp-log')||{}).textContent || ''");
    const prevMarker = new RegExp(`== STAGE ${PICK_STAGE} ==`);
    if (hasMarkers) {
      check(/== STAGE pm ==/.test(log2) && !prevMarker.test(log2), "日志切片跟着阶段切换，互不串台");
    } else {
      skip("旧运行无阶段标记，跳过「切片互不串台」断言");
    }

    // 产物联动：定位到下方「阶段产物与操作」并高亮
    await evalJs(`document.querySelector('.gnode[data-stage="${PICK_STAGE}"]').dispatchEvent(new MouseEvent('click',{bubbles:true}))`);
    await sleep(600);
    check(await evalJs("!!document.querySelector('#d-graph-detail [data-locate]')"),
      "产物条目带「在阶段产物中定位」按钮");
    await evalJs("$('d-graph-detail').querySelector('[data-locate]').click()");
    await sleep(500);
    check(await evalJs("document.querySelectorAll(`#d-stages .stage.hl[data-stage='" + PICK_STAGE + "']`).length >= 1"),
      "定位后对应产物块被高亮");
    check(await evalJs("document.querySelector('#d-stages .stage.hl .stage-body').hidden === false"),
      "定位时自动展开产物块");

    // 点击响应速度：从点击到详情面板内容可读
    const t0 = Date.now();
    await evalJs(`document.querySelector('.gnode[data-stage="review"]').dispatchEvent(new MouseEvent('click',{bubbles:true}))`);
    await evalJs("new Promise(r=>{const t=setInterval(()=>{const p=$('d-graph-detail');" +
      "if(p&&/评审/.test(p.innerText)&&!p.hidden){clearInterval(t);r(true)}}," +
      "20);setTimeout(()=>{clearInterval(t);r(false)},3000)})");
    const dt = Date.now() - t0;
    check(dt < 1200, `点击到详情可读的响应时间 ${dt}ms（阈值 1200ms）`);

    check(problems.length === 0, "页面无 console.error / 未捕获异常", problems.slice(0, 3).join(" | "));

    if (process.env.SHOT) {
      try {
        await evalJs(`document.querySelector('.gnode[data-stage="${PICK_STAGE}"]').dispatchEvent(new MouseEvent('click',{bubbles:true}))`);
        await sleep(800);
        const rect = await evalJs(
          "(()=>{const r=$('d-graph-card').getBoundingClientRect();" +
          "return {x:r.x+window.scrollX,y:r.y+window.scrollY,w:r.width,h:r.height}})()"
        );
        const shot = await send("Page.captureScreenshot", {
          format: "png", captureBeyondViewport: true,
          clip: { x: rect.x, y: rect.y, width: rect.w, height: rect.h, scale: 2 },
        });
        writeFileSync(process.env.SHOT, Buffer.from(shot.data, "base64"));
        console.log(`  截图: ${process.env.SHOT}（${Math.round(rect.w)}×${Math.round(rect.h)} @2x）`);
      } catch (e) {
        console.log("  截图失败:", e.message);
      }
    }
  } finally {
    try { ws && ws.close(); } catch { /* ignore */ }
    edge.kill();
    await sleep(300);
    try { rmSync(profile, { recursive: true, force: true }); } catch { /* ignore */ }
    if (picked.mine) {
      // 子进程可能刚落地还没被判定为结束，删失败就重试几次（别把测试数据留在 runs/ 里）
      for (let i = 0; i < 6; i++) {
        const res = await fetch(new URL(`/api/runs/${runId}`, BASE), { method: "DELETE" })
          .catch(() => null);
        if (res && res.ok) { console.log(`  已删除自建运行: ${runId}`); break; }
        await sleep(500);
      }
    }
  }

  const failed = results.filter((r) => !r).length;
  console.log(`\n${results.length - failed}/${results.length} 通过` + (failed ? "，有失败项" : ""));
  return failed ? 1 : 0;
}

let code = 1;
try {
  code = await run();
} catch (e) {
  console.error("UI 冒烟异常:", e.message);
  code = 1;
}
process.exit(code);
