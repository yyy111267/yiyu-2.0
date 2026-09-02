/* ============================================================
   持久化层 (window.Yiyu.store) — localStorage 封装
   ============================================================ */
(function () {
  const Yiyu = (window.Yiyu = window.Yiyu || {});
  const KEY = "yiyu_proto_v1";

  const defaults = {
    theme: "light",
    sidebarCollapsed: false,
    cognition: null, // 首次从 seed 初始化
  };

  function load() {
    try {
      const raw = localStorage.getItem(KEY);
      if (raw) return Object.assign({}, defaults, JSON.parse(raw));
    } catch (e) {}
    return Object.assign({}, defaults);
  }
  let state = load();
  if (!state.cognition) state.cognition = JSON.parse(JSON.stringify(Yiyu.data.cognitionSeed));

  function persist() {
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {}
  }

  const store = {
    get: (k) => state[k],
    set: (k, v) => { state[k] = v; persist(); },
    getCognition: () => state.cognition,
    saveCognition: (list) => { state.cognition = list; persist(); },
    addCog: (item) => { state.cognition.unshift(item); persist(); },
    updateCog: (id, patch) => {
      const i = state.cognition.findIndex((c) => c.id === id);
      if (i >= 0) { state.cognition[i] = Object.assign({}, state.cognition[i], patch); persist(); }
    },
    removeCog: (id) => { state.cognition = state.cognition.filter((c) => c.id !== id); persist(); },
    resetCog: () => { state.cognition = JSON.parse(JSON.stringify(Yiyu.data.cognitionSeed)); persist(); }
  };

  Yiyu.store = store;
})();
