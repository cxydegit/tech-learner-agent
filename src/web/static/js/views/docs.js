/* ============================================================
   资料库浏览视图：#/docs/:type
   - 主区列出 materials / reports / knowledge 的 markdown（GET /api/docs）
   - 点击文档 → 右侧阅读器打开（openDoc）
   - 顶部三个分类 tab 切换（navigate hash）
   ============================================================ */

import { setState, getState } from "../store.js";
import { listDocs } from "../api.js";
import { escapeHtml } from "../markdown.js";
import { openDoc } from "./reader.js";

const $ = (s) => document.querySelector(s);

/* 分类图标（SVG 字面量，与侧栏资料库入口一致） */
const ICONS = {
  materials: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>`,
  reports: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>`,
  knowledge: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>`,
};
const ICON_FOLDER = ICONS.materials;

export const DOC_TYPES = [
  { type: "materials", label: "学习资料", icon: ICONS.materials, badge: "资料" },
  { type: "reports", label: "解读报告", icon: ICONS.reports, badge: "报告" },
  { type: "knowledge", label: "知识笔记", icon: ICONS.knowledge, badge: "笔记" },
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
    html += `<a class="docs-tab${active}" href="#/docs/${d.type}">${d.icon}<span>${d.label}</span></a>`;
  }
  html += `</div>`;

  if (!items.length) {
    html += `<div class="chat-empty"><div class="big">${ICON_FOLDER}</div>暂无文档</div>`;
    body.innerHTML = html;
    body.scrollTop = 0;
    return;
  }

  if (type === "knowledge") {
    html += renderKnowledgeGroups(items);
  } else {
    html += `<div class="docs-view">`;
    for (const f of items) {
      if (f.path === "knowledge/INDEX.md") continue;
      html += itemHTML(f);
    }
    html += `</div>`;
  }
  body.innerHTML = html;
  // chatBody 是全站唯一的滚动容器：切视图/切分类时不清零会沿用上一个页面的位置
  body.scrollTop = 0;

  body.querySelectorAll(".docs-item").forEach((el) =>
    el.addEventListener("click", () => openDoc(el.getAttribute("data-path"))));
}

/* 知识笔记按技术子目录分组（knowledge/<tech>/<file>.md），每组可折叠 */
function renderKnowledgeGroups(items) {
  const groups = new Map();
  for (const f of items) {
    if (f.path === "knowledge/INDEX.md") continue;
    const tech = techOf(f.path);
    if (!groups.has(tech)) groups.set(tech, []);
    groups.get(tech).push(f);
  }
  // 组间按组内最新笔记时间倒序：最近在学的技术排在最上面
  const ordered = [...groups.entries()].sort(
    (a, b) => latestMtime(b[1]) - latestMtime(a[1]));

  let html = `<div class="tech-groups">`;
  ordered.forEach(([tech, files], i) => {
    html += `<details class="tech-group"${i === 0 ? " open" : ""}>` +
      `<summary class="tech-head">` +
      `<span class="tech-name">${escapeHtml(techLabel(tech))}</span>` +
      `<span class="tech-count">${files.length}</span>` +
      `</summary>` +
      `<div class="docs-view">`;
    for (const f of files) html += itemHTML(f);
    html += `</div></details>`;
  });
  html += `</div>`;
  return html;
}

function latestMtime(files) {
  let max = 0;
  for (const f of files) if ((f.mtime || 0) > max) max = f.mtime;
  return max;
}

/* knowledge/react/2026-10-18-xxx.md → react */
function techOf(path) {
  const parts = (path || "").split("/");
  return parts.length >= 3 ? parts[1] : "";
}

const TECH_LABEL = {
  llm: "LLM",
  rag: "RAG",
  react: "React",
  redis: "Redis",
  python: "Python",
  pytorch: "PyTorch",
  springai: "Spring AI",
  springboot: "Spring Boot",
  "skill-plugin": "Skill / Plugin",
};

function techLabel(tech) {
  if (!tech) return "未分类";
  const hit = TECH_LABEL[tech.toLowerCase()];
  if (hit) return hit;
  return tech.replace(/[-_]/g, " ").replace(/\b[a-z]/g, (c) => c.toUpperCase());
}

function itemHTML(f) {
  const badge = badgeFor(f.path);
  const time = f.mtime ? fmtTime(f.mtime) : "";
  return `<div class="docs-item" data-path="${escapeHtml(f.path)}">` +
    `<span class="docs-badge">${badge}</span>` +
    `<span class="docs-name">${escapeHtml(titleOf(f.path))}</span>` +
    `<span class="docs-time">${escapeHtml(time)}</span>` +
    `</div>`;
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
