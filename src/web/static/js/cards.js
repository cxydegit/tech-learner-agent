/* ============================================================
   场景卡片：配置 + 表单构建 + 校验（与 domain/card_input.py 契约一致）
   - collect → {command, tech, focus?}
   - read / ask → {command, args: [...]}（卡片命令名 ask → 图命令 qa）
   - route → {command, tech}
   ============================================================ */

import { escapeHtml } from "./markdown.js";

/* 卡片线性图标（SVG 字面量，颜色由 CSS 的 --accent-c 控制） */
const ICONS = {
  folderPlus: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/><line x1="12" y1="11" x2="12" y2="17"/><line x1="9" y1="14" x2="15" y2="14"/></svg>`,
  doc: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`,
  chatLines: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/><line x1="8" y1="9" x2="16" y2="9"/><line x1="8" y1="13" x2="12" y2="13"/></svg>`,
  target: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="3.5"/></svg>`,
};

export const CARDS = [
  {
    cmd: "collect", icon: ICONS.folderPlus, label: "收集深度资料", desc: "单次生成 · 输入主题，快速生成资料清单",
    accent: "#6FA8DC", soft: "rgba(111,168,220,.05)", border: "#6FA8DC",
    fields: [
      { name: "tech", label: "技术名", ph: "如 FastAPI", req: true },
      { name: "focus", label: "关注点", ph: "如 异步编程（可选）", req: false },
    ],
    btn: "开始收集",
  },
  {
    cmd: "read", icon: ICONS.doc, label: "解读文档", desc: "读懂一篇文档 · 上传或粘贴文档，解析核心",
    accent: "#5FB3A3", soft: "rgba(95,179,163,.05)", border: "#5FB3A3",
    fields: [
      { name: "url", label: "链接", ph: "https://…", req: true },
    ],
    btn: "开始解读",
  },
  {
    cmd: "ask", icon: ICONS.chatLines, label: "问我的笔记", desc: "联想检索回答 · 基于已有笔记，关联回答",
    accent: "#9B8CD9", soft: "rgba(155,140,217,.05)", border: "#9B8CD9",
    fields: [
      { name: "question", label: "问题", ph: "如 笔记里提到过异步和协程吗？", req: true },
    ],
    btn: "提问",
  },
  {
    cmd: "route", icon: ICONS.target, label: "定制路线", desc: "问卷 + 路线 + 陪练 · 多轮定制路径",
    accent: "#D98CB0", soft: "rgba(217,140,176,.05)", border: "#D98CB0",
    fields: [
      { name: "tech", label: "技术名", ph: "如 Spring Boot", req: true },
    ],
    btn: "开始定制",
  },
];

/* 校验文案（与 domain/card_input.py 的 _REQUIRED_MESSAGES 一致） */
export const VALIDATE_TEXT = {
  collect: "请输入技术名",
  read: "请输入链接",
  ask: "请输入问题",
  route: "请输入技术名",
};

export function cardByCmd(cmd) {
  return CARDS.find((c) => c.cmd === cmd);
}

/* 收集表单各字段的值 → 卡片契约 dict；校验不通过返回 {error: <文案>} */
export function collectPayload(cmd, panel) {
  const card = cardByCmd(cmd);
  const values = {};
  for (const f of card.fields) {
    const input = panel.querySelector(`[data-field="${f.name}"] input`);
    const v = (input ? input.value : "").trim();
    values[f.name] = v;
    const wrap = panel.querySelector(`[data-field="${f.name}"]`);
    if (f.req && !v) { wrap.classList.add("has-err"); return { error: VALIDATE_TEXT[cmd] }; }
    wrap.classList.remove("has-err");
  }
  if (cmd === "collect") {
    const payload = { command: "collect", tech: values.tech };
    if (values.focus) payload.focus = values.focus;
    return payload;
  }
  if (cmd === "read") return { command: "read", args: [values.url] };
  if (cmd === "route") return { command: "route", tech: values.tech };
  return { command: "ask", args: [values.question] };
}

/* 构建表单 HTML（复用原型样式 .form-panel/.field/.btn-primary） */
export function buildFormHTML(card, lastCollect) {
  let html = `<div class="form-panel" data-cmd="${card.cmd}">`;
  html += `<div class="form-head"><span class="fh-icon">${card.icon}</span>` +
          `<span class="fh-title">${card.label}</span>` +
          `<button class="fh-close" type="button" aria-label="关闭">✕</button></div>`;
  for (const f of card.fields) {
    const val = (f.name === "tech" && card.cmd === "collect" && lastCollect)
      ? escapeHtml(lastCollect.tech || "") : "";
    html += `<div class="field" data-field="${f.name}">` +
            `<label>${f.label}${f.req ? " *" : ""}</label>` +
            `<input type="text" placeholder="${f.ph}" value="${val}" />` +
            `<div class="err">${VALIDATE_TEXT[card.cmd]}</div></div>`;
  }
  html += `<button class="btn-primary" type="button">${card.btn}</button>`;
  html += `</div>`;
  return html;
}
