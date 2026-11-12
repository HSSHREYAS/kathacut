import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import { go } from "./App.jsx";

export function useMeta() {
  const [meta, setMeta] = useState(null);
  useEffect(() => { api.meta().then(setMeta).catch(() => {}); }, []);
  return meta;
}

export function Field({ label, children, hint }) {
  return <label className="f">{label}{children}{hint && <span className="small">{hint}</span>}</label>;
}

export function Tabs({ value, onChange, options }) {
  return (
    <div className="tabs">
      {options.map(([v, l]) => <button type="button" key={v} className={value === v ? "on" : ""} onClick={() => onChange(v)}>{l}</button>)}
    </div>
  );
}

const LANGS = ["English", "Hindi", "Kannada", "Telugu", "Tamil", "Hinglish (Hindi in Latin letters)"];

export function NewStory() {
  const meta = useMeta();
  const [source, setSource] = useState("idea");
  const [text, setText] = useState("");
  const [file, setFile] = useState(null);
  const [title, setTitle] = useState("");
  const [p, setP] = useState({
    genre: "horror", style: "dark_ink", voice: "kokoro:am_fenrir", caption_style: "pop", language: "English",
    n_shorts: 3, short_seconds: 45, long: false, long_minutes: 4, series: false, show_sources: false, notes: "", speed: 1,
  });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const set = (k, v) => setP((o) => ({ ...o, [k]: v }));
  if (!meta) return <div className="wrap"><p className="muted">Loading…</p></div>;

  const submit = async () => {
    setErr(""); setBusy(true);
    try {
      const params = { ...p, source, text: source === "pdf" ? "" : text, show_sources: source === "pdf" && p.show_sources };
      const r = await api.create("story", title, params, source === "pdf" ? file : null);
      go(`/p/${r.id}`);
    } catch (e) { setErr(e.message); setBusy(false); }
  };
  const ready = source === "pdf" ? !!file : text.trim().length > 8;
  return (
    <div className="wrap stack" style={{ paddingTop: 18 }}>
      <div><a href="#/" className="muted small">← Studio</a><h1>New story</h1></div>
      <div className="card stack">
        <Tabs value={source} onChange={setSource} options={[["idea", "Idea"], ["text", "Paste text"], ["pdf", "PDF"]]} />
        {source === "idea" && <Field label="What is the story?" hint="One line is enough. Name the place, the person or the legend if you have one.">
          <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="A night-shift lighthouse keeper in coastal Karnataka hears two honks from a boat that sank years ago…" /></Field>}
        {source === "text" && <Field label="Source text" hint="Articles, notes, a chapter. Scripts will stick to what this text says.">
          <textarea rows={9} value={text} onChange={(e) => setText(e.target.value)} placeholder="Paste the text here" /></Field>}
        {source === "pdf" && <Field label="PDF file" hint="Works best with real text PDFs. Pictures inside the PDF are reused as visuals. Use documents you have the right to adapt (public-domain or your own).">
          <input type="file" accept="application/pdf" onChange={(e) => setFile(e.target.files[0])} /></Field>}
        <Field label="Project name (optional)"><input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Lighthouse series" /></Field>
      </div>

      <div className="card stack">
        <h3>Tone</h3>
        <div className="pick">
          {meta.genres.map((g) => <button type="button" key={g.id} className={p.genre === g.id ? "on" : ""} onClick={() => set("genre", g.id)}>{g.label}</button>)}
        </div>
        <h3 style={{ marginTop: 6 }}>Art style</h3>
        <div className="pick">
          {meta.styles.map((s) => <button type="button" key={s.id} className={p.style === s.id ? "on" : ""} onClick={() => set("style", s.id)}><span className="dot" style={{ background: s.highlight }} />{s.label}</button>)}
        </div>
      </div>

      <div className="card stack">
        <h3>Output</h3>
        <div className="grid3">
          <Field label="Number of shorts"><select value={p.n_shorts} onChange={(e) => set("n_shorts", +e.target.value)}>{[1, 2, 3, 4, 5, 6, 7].map((n) => <option key={n}>{n}</option>)}</select></Field>
          <Field label="Length of each short"><select value={p.short_seconds} onChange={(e) => set("short_seconds", +e.target.value)}>{[30, 45, 60, 75].map((n) => <option key={n} value={n}>{n} seconds</option>)}</select></Field>
          <Field label="Narration language"><select value={p.language} onChange={(e) => set("language", e.target.value)}>{LANGS.map((l) => <option key={l}>{l}</option>)}</select></Field>
          <Field label="Voice"><select value={p.voice} onChange={(e) => set("voice", e.target.value)}>{meta.voices.map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}</select></Field>
          <Field label="Caption style"><select value={p.caption_style} onChange={(e) => set("caption_style", e.target.value)}>{meta.caption_styles.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}</select></Field>
          <Field label="Voice speed"><select value={p.speed} onChange={(e) => set("speed", +e.target.value)}>{[0.9, 1, 1.1, 1.2].map((n) => <option key={n} value={n}>{n}×</option>)}</select></Field>
        </div>
        <label className="check"><input type="checkbox" checked={p.series} onChange={(e) => set("series", e.target.checked)} />Make it a series — one story in parts, each ending on a cliffhanger and a "comment A or B" choice</label>
        <label className="check"><input type="checkbox" checked={p.long} onChange={(e) => set("long", e.target.checked)} />Also write one long 16:9 video
          {p.long && <select style={{ width: 130 }} value={p.long_minutes} onChange={(e) => set("long_minutes", +e.target.value)} onClick={(e) => e.preventDefault()}>{[2, 3, 4, 6, 8, 10].map((n) => <option key={n} value={n}>{n} minutes</option>)}</select>}</label>
        {source === "pdf" && <label className="check"><input type="checkbox" checked={p.show_sources} onChange={(e) => set("show_sources", e.target.checked)} />Grounded mode — show the source page number on screen for every claim</label>}
        {p.language !== "English" && !p.language.startsWith("Hinglish") && <div className="note">Pick a matching {p.language} voice above. On-screen captions for Indian scripts are not supported yet on Windows, so captions are best switched off or written in Hinglish.</div>}
        <Field label="Notes for the writer (optional)"><textarea rows={2} value={p.notes} onChange={(e) => set("notes", e.target.value)} placeholder="Anything to include or avoid, the audience, a channel catchphrase…" /></Field>
      </div>
      {err && <div className="error">{err}</div>}
      <div className="row"><button className="primary" disabled={!ready || busy} onClick={submit}>{busy ? "Starting…" : "Write the scripts"}</button><span className="muted small">You review and edit every script before anything is rendered.</span></div>
    </div>
  );
}

export function NewClips() {
  const [source, setSource] = useState("upload");
  const [file, setFile] = useState(null);
  const [url, setUrl] = useState("");
  const [title, setTitle] = useState("");
  const [p, setP] = useState({ n_clips: 5, min_seconds: 20, max_seconds: 60, language: "", notes: "" });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const set = (k, v) => setP((o) => ({ ...o, [k]: v }));
  const submit = async () => {
    setErr(""); setBusy(true);
    try {
      const r = await api.create("clips", title, { ...p, source, url }, source === "upload" ? file : null);
      go(`/p/${r.id}`);
    } catch (e) { setErr(e.message); setBusy(false); }
  };
  const ready = source === "upload" ? !!file : /^https?:\/\//.test(url);
  return (
    <div className="wrap stack" style={{ paddingTop: 18 }}>
      <div><a href="#/" className="muted small">← Studio</a><h1>New clip project</h1></div>
      <div className="card stack">
        <Tabs value={source} onChange={setSource} options={[["upload", "Upload a video"], ["youtube", "YouTube link"]]} />
        {source === "upload"
          ? <Field label="Video file" hint="Podcasts, interviews, lectures, streams — anything with speech."><input type="file" accept="video/*" onChange={(e) => setFile(e.target.files[0])} /></Field>
          : <Field label="Video link" hint="Works only when KathaCut runs on a home connection — YouTube blocks cloud servers. If it fails, download the video and upload the file."><input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.youtube.com/watch?v=…" /></Field>}
        <div className="note">Clip your own videos or ones you have permission to use. Re-uploading someone else's video without adding anything is not fair use and is not monetisable.</div>
        <Field label="Project name (optional)"><input value={title} onChange={(e) => setTitle(e.target.value)} /></Field>
      </div>
      <div className="card stack">
        <h3>What to look for</h3>
        <div className="grid3">
          <Field label="How many clips"><select value={p.n_clips} onChange={(e) => set("n_clips", +e.target.value)}>{[3, 5, 8, 10, 15].map((n) => <option key={n}>{n}</option>)}</select></Field>
          <Field label="Shortest clip"><select value={p.min_seconds} onChange={(e) => set("min_seconds", +e.target.value)}>{[10, 15, 20, 30].map((n) => <option key={n} value={n}>{n} seconds</option>)}</select></Field>
          <Field label="Longest clip"><select value={p.max_seconds} onChange={(e) => set("max_seconds", +e.target.value)}>{[30, 45, 60, 90, 120].map((n) => <option key={n} value={n}>{n} seconds</option>)}</select></Field>
          <Field label="Spoken language"><select value={p.language} onChange={(e) => set("language", e.target.value)}>
            <option value="">Detect automatically</option><option value="en">English</option><option value="hi">Hindi</option><option value="kn">Kannada</option><option value="te">Telugu</option><option value="ta">Tamil</option></select></Field>
        </div>
        <Field label="Notes for the editor (optional)"><textarea rows={2} value={p.notes} onChange={(e) => set("notes", e.target.value)} placeholder="e.g. prefer funny moments, skip the sponsor read" /></Field>
      </div>
      {err && <div className="error">{err}</div>}
      <div className="row"><button className="primary" disabled={!ready || busy} onClick={submit}>{busy ? "Uploading…" : "Find the best moments"}</button></div>
    </div>
  );
}
