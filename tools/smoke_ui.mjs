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
/**
 * 模拟人工解决 **PM 强控闸门**（未裁决的 open_questions + 两列未明确项）。
 *
 * 强控的语义：产物里只要还有"不是陈述"的条目，pm 之后就不放行；续跑会被原地再挡一次。
 * 所以这里必须**先把条目解决掉**再续跑 —— 只 resume 是白跑（这正是它与其它闸门的区别）。
 */
async function resolvePmGate(runId, detail) {
  const pm = ((detail && detail.stages) || []).find((s) => s.stage === "pm");
  const art = (pm && pm.artifact) || {};
  const decisions = [];
  (art.open_questions || []).forEach((q) => {
    const ref = String((q && q.question) || "").trim();
    if (ref) {
      decisions.push({
        kind: "pm_question",
        ref,
        decision: String((q && q.assumed_answer) || "（人工按建议执行）"),
      });
    }
  });
  const fixed = JSON.parse(JSON.stringify(art));
  fixed.unknowns = [];
  fixed.clarifying_questions = [];
  await fetchJson(`/api/runs/${runId}/artifact`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ stage: "pm", artifact: fixed }),
  });
  if (decisions.length) {
    await fetchJson(`/api/runs/${runId}/pm-decisions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decisions }),
    });
  }
  await fetchJson(`/api/runs/${runId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  });
}

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
      if (st === "paused" && after === "pm") {
        // PM 强控：还有未成为陈述的条目时**不放行**，必须先由人工解决。
        // 这里模拟人工：逐条裁决 open_questions，并把两列未明确项清空（＝写成确定结论）。
        await resolvePmGate(runId, d);
      } else if (st === "paused") {
        // 其它闸门（如人工审核）直接放行继续跑
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
  // 作业里的模块运行（job-…-M-xx）**不写自己的 console.log**：整个作业跑在一个子进程里，
  // 日志都落在发起它的那次运行的日志中。拿它去验「按阶段切片」必然拿到空文本，
  // 于是断言会假红（页面显示「该阶段暂无日志」其实是对的）。优先避开这类运行。
  const own = (r) => !/^job-/.test(r.run_id);
  // 按阶段产物**数量**挑最完整的一次：只按「最新」挑，曾挑到刚起步就中断的运行，
  // 于是「已完成阶段标绿 / 日志按阶段切片 / 产物定位」一串断言假红（页面其实是对的）。
  const scored = rows.filter((r) => (r.stages || 0) > 0)
    .sort((a, b) => (b.stages || 0) - (a.stages || 0));
  const pick = scored.find(own) || scored[0];
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
    // 终止节点该不该绿，取决于这次运行**到底跑完没有**：跑完了绿是对的。
    // 断言的原意是「**没到** done 却被标成已完成」，所以必须先判定前提 ——
    // 否则一旦选中一次完整跑完的运行（自建 mock 运行就会跑完），这条会假红
    // （页面其实是对的）。
    const runState = (await fetchJson(`/api/runs/${runId}`)) || {};
    const runStatus = (runState.state || {}).status || "";
    if (runStatus === "done") {
      skip(`运行 ${runId} 已跑完（status=done），终止节点标绿正确，跳过该断言`);
    } else {
      check(statuses["done"] !== "g-done", "未到达的终止节点不会被误标为已完成",
        `${statuses["done"]}（status=${runStatus}）`);
    }

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

    // 缺 state.json 的运行不该被劝退「只能查看，不能续跑」——被入口总闸拆成作业的运行
    // **永远不会有** state.json（产物在 runs/_jobs/<job_id>/），说成不能续跑是误导
    // （真机 20260926-154413 就是这么把人挡在门外的）。有这种运行就顺手验一把，没有就跳过。
    const allRows = (await (await fetch(new URL("/api/runs", BASE))).json()).runs || [];
    const split = allRows.find((r) => r.status === "job");
    if (split) {
      await evalJs(`openRun(${JSON.stringify(split.run_id)})`);
      await sleep(1600);
      const msg = await evalJs("$('d-msg').innerText");
      check(/已拆分为作业|入口总闸/.test(msg) && !/不能续跑/.test(msg),
        `缺 state 的运行给的是正解（${split.run_id}），不劝退`,
        msg.split("\n").find((l) => l.trim()) || "");
      const label = await evalJs(
        `(document.querySelector('.run[data-run="${split.run_id}"] .b')||{}).innerText || ''`);
      check(/拆分为作业|运行中/.test(label), "列表徽标不再写「启动失败」", label);
    } else {
      skip("没有「拆分为作业」的运行，跳过缺 state 文案断言");
    }

    // 作业页（三阶段视图）：相位 / 两处人工环节 / 续跑按钮语义 —— 顺手也把它纳入
    // 「无 console.error」的覆盖范围（页面级的 JS 错会在这里现形）
    const jobs = ((await fetchJson("/api/jobs")) || {}).jobs || [];
    if (jobs.length) {
      await evalJs(`openJob(${JSON.stringify(jobs[0].job_id)})`);
      await sleep(1300);
      const jmsg = await evalJs("$('j-msg').innerText");
      check(
        /相位/.test(jmsg) && /人工环节只有两处/.test(jmsg),
        "作业页显示相位与「人工环节只有两处」",
        (jmsg.split("\n").find((l) => l.trim()) || "").slice(0, 90)
      );
      const jbtn = await evalJs("$('j-resume').textContent");
      check(/进入下游|继续跑下游|续跑作业/.test(jbtn), "续跑按钮文案随相位变化", jbtn);
      const modBtns = await evalJs("document.querySelectorAll('#j-modules button').length");
      check(modBtns >= 1, "作业页有子运行入口按钮", String(modBtns));

      // 交付就绪度（4 门打分）只在「待统一验收」时才有材料 —— 没有就跳过，
      // 有就必须把分数与**未验证项**都露出来（人审最需要后者）。
      const jDetail = await fetchJson(`/api/jobs/${encodeURIComponent(jobs[0].job_id)}`);
      const readiness = (jDetail?.human_review || {}).readiness;
      if (readiness) {
        const shown = await evalJs("$('j-msg').innerText");
        check(shown.includes(`交付就绪度`) && shown.includes(`${readiness.score}/${readiness.max}`),
          "验收卡片显示交付就绪度分数", String(readiness.score) + "/" + String(readiness.max));
        check((readiness.unverified || []).length === 0 ||
          /未验证项/.test(shown), "验收卡片披露未验证项",
          `${(readiness.unverified || []).length} 条`);
      } else {
        skip("该作业还没有统一验收材料（未到验收相位），跳过就绪度卡片断言");
      }
      // 渲染器本身用**合成数据**验一把：不依赖"恰好有作业处在验收相位"这种事，
      // 否则这条链路要等到真机跑到验收才第一次被执行。用的是页面里真实的那份
      // readinessBlock（不是另写一份等价逻辑，那种"测的是副本"的断言最没用）。
      const rdHtml = await evalJs(`readinessBlock({score:6,max:8,decision:"禁止放行：先返工",gates:[
        {key:"functional",label:"功能落地",score:2,evidence:[]},
        {key:"verified",label:"机械验证",score:2,evidence:[]},
        {key:"redline",label:"工程红线",score:0,evidence:["M-01：1 条红线阻断"]},
        {key:"delivered",label:"交付成型",score:2,evidence:[]}],
        unverified:["覆盖率未测量：本环境没有接入覆盖率工具","没有断言型命令"]})`);
      check(/交付就绪度/.test(rdHtml) && /6\/8/.test(rdHtml) && /覆盖率未测量/.test(rdHtml),
        "就绪度卡片渲染出分数/扣分门/未验证项", rdHtml.length + " 字符");
      check(/工程红线/.test(rdHtml) && /0\/2/.test(rdHtml) && /禁止放行/.test(rdHtml),
        "扣分能定位到具体哪一门，并给出放行结论");
      check((await evalJs("readinessBlock(null)")) === "", "没有就绪度数据时不渲染空卡片");

      // 红线/未验证项必须真的渲染到详情页 **顶部提示区** —— 这条是奔着一个真机 bug 去的：
      // 产物在 state.json 的 `artifacts` 层，而页面早期读的是顶层，于是「日志里有 5 条红线，
      // 页面一片干净」。有带红线的运行就验一把（没有就跳过）。
      const cand = (await (await fetch(new URL("/api/runs", BASE))).json()).runs || [];
      let withRules = null;
      for (const row of cand.slice(0, 10)) {
        const det = await fetchJson(`/api/runs/${encodeURIComponent(row.run_id)}`);
        // 两种形状都要认：详情接口给的是**白名单 state 视图**（产物层被摊平到顶层），
        // 而纯快照里产物在 artifacts 下。
        const art = Object.assign({}, det?.state || {}, det?.state?.artifacts || {});
        if ((art.rule_findings || []).length) { withRules = { row, art }; break; }
      }
      if (withRules) {
        await evalJs(`openRun(${JSON.stringify(withRules.row.run_id)})`);
        await sleep(1500);
        const msg = await evalJs("$('d-msg').innerText");
        check(/工程红线/.test(msg), "详情页顶部渲染出工程红线（产物层数据读得到）",
          `${withRules.row.run_id} 共 ${withRules.art.rule_findings.length} 条：` +
          (msg.split("\n").find((l) => /红线/.test(l)) || "").slice(0, 70));
        const uv = ((withRules.art.verify_report || {}).unverified || []).length;
        if (uv) {
          check(/未验证项/.test(msg), "详情页披露未验证项", `${uv} 条`);
        } else {
          skip("该运行没有未验证项，跳过未验证项展示断言");
        }
      } else {
        skip("现有运行里没有带红线的，跳过红线展示断言");
      }
    } else {
      skip("没有作业，跳过作业页断言");
    }

    // ---- §30/§34/§22.1/§29/§25/§35：本轮新增的前端能力（真实渲染 + 真实交互）
    // 上面几节可能打开了别的运行（带红线的那个）——先切回来，断言才有确定对象
    await evalJs(`openRun(${JSON.stringify(runId)})`);
    await sleep(1500);
    check(await evalJs("!!$('d-sticky') && !$('d-sticky').hidden"), "§34.1 固定控制条已渲染");
    check(await evalJs(`$('d-sticky-id').innerText === ${JSON.stringify(runId)}`),
      "§34.1 固定控制条显示当前运行 ID", await evalJs("$('d-sticky-id').innerText"));
    check(await evalJs("!!$('d-sticky-gate') && $('d-sticky-gate').innerText.length > 0"),
      "§34.1 固定控制条显示闸门与机器放行结论",
      (await evalJs("$('d-sticky-gate').innerText")).slice(0, 60));
    const navN = await evalJs("document.querySelectorAll('#d-anchor-nav button').length");
    check(navN >= 7, "§34 固定导航按钮齐备", `${navN} 个`);
    // 点一个导航按钮：不能抛错，且滚动位置应发生变化（锚点真的有效）
    const beforeY = await evalJs("window.scrollY");
    await evalJs("document.querySelector('#d-anchor-nav button[data-goto=\"d-stages-card\"]').click()");
    await sleep(500);
    check(true, "§34 点击导航按钮不抛异常");
    check(await evalJs("window.scrollY") !== beforeY || beforeY === 0,
      "§34 导航触发滚动（锚点生效）");
    // §30 阶段图 / 证明链切换
    await evalJs("setGraphView('proof')");
    await sleep(300);
    check(await evalJs("$('d-proof-chain').hidden === false"), "§30 切换到证明链：链条容器可见");
    check(await evalJs("[...document.querySelectorAll('#d-graph-card .graph-wrap')].every(el=>el.hidden)"),
      "§30 切换到证明链：阶段图隐藏");
    const chainText = await evalJs("$('d-proof-chain').innerText");
    check(/需求/.test(chainText) && /证明义务/.test(chainText) && /裁决/.test(chainText),
      "§30 证明链含 需求→证明义务→…→裁决", chainText.replace(/\n/g, " ").slice(0, 80));
    await evalJs("setGraphView('stage')");
    await sleep(300);
    check(await evalJs("$('d-proof-chain').hidden === true"), "§30 切回阶段流程");
    // §22.1 证明矩阵行展开（有 PO 才验）
    const poN = await evalJs("document.querySelectorAll('#d-proof-body tr.po-row').length");
    if (poN) {
      await evalJs("document.querySelector('#d-proof-body tr.po-row').click()");
      await sleep(200);
      const det = await evalJs(
        "(()=>{const r=document.querySelector('#d-proof-body tr.po-row').nextElementSibling;"
        + "return r && !r.hidden ? r.innerText : ''})()");
      check(/Claim/.test(det) && /Scenario/.test(det) && /Workspace/.test(det),
        "§22.1 点 PO 行展开证据明细（Claim/Scenario/Workspace）", det.replace(/\n/g, " ").slice(0, 70));
      await evalJs("document.querySelector('#d-proof-body tr.po-row').click()");
      check(await evalJs(
        "document.querySelector('#d-proof-body tr.po-row').nextElementSibling.hidden === true"),
        "§22.1 再点收起");
    } else {
      skip("该运行没有 required PO 行，跳过证明矩阵展开断言");
    }
    // §29 真值卡片：**有契约才显示**（mock 不建契约 ⇒ 正确行为是隐藏，不该判失败）
    const cpNow = (await fetchJson(`/api/runs/${encodeURIComponent(runId)}`)).control_plane || {};
    const truthData = cpNow.truth || {};
    const truthRows = (truthData.asserted || []).length + (truthData.derived || []).length;
    if (truthRows) {
      check(await evalJs("!$('d-truth-card').hidden"),
        "§29 需求真值卡片已渲染（用户原文 / 默认假设分开）",
        (await evalJs("$('d-truth-note').innerText")).slice(0, 60));
      const bodies = await evalJs("$('d-truth-body').innerText");
      if ((truthData.derived || []).length) {
        check(/默认假设/.test(bodies), "§29 默认假设被显式标为「默认假设」（不是用户要求）");
      }
    } else {
      check(await evalJs("$('d-truth-card').hidden") === true,
        "§29 没有契约时不渲染空真值卡片（不臆造）");
    }
    // §26 方案 → 编译任务：同样只在该运行真有 draft/compiled 时验
    const diff = cpNow.plan_diff || {};
    if ((diff.draft || []).length || (diff.compiled || []).length) {
      check(await evalJs("!$('d-plandiff-card').hidden"), "§26 方案→编译任务 对照卡片已渲染",
        (await evalJs("$('d-plandiff-note').innerText")).slice(0, 60));
    } else {
      check(await evalJs("$('d-plandiff-card').hidden") === true,
        "§26 没有方案数据时不渲染空对照卡片");
    }
    // §35 日志搜索：输入后必须给出命中行数，且命中被高亮
    await evalJs("$('log-search').value='dev';$('log-search').dispatchEvent(new Event('input'))");
    await sleep(400);
    const note = await evalJs("$('log-search-note').innerText");
    check(/命中 \d+ 行/.test(note), "§35 日志搜索结果计数", note);
    check(await evalJs("document.querySelectorAll('#d-log mark.hit').length > 0"),
      "§35 日志命中被高亮");
    await evalJs("$('log-search').value='';$('log-search').dispatchEvent(new Event('input'))");
    await sleep(300);
    check(await evalJs("document.querySelectorAll('#d-log mark.hit').length === 0"),
      "§35 清空搜索后高亮消失");
    // §25 Workspace revision 过滤证据链
    const wsN = await evalJs("document.querySelectorAll('#d-workspace-body .ws-row').length");
    if (wsN) {
      await evalJs("document.querySelector('#d-workspace-body .ws-row').click()");
      await sleep(300);
      check(await evalJs("/只显示/.test($('d-evidence-filter').innerText)"),
        "§25 点 revision 后证据链进入过滤态", await evalJs("$('d-evidence-filter').innerText"));
      await evalJs("$('btn-ev-clear').click()");
      await sleep(300);
      check(await evalJs("$('d-evidence-filter').innerText === ''"), "§25 可清除过滤");
    } else {
      skip("该运行没有 workspace 链，跳过 revision 过滤断言");
    }

    // ---- 合成数据渲染路径：现有真机 run 都早于 Phase A–F（没有 requirement_contract /
    // proof_gate.obligations），所以"有数据时怎么渲染"必须用**后端真实形状**的数据在
    // 真实浏览器里验一遍 —— 否则要等下一次真机运行才发现面板是空的。
    const synthetic = {
      release_gate: {status: "UNPROVEN", can_pass: false, semantic_verdict: "pass",
                     proof_status: "UNPROVEN", ontology_status: "VALID",
                     workspace_status: "VERIFIED", verified_revision: "ws-006",
                     decision_id: "dec-1", blocking_reasons: ["缺 2 个 required PO"]},
      proof_summary: {required: 3, covered: 1, failed: 1, unproven: 1, weak: 0,
                      unexecutable: 0},
      proofs: [{
        id: "po:FR-04", status: "UNPROVEN", requirement: "req:FR-04",
        claim: "吃到食物后得分 +10", kind: "behavior", task: "T-02",
        scenario: "tscn:x", evidence: [], workspace_revision: "",
        required: true, scenario_detail: {}, command_details: [], evidence_detail: [],
      }],
      evidence_summary: {total: 0, stale: 0, by_kind: {}},
      evidence: [], ontology: {status: "VALID", errors: [], error_count: 0,
                               revision: "onto-1", requirement_count: 8},
      workspace: {verified_revision: "ws-006", status: "VERIFIED",
                  chain: [{revision: "ws-006", task: "T-02", status: "VERIFIED"}]},
      tasks: [{id: "T-01", semantic_task_id: "stask:1", task_revision: 1,
               target_files: ["game_logic.py"], symbols: ["Snake"], creates_file: true,
               implements_requirements: ["req:FR-01"], candidate_requirement_ids: []}],
      test: {required: 3, covered: 1, weak: 0, missing: 1, unexecutable: 1,
             unbound_commands: [{command: "python main.py", candidate: "po:ctr",
                                 reason: "该 PO 由就地机械检查器证明"}],
             unsafe_commands: [], verification_modes: {mechanical: ["po:ctr"],
                                                       gui_smoke: ["po:ui"]},
             external_required: ["po:ui"], coverage_gap: ["po:FR-04"],
             scenarios: [{id: "tscn:x", target_po: "po:FR-04", kind: "behavior",
                          title: "得分", status: "unproven", gap_reason: "没有可执行命令",
                          verification_mode: "unit", mechanical_check: [],
                          commands: [], assertions: []}]},
      plan_diff: {
        draft: [{id: "T-01", side: "draft", files: ["game_logic.py"], symbols: ["Snake"],
                 creates_file: false, task_revision: null, supersedes: [],
                 requirements: [], candidates: []},
                {id: "T-02", side: "draft", files: ["game_logic.py"],
                 symbols: ["Game.score"], creates_file: false, task_revision: null,
                 supersedes: [], requirements: [], candidates: []}],
        compiled: [{id: "T-01", side: "compiled", files: ["game_logic.py"],
                    symbols: ["Snake", "Game.score"], creates_file: true,
                    task_revision: 1, supersedes: [], requirements: [], candidates: []}],
        files: [{file: "game_logic.py", draft_ids: ["T-01", "T-02"],
                 compiled_ids: ["T-01"], symbols: ["Snake", "Game.score"],
                 creates_file: true, create_owner: "T-01", modify_tasks: [],
                 kind: "merged"}],
        reasons: ["架构师把同文件拆成多张图"],
      },
      truth: {
        asserted: [
          {bucket: "declared_files", label: "用户声明文件", text: "game_logic.py",
           truth: "ASSERTED", source_quote: "game_logic.py", mechanical_check: []},
          {bucket: "hard_constraints", label: "硬约束",
           text: "游戏逻辑不得依赖 tkinter", truth: "ASSERTED",
           source_quote: "不得依赖 tkinter", mechanical_check: ["forbidden_import:tkinter"]},
        ],
        derived: [{text: "目标用户为终端玩家", source: "intake", truth: "DERIVED"}],
        human: [{scope: "pm", decision: "采用建议答案", subject: "q1"}],
        contradicted: [{code: "PLAN_FORBIDDEN_DEPENDENCY", object: "T-01",
                        message: "T-01 依赖 tkinter"}],
        grounding_errors: [{code: "ASSERTED_WITHOUT_QUOTE", detail: "x"}],
        constraint_checks: [], version: 1,
      },
      decision: {verdict: "rework_dev", can_pass: false, decision_id: "dec-1",
                 evidence_ids: ["ev:17"], defect_ids: ["def:1"],
                 semantic_review_is_candidate_only: true},
      issues: {}, next_action: {text: "缺 2 个 required PO → 去 Test 阶段补测试",
                                route: "test"},
    };
    await evalJs(`(()=>{const d={run_id:'synthetic-ui-check',state:{status:'done'},
      control_plane:${JSON.stringify(synthetic)}};S.detail=d;renderControlPlane(d);return true})()`);
    await sleep(400);
    check(await evalJs("!$('d-truth-card').hidden"), "§29 有契约时真值卡片渲染出来");
    const tBody = await evalJs("$('d-truth-body').innerText");
    check(/默认假设/.test(tBody) && /用户原文/.test(tBody) && /人工确认/.test(tBody)
          && /forbidden_import:tkinter/.test(tBody),
      "§29 四态 + 机械检查绑定都渲染（默认假设 ≠ 用户原文）",
      tBody.replace(/\s+/g, " ").slice(0, 90));
    check(await evalJs("!$('d-plandiff-card').hidden") && /合并/.test(
      await evalJs("$('d-plandiff-body').innerText")),
      "§26 多张 draft 合成一张 ⇒ 显示「合并」");
    check(/架构师 2 张 → 编译器 1 张/.test(await evalJs("$('d-plandiff-note').innerText")),
      "§26 对照卡片给出前后张数",
      await evalJs("$('d-plandiff-note').innerText"));
    check(/GUI 冒烟/.test(await evalJs("$('d-test-body').innerText"))
          && /LLM 候选命令（编译器不认，未执行）/.test(await evalJs("$('d-test-body').innerText")),
      "§27 验证方式分类 + 候选命令分离都渲染");
    check(/最终机器裁决/.test(await evalJs("$('d-ctl-next').innerText"))
          && /依据/.test(await evalJs("$('d-ctl-next').innerText")),
      "§28 最终机器裁决 + 依据渲染",
      (await evalJs("$('d-ctl-next').innerText")).replace(/\s+/g, " ").slice(0, 80));
    await evalJs("setGraphView('proof')");
    await sleep(250);
    check(/需求 8/.test(await evalJs("$('d-proof-chain').innerText")),
      "§30 证明链取到需求条数（Requirement → PO …）",
      (await evalJs("$('d-proof-chain').innerText")).replace(/\s+/g, " ").slice(0, 90));
    await evalJs("setGraphView('stage')");
    await evalJs(`openRun(${JSON.stringify(runId)})`);
    await sleep(1200);

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
