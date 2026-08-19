/* ============================================================
   入口：加载会话列表 → 初始化视图 → 处理 hash 路由
   #/chat/:id → 打开会话；#/docs/:type → 资料库浏览；无 hash → 默认列表首个
   ============================================================ */

import { getState, setState, subscribe } from "./store.js";
import { initChat, loadSessions, selectSession } from "./views/chat.js";
import { initReader } from "./views/reader.js";
import { showDocs } from "./views/docs.js";
import { parseHash, onRouteChange } from "./router.js";

/* docs 视图时给 .app 加 class（隐藏输入区等），其余恢复 */
function syncViewClass() {
  const app = document.querySelector(".app");
  if (!app) return;
  const { view } = getState();
  app.classList.toggle("view-docs", view === "docs");
}

async function route(view, id) {
  if (view === "chat") {
    setState({ view: "chat" });
    const { sessions } = getState();
    if (!id && sessions.length) id = sessions[0].thread_id;
    await selectSession(id);
  } else if (view === "docs") {
    await showDocs(id);
  } else {
    location.hash = "";
  }
}

async function init() {
  try {
    await loadSessions();
  } catch (e) {
    console.error("加载会话列表失败:", e);
  }
  initChat();
  initReader();
  subscribe(syncViewClass);
  const { view, id } = parseHash();
  await route(view, id);
  onRouteChange(({ view, id }) => route(view, id));
}

/* 关闭 / 刷新页面时，有任务在跑或有待确认沉淀 → 弹系统确认框（文案浏览器会忽略但保留提示） */
window.addEventListener("beforeunload", (e) => {
  const { running, noteRunning, notePending } = getState();
  const noteBusy = noteRunning || Object.keys(notePending || {}).length > 0;
  if (running || noteBusy) {
    e.preventDefault();
    e.returnValue = "有任务正在执行/沉淀进行中";
  }
});

document.addEventListener("DOMContentLoaded", init);
