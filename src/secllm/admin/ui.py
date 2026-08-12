"""The SecLLM management console — a single self-contained HTML page (no external assets)."""

CONSOLE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>SecLLM — Model Console</title>
<style>
  :root {
    --bg:#0b0e12; --panel:#111620; --panel2:#0e131b; --line:#232c3a;
    --ink:#d8e0ea; --dim:#8a97a8; --accent:#5b9bd5; --ok:#6bbf72; --warn:#e0b341; --bad:#e06c6c;
    --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink); font-family:var(--mono); font-size:14px; }
  header { display:flex; align-items:center; gap:14px; padding:16px 22px; border-bottom:1px solid var(--line); background:var(--panel2); }
  header h1 { font-size:16px; margin:0; letter-spacing:.5px; }
  header .tag { color:var(--dim); font-size:12px; }
  .pill { margin-left:auto; padding:3px 10px; border-radius:999px; font-size:12px; border:1px solid var(--line); color:var(--dim); }
  .pill.ok { color:var(--ok); border-color:#2c4a30; } .pill.bad { color:var(--bad); border-color:#4a2a2a; }
  main { max-width:960px; margin:0 auto; padding:22px; display:grid; gap:16px; }
  .card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:16px 18px; }
  .row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
  input[type=password]{ background:var(--panel2); border:1px solid var(--line); color:var(--ink); padding:8px 10px; border-radius:6px; font-family:var(--mono); min-width:280px; }
  input.ctx { background:var(--panel2); border:1px solid var(--line); color:var(--ink); padding:6px 8px; border-radius:6px; font-family:var(--mono); width:110px; font-size:12.5px; }
  button { background:var(--accent); color:#0b0e12; border:0; padding:7px 13px; border-radius:6px; font-family:var(--mono); font-weight:600; cursor:pointer; }
  button.ghost { background:transparent; color:var(--ink); border:1px solid var(--line); }
  button.danger { background:transparent; color:var(--bad); border:1px solid #4a2a2a; }
  button:disabled { opacity:.4; cursor:not-allowed; }
  .model { display:grid; grid-template-columns:1fr auto; gap:8px 14px; padding:14px 0; border-bottom:1px solid var(--line); }
  .model:last-child { border-bottom:0; }
  .model h3 { margin:0; font-size:14px; }
  .meta { color:var(--dim); font-size:12px; margin-top:3px; }
  .desc { color:var(--ink); font-size:12.5px; margin-top:6px; }
  .badge { padding:2px 8px; border-radius:4px; font-size:11px; border:1px solid var(--line); color:var(--dim); }
  .badge.healthy { color:var(--ok); border-color:#2c4a30; } .badge.starting { color:var(--warn); border-color:#4a4020; }
  .badge.unhealthy, .badge.error { color:var(--bad); border-color:#4a2a2a; }
  .badge.cached { color:var(--ok); border-color:#2c4a30; } .badge.downloading { color:var(--warn); border-color:#4a4020; }
  .origin { color:var(--accent); }
  .actions { display:flex; gap:8px; align-items:flex-start; }
  .hint { color:var(--dim); font-size:12px; margin-top:8px; }
  code { color:var(--warn); }
  .progress { display:inline-block; width:120px; height:8px; background:var(--panel2); border:1px solid var(--line); border-radius:999px; overflow:hidden; vertical-align:middle; }
  .progress .bar { height:100%; background:var(--accent); transition:width .4s ease; }
  .progress.indeterminate .bar { width:100% !important; opacity:.35; }
  .pct { color:var(--dim); font-size:11px; margin-left:2px; }
  .gpuinfo { color:var(--dim); font-size:12px; margin-top:6px; }
  .gpuinfo .card-chip { color:var(--accent); }
</style>
</head>
<body>
<header>
  <h1>SecLLM</h1><span class="tag">model console</span>
  <span id="pill" class="pill">connecting…</span>
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
    <h2 style="margin:0 0 8px; font-size:13px; text-transform:uppercase; letter-spacing:1px; color:var(--dim)">Models</h2>
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
loadAuth();loadHealth();refresh();
setInterval(()=>{loadHealth();refresh();},4000);
</script>
</body>
</html>
"""
