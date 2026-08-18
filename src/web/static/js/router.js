/* ============================================================
   hash 路由：#/chat/:id、#/docs/:type
   - chat/:id  → 打开某会话
   - 无 hash   → 默认列表首个会话（或空）
   ============================================================ */

export function parseHash() {
  const raw = location.hash.replace(/^#\/?/, "").trim();
  if (!raw) return { view: "chat", id: null };
  const parts = raw.split("/");
  return { view: parts[0] || "chat", id: parts[1] || null };
}

export function navigate(path) {
  if (location.hash === "#" + path) return;
  location.hash = path;
}

export function onRouteChange(fn) {
  window.addEventListener("hashchange", () => fn(parseHash()));
}
