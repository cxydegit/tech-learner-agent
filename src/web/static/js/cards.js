/* ============================================================
   场景卡片：配置 + 表单构建 + 校验（与 domain/card_input.py 契约一致）
   - collect → {command, tech, focus?}
   - read / ask → {command, args: [...]}（卡片命令名 ask → 图命令 qa）
   - "定制路线" disabled（Step 6）
   ============================================================ */

import { escapeHtml } from "./markdown.js";

export const CARDS = [
  {
    cmd: "collect", icon: "📚", label: "学习新技术", desc: "搜集资料清单",
    accent: "#4A7A78", soft: "#E6EFED",
    fields: [
      { name: "tech", label: "技术名", ph: "如 FastAPI", req: true },
      { name: "focus", label: "关注点", ph: "如 异步编程（可选）", req: false },
    ],
    btn: "开始收集",
  },
  {
    cmd: "read", icon: "📖", label: "解读文档", desc: "读懂一篇文档",
    accent: "#8A6B4F", soft: "#F2EAE1",
    fields: [
      { name: "url", label: "链接", ph: "https://…", req: true },
    ],
    btn: "开始解读",
  },
  {
    cmd: "ask", icon: "💬", label: "问我的笔记", desc: "联想检索回答",
    accent: "#4A628A", soft: "#E7ECF4",
    fields: [
      { name: "question", label: "问题", ph: "如 笔记里提到过异步和协程吗？", req: true },
    ],
    btn: "提问",
  },
  {
    cmd: "route", icon: "🧭", label: "定制路线", desc: "问卷 + 路线 + 陪练",
    accent: "#8A5A6B", soft: "#F3E9EC",
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
