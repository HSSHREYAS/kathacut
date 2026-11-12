// Same-origin by default; set VITE_API_BASE when the UI is hosted apart from the worker.
export const BASE = (import.meta.env.VITE_API_BASE || "").replace(/\/$/, "");

async function req(path, opts = {}) {
  const r = await fetch(BASE + path, { credentials: "include", ...opts });
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch { /* not json */ }
    const e = new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
    e.status = r.status;
    throw e;
  }
  return r.json();
}
const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

export const api = {
  health: () => req("/api/health"),
  login: (password) => req("/api/login", json("POST", { password })),
  meta: () => req("/api/meta"),
  settings: () => req("/api/settings"),
  saveSettings: (values) => req("/api/settings", json("POST", { values })),
  usage: () => req("/api/usage"),
  projects: () => req("/api/projects"),
  project: (id) => req(`/api/projects/${id}`),
  create: (kind, title, params, file) => {
    const fd = new FormData();
    fd.append("kind", kind);
    fd.append("title", title || "");
    fd.append("params", JSON.stringify(params));
    if (file) fd.append("file", file);
    return req("/api/projects", { method: "POST", body: fd });
  },
  update: (id, body) => req(`/api/projects/${id}`, json("PUT", body)),
  remove: (id) => req(`/api/projects/${id}`, { method: "DELETE" }),
  job: (id, type, params) => req(`/api/projects/${id}/jobs`, json("POST", { type, params })),
  sceneImage: (id, videoId, sceneId, file) => {
    const fd = new FormData();
    fd.append("video_id", videoId);
    fd.append("scene_id", sceneId);
    fd.append("file", file);
    return req(`/api/projects/${id}/scene-image`, { method: "POST", body: fd });
  },
};

export const fileUrl = (pid, path, download) => `${BASE}/files/${pid}/${path}${download ? "?download=1" : ""}`;
export const fmtTime = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
