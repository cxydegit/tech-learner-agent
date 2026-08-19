/* ============================================================
   store：共享状态 + 极简发布订阅
   - sessions   会话列表（后端真实数据）
   - activeId   当前会话 thread_id
   - active     当前会话详情 {conversation, state}
   - running    是否有图任务在跑（禁用提交防并发）
   - currentDoc 阅读器当前打开的文档 {path, title, badge}
   - docsCache  文档内容缓存 {path: {title, badge, content}}
   ============================================================ */

const state = {
  sessions: [],
  activeId: null,
  active: null,
  running: false,
  noteRunning: false,        // 当前 note 流进行中（一键沉淀）
  notePending: {},           // thread_id → merge 候选文本，跨会话/跨刷新保持，等待用户决策
  currentDoc: null,
  docsCache: {},
  view: "chat",          // chat(对话流) / docs(资料库浏览)
  activeDocType: null,   // docs 视图当前分类：materials / reports / knowledge
};

const listeners = new Set();

export function getState() { return state; }

export function setState(patch) {
  Object.assign(state, patch);
  listeners.forEach((fn) => fn(state));
}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

/* ---- 便捷 getter ---- */
export function activeSession() {
  return state.sessions.find((s) => s.thread_id === state.activeId) || null;
}
