/* ============================================================
   对话视图：会话列表 / 对话流 / 场景卡片 / SSE 进度 / note 合并确认
   数据源全部来自后端 API（替换原型 mock）。
   ============================================================ */

import {
  getState, setState, subscribe, activeSession,
} from "../store.js";
import {
  listSessions, createSession, getSession, deleteSession,
  runSession, resumeSession, streamEvents,
} from "../api.js";
import { CARDS, cardByCmd, buildFormHTML, collectPayload } from "../cards.js";
import { mdToHtml, escapeHtml } from "../markdown.js";
import { navigate } from "../router.js";
import { closeReader, openDoc } from "./reader.js";

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const CHIP_LABEL = { collect: "查看资料清单", read: "阅读全文", qa: "查看来源笔记" };

let expandedCmd = null;      // 当前展开的卡片
let lastCollect = { tech: "", focus: "" }; // 记忆回填上次 collect 的技术名
let closeStream = null;      // 当前 SSE 关闭函数（切换会话时关闭）
let interruptKind = null;    // 当前 interrupt 类型：note（合并确认）/ coach（陪练问答）
let coachPayload = null;     // 当前 coach 问题负载（resume 失败时恢复面板）

/* ============================================================
   渲染
   ============================================================ */

function renderAll() {
  renderSessionList();
  renderChat();
  renderInputZone();
}

export function renderSessionList() {
  const el = $("#sessionList");
  if (!el) return;
  const { sessions, activeId } = getState();
  el.innerHTML = "";
  for (const s of sessions) {
    const div = document.createElement("div");
    div.className = "session-item" + (s.thread_id === activeId ? " active" : "");
    const meta = formatTime(s.updated_at) +
      (s.note_count || s.qa_count ? ` · ${s.note_count}篇笔记 ${s.qa_count}问` : "");
    div.innerHTML =
      `<div class="si-title">${escapeHtml(s.title || "新会话")}</div>` +
      `<div class="si-meta">${escapeHtml(meta)}</div>` +
      `<button class="si-del" type="button" aria-label="删除会话" title="删除">✕</button>`;
    div.addEventListener("click", () => {
      const target = `chat/${encodeURIComponent(s.thread_id)}`;
      if (location.hash === "#" + target) {
        selectSession(s.thread_id); // 已在当前会话 hash，直接刷新详情
      } else {
        // 统一走路由：确保切回 chat 视图、更新链接（从资料库/docs 视图点击时关键）
        navigate(target);
      }
    });
    div.querySelector(".si-del").addEventListener("click", (e) => {
      e.stopPropagation();
      removeSession(s.thread_id);
    });
    el.appendChild(div);
  }
}

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function renderChat() {
  const body = $("#chatBody");
  const title = $("#chatTitle");
  const meta = $("#chatMeta");
  const s = activeSession();
  if (!s) {
    title.textContent = "学习之旅";
    meta.textContent = "";
    body.innerHTML =
      `<div class="chat-empty"><div class="big">📚</div>` +
      `还没有会话——点「＋ 新建会话」开始</div>`;
    return;
  }
  title.textContent = s.title || "新会话";
  meta.textContent = s.tech ? `主题：${s.tech}` : "";

  const { active, activeId, noteRunning, notePending } = getState();
  const conversation = (active && active.conversation) || [];

  if (!conversation.length) {
    body.innerHTML =
      `<div class="chat-empty"><div class="big">📚</div>` +
      `新会话已就绪——选一张卡片开始学习</div>`;
    return;
  }

  let html = `<div class="chat-stream">`;
  for (const m of conversation) {
    if (m.role === "user") {
      html += `<div class="msg-user"><div class="bubble">` +
        `<div class="msg-meta"><span class="msg-role">你</span>` +
        `<span>${escapeHtml(fmtTime(m.ts))}</span></div>` +
        `${escapeHtml(m.content)}</div></div>`;
    } else {
      html += `<div class="msg-ai"><div class="bubble">`;
      html += `<div class="msg-meta"><span class="msg-role">AI 学习助手</span>` +
        `<span>${escapeHtml(fmtTime(m.ts))}</span></div>`;
      html += `<div class="md-body">${mdToHtml(m.content)}</div>`;
      if (m.doc) {
        html += `<button class="doc-chip" data-doc="${escapeHtml(m.doc)}">` +
          `📄 ${CHIP_LABEL[m.type] || CHIP_LABEL[m.doc_type] || "阅读全文"} ↗</button>`;
      }
      if (m.sources && m.sources.length) {
        html += `<div class="source-cards"><div class="sc-label">📚 来源笔记</div><div class="sc-list">`;
        for (const s of m.sources) {
          const name = s.path
            ? (s.path.split("/").pop() || "").replace(/\.md$/, "")
            : (s.topic || "笔记");
          const sim = typeof s.similarity === "number" ? s.similarity.toFixed(2) : "";
          html += `<button class="sc-item" data-src="${escapeHtml(s.path || "")}">` +
            `<span class="sc-name">${escapeHtml(name)}</span>` +
            (sim ? `<span class="sc-sim">${sim}</span>` : "") + `</button>`;
        }
        html += `</div></div>`;
      }
      html += `</div></div>`;
    }
  }
  // read 完成提醒：最后一条是「成功 read」且无 note 在跑 / 无待确认沉淀时，提示一键沉淀
  const lastMsg = conversation[conversation.length - 1];
  const readDone = lastMsg && lastMsg.role === "assistant" && lastMsg.type === "read"
    && !String(lastMsg.content || "").startsWith("❌");
  if (readDone && !noteRunning && !notePending[activeId]) {
    html += `<div class="note-reminder"><div class="nr-row">` +
      `<span class="nr-text">本次解读完成，是否沉淀为知识笔记？</span>` +
      `<button class="nr-btn" data-note type="button">📝 一键沉淀</button></div></div>`;
  }
  html += `</div>`;
  body.innerHTML = html;

  $$(".note-reminder .nr-btn").forEach((btn) =>
    btn.addEventListener("click", triggerNote));
  $$(".doc-chip").forEach((btn) =>
    btn.addEventListener("click", () => openDoc(btn.getAttribute("data-doc"))));
  $$(".source-cards .sc-item").forEach((btn) =>
    btn.addEventListener("click", () => openDoc(btn.getAttribute("data-src"), { badge: "笔记" })));
  scrollChatBottom();
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return String(ts);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

function scrollChatBottom() {
  const body = $("#chatBody");
  body.scrollTop = body.scrollHeight;
}

/* ============================================================
   会话操作：选择 / 新建 / 删除
   ============================================================ */

export async function selectSession(id) {
  closeReader(); // 切换会话收起阅读器，避免残留上一会话文档
  // 切走当前会话：后台任务靠 getSession 的 job_active/pending_note 恢复，不阻塞新会话
  setState({ activeId: id, active: null, currentDoc: null, running: false });
  closeStream && closeStream();
  closeStream = null;
  renderAll();
  if (!id) return;
  try {
    const detail = await getSession(id);
    setState({ active: detail });
    // 有待答的 coach 问题 → 恢复问答面板（定制路线：切会话 / 刷新不丢）
    if (detail.pending_coach) {
      interruptKind = "coach";
      coachPayload = detail.pending_coach;
      setState({ running: false, coachPending: { ...getState().coachPending, [id]: detail.pending_coach } });
      renderChat();
      appendAiMessage(detail.pending_coach.message || "",
        detail.pending_coach.doc, detail.pending_coach.doc_type);
      showCoachReplyInput();
      return;
    }
    // 有待确认的 note 合并 → 恢复决策面板（核心需求 4：切会话 / 刷新不丢）
    if (detail.pending_note) {
      setState({ notePending: { ...getState().notePending, [id]: detail.pending_note.candidates_text } });
      renderChat();
      showInterrupt(detail.pending_note.candidates_text);
      return;
    }
    // 后台任务还在跑 → 渲染已有会话流 + 禁用输入，按命令类型重连 SSE
    if (detail.job_active) {
      setState({ running: true });
      renderChat();
      renderInputZone();
      attachStream(id, detail.job_command === "note" ? "note" : "task");
      return;
    }
    renderChat();
  } catch (err) {
    showError(err.message);
  }
}

export async function newSession() {
  try {
    const { thread_id } = await createSession();
    const sessions = await listSessions();
    setState({ sessions, active: null });
    renderSessionList();
    // navigate 触发 hashchange → main.route → selectSession（不在此处重复拉详情）
    navigate(`chat/${encodeURIComponent(thread_id)}`);
  } catch (err) {
    showError(err.message);
  }
}

export async function removeSession(id) {
  const { activeId, notePending, coachPending } = getState();
  // 删除会话时清理其待确认沉淀 / 待答问题，避免残留状态影响 isBusy / beforeunload
  const next = { ...notePending };
  delete next[id];
  const cnext = { ...coachPending };
  delete cnext[id];
  setState({ notePending: next, coachPending: cnext });
  try {
    await deleteSession(id);
    const sessions = await listSessions();
    setState({ sessions });
    renderSessionList();
    if (activeId === id) {
      closeStream && closeStream();
      closeStream = null;
      const next = sessions[0] ? sessions[0].thread_id : null;
      // hashchange → main.route → selectSession（切到下一个或空）
      if (next) navigate(`chat/${encodeURIComponent(next)}`);
      else location.hash = "";
    }
  } catch (err) {
    showError(err.message);
  }
}

/* ============================================================
   输入区：场景卡片 + 表单
   ============================================================ */

export function renderInputZone() {
  const zone = $("#inputZone");
  if (!zone) return;
  const { activeId } = getState();
  let html = `<div class="zone-a"><div class="cards-a">`;
  for (const c of CARDS) {
    const active = expandedCmd === c.cmd ? " active-card" : "";
    const dis = c.disabled ? " disabled" : "";
    html += `<div class="card${active}${dis}" data-cmd="${c.cmd}" ` +
      `style="--accent-c:${c.accent};--soft-c:${c.soft}">` +
      `<span class="card-icon">${c.icon}</span>` +
      `<span class="card-label">${c.label}</span>` +
      `<span class="card-desc">${c.desc}</span></div>`;
  }
  html += `</div>`;
  if (expandedCmd) {
    const card = cardByCmd(expandedCmd);
    if (card && !card.disabled) html += buildFormHTML(card, lastCollect);
  }
  html += `</div>`;
  zone.innerHTML = html;

  $$("#inputZone .card").forEach((el) => {
    el.addEventListener("click", () => {
      const cmd = el.getAttribute("data-cmd");
      const card = cardByCmd(cmd);
      if (!card || card.disabled || isBusy(activeId)) return;
      expandedCmd = expandedCmd === cmd ? null : cmd;
      renderInputZone();
    });
  });

  $$("#inputZone .form-panel").forEach((panel) => {
    const cmd = panel.getAttribute("data-cmd");
    panel.querySelector(".btn-primary").addEventListener("click", () => submitCard(cmd, panel));
    panel.querySelector(".fh-close").addEventListener("click", () => {
      expandedCmd = null;
      renderInputZone();
    });
    panel.querySelectorAll("input").forEach((inp) => {
      inp.addEventListener("keydown", (e) => {
        if (e.key === "Enter") submitCard(cmd, panel);
      });
      inp.addEventListener("input", () => inp.closest(".field").classList.remove("has-err"));
    });
  });

  const first = zone.querySelector(".form-panel input");
  if (first) first.focus();
}

/* ============================================================
   提交 → run → SSE 进度 → final / interrupt
   ============================================================ */

async function submitCard(cmd, panel) {
  const payload = collectPayload(cmd, panel);
  if (payload.error) return;
  const { activeId } = getState();
  if (!activeId) { showError("请先新建或选择会话"); return; }
  if (isBusy(activeId)) return; // 有任务在跑 / 有待确认沉淀：卡片入口已禁用，此处兜底

  if (cmd === "collect") lastCollect = { tech: payload.tech || "", focus: payload.focus || "" };
  expandedCmd = null;
  renderInputZone();

  // 乐观显示用户消息 + 运行状态（最终以 SSE final 后刷新为准）
  showUserMessage(cmd === "collect" ? `${payload.tech}${payload.focus ? " " + payload.focus : ""}`
    : cmd === "route" ? (payload.tech || "")
    : (payload.args ? payload.args[0] : ""));

  let started = false;
  try {
    await runSession(activeId, payload);
    started = true;
  } catch (err) {
    if (err.status === 409) { // 已有任务在跑，仍订阅现有流
      started = true;
    } else {
      showError(err.message);
      return;
    }
  }
  setState({ running: true });
  renderInputZone();
  attachStream(activeId);
}

/* 一键沉淀：复用 run 流程发 note 命令，按 note 流订阅 SSE */
async function triggerNote() {
  const { activeId } = getState();
  if (!activeId || isBusy(activeId)) return;
  expandedCmd = null;
  renderInputZone();
  try {
    await runSession(activeId, { command: "note" });
  } catch (err) {
    if (err.status !== 409) { showError(err.message); return; }
    // 409：已有任务在跑，仍订阅现有流
  }
  setState({ running: true, noteRunning: true });
  renderInputZone();
  attachStream(activeId, "note");
}

/* 有任务在跑 / 有待确认沉淀 / 有待答 coach 问题 → 禁用卡片与沉淀按钮 */
function isBusy(id) {
  const { running, notePending, coachPending, activeId } = getState();
  const key = id || activeId;
  return running || !!(notePending || {})[key] || !!(coachPending || {})[key];
}

/* 订阅 SSE 进度：progress 追加 / interrupt 决策 / final 刷新
   kind="note" 时 final/error 走 clearNoteState 清理 pending 状态（task 不碰 note 状态）。 */
function attachStream(id, kind = "task") {
  closeStream && closeStream();
  showRunStatus();
  closeStream = streamEvents(id, {
    progress: (e) => appendProgress(e.message),
    interrupt: (e) => onInterrupt(e),
    final: async () => {
      clearRunStatus();
      if (kind === "note") clearNoteState();
      clearCoachState(id);
      await refreshActive();
      setState({ running: false, noteRunning: false });
      renderInputZone();
    },
    error: (e) => {
      clearRunStatus();
      if (kind === "note") clearNoteState();
      clearCoachState(id);
      showError(e.message || "执行失败");
      setState({ running: false, noteRunning: false });
      renderInputZone();
    },
    done: () => {
      closeStream && closeStream();
      closeStream = null;
    },
  });
}

/* interrupt 分发：coach 问答（定制路线）/ note 合并确认 */
function onInterrupt(e) {
  const { activeId } = getState();
  if (!activeId) return;
  if (e.kind === "coach_question") {
    interruptKind = "coach";
    coachPayload = e.payload;
    setState({
      coachPending: { ...getState().coachPending, [activeId]: e.payload },
      running: false, // worker 已退出（interrupt 暂停），其他会话可继续用
    });
    renderInputZone();
    appendAiMessage((e.payload && e.payload.message) || "", // 陪练问题即时上屏
      e.payload && e.payload.doc, e.payload && e.payload.doc_type);
    showCoachReplyInput();
    return;
  }
  // note 合并确认（原流程）
  interruptKind = "note";
  setState({
    notePending: { ...getState().notePending, [activeId]: e.payload },
    noteRunning: false,
    running: false,
  });
  renderInputZone();
  showInterrupt(e.payload);
}

/* 清理某会话的 note 待决策状态（note 流 final/error 时调用） */
function clearNoteState() {
  const { activeId, notePending } = getState();
  if (!activeId) return;
  const next = { ...notePending };
  delete next[activeId];
  setState({ notePending: next, noteRunning: false });
}

/* 清理某会话的 coach 待答状态（coach 流 final/error 时调用） */
function clearCoachState(id) {
  if (!id) return;
  const next = { ...getState().coachPending };
  delete next[id];
  setState({ coachPending: next });
}

function showUserMessage(content) {
  const body = $("#chatBody");
  let stream = body.querySelector(".chat-stream");
  if (!stream) {
    body.innerHTML = "";
    stream = document.createElement("div");
    stream.className = "chat-stream";
    body.appendChild(stream);
  }
  const div = document.createElement("div");
  div.className = "msg-user";
  div.innerHTML = `<div class="bubble"><div class="msg-meta"><span class="msg-role">你</span>` +
    `<span>${escapeHtml(fmtTime(new Date().toISOString()))}</span></div>${escapeHtml(content)}</div>`;
  stream.appendChild(div);
  scrollChatBottom();
}

/* ---- 运行状态区（进度 / 中断） ---- */
function runStatusEl() {
  let el = document.getElementById("runStatus");
  if (!el) {
    el = document.createElement("div");
    el.id = "runStatus";
    el.className = "run-status";
    $("#chatBody").appendChild(el);
  }
  return el;
}

function showRunStatus() {
  const el = runStatusEl();
  el.innerHTML = `<div class="run-title">⏳ 执行中…</div><div class="run-progress"></div>`;
  el.hidden = false;
  scrollChatBottom();
}

function appendProgress(msg) {
  const el = runStatusEl();
  const line = document.createElement("div");
  line.className = "progress-line";
  line.textContent = msg;
  el.querySelector(".run-progress").appendChild(line);
  scrollChatBottom();
}

function clearRunStatus() {
  const el = document.getElementById("runStatus");
  if (el) el.hidden = true;
}

/* note 合并确认：展示候选 + 决策按钮 → resume */
function showInterrupt(payload) {
  const el = runStatusEl();
  el.innerHTML =
    `<div class="interrupt-panel">` +
    `<div class="interrupt-title">🧭 需要你决定如何处理相似笔记</div>` +
    `<pre class="interrupt-payload">${escapeHtml(payload || "")}</pre>` +
    `<div class="interrupt-actions">` +
    `<button class="btn-primary" data-answer="all" type="button">全部合并</button>` +
    `<button class="btn-ghost" data-answer="skip" type="button">全部跳过</button>` +
    `</div>` +
    `<div class="interrupt-custom">` +
    `<label>或输入要合并的编号（逗号分隔）：</label>` +
    `<input id="interruptPick" type="text" placeholder="如 1,3" />` +
    `<button class="btn-primary" data-answer="pick" type="button">提交</button>` +
    `</div></div>`;
  el.hidden = false;
  scrollChatBottom();

  el.querySelectorAll(".interrupt-actions button").forEach((btn) => {
    btn.addEventListener("click", () => resume(btn.getAttribute("data-answer")));
  });
  const pick = el.querySelector("#interruptPick");
  el.querySelector('button[data-answer="pick"]').addEventListener("click", () => {
    const v = (pick.value || "").trim();
    if (v) resume(v);
  });
  pick.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && pick.value.trim()) resume(pick.value.trim());
  });
}

/* 把陪练问题作为 AI 气泡追加到聊天区（coach 问答即时上屏；final 时由权威状态整体重渲染，无重复）。
   doc/docType 来自 coach_question 负载：collect/read 产出文档时实时渲染「查看完整文档」chip。 */
function appendAiMessage(content, doc, docType) {
  const body = $("#chatBody");
  let stream = body.querySelector(".chat-stream");
  if (!stream) {
    body.innerHTML = "";
    stream = document.createElement("div");
    stream.className = "chat-stream";
    body.appendChild(stream);
  }
  const div = document.createElement("div");
  div.className = "msg-ai";
  let chip = "";
  if (doc) {
    chip = `<button class="doc-chip" data-doc="${escapeHtml(doc)}">` +
      `📄 ${CHIP_LABEL[docType] || "阅读全文"} ↗</button>`;
  }
  div.innerHTML = `<div class="bubble"><div class="msg-meta"><span class="msg-role">AI 学习助手</span>` +
    `<span>${escapeHtml(fmtTime(new Date().toISOString()))}</span></div>` +
    `<div class="md-body">${mdToHtml(content)}</div>${chip}</div>`;
  stream.appendChild(div);
  if (chip) {
    div.querySelector(".doc-chip").addEventListener("click", () =>
      openDoc(div.querySelector(".doc-chip").getAttribute("data-doc")));
  }
  scrollChatBottom();
}

/* coach 问答输入框：问题已作为气泡上屏，这里只放回复输入（发送时把用户回复先上屏再 resume）。
   保留已有进度行（如"已自动沉淀"通知）不覆盖——只在进度下方追加输入区，避免通知被瞬间抹掉。 */
function showCoachReplyInput() {
  const el = runStatusEl();
  const inputHtml =
    `<div class="interrupt-panel coach-panel">` +
    `<div class="interrupt-custom coach-reply">` +
    `<input id="coachReply" type="text" placeholder="回复陪练…（输入 结束 可退出）" />` +
    `<button class="btn-primary" data-coach-send type="button">发送</button>` +
    `</div></div>`;
  const progress = el.querySelector(".run-progress");
  if (progress) {
    const title = el.querySelector(".run-title");
    if (title) title.textContent = "⌨️ 等你回复";
    const panel = document.createElement("div");
    panel.innerHTML = inputHtml;
    el.appendChild(panel.firstChild);
  } else {
    el.innerHTML = inputHtml;
  }
  el.hidden = false;
  scrollChatBottom();
  const input = el.querySelector("#coachReply");
  const send = () => {
    const v = (input.value || "").trim();
    if (!v) return;
    showUserMessage(v); // 用户回复先上屏（final 时由权威状态重渲染）
    resume(v);
  };
  el.querySelector("[data-coach-send]").addEventListener("click", send);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
  input.focus();
}

async function resume(answer) {
  const { activeId } = getState();
  if (!activeId) return;
  const kind = interruptKind === "coach" ? "task" : "note";
  setState({ running: true });
  if (kind === "note") setState({ noteRunning: true });
  showRunStatus();
  try {
    await resumeSession(activeId, answer);
    attachStream(activeId, kind); // coach → task 流；note → note 流
  } catch (err) {
    if (err.status === 409) {
      attachStream(activeId, kind);
    } else {
      // resume 启动失败：interrupt 仍挂在后端，重置状态并恢复面板，避免卡死「执行中」反复重试
      setState({ running: false, noteRunning: false });
      renderInputZone();
      showError(err.message);
      if (kind === "note") {
        const pending = (getState().notePending || {})[activeId];
        if (pending) showInterrupt(pending);
      } else if (coachPayload) {
        showCoachReplyInput();
      }
    }
  }
}

async function refreshActive() {
  const { activeId } = getState();
  if (!activeId) return;
  try {
    const detail = await getSession(activeId);
    const sessions = await listSessions();
    setState({ active: detail, sessions });
    // 安全网：SSE 未收到 interrupt 但后端已暂停（如刷新/切走再回）→ 恢复面板
    if (detail.pending_coach) {
      interruptKind = "coach";
      coachPayload = detail.pending_coach;
      setState({ running: false, coachPending: { ...getState().coachPending, [activeId]: detail.pending_coach } });
      renderChat();
      appendAiMessage(detail.pending_coach.message || "",
        detail.pending_coach.doc, detail.pending_coach.doc_type);
      showCoachReplyInput();
      return;
    }
    if (detail.pending_note) {
      setState({ notePending: { ...getState().notePending, [activeId]: detail.pending_note.candidates_text } });
      renderChat();
      showInterrupt(detail.pending_note.candidates_text);
      return;
    }
    renderChat();
    renderSessionList();
  } catch (_) { /* 刷新失败不阻断 */ }
}

function showError(msg) {
  const body = $("#chatBody");
  const div = document.createElement("div");
  div.className = "run-error";
  div.textContent = "⚠ " + (msg || "出错了");
  body.appendChild(div);
  scrollChatBottom();
}

/* ============================================================
   初始化
   ============================================================ */

export function initChat() {
  // store 变更驱动列表与输入区重渲染（对话流由 selectSession 显式控制；docs 视图不重写）
  subscribe((state) => {
    renderSessionList();
    if (state.view === "chat" && !state.active) renderChat();
  });
  $("#btnNewSession").addEventListener("click", newSession);
  renderAll();
}

export async function loadSessions() {
  const sessions = await listSessions();
  setState({ sessions });
}
