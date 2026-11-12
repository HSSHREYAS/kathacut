import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import { NewStory, NewClips } from "./NewProject.jsx";
import Project from "./Project.jsx";

const useHash = () => {
  const [h, setH] = useState(window.location.hash.slice(1) || "/");
  useEffect(() => {
    const on = () => setH(window.location.hash.slice(1) || "/");
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  return h;
};
export const go = (to) => { window.location.hash = to; };

function Chip({ ok, label, opt }) {
  return <span className={`chip ${ok ? "ok" : "off"} ${opt ? "opt" : ""}`}><i />{label}</span>;
}

function Login({ onDone }) {
  const [pw, setPw] = useState("");
  const [err, setErr] = useState("");
  const submit = async (e) => {
    e.preventDefault();
    try { await api.login(pw); onDone(); } catch (x) { setErr(x.message); }
  };
  return (
    <div className="center">
      <form className="card stack" style={{ width: 340 }} onSubmit={submit}>
        <h2>KathaCut</h2>
        <p className="muted">This studio is password protected.</p>
        <input type="password" placeholder="Password" value={pw} onChange={(e) => setPw(e.target.value)} autoFocus />
        {err && <div className="error">{err}</div>}
        <button className="primary">Enter</button>
      </form>
    </div>
  );
}

function Dashboard() {
  const [list, setList] = useState(null);
  useEffect(() => {
    let live = true;
    const load = () => api.projects().then((r) => live && setList(r)).catch(() => {});
    load();
    const t = setInterval(load, 4000);
    return () => { live = false; clearInterval(t); };
  }, []);
  const busy = (s) => ["planning", "analyzing", "rendering", "new"].includes(s);
  return (
    <div className="wrap">
      <div className="hero">
        <h1>Turn stories and long videos into scroll-stopping shorts.</h1>
        <p>Everything runs on free tiers and on this computer. Write or upload, review the script or the picked moments, then render.</p>
      </div>
      <div className="grid2">
        <div className="card studio" onClick={() => go("/new/story")}>
          <span className="tag">Story Studio</span>
          <h2>Idea, text or PDF → narrated animated story</h2>
          <p className="muted">Hook-first scripts, AI illustrations in one art style, natural voice, word-by-word captions. Shorts, a series, or a long video.</p>
          <span className="go">Start a story →</span>
        </div>
        <div className="card studio" onClick={() => go("/new/clips")}>
          <span className="tag">Clip Studio</span>
          <h2>Long video → ranked vertical clips</h2>
          <p className="muted">Transcribes, finds the strongest moments, tracks the speaker's face into 9:16 and burns in animated captions. Plus a best-of cut.</p>
          <span className="go">Clip a video →</span>
        </div>
      </div>
      <div className="spread" style={{ margin: "30px 0 12px" }}>
        <h2>Projects</h2>
        {list && list.active.length > 0 && <span className="chip ok"><span className="spin" /> {list.active.length} job{list.active.length > 1 ? "s" : ""} in the queue</span>}
      </div>
      {!list ? <p className="muted">Loading…</p> : list.projects.length === 0 ? (
        <div className="card muted">Nothing here yet. Start with a story idea — one sentence is enough.</div>
      ) : (
        <div className="plist">
          {list.projects.map((p) => (
            <div key={p.id} className="pitem" onClick={() => go(`/p/${p.id}`)}>
              <div className="kind">{p.kind === "story" ? "📖" : "✂️"}</div>
              <div className="grow">
                <div className="t">{p.title}</div>
                <div className="muted small">{p.kind === "story" ? "Story Studio" : "Clip Studio"} · {new Date(p.created * 1000).toLocaleString()}</div>
              </div>
              <span className={`badge ${busy(p.status) ? "busy" : p.status}`}>{p.status}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function Settings() {
  const [fields, setFields] = useState(null);
  const [vals, setVals] = useState({});
  const [usage, setUsage] = useState([]);
  const [saved, setSaved] = useState("");
  const load = () => { api.settings().then((r) => setFields(r.fields)); api.usage().then((r) => setUsage(r.today)); };
  useEffect(load, []);
  const save = async () => {
    const r = await api.saveSettings(vals);
    setFields(r.fields); setVals({}); setSaved("Saved"); setTimeout(() => setSaved(""), 2500);
  };
  if (!fields) return <div className="wrap"><p className="muted">Loading…</p></div>;
  const keys = fields.filter((f) => f.url);
  const adv = fields.filter((f) => !f.url);
  const Row = (f) => (
    <div className="keyrow" key={f.name}>
      <div>
        <div style={{ fontWeight: 600 }}>{f.label}</div>
        <div className="muted small">{f.name}{f.set ? " · set" : ""}</div>
      </div>
      <input type={f.secret ? "password" : "text"} placeholder={f.set ? f.value : (f.secret ? "Paste key" : "default")}
        value={vals[f.name] ?? ""} onChange={(e) => setVals({ ...vals, [f.name]: e.target.value })} autoComplete="off" />
      {f.url ? <a className="btn mini" href={f.url} target="_blank" rel="noreferrer">Get it free ↗</a> : <span />}
    </div>
  );
  return (
    <div className="wrap stack" style={{ paddingTop: 20 }}>
      <h1>Settings</h1>
      <div className="note">Keys are stored only on the machine running KathaCut (in <code>data/settings.json</code>). All of these have a free tier and none needs a card. The app works with no keys at all — local voice, local Whisper, Ollama and Pollinations — but Gemini and Groq make it much faster and smarter.</div>
      <div className="card stack">
        <h3>Free API keys</h3>
        <div className="keys">{keys.map(Row)}</div>
      </div>
      <div className="card stack">
        <h3>Advanced</h3>
        <div className="keys">{adv.map(Row)}</div>
      </div>
      <div className="row"><button className="primary" onClick={save} disabled={!Object.keys(vals).length}>Save settings</button><span className="muted">{saved}</span></div>
      <div className="card stack">
        <h3>Free quota used today</h3>
        {usage.length === 0 ? <p className="muted">No calls yet today.</p> : (
          <table className="usage"><thead><tr><th>Provider</th><th>What</th><th>Amount</th></tr></thead>
            <tbody>{usage.map((u, i) => <tr key={i}><td>{u.provider}</td><td>{u.unit}</td><td>{u.unit === "audio_seconds" ? `${(u.total / 60).toFixed(1)} min` : Math.round(u.total)}</td></tr>)}</tbody>
          </table>
        )}
      </div>
    </div>
  );
}

export default function App() {
  const hash = useHash();
  const [health, setHealth] = useState(null);
  const [down, setDown] = useState(false);
  const check = () => api.health().then((h) => { setHealth(h); setDown(false); }).catch(() => setDown(true));
  useEffect(() => { check(); const t = setInterval(check, 8000); return () => clearInterval(t); }, []);

  if (down && !health) return <div className="center"><div className="card stack" style={{ maxWidth: 420 }}><h2>Worker offline</h2><p className="muted">The KathaCut worker isn't reachable. Start it on your computer (run.ps1) and this page will reconnect on its own.</p></div></div>;
  if (!health) return <div className="center"><span className="spin" /></div>;
  if (health.auth_required) return <Login onDone={check} />;

  let page;
  if (hash === "/new/story") page = <NewStory />;
  else if (hash === "/new/clips") page = <NewClips />;
  else if (hash.startsWith("/p/")) page = <Project id={hash.slice(3)} key={hash} />;
  else if (hash === "/settings") page = <Settings />;
  else page = <Dashboard />;
  const llm = health.llm || {};
  const anyLlm = Object.values(llm).some(Boolean);
  return (
    <>
      <header className="top">
        <a className="logo" href="#/"><span className="mark">K</span><span>Katha<b>Cut</b></span></a>
        <nav>
          <a href="#/" className={hash === "/" || hash.startsWith("/p/") || hash.startsWith("/new") ? "on" : ""}>Studio</a>
          <a href="#/settings" className={hash === "/settings" ? "on" : ""}>Settings</a>
        </nav>
        <div className="status">
          <span className={`chip ${down ? "err" : "ok"}`}><i />{down ? "Worker offline" : "Worker online"}</span>
          <Chip ok={anyLlm} label={llm.gemini ? "Gemini" : llm.groq ? "Groq" : llm.ollama ? "Ollama (local)" : "No LLM"} />
          <Chip opt ok={health.stt?.groq} label={health.stt?.groq ? "Groq Whisper" : "Local Whisper"} />
          <Chip opt ok={health.encoder === "h264_nvenc"} label={health.encoder === "h264_nvenc" ? "GPU encode" : "CPU encode"} />
        </div>
      </header>
      {page}
    </>
  );
}
