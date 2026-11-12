import React, { useEffect, useRef, useState } from "react";
import { api, fileUrl, fmtTime } from "./api.js";
import { go } from "./App.jsx";
import { Field, useMeta } from "./NewProject.jsx";

const rid = () => Math.random().toString(36).slice(2, 10);

function Output({ pid, o, wide }) {
  const [copied, setCopied] = useState(false);
  const copy = () => navigator.clipboard.writeText(o.caption || "").then(() => { setCopied(true); setTimeout(() => setCopied(false), 1800); });
  return (
    <div className={`out ${wide || o.width > o.height ? "wide" : ""}`}>
      <video controls preload="none" poster={fileUrl(pid, o.thumb)} src={fileUrl(pid, o.file)} />
      <div className="row">
        <span className="chip">{o.quality} · {o.width}×{o.height} · {fmtTime(o.duration)}</span>
      </div>
      <div className="row">
        <a className="btn mini" href={fileUrl(pid, o.file, true)}>Download</a>
        {o.caption && <button className="mini" onClick={copy}>{copied ? "Copied" : "Copy caption"}</button>}
      </div>
    </div>
  );
}

function JobBar({ jobs }) {
  const active = jobs.filter((j) => j.status === "running" || j.status === "queued");
  const last = jobs[0];
  if (active.length === 0) {
    if (last && last.status === "error") return <div className="error"><b>Last job failed.</b> {last.error}</div>;
    return null;
  }
  const j = active.find((x) => x.status === "running") || active[active.length - 1];
  return (
    <div className="card jobbar stack" style={{ padding: 14, gap: 9 }}>
      <div className="spread"><span className="row"><span className="spin" /> <b>{j.message || "Working"}</b></span>
        <span className="muted small">{active.length > 1 ? `${active.length - 1} more queued · ` : ""}{Math.round(j.progress * 100)}%</span></div>
      <div className="progress"><div style={{ width: `${Math.max(2, j.progress * 100)}%` }} /></div>
    </div>
  );
}

/* ------------------------------------------------------------------ Story */
function LookPanel({ project, meta, onSaved }) {
  const [p, setP] = useState(project.params);
  const [state, setState] = useState("");
  const set = (k, v) => setP((o) => ({ ...o, [k]: v }));
  const save = async () => { await api.update(project.id, { params: p }); setState("Saved"); onSaved(); setTimeout(() => setState(""), 2000); };
  return (
    <div className="card stack">
      <h3>Look and voice</h3>
      <div className="grid3">
        <Field label="Art style"><select value={p.style} onChange={(e) => set("style", e.target.value)}>{meta.styles.map((s) => <option key={s.id} value={s.id}>{s.label}</option>)}</select></Field>
        <Field label="Voice"><select value={p.voice} onChange={(e) => set("voice", e.target.value)}>{meta.voices.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}</select></Field>
        <Field label="Voice speed"><select value={p.speed || 1} onChange={(e) => set("speed", +e.target.value)}>{[0.9, 1, 1.1, 1.2].map((n) => <option key={n} value={n}>{n}×</option>)}</select></Field>
        <Field label="Caption style"><select value={p.caption_style} onChange={(e) => set("caption_style", e.target.value)}>{meta.caption_styles.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}</select></Field>
        <Field label="Background music" hint="Drop mp3 files into data/music to list them here."><select value={p.music || ""} onChange={(e) => set("music", e.target.value)}><option value="">None</option>{meta.music.map((m) => <option key={m}>{m}</option>)}</select></Field>
        <Field label="Image variation" hint="Change to get different pictures for the same prompts."><input type="number" min="1" max="999" value={p.seed || 7} onChange={(e) => set("seed", +e.target.value)} /></Field>
      </div>
      <label className="check"><input type="checkbox" checked={p.captions !== false} onChange={(e) => set("captions", e.target.checked)} />Burn in word-by-word captions</label>
      <div className="row"><button onClick={save}>Save look</button><span className="muted small">{state || "Applies to the next render."}</span></div>
    </div>
  );
}

function StoryView({ project, reload }) {
  const meta = useMeta();
  const pid = project.id;
  const [videos, setVideos] = useState(project.data.videos || []);
  const [sel, setSel] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [showLook, setShowLook] = useState(false);
  const [msg, setMsg] = useState("");
  const fileRef = useRef({});

  useEffect(() => {      // take server copy unless there are unsaved edits; always pick up new renders
    const server = project.data.videos || [];
    if (!dirty) setVideos(server);
    else setVideos((loc) => loc.map((v) => { const s = server.find((x) => x.id === v.id); return s ? { ...v, outputs: s.outputs } : v; }));
  }, [project.updated]);   // eslint-disable-line

  const v = videos[sel];
  const edit = (fn) => { setVideos((vs) => vs.map((x, i) => (i === sel ? fn({ ...x }) : x))); setDirty(true); };
  const editScene = (i, k, val) => edit((x) => { x.scenes = x.scenes.map((s, j) => (j === i ? { ...s, [k]: val } : s)); return x; });
  const save = async () => { await api.update(pid, { data: { videos } }); setDirty(false); setMsg("Saved"); setTimeout(() => setMsg(""), 1800); reload(); };
  const render = async (quality) => {
    if (dirty) await api.update(pid, { data: { videos } });
    setDirty(false);
    await api.job(pid, "story_render", { video_id: v.id, quality });
    reload();
  };
  const renderAll = async (quality) => {
    if (dirty) await api.update(pid, { data: { videos } });
    setDirty(false);
    for (const x of videos) if (x.scenes?.length) await api.job(pid, "story_render", { video_id: x.id, quality });
    reload();
  };
  const upload = async (scene, file) => {
    if (!file) return;
    if (dirty) await api.update(pid, { data: { videos } });
    setDirty(false);
    await api.sceneImage(pid, v.id, scene.id, file);
    reload();
  };

  if (!videos.length) return <div className="card muted">{project.status === "planning" ? "Scripts will appear here as they are written." : "No scripts yet."}</div>;
  if (!v) { setSel(0); return null; }
  const words = v.scenes.reduce((n, s) => n + s.narration.split(/\s+/).filter(Boolean).length, 0);
  const figs = Object.fromEntries((project.data.figures || []).map((f) => [f.id, f]));
  return (
    <div className="split">
      <div className="side">
        {videos.map((x, i) => (
          <button key={x.id} className={i === sel ? "on" : ""} onClick={() => setSel(i)}>
            <span><span className="sub">{x.format === "long" ? "Long 16:9" : x.part ? `Part ${x.part} of ${x.parts}` : "Short 9:16"}{x.outputs?.length ? " · rendered" : ""}</span>{x.title}</span>
          </button>
        ))}
        <button className="ghost" onClick={() => setShowLook(!showLook)}>🎨 Look and voice</button>
        <button className="ghost" onClick={() => renderAll("final")}>⚡ Render all (final)</button>
      </div>
      <div className="stack">
        {showLook && meta && <LookPanel project={project} meta={meta} onSaved={reload} />}
        {v.error ? <div className="error">This script could not be written: {v.error}</div> : (
          <>
            <div className="card stack">
              <div className="spread">
                <span className="row"><span className="chip">{v.scenes.length} scenes</span><span className="chip">{words} words · about {fmtTime(words / 2.55)}</span></span>
                <span className="row">
                  <span className="muted small">{msg || (dirty ? "Unsaved edits" : "")}</span>
                  <button onClick={save} disabled={!dirty}>Save</button>
                  <button onClick={() => render("draft")}>Draft render</button>
                  <button className="primary" onClick={() => render("final")}>Render final</button>
                </span>
              </div>
              <div className="grid2">
                <Field label="Title"><input value={v.title} onChange={(e) => edit((x) => ({ ...x, title: e.target.value }))} /></Field>
                <Field label="End-screen call to action"><input value={v.cta || ""} onChange={(e) => edit((x) => ({ ...x, cta: e.target.value }))} placeholder="Comment STAY or GO" /></Field>
              </div>
              <Field label="Post caption"><textarea rows={2} value={v.description || ""} onChange={(e) => edit((x) => ({ ...x, description: e.target.value }))} /></Field>
              <Field label="Hashtags"><input value={(v.hashtags || []).join(" ")} onChange={(e) => edit((x) => ({ ...x, hashtags: e.target.value.split(/[\s,#]+/).filter(Boolean) }))} /></Field>
            </div>

            {v.outputs?.length > 0 && <div className="card stack"><h3>Renders</h3><div className="outs">{v.outputs.slice(0, 4).map((o) => <Output key={o.file} pid={pid} o={o} />)}</div></div>}

            <div className="stack" style={{ gap: 10 }}>
              {v.scenes.map((s, i) => (
                <div className="scene" key={s.id}>
                  <div className="n">{i + 1}</div>
                  <Field label={i === 0 ? "Narration — the hook" : "Narration"}><textarea rows={3} value={s.narration} onChange={(e) => editScene(i, "narration", e.target.value)} /></Field>
                  <Field label="Picture prompt"><textarea rows={3} value={s.prompt} onChange={(e) => editScene(i, "prompt", e.target.value)} /></Field>
                  <div className="tools">
                    <input style={{ width: 170 }} placeholder="On-screen label" value={s.on_screen || ""} onChange={(e) => editScene(i, "on_screen", e.target.value)} />
                    {s.page && <span className={`chip ${s.grounded === false ? "err" : "ok"}`}><i />p.{s.page}{s.grounded === false ? " · check this fact" : ""}</span>}
                    {s.figure && figs[s.figure] && <span className="chip ok"><i />PDF picture {s.figure} <a onClick={() => editScene(i, "figure", null)} style={{ cursor: "pointer" }}>remove</a></span>}
                    {s.image && <span className="chip ok"><i />Your image <a onClick={() => editScene(i, "image", null)} style={{ cursor: "pointer" }}>remove</a></span>}
                    <input type="file" accept="image/*" hidden ref={(el) => { fileRef.current[s.id] = el; }} onChange={(e) => upload(s, e.target.files[0])} />
                    <button className="mini" onClick={() => fileRef.current[s.id]?.click()}>Use my own image</button>
                    <span style={{ flex: 1 }} />
                    <button className="mini" onClick={() => edit((x) => { const a = [...x.scenes]; a.splice(i + 1, 0, { id: rid(), narration: "", prompt: "", on_screen: "", figure: null, page: null }); x.scenes = a; return x; })}>+ Scene below</button>
                    {v.scenes.length > 1 && <button className="mini danger" onClick={() => edit((x) => ({ ...x, scenes: x.scenes.filter((_, j) => j !== i) }))}>Delete</button>}
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ Clips */
function ClipsView({ project, reload }) {
  const meta = useMeta();
  const pid = project.id;
  const d = project.data;
  const [clips, setClips] = useState(d.clips || []);
  const [dirty, setDirty] = useState(false);
  const [opt, setOpt] = useState({ aspect: "9:16", caption_style: "pop", quality: "final", best_of: false, captions: true, show_title: true });
  const vref = useRef(null);

  useEffect(() => {
    const server = d.clips || [];
    if (!dirty) setClips(server);
    else setClips((loc) => loc.map((c) => { const s = server.find((x) => x.id === c.id); return s ? { ...c, outputs: s.outputs } : c; }));
  }, [project.updated]);   // eslint-disable-line

  const edit = (id, k, val) => { setClips((cs) => cs.map((c) => (c.id === id ? { ...c, [k]: val } : c))); setDirty(true); };
  const preview = (c) => { const el = vref.current; if (!el) return; el.currentTime = c.start; el.play(); const stop = () => { if (el.currentTime >= c.end) { el.pause(); el.removeEventListener("timeupdate", stop); } }; el.addEventListener("timeupdate", stop); el.scrollIntoView({ behavior: "smooth", block: "center" }); };
  const render = async () => {
    if (dirty) await api.update(pid, { data: { clips } });
    setDirty(false);
    await api.job(pid, "clips_render", { ...opt, clip_ids: clips.filter((c) => c.selected).map((c) => c.id) });
    reload();
  };
  if (!clips.length) return <div className="card muted">{project.status === "analyzing" ? "Moments will appear here once the video is transcribed and ranked." : "No clips found yet."}</div>;
  const nSel = clips.filter((c) => c.selected).length;
  return (
    <div className="stack">
      <div className="grid2">
        <div className="card stack">
          <h3>Source</h3>
          {d.source && <video ref={vref} controls preload="metadata" src={fileUrl(pid, d.source.file)} style={{ width: "100%", borderRadius: 12, background: "#000" }} />}
          {d.source && <div className="row"><span className="chip">{fmtTime(d.source.duration)} · {d.source.width}×{d.source.height}</span><span className="chip">Transcript: {d.stt}{d.language ? ` · ${d.language}` : ""}</span><span className="chip">Picked by: {d.picked_by === "llm" ? "AI editor" : "audio heuristics"}</span></div>}
        </div>
        <div className="card stack">
          <h3>Render</h3>
          <div className="grid2">
            <Field label="Shape"><select value={opt.aspect} onChange={(e) => setOpt({ ...opt, aspect: e.target.value })}>{(meta?.aspects || ["9:16"]).map((a) => <option key={a}>{a}</option>)}</select></Field>
            <Field label="Caption style"><select value={opt.caption_style} onChange={(e) => setOpt({ ...opt, caption_style: e.target.value })}>{(meta?.caption_styles || []).map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}</select></Field>
            <Field label="Quality"><select value={opt.quality} onChange={(e) => setOpt({ ...opt, quality: e.target.value })}><option value="final">Final (1080p)</option><option value="draft">Draft (fast, half size)</option></select></Field>
          </div>
          <label className="check"><input type="checkbox" checked={opt.captions} onChange={(e) => setOpt({ ...opt, captions: e.target.checked })} />Animated captions</label>
          <label className="check"><input type="checkbox" checked={opt.show_title} onChange={(e) => setOpt({ ...opt, show_title: e.target.checked })} />Show the hook title for the first 3 seconds</label>
          <label className="check"><input type="checkbox" checked={opt.best_of} onChange={(e) => setOpt({ ...opt, best_of: e.target.checked })} />Also build a 16:9 best-of cut with title cards</label>
          <div className="row"><button className="primary" disabled={!nSel} onClick={render}>Render {nSel} clip{nSel === 1 ? "" : "s"}</button>{dirty && <span className="muted small">Edits are saved when you render.</span>}</div>
        </div>
      </div>
      {d.best_of?.length > 0 && <div className="card stack"><h3>Best-of cut</h3><div className="outs">{d.best_of.slice(0, 2).map((o) => <Output key={o.file} pid={pid} o={o} wide />)}</div></div>}
      <div className="grid2">
        {clips.map((c) => (
          <div key={c.id} className={`clip ${c.selected ? "on" : ""}`}>
            <div className="spread">
              <label className="check"><input type="checkbox" checked={!!c.selected} onChange={(e) => edit(c.id, "selected", e.target.checked)} /><span className="chip score">{c.score}</span><span className="muted small">{fmtTime(c.start)} – {fmtTime(c.end)} · {Math.round(c.end - c.start)}s</span></label>
              <button className="mini" onClick={() => preview(c)}>▶ Preview</button>
            </div>
            <input value={c.title} onChange={(e) => edit(c.id, "title", e.target.value)} placeholder="Hook title shown on screen" />
            <div className="row">
              <Field label="Start (s)"><input type="number" step="0.1" style={{ width: 110 }} value={c.start} onChange={(e) => edit(c.id, "start", Math.max(0, +e.target.value))} /></Field>
              <Field label="End (s)"><input type="number" step="0.1" style={{ width: 110 }} value={c.end} onChange={(e) => edit(c.id, "end", +e.target.value)} /></Field>
            </div>
            {c.reason && <p className="small"><b>Why:</b> {c.reason}</p>}
            <p className="excerpt">{c.text}</p>
            {c.outputs?.length > 0 && <div className="outs">{c.outputs.slice(0, 2).map((o) => <Output key={o.file} pid={pid} o={o} />)}</div>}
          </div>
        ))}
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ shell */
export default function Project({ id }) {
  const [p, setP] = useState(null);
  const [err, setErr] = useState("");
  const load = () => api.project(id).then(setP).catch((e) => setErr(e.message));
  useEffect(() => {
    load();
    const t = setInterval(load, 2500);
    return () => clearInterval(t);
  }, [id]);   // eslint-disable-line
  if (err && !p) return <div className="wrap"><div className="error">{err}</div></div>;
  if (!p) return <div className="wrap"><p className="muted">Loading…</p></div>;
  const del = async () => { if (window.confirm("Delete this project and all its files?")) { await api.remove(id); go("/"); } };
  const redo = async () => { await api.job(id, p.kind === "story" ? "story_plan" : "clips_analyze", {}); load(); };
  const idle = !p.jobs.some((j) => j.status === "running" || j.status === "queued");
  return (
    <div className="wrap stack" style={{ paddingTop: 18 }}>
      <div className="spread">
        <div><a href="#/" className="muted small">← Studio</a><h1>{p.title}</h1></div>
        <div className="row">
          {idle && <button className="ghost" onClick={redo}>{p.kind === "story" ? "Rewrite scripts" : "Re-pick moments"}</button>}
          <button className="danger" onClick={del}>Delete</button>
        </div>
      </div>
      <JobBar jobs={p.jobs} />
      {p.kind === "story" ? <StoryView project={p} reload={load} /> : <ClipsView project={p} reload={load} />}
    </div>
  );
}
