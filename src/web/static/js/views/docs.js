/* ============================================================
   资料库浏览视图（P3）：#/docs/:type
   - 主区列出 materials / reports / knowledge 的 markdown（GET /api/docs）
   - 点击文档 → 右侧阅读器打开（openDoc）
   - 顶部三个分类 tab 切换（navigate hash）
   ============================================================ */

import { setState, getState } from "../store.js";
import { listDocs } from "../api.js";
import { escapeHtml } from "../markdown.js";
import { openDoc } from "./reader.js";

const $ = (s) => document.querySelector(s);

export const DOC_TYPES = [
  { type: "materials", label: "📁 学习资料", badge: "资料" },
  { type: "reports", label: "📄 解读报告", badge: "报告" },
  { type: "knowledge", label: "📝 知识笔记", badge: "笔记" },
];

export function isDocType(t) {
  return DOC_TYPES.some((d) => d.type === t);
}

export async function showDocs(type) {
  const t = isDocType(type) ? type : "materials";
  setState({ view: "docs", activeDocType: t });
  await renderDocs(t);
}

async function renderDocs(type) {
  const body = $("#chatBody");
  if (!body) return;
  $("#chatTitle").textContent = "资料库";
  $("#chatMeta").textContent = "";

  let items = [];
  try {
    const docs = await listDocs();
    items = (docs[type] || []).slice().sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  } catch (_) { /* 拉取失败显示空态 */ }

  // 分类 tab
  let html = `<div class="docs-tabs">`;
  for (const d of DOC_TYPES) {
    const active = d.type === type ? " active" : "";
    html += `<a class="docs-tab${active}" href="#/docs/${d.type}">${d.label}</a>`;
  }
  html += `</div>`;

  if (!items.length) {
    html += `<div class="chat-empty"><div class="big">📂</div>暂无文档</div>`;
    body.innerHTML = html;
    return;
  }

  html += `<div class="docs-view">`;
  for (const f of items) {
    if (f.path === "knowledge/INDEX.md") continue;
    const badge = badgeFor(f.path);
    const time = f.mtime ? fmtTime(f.mtime) : "";
    html += `<div class="docs-item" data-path="${escapeHtml(f.path)}">` +
      `<span class="docs-badge">${badge}</span>` +
      `<span class="docs-name">${escapeHtml(titleOf(f.path))}</span>` +
      `<span class="docs-time">${escapeHtml(time)}</span>` +
      `</div>`;
  }
  html += `</div>`;
  body.innerHTML = html;

  body.querySelectorAll(".docs-item").forEach((el) =>
    el.addEventListener("click", () => openDoc(el.getAttribute("data-path"))));
}

function badgeFor(path) {
  if (path.startsWith("materials/")) return "资料";
  if (path.startsWith("reports/")) return "报告";
  return "笔记";
}

function titleOf(path) {
  const name = (path || "").split("/").pop() || path;
  return name.replace(/\.md$/, "").replace(/-解读$/, "");
}

function fmtTime(epoch) {
  const d = new Date(epoch * 1000);
  if (Number.isNaN(d.getTime())) return "";
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/* 供 main.js 在 docs 视图下切换会话栏高亮等无需操作；保留导出备用 */
export function currentDocType() {
  return getState().activeDocType;
}
