/* ============================================================
   轻量 markdown → HTML 渲染器（零依赖，原型够用）
   支持：标题 / 列表 / 表格 / 代码围栏 / 引用 / 粗体斜体 / 行内代码 / 链接 / 分割线
   ============================================================ */

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function mdInline(t) {
  let out = escapeHtml(t);
  out = out.replace(/`([^`]+)`/g, "<code>$1</code>");
  out = out.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  out = out.replace(/\*([^*]+)\*/g, "<em>$1</em>");
  out = out.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  return out;
}

function mdToHtml(src) {
  if (!src) return "";
  const lines = String(src).replace(/\r\n/g, "\n").split("\n");
  const html = [];
  let i = 0;

  const isTableSep = (ln) => /^\s*\|?[\s:|-]+\|?\s*$/.test(ln) && ln.includes("-");

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) { i++; continue; }

    // 代码围栏
    if (/^```/.test(line.trim())) {
      const code = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i].trim())) { code.push(lines[i]); i++; }
      i++; // 跳过收尾 ```
      html.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
      continue;
    }

    // 标题
    const h = line.match(/^(#{1,6})\s+(.*)/);
    if (h) {
      const lvl = h[1].length;
      html.push(`<h${lvl}>${mdInline(h[2])}</h${lvl}>`);
      i++;
      continue;
    }

    // 分割线
    if (/^---+\s*$/.test(line.trim())) { html.push("<hr/>"); i++; continue; }

    // 引用块（连续 > 行）
    if (/^>\s?/.test(line)) {
      const q = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) { q.push(lines[i].replace(/^>\s?/, "")); i++; }
      html.push(`<blockquote>${q.map(mdInline).join("<br/>")}</blockquote>`);
      continue;
    }

    // 无序列表
    if (/^[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^[-*]\s+/.test(lines[i])) { items.push(lines[i].replace(/^[-*]\s+/, "")); i++; }
      html.push(`<ul>${items.map((it) => `<li>${mdInline(it)}</li>`).join("")}</ul>`);
      continue;
    }

    // 有序列表
    if (/^\d+[.)]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\d+[.)]\s+/.test(lines[i])) { items.push(lines[i].replace(/^\d+[.)]\s+/, "")); i++; }
      html.push(`<ol>${items.map((it) => `<li>${mdInline(it)}</li>`).join("")}</ol>`);
      continue;
    }

    // 表格：当前行以 | 开头且下一行是分隔行
    if (line.trim().startsWith("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const splitCells = (row) => row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((s) => s.trim());
      const header = splitCells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) { rows.push(splitCells(lines[i])); i++; }
      let tb = "<table><thead><tr>";
      header.forEach((c) => { tb += `<th>${mdInline(c)}</th>`; });
      tb += "</tr></thead><tbody>";
      rows.forEach((r) => {
        tb += "<tr>";
        r.forEach((c) => { tb += `<td>${mdInline(c)}</td>`; });
        tb += "</tr>";
      });
      html.push(tb + "</tbody></table>");
      continue;
    }

    // 段落：累积普通行
    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^```/.test(lines[i].trim()) &&
      !/^(#{1,6})\s/.test(lines[i]) &&
      !/^(>|[-*]|\d+[.)])\s/.test(lines[i]) &&
      !lines[i].trim().startsWith("|")
    ) {
      para.push(lines[i]);
      i++;
    }
    html.push(`<p>${para.map(mdInline).join("\n")}</p>`);
  }

  return html.join("\n");
}
