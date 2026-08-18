/* ============================================================
   API 层：fetch + EventSource 封装（对接 src/web/server.py）
   全部端点见 WEB_PLAN.md §5；错误统一抛 Error(detail)。
   ============================================================ */

const BASE = "/api";

async function req(path, options = {}) {
  const r = await fetch(BASE + path, options);
  if (!r.ok) {
    let detail = r.statusText;
    try {
      const body = await r.json();
      if (body && body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch (_) { /* 非 JSON 错误体，保留 statusText */ }
    const err = new Error(detail);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

function json(method, body) {
  return {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

/* ---- 会话 ---- */
export function listSessions() { return req("/sessions"); }
export function createSession() { return req("/sessions", { method: "POST" }); }
export function getSession(id) { return req(`/sessions/${encodeURIComponent(id)}`); }
export function deleteSession(id) { return req(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }); }

/* ---- 图执行：run / resume ---- */
export function runSession(id, payload) { return req(`/sessions/${encodeURIComponent(id)}/run`, json("POST", payload)); }
export function resumeSession(id, answer) { return req(`/sessions/${encodeURIComponent(id)}/resume`, json("POST", { answer })); }

/* ---- 文档浏览 ---- */
export function listDocs() { return req("/docs"); }
export async function readDoc(path) {
  const r = await fetch(`${BASE}/docs/content?path=${encodeURIComponent(path)}`);
  if (!r.ok) return null;
  return r.json();
}

/* ---- SSE 进度流 ----
   返回关闭函数；handlers 按事件 type 分发：progress / interrupt / final / error / done。
   EventSource 是 GET，服务端在流结束（done）或网络断开时触发 onerror。 */
export function streamEvents(id, handlers) {
  const es = new EventSource(`${BASE}/sessions/${encodeURIComponent(id)}/stream`);
  es.onmessage = (e) => {
    let evt;
    try { evt = JSON.parse(e.data); } catch (_) { return; }
    if (handlers["*"]) handlers["*"](evt);
    const fn = handlers[evt.type];
    if (fn) fn(evt);
  };
  // 服务端主动关闭（done）或连接出错：不做重连（新任务需重新 run/resume 再开流）
  es.onerror = () => {};
  return () => es.close();
}
