"""The SecLLM management console — a single self-contained HTML page (no external assets)."""

CONSOLE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>SecLLM — Model Console</title>
<style>
  /* "Field console" theme (SecRouter suite design language) — warm manila paper, olive drab,
     oxide red. System fonts only (air-gap posture). */
  :root {
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    --sans: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    /* light: warm manila "field console" */
    --bg:#e7e3d8; --panel:#f3f0e8; --panel2:#fbfaf4; --fg:#211f18; --muted:#6c6552;
    --accent:#4f6a2e; --accent-ink:#f6f3ea; --accent-soft:rgba(79,106,46,.18);
    --ok:#2f5a22; --warn:#8a5a12; --bad:#8a2b1d;
    --border:#cdc6b2; --rule:#dad4c2; --shadow:2px 2px 0 rgba(33,31,24,.06);
    --pill-bg:#e2ddcd; --pill-ok-bg:#e3ebd7; --pill-ok-bd:#b9c9a8;
    --pill-bad-bg:#f0ddd7; --pill-bad-bd:#d8b3aa; --pill-warn-bg:#efe6cf; --pill-warn-bd:#d8c69a;
    --code-bg:#e2ddcd;
  }
  /* dark: warm "night ops" — same identity, charcoal + brighter olive/terracotta */
  :root[data-theme="dark"] {
    --bg:#171511; --panel:#201e17; --panel2:#29271e; --fg:#e8e3d3; --muted:#9a9077;
    --accent:#94ad50; --accent-ink:#16140e; --accent-soft:rgba(148,173,80,.26);
    --ok:#86b257; --warn:#cb9c3e; --bad:#d4634c;
    --border:#3a3730; --rule:#272520; --shadow:2px 2px 0 rgba(0,0,0,.30);
    --pill-bg:#2b2920; --pill-ok-bg:#26331c; --pill-ok-bd:#3f5230;
    --pill-bad-bg:#37201a; --pill-bad-bd:#5c2f25; --pill-warn-bg:#332a17; --pill-warn-bd:#544321;
    --code-bg:#2b2920;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg:#171511; --panel:#201e17; --panel2:#29271e; --fg:#e8e3d3; --muted:#9a9077;
      --accent:#94ad50; --accent-ink:#16140e; --accent-soft:rgba(148,173,80,.26);
      --ok:#86b257; --warn:#cb9c3e; --bad:#d4634c;
      --border:#3a3730; --rule:#272520; --shadow:2px 2px 0 rgba(0,0,0,.30);
      --pill-bg:#2b2920; --pill-ok-bg:#26331c; --pill-ok-bd:#3f5230;
      --pill-bad-bg:#37201a; --pill-bad-bd:#5c2f25; --pill-warn-bg:#332a17; --pill-warn-bd:#544321;
      --code-bg:#2b2920;
    }
  }
  * { box-sizing:border-box; }
  body { margin:0; font:14px/1.55 var(--sans); background:var(--bg); color:var(--fg);
         background-image:linear-gradient(var(--rule) 1px, transparent 1px); background-size:100% 28px; background-attachment:fixed; }
  header { display:flex; align-items:center; gap:14px; padding:14px 22px; background:var(--panel);
           border-bottom:1px solid var(--border); border-top:3px solid var(--accent); }
  .brand { display:flex; align-items:center; gap:10px; }
  .brand .logo-light, .brand .logo-dark { height:26px; width:auto; display:block; }
  .brand .logo-dark { display:none; }
  :root[data-theme="dark"] .brand .logo-light { display:none; }
  :root[data-theme="dark"] .brand .logo-dark { display:block; }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) .brand .logo-light { display:none; }
    :root:not([data-theme="light"]) .brand .logo-dark { display:block; }
  }
  header h1 { font-size:15px; margin:0; font-weight:700; text-transform:uppercase; letter-spacing:.14em; }
  header h1 .sec { color:var(--accent); }
  header .tag { color:var(--muted); font:11px var(--mono); text-transform:uppercase; letter-spacing:.08em; }
  .pill { margin-left:auto; display:inline-block; padding:3px 10px; border-radius:2px; font:11px var(--mono);
          text-transform:uppercase; letter-spacing:.06em; background:var(--pill-bg); color:var(--muted); border:1px solid var(--border); }
  .pill.ok { color:var(--ok); border-color:var(--pill-ok-bd); background:var(--pill-ok-bg); }
  .pill.bad { color:var(--bad); border-color:var(--pill-bad-bd); background:var(--pill-bad-bg); }
  .theme-toggle { padding:5px 11px; }
  main { max-width:960px; margin:0 auto; padding:22px; display:grid; gap:16px; }
  .card { background:var(--panel); border:1px solid var(--border); border-radius:2px; padding:16px 18px;
          box-shadow:var(--shadow); }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  input[type=password]{ background:var(--panel2); border:1px solid var(--border); color:var(--fg); padding:8px 10px; border-radius:2px; font-family:var(--mono); min-width:280px; }
  input.ctx { background:var(--panel2); border:1px solid var(--border); color:var(--fg); padding:6px 8px; border-radius:2px; font-family:var(--mono); width:110px; font-size:12.5px; }
  input[type=password]:focus, input.ctx:focus { outline:none; border-color:var(--accent); box-shadow:0 0 0 2px var(--accent-soft); }
  button { background:var(--accent); color:var(--accent-ink); border:1px solid var(--accent); padding:7px 13px; border-radius:2px; font-family:var(--mono); font-weight:600; cursor:pointer;
           text-transform:uppercase; letter-spacing:.06em; font-size:12px; }
  button:hover { filter:brightness(1.08); }
  button.ghost { background:var(--panel2); color:var(--fg); border-color:var(--border); }
  button.danger { background:transparent; color:var(--bad); border:1px solid var(--pill-bad-bd); }
  button:disabled { opacity:.4; cursor:not-allowed; }
  .model { display:grid; grid-template-columns:1fr auto; gap:8px 14px; padding:14px 0; border-bottom:1px solid var(--rule); }
  .model:last-child { border-bottom:0; }
  .model h3 { margin:0; font-size:14px; font-family:var(--sans); }
  .meta { color:var(--muted); font:12px var(--mono); margin-top:3px; }
  .desc { color:var(--fg); font-size:12.5px; margin-top:6px; }
  .badge { padding:2px 8px; border-radius:2px; font:11px var(--mono); text-transform:uppercase; letter-spacing:.04em;
           background:var(--pill-bg); border:1px solid var(--border); color:var(--muted); }
  .badge.healthy { color:var(--ok); border-color:var(--pill-ok-bd); background:var(--pill-ok-bg); }
  .badge.starting { color:var(--warn); border-color:var(--pill-warn-bd); background:var(--pill-warn-bg); }
  .badge.unhealthy, .badge.error { color:var(--bad); border-color:var(--pill-bad-bd); background:var(--pill-bad-bg); }
  .badge.cached { color:var(--ok); border-color:var(--pill-ok-bd); background:var(--pill-ok-bg); }
  .badge.downloading { color:var(--warn); border-color:var(--pill-warn-bd); background:var(--pill-warn-bg); }
  .origin { color:var(--accent); }
  .actions { display:flex; gap:8px; align-items:flex-start; }
  .hint { color:var(--muted); font-size:12px; margin-top:8px; }
  code { background:var(--code-bg); color:var(--fg); padding:1px 5px; border-radius:2px; font:12px var(--mono); }
  .progress { display:inline-block; width:120px; height:8px; background:var(--panel2); border:1px solid var(--border); border-radius:999px; overflow:hidden; vertical-align:middle; }
  .progress .bar { height:100%; background:var(--accent); transition:width .4s ease; }
  .progress.indeterminate .bar { width:100% !important; opacity:.35; }
  .pct { color:var(--muted); font-size:11px; margin-left:2px; }
  .gpuinfo { color:var(--muted); font-size:12px; margin-top:6px; }
  .gpuinfo .card-chip { color:var(--accent); }
</style>
<script>
  /* Apply the saved theme before first paint (no flash). Default = follow OS. */
  (function(){ try { var t = localStorage.getItem('secrouter-theme'); if (t === 'dark' || t === 'light') document.documentElement.setAttribute('data-theme', t); } catch (e) {} })();
</script>
</head>
<body>
<header>
  <div class="brand">
    <svg class="logo-light" viewBox="0 0 48 58" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <g transform="translate(4,5)">
        <polygon points="24,2 44,13 44,37 24,54 4,37 4,13" fill="none" stroke="#17140d" stroke-width="2" stroke-linejoin="round"/>
        <path d="M24 28 L24 14 M24 28 L14 38 M24 28 L34 38" stroke="#17140d" stroke-width="1.9"/>
        <path d="M24 14 L14 38 M24 14 L34 38 M14 38 L34 38" stroke="#17140d" stroke-width="1.4" stroke-opacity="0.4"/>
        <circle cx="24" cy="14" r="2.7" fill="#17140d"/><circle cx="14" cy="38" r="2.7" fill="#17140d"/><circle cx="34" cy="38" r="2.7" fill="#17140d"/>
        <circle cx="24" cy="28" r="4.4" fill="#54672f"/>
      </g>
    </svg>
    <svg class="logo-dark" viewBox="0 0 48 58" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <g transform="translate(4,5)">
        <polygon points="24,2 44,13 44,37 24,54 4,37 4,13" fill="none" stroke="#f5f3ea" stroke-width="2" stroke-linejoin="round"/>
        <path d="M24 28 L24 14 M24 28 L14 38 M24 28 L34 38" stroke="#f5f3ea" stroke-width="1.9"/>
        <path d="M24 14 L14 38 M24 14 L34 38 M14 38 L34 38" stroke="#f5f3ea" stroke-width="1.4" stroke-opacity="0.4"/>
        <circle cx="24" cy="14" r="2.7" fill="#f5f3ea"/><circle cx="14" cy="38" r="2.7" fill="#f5f3ea"/><circle cx="34" cy="38" r="2.7" fill="#f5f3ea"/>
        <circle cx="24" cy="28" r="4.4" fill="#cdd6a6"/>
      </g>
    </svg>
    <h1><span class="sec">SEC</span>LLM</h1>
  </div>
  <span class="tag">model console</span>
  <span id="pill" class="pill">connecting…</span>
  <button class="ghost theme-toggle" id="themeBtn" title="Toggle light / dark">◐</button>
</header>
<main>
  <section class="card">
    <div class="row">
      <span id="who" class="hint"></span>
      <button id="signin" style="display:none">Sign in with SecSSO</button>
      <button id="signout" class="ghost" style="display:none">Sign out</button>
      <input id="token" type="password" placeholder="admin token (SECLLM_ADMIN_TOKEN)" />
      <button id="connect">Connect</button>
      <span id="info" class="hint"></span>
    </div>
    <div class="hint">Load a model to serve it on SecLLM's OpenAI endpoint. Several models run <b>at once</b>, packed onto your GPUs by available VRAM — Load a new one and it coexists with the others (set <code>SECLLM_MAX_LOADED=1</code> if you instead want loading a model to <b>switch</b> by evicting the current one). When the GPUs are full a Load is refused rather than crowding a card. Point SecRouter at <code>http://&lt;host&gt;:11400/v1</code>. The context field overrides that model's default context length (tokens) for this load only — blank uses the catalog default. <b>Download</b> pre-fetches a model's weights without loading/serving it — useful for warming several models ahead of time; Load downloads automatically too if you skip this.</div>
  </section>
  <section class="card">
    <h2 style="margin:0 0 8px; font:11px var(--mono); text-transform:uppercase; letter-spacing:.12em; color:var(--muted)">Models</h2>
    <div id="models"><div class="hint">sign in with SecSSO, or connect with an admin token, to manage models</div></div>
  </section>
</main>
<script>
const $=(id)=>document.getElementById(id);
let token=localStorage.getItem("secllm_token")||""; $("token").value=token;
let signedIn=false; // true once an SSO session cookie is active (no admin token needed then)
function pill(s,t){const p=$("pill");p.className="pill "+s;p.textContent=t;}
function esc(s){return String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));}
function fmtBytes(n){n=Number(n)||0;const u=["B","KB","MB","GB","TB"];let i=0;while(n>=1024&&i<u.length-1){n/=1024;i++;}return (i===0?String(n):n.toFixed(1))+u[i];}
function fmtEta(s){s=Math.round(Number(s)||0);if(s<=0)return"";const h=Math.floor(s/3600),m=Math.floor(s%3600/60),sec=s%60;if(h)return h+"h "+m+"m";if(m)return m+"m "+sec+"s";return sec+"s";}
// credentials:same-origin sends the SSO session cookie (when signed in); the admin token header is
// added only when a token is present (break-glass / bearer-only / SSO-off). Either satisfies require_admin.
async function api(path,opts={}){opts.credentials="same-origin";opts.headers=Object.assign({},opts.headers,token?{Authorization:"Bearer "+token}:{});const r=await fetch(path,opts);if(!r.ok)throw new Error(path+" → "+r.status);return r.json();}
async function loadAuth(){try{applyAuth(await fetch("/auth/status",{credentials:"same-origin"}).then(r=>r.json()));}catch(e){}}
function applyAuth(a){
  signedIn=!!(a&&a.user);
  const admin=signedIn&&a.user.admin;
  // Sign-in button only when the browser (BFF) login is available and we're not already signed in.
  $("signin").style.display=(a&&a.sso&&!signedIn)?"":"none";
  $("signout").style.display=signedIn?"":"none";
  // Hide the manual admin-token box once a session is active — the cookie authorizes the API.
  $("token").style.display=signedIn?"none":"";
  $("connect").style.display=signedIn?"none":"";
  $("who").textContent=signedIn
    ?("signed in as "+(a.user.name||a.user.sub)+(admin?"":" — not a member of "+(a.admin_group||"the admin group")))
    :"";
  if(signedIn)refresh();
}
async function loadHealth(){try{const h=await fetch("/health").then(r=>r.json());pill("ok","healthy · "+h.backend);}catch(e){pill("bad","unreachable");}}
async function refresh(){
  if(!token&&!signedIn)return;
  // Preserve whatever the operator has typed but not yet submitted — refresh runs every 4s
  // (see setInterval below), and a freshly-rebuilt <input> would otherwise wipe it mid-edit.
  const typed={};
  document.querySelectorAll("input.ctx").forEach(el=>{typed[el.dataset.id]=el.value;});
  try{
    const d=await api("/admin/api/models");
    const maxLoaded=d.max_loaded>0?String(d.max_loaded):"∞ (GPU-bound)";
    let gpuInfo="";
    if(d.gpu&&d.gpu.managed){
      const cards=d.gpu.gpus.map(g=>`gpu${g.index} ${esc(g.name)} ${Math.round((g.allocated||0)*100)}%`).join(" · ");
      gpuInfo=` · GPUs [cap ${Math.round((d.gpu.cap||0)*100)}%]: ${cards||"(none detected)"}`;
    }
    const totalCalls=d.models.reduce((a,m)=>a+((m.stats&&m.stats.requests)||0),0);
    $("info").textContent=`backend: ${d.backend} · max loaded: ${maxLoaded} · ${totalCalls} API calls${gpuInfo}`;
    $("models").innerHTML=d.models.map(m=>{
      const w=m.worker; const state=w?w.state:"not loaded";
      const badge=w?`<span class="badge ${esc(w.state)}">${esc(w.state)}</span>`:'<span class="badge">not loaded</span>';
      const up=w&&w.state==="healthy"?` · up ${Math.round(w.uptime_s)}s`:"";
      const err=w&&w.error?`<div class="meta" style="color:var(--bad)">${esc(w.error)}</div>`:"";
      const ctxDefault=m.context_length?esc(String(m.context_length)):"unset";
      const ctxActive=w&&w.context_length?` · <span class="badge">ctx override ${w.context_length}</span>`:"";
      // Where the scheduler pinned this worker: which GPU(s) and the VRAM fraction it holds.
      const gpuTag=(w&&w.gpus&&w.gpus.length)?` · <span class="badge">gpu ${esc(w.gpus.join(","))} @ ${Math.round((w.memory_fraction||0)*100)}%</span>`:"";
      const ctxInput=`<input class="ctx" type="number" min="1" id="ctx-${esc(m.id)}" data-id="${esc(m.id)}" placeholder="ctx (default ${ctxDefault})" title="context length override (tokens) for the next Load/Reload — blank = catalog default">`;
      // Download: decoupled from Load — pre-fetches weights without starting/serving the
      // model. Hidden once cached (nothing left to fetch) or already loaded (Load itself
      // downloads first if needed, so a separate fetch would just be redundant).
      const downloading=m.download_status==="downloading";
      let cacheBadge;
      if(m.cached) cacheBadge='<span class="badge cached">cached</span>';
      else if(downloading) cacheBadge='<span class="badge downloading">downloading…</span>';
      else if(m.download_status==="error") cacheBadge='<span class="badge error" title="'+esc(m.download_error)+'">download failed</span>';
      else cacheBadge='<span class="badge">not cached</span>';
      // Live progress bar while downloading. percent is null until the repo's total size is
      // known (or if the lookup failed) — show an indeterminate bar in that case.
      let progress="";
      if(downloading){
        const known=m.download_percent!=null;
        const pct=known?m.download_percent:100;
        const tip=m.download_total_bytes?`${fmtBytes(m.download_downloaded_bytes)} / ${fmtBytes(m.download_total_bytes)}`:"total size unknown";
        // Live rate + time-remaining alongside the bar (both null until some bytes land; ETA also
        // needs the total size). e.g. "42.1% · 118.3MB/s · 2m 4s left".
        const rate=m.download_speed_bps?` · ${fmtBytes(m.download_speed_bps)}/s`:"";
        const eta=m.download_eta_seconds?` · ${fmtEta(m.download_eta_seconds)} left`:"";
        progress=`<span class="progress${known?"":" indeterminate"}" title="${tip}"><span class="bar" style="width:${pct}%"></span></span><span class="pct">${known?pct+"%":"…"}${rate}${eta}</span>`;
      }
      const downloadBtn=(!m.cached && !m.loaded)
        ? `<button class="ghost" onclick="act('${m.id}','download')" ${downloading?"disabled":""}>${downloading?"Downloading…":"Download"}</button>`
        : "";
      const st=m.stats||{};
      const tok=st.tokens>=1000?(st.tokens/1000).toFixed(1)+"k":st.tokens;
      const statsLine=st.requests?`<div class="meta">calls: ${st.requests}${st.errors?` · <span style="color:var(--bad)">${st.errors} err</span>`:""}${st.avg_latency_ms!=null?` · ${Math.round(st.avg_latency_ms)}ms avg`:""}${st.tokens?` · ${tok} tok`:""}</div>`:"";
      const toolTag=m.tool_call_parser?`<span class="badge healthy" title="server-side tool/function calling is ON for this model (vLLM --tool-call-parser=${esc(m.tool_call_parser)})">tools: ${esc(m.tool_call_parser)}</span>`:`<span class="badge" title="tool/function calling is not configured for this model — the coding agent can't call tools against it">tools: off</span>`;
      const memTag=m.vram_fraction?`<span class="badge" title="unified-memory reservation per worker on the metal backend (vLLM --gpu-memory-utilization)">mem ${Math.round(m.vram_fraction*100)}%</span>`:"";
      let actions;
      if(!m.loaded) actions=`${downloadBtn}${progress}${ctxInput}<button onclick="act('${m.id}','load')">Load</button>`;
      else actions=`${ctxInput}<button class="ghost" onclick="act('${m.id}','reload')">Reload</button><button class="danger" onclick="act('${m.id}','unload')">Unload</button>`;
      return `<div class="model"><div>
        <h3>${esc(m.name)} ${badge}${up}${ctxActive}${gpuTag}</h3>
        <div class="meta"><span class="origin">${esc(m.origin)}</span> · ${esc(m.size_class)} · <code>${esc(m.id)}</code> · ${esc(m.hf_model)} · context: ${ctxDefault} · ${cacheBadge}</div>
        <div class="meta">config: ${toolTag} ${memTag}</div>
        <div class="desc">${esc(m.description)}</div>${err}${statsLine}
      </div><div class="actions">${actions}</div></div>`;
    }).join("");
    document.querySelectorAll("input.ctx").forEach(el=>{
      if(typed[el.dataset.id])el.value=typed[el.dataset.id];
    });
  }catch(e){$("models").innerHTML='<div class="hint">'+esc(e.message)+' — check the admin token</div>';}
}
async function act(id,verb){
  try{
    const body={};
    if(verb==="load"||verb==="reload"){
      const el=$("ctx-"+id);
      const v=el&&el.value.trim();
      if(v)body.context_length=parseInt(v,10);
    }
    await api(`/admin/api/models/${id}/${verb}`,
      {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
    setTimeout(refresh,300);
  }
  catch(e){alert(verb+" failed: "+e.message);}
}
$("connect").onclick=()=>{token=$("token").value.trim();localStorage.setItem("secllm_token",token);refresh();};
$("signin").onclick=()=>{location.href="/auth/login?next=/admin";};
$("signout").onclick=()=>{fetch("/auth/logout",{method:"POST",credentials:"same-origin"}).then(()=>location.reload());};
window.act=act;
// ── Theme (light / dark, follows OS by default, choice persisted) — matches the SecRouter
// admin console's contract verbatim: attribute + localStorage key are frozen, do not rename.
function effectiveTheme(){const a=document.documentElement.getAttribute("data-theme");if(a==="dark"||a==="light")return a;return(window.matchMedia&&matchMedia("(prefers-color-scheme: dark)").matches)?"dark":"light";}
/* The ◐ glyph is theme-neutral (secrecorder's toggle pattern) — no label swap needed. */
function setTheme(t){document.documentElement.setAttribute("data-theme",t);try{localStorage.setItem("secrouter-theme",t);}catch(e){}}
function toggleTheme(){setTheme(effectiveTheme()==="dark"?"light":"dark");}
$("themeBtn").onclick=toggleTheme;
loadAuth();loadHealth();refresh();
setInterval(()=>{loadHealth();refresh();},4000);
</script>
</body>
</html>
"""
