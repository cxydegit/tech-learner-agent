/* ============================================================
   阅读器：从后端 API 读取文档 → 渲染 markdown
   - 对话里点「阅读全文」chip → openDoc(path)
   - 空态展示最近文档（三目录按 mtime 最新）
   打开时会话栏自动隐藏、对话/阅读器 1:1（复用原型样式 .has-reader）
   ============================================================ */

import { getState, setState } from "../store.js";
import { listDocs, readDoc } from "../api.js";
import { mdToHtml, escapeHtml } from "../markdown.js";

const $ = (s) => document.querySelector(s);

/* 顶栏切换按钮 / 空态图标（SVG 字面量，与 index.html 初始模板一致） */
const ICON_BOOK = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>`;
const ICON_CLOSE = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`;

export function badgeFor(path) {
  if (path.startsWith("materials/")) return "资料";
  if (path.startsWith("reports/")) return "报告";
  return "笔记";
}

export function titleOf(path) {
  const name = (path || "").split("/").pop() || path;
  return name.replace(/\.md$/, "").replace(/-解读$/, "");
}

export async function openDoc(path, opts = {}) {
  const doc = await readDoc(path);
  if (!doc) return;
  const currentDoc = { path, title: titleOf(path), badge: opts.badge || badgeFor(path) };
  setState({ currentDoc });
  $("#docBadge").textContent = currentDoc.badge;
  $("#docTitle").textContent = currentDoc.title;
  $("#readerHead").classList.remove("hidden");
  // md-body 提供完整 markdown 样式（表格边框/代码块/引用等），reader-doc 负责阅读器专属排版
  $("#readerBody").innerHTML = `<div class="reader-doc md-body">${mdToHtml(doc.content)}</div>`;
  showReader();
}

/* 直接渲染一段 markdown（不走 docs 白名单）：定制路线的「查看路线」用 */
export function openMarkdown(title, markdown, badge = "文档") {
  const currentDoc = { path: "", title, badge };
  setState({ currentDoc });
  $("#docBadge").textContent = badge;
  $("#docTitle").textContent = title;
  $("#readerHead").classList.remove("hidden");
  $("#readerBody").innerHTML = `<div class="reader-doc md-body">${mdToHtml(markdown || "")}</div>`;
  showReader();
}

function showReader() {
  document.querySelector(".app").classList.add("has-reader");
  $("#btnToggleReader").innerHTML = `${ICON_CLOSE}<span>收起</span>`;
}

export function closeReader() {
  setState({ currentDoc: null });
  $("#readerHead").classList.add("hidden");
  document.querySelector(".app").classList.remove("has-reader");
  $("#btnToggleReader").innerHTML = `${ICON_BOOK}<span>阅读器</span>`;
  renderReaderEmpty();
}

export async function renderReaderEmpty() {
  let items = [];
  try {
    const docs = await listDocs();
    for (const list of [docs.materials, docs.reports, docs.knowledge]) {
      for (const f of list || []) {
        if (f.path === "knowledge/INDEX.md") continue; // 跳过索引文件
        items.push({ path: f.path, badge: badgeFor(f.path), mtime: f.mtime });
      }
    }
    items.sort((a, b) => (b.mtime || 0) - (a.mtime || 0));
  } catch (_) { /* 拉取失败显示空态 */ }

  if (!items.length) {
    $("#readerBody").innerHTML = `<div class="reader-empty"><div class="big">${ICON_BOOK}</div>暂无文档</div>`;
    return;
  }
  let html =
    `<div class="reader-empty"><div class="big">${ICON_BOOK}</div>` +
    `在对话里点开「阅读全文」，<br/>或从下方打开最近文档</div>`;
  html += `<div class="recent-docs"><div class="rd-label">最近文档</div>`;
  for (const it of items.slice(0, 6)) {
    html += `<div class="rd-item" data-doc="${escapeHtml(it.path)}">` +
      `<span class="rd-type">${it.badge}</span>` +
      `<span class="rd-name">${escapeHtml(titleOf(it.path))}</span></div>`;
  }
  html += `</div>`;
  $("#readerBody").innerHTML = html;
  $("#readerBody").querySelectorAll(".rd-item").forEach((el) =>
    el.addEventListener("click", () => openDoc(el.getAttribute("data-doc"))));
}

export function initReader() {
  $("#btnCloseReader").addEventListener("click", closeReader);
  $("#btnToggleReader").addEventListener("click", () => {
    if (document.querySelector(".app").classList.contains("has-reader")) closeReader();
    else {
      showReader();
      renderReaderEmpty();
    }
  });
  renderReaderEmpty();
}
