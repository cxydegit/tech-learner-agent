/* ============================================================
   原型交互逻辑：会话列表 / 对话流 / 场景卡片 / 阅读器
   - 阅读器打开 → 会话栏自动隐藏，对话/阅读器 1:1
   - AI 消息通栏平铺居中；用户消息右侧气泡
   - 卡片为透明无底色、一行四个、圆形图标徽标
   （纯前端 mock，后续接入真实后端）
   ============================================================ */

(function () {
  "use strict";

  // ---------- 状态 ----------
  const state = {
    sessions: JSON.parse(JSON.stringify(MOCK_SESSIONS)),
    activeId: "s1",
    docs: JSON.parse(JSON.stringify(MOCK_DOCS)),
    expandedCmd: null,            // 当前展开的卡片
    lastCollect: { tech: "", focus: "" }, // 记忆回填：上次 collect 的技术名
    currentDoc: null,
    docSeq: 0,
  };

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => Array.from(document.querySelectorAll(s));

  const CHIP_LABEL = { collect: "查看资料清单", read: "阅读全文", ask: "查看来源笔记" };

  function session() {
    return state.sessions.find((s) => s.id === state.activeId) || state.sessions[0];
  }

  function renderAll() {
    renderSessionList();
    renderChat();
    renderInputZone();
    renderReader();
  }

  // ============================================================
  // 会话列表
  // ============================================================
  function renderSessionList() {
    const el = $("#sessionList");
    el.innerHTML = "";
    state.sessions.forEach((s) => {
      const div = document.createElement("div");
      div.className = "session-item" + (s.id === state.activeId ? " active" : "");
      div.innerHTML =
        `<div class="si-title">${escapeHtml(s.title)}</div>` +
        `<div class="si-meta">${escapeHtml(s.updatedAt)} · ${s.messages.length} 条消息</div>`;
      div.addEventListener("click", () => selectSession(s.id));
      el.appendChild(div);
    });
  }

  function selectSession(id) {
    state.activeId = id;
    state.currentDoc = null;
    renderAll();
  }

  function newSession() {
    const id = "s" + Date.now();
    state.sessions.unshift({
      id, title: "新会话", tech: "", updatedAt: "刚刚", messages: [],
    });
    state.activeId = id;
    state.currentDoc = null;
    renderAll();
  }

  // ============================================================
  // 对话流
  // ============================================================
  function renderChat() {
    const body = $("#chatBody");
    const s = session();
    $("#chatTitle").textContent = s.title;
    $("#chatMeta").textContent = s.updatedAt + (s.tech ? " · " + s.tech : "");

    if (!s.messages.length) {
      body.innerHTML =
        `<div class="chat-empty"><div class="big">📚</div>` +
        `新会话已就绪——选一张卡片开始学习</div>`;
      return;
    }

    let html = `<div class="chat-stream">`;
    s.messages.forEach((m) => {
      if (m.role === "user") {
        html +=
          `<div class="msg-user"><div class="bubble">` +
          `<div class="msg-meta"><span class="msg-role">你</span><span>${escapeHtml(m.ts)}</span></div>` +
          `${escapeHtml(m.content)}</div></div>`;
      } else {
        html += `<div class="msg-ai"><div class="bubble">`;
        html += `<div class="msg-meta"><span class="msg-role">AI 学习助手</span><span>${escapeHtml(m.ts)}</span></div>`;
        html += `<div class="md-body">${mdToHtml(m.content)}</div>`;
        if (m.doc && state.docs[m.doc]) {
          html += `<button class="doc-chip" data-doc="${m.doc}">📄 ${CHIP_LABEL[m.type] || "阅读全文"} ↗</button>`;
        }
        html += `</div></div>`;
      }
    });
    html += `</div>`;
    body.innerHTML = html;

    $$(".doc-chip").forEach((btn) =>
      btn.addEventListener("click", () => openReader(btn.getAttribute("data-doc"))));
    scrollChatBottom();
  }

  function addUserMessage(content) {
    const s = session();
    s.messages.push({ role: "user", type: "", content, ts: nowTime() });
    s.updatedAt = "刚刚";
    if (!s.title || s.title === "新会话") {
      s.title = content.length > 14 ? content.slice(0, 14) + "…" : content;
    }
    renderChat();
    renderSessionList();
  }

  function showTypingThen(reply, delay) {
    const body = $("#chatBody");
    let streamEl = body.querySelector(".chat-stream");
    if (!streamEl) {
      streamEl = document.createElement("div");
      streamEl.className = "chat-stream";
      body.appendChild(streamEl);
    }
    const wrap = document.createElement("div");
    wrap.className = "msg-ai";
    wrap.innerHTML =
      `<div class="bubble"><div class="msg-meta"><span class="msg-role">AI 学习助手</span><span>…</span></div>` +
      `<div class="typing"><i></i><i></i><i></i></div></div>`;
    streamEl.appendChild(wrap);
    scrollChatBottom();

    setTimeout(() => {
      session().messages.push({
        role: "assistant", type: reply.type, content: reply.content, doc: reply.doc, ts: nowTime(),
      });
      renderChat();
      renderSessionList();
      if (reply.doc) openReader(reply.doc); // 演示：提交后自动在右侧打开产物
    }, delay);
  }

  function scrollChatBottom() {
    const body = $("#chatBody");
    body.scrollTop = body.scrollHeight;
  }

  function nowTime() {
    const d = new Date();
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  // ============================================================
  // 阅读器（打开时会话栏自动隐藏，对话/阅读器 1:1）
  // ============================================================
  function openReader(key) {
    const doc = state.docs[key];
    if (!doc) return;
    state.currentDoc = key;
    $("#docBadge").textContent = doc.type;
    $("#docTitle").textContent = doc.title;
    $("#readerHead").classList.remove("hidden");
    $("#readerBody").innerHTML = `<div class="reader-doc">${mdToHtml(doc.content)}</div>`;
    document.querySelector(".app").classList.add("has-reader");
    $("#btnToggleReader").textContent = "✕ 收起";
  }

  function closeReader() {
    state.currentDoc = null;
    $("#readerHead").classList.add("hidden");
    renderReaderEmpty();
    document.querySelector(".app").classList.remove("has-reader");
    $("#btnToggleReader").textContent = "📂 阅读器";
  }

  function renderReaderEmpty() {
    const keys = Object.keys(state.docs);
    if (!keys.length) {
      $("#readerBody").innerHTML = `<div class="reader-empty"><div class="big">📖</div>暂无文档</div>`;
      return;
    }
    let html =
      `<div class="reader-empty"><div class="big">📖</div>` +
      `在对话里点开「阅读全文」，<br/>或从下方打开最近文档</div>`;
    html += `<div class="recent-docs"><div class="rd-label">最近文档</div>`;
    keys.slice(0, 6).forEach((k) => {
      const d = state.docs[k];
      html +=
        `<div class="rd-item" data-doc="${k}">` +
        `<span class="rd-type">${d.type}</span><span class="rd-name">${escapeHtml(d.title)}</span></div>`;
    });
    html += `</div>`;
    $("#readerBody").innerHTML = html;
    $$(".rd-item").forEach((el) =>
      el.addEventListener("click", () => openReader(el.getAttribute("data-doc"))));
  }

  function renderReader() {
    if (state.currentDoc && state.docs[state.currentDoc]) openReader(state.currentDoc);
    else renderReaderEmpty();
  }

  // ============================================================
  // 输入区：场景卡片（一行四个 · 透明 · 圆形图标徽标）
  // ============================================================
  function renderInputZone() {
    const zone = $("#inputZone");
    let html = `<div class="zone-a"><div class="cards-a">`;
    CARDS.forEach((c) => {
      const active = state.expandedCmd === c.cmd ? " active-card" : "";
      const dis = c.disabled ? " disabled" : "";
      html +=
        `<div class="card${active}${dis}" data-cmd="${c.cmd}" ` +
        `style="--accent-c:${c.accent};--soft-c:${c.soft}">` +
        `<span class="card-icon">${c.icon}</span>` +
        `<span class="card-label">${c.label}</span>` +
        `<span class="card-desc">${c.desc}</span></div>`;
    });
    html += `</div>`;
    if (state.expandedCmd) {
      const card = CARDS.find((c) => c.cmd === state.expandedCmd);
      if (card && !card.disabled) html += buildForm(card);
    }
    html += `</div>`;
    zone.innerHTML = html;

    // 卡片点击 → 展开 / 收起表单
    $$("#inputZone .card").forEach((el) => {
      el.addEventListener("click", () => {
        const cmd = el.getAttribute("data-cmd");
        const card = CARDS.find((c) => c.cmd === cmd);
        if (!card || card.disabled) return;
        state.expandedCmd = state.expandedCmd === cmd ? null : cmd;
        renderInputZone();
      });
    });

    // 表单事件
    bindFormEvents(zone);

    const first = zone.querySelector(".form-panel input");
    if (first) first.focus();
  }

  // ---- 表单 ----
  function buildForm(card) {
    let html = `<div class="form-panel" data-cmd="${card.cmd}">`;
    html += `<div class="form-head"><span class="fh-icon">${card.icon}</span><span class="fh-title">${card.label}</span>` +
            `<button class="fh-close" type="button" aria-label="关闭">✕</button></div>`;
    card.fields.forEach((f) => {
      const val = f.name === "tech" && card.cmd === "collect"
        ? escapeHtml(state.lastCollect.tech) : "";
      html +=
        `<div class="field" data-field="${f.name}">` +
        `<label>${f.label}${f.req ? " *" : ""}</label>` +
        `<input type="text" placeholder="${f.ph}" value="${val}" />` +
        `<div class="err">${VALIDATE_TEXT[card.cmd]}</div></div>`;
    });
    html += `<button class="btn-primary" type="button">${card.btn}</button>`;
    html += `</div>`;
    return html;
  }

  function bindFormEvents(root) {
    root.querySelectorAll(".form-panel").forEach((panel) => {
      const cmd = panel.getAttribute("data-cmd");
      const card = CARDS.find((c) => c.cmd === cmd);
      panel.querySelector(".btn-primary").addEventListener("click", () => submitForm(card, panel));
      panel.querySelector(".fh-close").addEventListener("click", () => {
        state.expandedCmd = null;
        renderInputZone();
      });
      panel.querySelectorAll("input").forEach((inp) => {
        inp.addEventListener("keydown", (e) => {
          if (e.key === "Enter") submitForm(card, panel);
        });
        inp.addEventListener("input", () => inp.closest(".field").classList.remove("has-err"));
      });
    });
  }

  // ============================================================
  // 提交 → 校验 → 追加消息 → 模拟 AI 回复
  // ============================================================
  function submitForm(card, panel) {
    const values = {};
    let ok = true;
    card.fields.forEach((f) => {
      const input = panel.querySelector(`[data-field="${f.name}"] input`);
      const v = (input.value || "").trim();
      values[f.name] = v;
      const wrap = panel.querySelector(`[data-field="${f.name}"]`);
      if (f.req && !v) { wrap.classList.add("has-err"); ok = false; }
      else wrap.classList.remove("has-err");
    });
    if (!ok) return;

    let userText;
    if (card.cmd === "collect") userText = values.tech + (values.focus ? " " + values.focus : "");
    else if (card.cmd === "read") userText = values.url;
    else userText = values.question;

    state.lastCollect = { tech: values.tech || "", focus: values.focus || "" };
    state.expandedCmd = null;
    renderInputZone();

    addUserMessage(userText);
    const reply = mockReply(card, values);
    showTypingThen(reply, 900 + Math.round(Math.random() * 500));
  }

  // ---- 模拟 AI 回复（含动态产物文档）----
  function mockReply(card, v) {
    const seq = ++state.docSeq;
    if (card.cmd === "collect") {
      const tech = v.tech;
      const focus = v.focus;
      const title = `${tech} · 学习资料清单`;
      const docContent =
        `# ${title}\n\n> 生成时间：2026-08-13${focus ? " · 关注点：" + focus : ""}\n\n` +
        `## 核心必读资源\n\n` +
        `| 优先级 | 资料名称 | 来源 | 链接 | 为什么推荐 |\n` +
        `| ------ | ------- | ---- | ---- | ---------- |\n` +
        `| ★★★ | ${tech} 官方文档 | 官方 | [链接](https://example.com) | 权威、结构清晰 |\n` +
        `| ★★ | 社区精选教程 | 博客 | [链接](https://example.com) | 示例丰富、贴近实战 |\n` +
        `| ★ | 可运行示例项目 | GitHub | [链接](https://example.com) | 能跑起来、可直接借鉴 |\n\n` +
        `## 学习路线建议\n\n1. 通读官方文档入门章节\n2. 跟着示例项目动手写一个 Demo\n3. 结合关注点做一次小实践`;
      const key = `docs/gen-${seq}.md`;
      state.docs[key] = { type: "资料", title, content: docContent };
      const chatContent =
        `# ${title}\n\n> 生成时间：2026-08-13${focus ? " · 关注点：" + focus : ""}\n\n` +
        `## 核心必读资源\n\n` +
        `| 优先级 | 资料名称 | 来源 | 链接 |\n` +
        `| ------ | ------- | ---- | ---- |\n` +
        `| ★★★ | ${tech} 官方文档 | 官方 | [链接](https://example.com) |\n` +
        `| ★★ | 社区精选教程 | 博客 | [链接](https://example.com) |\n` +
        `| ★ | 示例项目 | GitHub | [链接](https://example.com) |\n\n共收集到 **5 条去重资源**。`;
      return { type: "collect", doc: key, content: chatContent };
    }

    if (card.cmd === "read") {
      const url = v.url;
      const title = "文档解读报告";
      const docContent =
        `# 文档解读报告\n\n> 原文链接：${url}\n\n` +
        `## 核心概念\n\n该文档主要讲解**关键机制与最佳实践**，从入门用法到进阶配置逐步展开。\n\n` +
        `## 关键结论\n\n- 保持依赖最小化，按需引入。\n- 关注官方推荐的配置项与默认值。\n- 通过实际示例验证理解，而不是只看文档。\n\n` +
        `## 待深入\n\n- 阅读源码与官方注释，理解设计取舍。`;
      const key = `docs/gen-${seq}.md`;
      state.docs[key] = { type: "报告", title, content: docContent };
      return { type: "read", doc: key, content: docContent };
    }

    // ask
    const q = v.question;
    const title = "综合回答 · 来源笔记";
    const docContent =
      `# 综合回答\n\n基于笔记库联想检索，回答你的问题：「${q}」\n\n` +
      `- **直接相关**：笔记中有 2 处相关记录，要点已归纳。\n` +
      `- **推断**：结合已有笔记可以给出方向性建议。\n` +
      `- **笔记未覆盖**：这一细节笔记里没有记录，建议先 collect 相关资料再沉淀。\n\n` +
      `## 📚 来源笔记\n\n| 来源 | 相关度 |\n| --- | --- |\n` +
      `| knowledge/异步与协程.md | 0.71 |\n| knowledge/并发模型.md | 0.58 |`;
    const key = `docs/gen-${seq}.md`;
    state.docs[key] = { type: "笔记", title, content: docContent };
    const chatContent =
      `## 回答\n\n基于笔记库联想检索，回答你的问题：「${q}」\n\n` +
      `- 直接相关的要点已归纳（共 2 处来源）。\n- 笔记里没有记录的部分，我明确标注、未编造。\n\n` +
      `> 来源：[异步与协程](docs/async-note.md) · 相关度 0.71`;
    return { type: "ask", doc: key, content: chatContent };
  }

  // ============================================================
  // 顶栏 / 侧栏按钮
  // ============================================================
  function init() {
    renderAll();

    $("#btnNewSession").addEventListener("click", newSession);
    $("#btnCloseReader").addEventListener("click", closeReader);
    $("#btnToggleReader").addEventListener("click", () => {
      if (document.querySelector(".app").classList.contains("has-reader")) {
        closeReader();
      } else {
        document.querySelector(".app").classList.add("has-reader");
        renderReader();
        $("#btnToggleReader").textContent = "✕ 收起";
      }
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
