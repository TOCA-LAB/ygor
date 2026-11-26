/* Ygor Explorer — local, dependency-free, read-only. */
"use strict";
const paths = {
  grid: '<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  box: '<path d="m3 7 9-4 9 4v10l-9 4-9-4zM3 7l9 4 9-4M12 11v10M7.5 5l9 4"/>',
  flask: '<path d="M9 3h6M10 3v7L4 19a1.5 1.5 0 0 0 1 2h14a1.5 1.5 0 0 0 1-2l-6-9V3M7 15h10"/>',
  activity: '<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  check: '<path d="m6 12 4 4 8-9"/><circle cx="12" cy="12" r="9"/>',
  alert: '<path d="m10 4-8 14a2 2 0 0 0 2 3h16a2 2 0 0 0 2-3L14 4a2 2 0 0 0-4 0zM12 9v4M12 17h.01"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  right: '<path d="m9 5 7 7-7 7"/>', left: '<path d="m15 5-7 7 7 7"/>',
  down: '<path d="m6 9 6 6 6-6"/>',
  refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 6a8 8 0 0 1 14 6M18 18A8 8 0 0 1 4 12"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  moon: '<path d="M20 14a8 8 0 0 1-10-10A8.5 8.5 0 1 0 20 14z"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3M12 14v3"/>',
  file: '<path d="M14 3H5v18h14V8zM14 3v5h5M8 12h8M8 16h6"/>',
  code: '<path d="m8 8-5 4 5 4m8-8 5 4-5 4M14 5l-4 14"/>',
  folder: '<path d="M3 7V4h6l2 3h10v13H3z"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M10 10h4v4h-4zM9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 10v7M12 7h.01"/>',
  terminal: '<path d="m5 6 5 5-5 5M13 17h6"/>',
  leaf: '<path d="M20 3C8 2 2 7 5 15s15 5 15-12zM5 20 16 9"/>',
};
const icon = name => `<svg class="icon" viewBox="0 0 24 24" aria-hidden="true">${paths[name] || paths.file}</svg>`;
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const $ = (selector, root = document) => root.querySelector(selector);
const num = n => new Intl.NumberFormat("pt-BR").format(n || 0);
const decimal = (n, digits = 1) => new Intl.NumberFormat("pt-BR", {maximumFractionDigits:digits}).format(n);
const duration = n => n == null ? "—" : n >= 86400 ? `${decimal(n/86400)} d` : n >= 3600 ? `${decimal(n/3600)} h` : n >= 60 ? `${decimal(n/60)} min` : `${decimal(n,3)} s`;
const size = n => n == null ? "Pasta" : n >= 1048576 ? `${decimal(n/1048576)} MB` : n >= 1024 ? `${decimal(n/1024)} KB` : `${num(n)} B`;
const date = value => value && !Number.isNaN(Date.parse(value)) ? new Intl.DateTimeFormat("pt-BR", {day:"2-digit",month:"short",year:"numeric"}).format(new Date(value)) : "—";
const dateTime = value => value && !Number.isNaN(Date.parse(value)) ? new Date(value).toLocaleString("pt-BR") : "—";
const statusName = {completed:"Concluído", failed:"Com falhas",running:"Em execução",paused:"Pausado",pending:"Pendente"};
const executionStatus = {completed:"Processo concluído", failed:"Falhou",running:"Em execução",pending:"Pendente"};
const badge = (status, execution = false) => `<span class="status ${esc(status)}"><span class="dot"></span>${esc((execution ? executionStatus : statusName)[status] || status || "Desconhecido")}</span>`;
const counts = run => run.state?.counts || {};
const progress = (c, total, large = false) => {
  let x = 0;
  const rects = [[c.completed,"--green"],[c.failed,"--orange"],[c.running,"--blue"]].map(([v,color]) => {
    const width = total ? (v || 0) / total * 100 : 0;
    const r = `<rect x="${x}" width="${width}" height="10" fill="var(${color})"/>`; x += width; return r;
  }).join("");
  return `<svg viewBox="0 0 100 10" preserveAspectRatio="none" class="${large ? "large-progress" : "progress-stack"}" role="img" aria-label="${num(c.completed)} concluídas de ${num(total)}">${rects}</svg>`;
};
let data = {runs:[],errors:[]}, epoch = 0, currentRun = null, currentTab = "overview", toastTimer;
const detailCache = new Map();
const list = {q:"",status:"",recipe:"",sort:"newest",page:0};
const executionStates = new Map();
let drawerReturnFocus = null, drawerEpoch = 0;

async function api(path, args) {
  const response = await fetch(path, args);
  let value;
  try { value = await response.json(); } catch { throw new Error("O servidor retornou uma resposta inválida."); }
  if (!response.ok) throw new Error(value.error || `Erro ${response.status}`);
  return value;
}
const runApi = (id, endpoint = "") => `/api/runs/${encodeURIComponent(id)}${endpoint ? "/"+endpoint : ""}`;
const download = (id,path) => runApi(id,"download")+"?path="+encodeURIComponent(path);
function toast(message) {
  const el = $("#toast"); el.textContent = message; el.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.hidden = true; },4000);
}
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Copiado para a área de transferência."); }
  catch { toast("A cópia não está disponível neste navegador. Use o download."); }
}
function empty(title, message, glyph = "box") {
  return `<div class="empty-state">${icon(glyph)}<h2>${esc(title)}</h2><p>${esc(message)}</p></div>`;
}
function loading(message = "Preparando os dados…") {
  return `<div class="loading-state"><span class="spinner"></span><p>${esc(message)}</p></div>`;
}
function errorView(error) {
  return `<div class="error-panel" role="alert">${icon("alert")} ${esc(error.message || error)}</div>`;
}
function notice(message, warning = false) {
  return `<div class="notice ${warning ? "warning" : ""}">${icon(warning ? "alert" : "info")}<div>${message}</div></div>`;
}
function stat(label, value, foot, glyph, color = "") {
  return `<div class="stat-card"><div class="stat-head"><span class="stat-icon ${color}">${icon(glyph)}</span>${esc(label)}</div><div class="stat-value">${esc(value)}</div><div class="stat-foot">${esc(foot)}</div></div>`;
}
function definition(entries) {
  return `<dl class="definition-list">${entries.map(([key,value]) => `<div><dt>${esc(key)}</dt><dd>${value}</dd></div>`).join("")}</dl>`;
}
function panel(title, sub, body) {
  return `<section class="panel"><div class="panel-heading"><div><h2>${esc(title)}</h2>${sub ? `<p>${esc(sub)}</p>` : ""}</div></div><div class="panel-body">${body}</div></section>`;
}
function footer() { return `<div class="page-footer"><span>Ygor Explorer <span class="muted">·</span> Feito para explorar, não para alterar.</span><span>Seus experimentos. Seus dados.</span></div>`; }

function renderSidebar() {
  $("#run-count").textContent = num(data.runs.length);
  $("#store-name").textContent = data.store?.split("/").filter(Boolean).slice(-2).join(" / ") || "Store local";
  $("#store-name").title = data.store || "";
  $("#status-nav").innerHTML = ["completed","failed","running","paused"].map(s => `<button class="nav-item ${list.status === s ? "active" : ""}" data-state="${s}"><span class="nav-dot ${s}"></span><span>${statusName[s]}</span><span class="nav-count">${num(data.runs.filter(r => r.state.status === s).length)}</span></button>`).join("");
  $("#status-nav").querySelectorAll("button").forEach(b => b.onclick = () => { list.status = list.status === b.dataset.state ? "" : b.dataset.state; list.page = 0; goList(); });
  const recipes = [...new Set(data.runs.map(r => r.recipe_path || r.name))].sort();
  $("#recipe-count").textContent = num(recipes.length);
  $("#recipe-nav").innerHTML = recipes.map(r => `<button class="nav-item ${list.recipe === r ? "active" : ""}" data-recipe="${esc(r)}" title="${esc(r)}">${icon("file")}<span>${esc(r.split("/").pop().replace(/\.ya?ml$/, ""))}</span></button>`).join("");
  $("#recipe-nav").querySelectorAll("button").forEach(b => b.onclick = () => { list.recipe = list.recipe === b.dataset.recipe ? "" : b.dataset.recipe; list.page=0; goList(); });
  $("#all-runs-nav").classList.toggle("active",!list.status && !list.recipe);
}
function goList() { renderSidebar(); if (location.hash === "#/" || !location.hash) renderList(); else location.hash="/"; }

function renderList() {
  currentRun = null;
  document.title = "Seus experimentos · Ygor";
  const focused = document.activeElement?.id, cursor = $("#run-search")?.selectionStart;
  let runs = data.runs.filter(r => (!list.status || r.state.status === list.status) && (!list.recipe || (r.recipe_path || r.name) === list.recipe) && (!list.q || `${r.name} ${r.id} ${r.recipe_path} ${r.description}`.toLowerCase().includes(list.q.toLowerCase())));
  runs.sort((a,b) => list.sort === "oldest" ? (a.state.started_at || "").localeCompare(b.state.started_at || "") : list.sort === "largest" ? (b.state.total_executions || 0) - (a.state.total_executions || 0) : (b.state.started_at || "").localeCompare(a.state.started_at || ""));
  list.page = Math.min(list.page,Math.max(0,Math.ceil(runs.length/15)-1));
  const visible = runs.slice(list.page*15,list.page*15+15);
  const total = data.runs.reduce((s,r) => s+(r.state.total_executions || 0),0);
  const completed = data.runs.reduce((s,r) => s+(counts(r).completed || 0),0);
  const failed = data.runs.reduce((s,r) => s+(counts(r).failed || 0),0);
  const finishedRuns = data.runs.filter(r => r.state.status === "completed").length;
  $("#main").innerHTML = `<div class="breadcrumb">${icon("grid")} Biblioteca ${icon("right")} Visão geral</div>
    <div class="page-heading"><div><h1>Seus experimentos</h1><p>Da primeira execução ao próximo insight. Tudo em um só lugar.</p></div><span class="subtle-tag">${icon("lock")} Store local · somente leitura</span></div>
    <div class="stats-grid">${stat("Runs no laboratório",num(data.runs.length),`${num(finishedRuns)} sem falhas de processo`,"flask")}${stat("Execuções registradas",num(total),"Cada combinação conta uma história","box","purple")}${stat("Execuções concluídas",total ? `${decimal(completed/total*100)}%` : "—",`${num(completed)} processos concluídos`,"check","green")}${stat("Execuções com falha",num(failed),"Timeouts e outros encerramentos","alert","orange")}</div>
    ${data.errors.length ? notice(`${num(data.errors.length)} manifesto(s) não puderam ser lidos. Os demais runs continuam disponíveis.`,true) : ""}
    <section class="panel"><div class="panel-heading"><div><h2>Biblioteca de runs</h2><p>Explore os registros e acompanhe cada detalhe.</p></div><span class="subtle-tag">${num(runs.length)} runs</span></div>
    <div class="toolbar"><label class="search-field">${icon("search")}<input type="search" id="run-search" placeholder="Buscar por receita, nome ou hash…" aria-label="Buscar runs" value="${esc(list.q)}"></label><div class="filters"><select id="run-status" aria-label="Filtrar runs por estado"><option value="">Todos os estados</option>${Object.entries(statusName).map(([v,l])=>`<option value="${v}" ${list.status===v ? "selected" : ""}>${l}</option>`).join("")}</select><select id="run-sort" aria-label="Ordenar runs"><option value="newest">Mais recentes primeiro</option><option value="oldest">Mais antigos primeiro</option><option value="largest">Mais execuções primeiro</option></select>${list.recipe ? `<button class="button" id="clear-recipe">${esc(list.recipe.split("/").pop())} ${icon("close")}</button>` : ""}</div></div>
    ${visible.length ? `<div class="table-wrap"><table><thead><tr><th>Experimento</th><th>Estado</th><th>Execuções</th><th>Iniciado em</th><th aria-label="Abrir run"></th></tr></thead><tbody>${visible.map((r,i) => `<tr class="run-row"><td><a class="run-link run-row-link" href="#/run/${encodeURIComponent(r.id)}/overview"><span class="run-avatar ${["","purple","green"][i%3]}">${icon("flask")}</span><span><strong>${esc(r.name)}</strong><small>${esc(r.recipe_path || "Receita sem nome")} <span aria-hidden="true">·</span> <code>${esc(r.short_id)}</code></small></span></a></td><td>${badge(r.state.status)}</td><td><span class="progress-caption">${num(counts(r).completed)} <span class="muted">/ ${num(r.state.total_executions)}</span></span>${progress(counts(r),r.state.total_executions)}</td><td class="muted" title="${esc(dateTime(r.state.started_at))}">${date(r.state.started_at)}</td><td><a class="row-arrow icon-button" href="#/run/${encodeURIComponent(r.id)}/overview" aria-label="Abrir ${esc(r.name)}">${icon("right")}</a></td></tr>`).join("")}</tbody></table></div>` : empty(data.runs.length ? "Nenhum run por aqui" : "Seu laboratório está pronto",data.runs.length ? "Tente outro termo ou remova os filtros para encontrar seu experimento." : "Execute uma receita com ygor run. Seus experimentos aparecerão aqui automaticamente.","flask")}
    <div class="table-footer"><span>${runs.length ? `${num(list.page*15+1)}–${num(Math.min((list.page+1)*15,runs.length))} de ${num(runs.length)} runs` : "Nenhum resultado"}</span><div class="pagination"><button class="icon-button" id="run-prev" aria-label="Página anterior" ${list.page===0 ? "disabled" : ""}>${icon("left")}</button><span>${list.page+1} / ${Math.max(1,Math.ceil(runs.length/15))}</span><button class="icon-button" id="run-next" aria-label="Próxima página" ${(list.page+1)*15>=runs.length ? "disabled" : ""}>${icon("right")}</button></div></div></section>
    <div class="section-caption">${icon("info")} Conclusão indica término do processo; não certifica a validade da saída nem a otimalidade.</div>${footer()}`;
  $("#run-sort").value=list.sort;
  $("#run-search").oninput = e => { list.q=e.target.value; list.page=0; renderList(); };
  $("#run-status").onchange = e => { list.status=e.target.value; list.page=0; renderSidebar(); renderList(); };
  $("#run-sort").onchange = e => { list.sort=e.target.value; list.page=0; renderList(); };
  $("#run-prev").onclick=()=>{list.page--;renderList();}; $("#run-next").onclick=()=>{list.page++;renderList();};
  if ($("#clear-recipe")) $("#clear-recipe").onclick=()=>{list.recipe="";list.page=0;renderSidebar();renderList();};
  if (focused === "run-search") { $("#run-search").focus(); $("#run-search").setSelectionRange(cursor,cursor); }
}

const tabs = [["overview","Visão geral","grid"],["executions","Execuções","activity"],["recipe","Receita","code"],["environment","Ambiente","cpu"],["files","Arquivos","folder"]];
async function renderRun(id, tab = "overview", token = epoch) {
  currentRun=id; currentTab=tabs.some(t=>t[0]===tab) ? tab : "overview";
  const known = data.runs.find(r=>r.id===id || r.id.startsWith(id));
  const title = known?.name || id.slice(0,8);
  document.title = `${title} · Ygor`;
  $("#main").innerHTML = `<div class="breadcrumb"><a href="#/">${icon("grid")} Biblioteca</a>${icon("right")} ${esc(title)}</div><div class="page-heading detail-heading"><div><h1>${esc(title)}</h1><div class="detail-subtitle"><code class="hash">${esc(id.slice(0,8))}</code>${known ? badge(known.state.status) : ""}<span class="muted"><small>${known ? date(known.state.started_at) : "Run local"}</small></span></div></div><a class="button" href="#/">${icon("left")} Todos os runs</a></div><nav class="tabs" aria-label="Seções do run">${tabs.map(([v,l,g])=>`<a class="tab ${v===currentTab ? "active" : ""}" ${v===currentTab ? 'aria-current="page"' : ""} href="#/run/${encodeURIComponent(id)}/${v}">${icon(g)}${l}</a>`).join("")}</nav><div id="run-content">${loading("Lendo os registros do run… Na primeira visita, runs grandes podem levar alguns segundos.")}</div>${footer()}`;
  try {
    let detail = detailCache.get(id);
    if (!detail) { detail = await api(runApi(id,"metadata")); detailCache.set(id,detail); }
    if (token!==epoch) return;
    currentRun=detail.id;
    if (currentTab === "overview") {
      renderOverview(detail);
      if(!detail.has_statistics) {
        api(runApi(id,"statistics")).then(stats=>{
          if(token!==epoch) return;
          detailCache.set(id,stats); renderOverview(stats);
        }).catch(error=>{if(token===epoch && $("#run-content")) $("#run-content").insertAdjacentHTML("afterbegin",notice(`Não foi possível calcular as estatísticas: ${esc(error.message)}`,true));});
      }
    }
    else if (currentTab === "executions") renderExecutions(detail);
    else if (currentTab === "recipe") await renderRecipe(detail,token);
    else if (currentTab === "environment") renderEnvironment(detail);
    else await renderFiles(detail.id,"",0,token);
  } catch (error) { if (token===epoch && $("#run-content")) $("#run-content").innerHTML=errorView(error); }
}

function renderOverview(d) {
  const m=d.metadata, r=m.recipe || {}, c=d.counts, state=m.state || {}, total=Object.values(c).reduce((a,b)=>a+b,0);
  const times=d.durations || {buckets:[],max:0,mean:null};
  const elapsed=state.started_at && state.ended_at ? (Date.parse(state.ended_at)-Date.parse(state.started_at))/1000 : null;
  const bins=times.buckets, max=Math.max(...bins,1);
  const histogram=d.has_statistics ? `<svg class="chart" viewBox="0 0 480 145" preserveAspectRatio="none" role="img" aria-label="Histograma do tempo de parede das execuções concluídas">${bins.map((v,i)=>`<rect x="${i*20+2}" y="${140-v/max*130}" width="14" height="${Math.max(0,v/max*130)}"><title>${num(v)} execuções · ${duration(times.max*i/24)} a ${duration(times.max*(i+1)/24)}</title></rect>`).join("")}</svg><div class="chart-axis"><span>0 s</span><span>${duration(times.max)}</span></div>` : loading("Indexando execuções para calcular a distribuição…");
  const params=r.parameters?.values || {};
  const occurrence = !d.has_statistics ? loading("Lendo os motivos registrados…") : d.failures.length ? d.failures.map(f=>`<div class="failure-row"><code>${esc(f.reason)}</code><strong>${num(f.count)}</strong></div>`).join("") : empty("Tudo em ordem","Não há falhas de processo registradas neste run.","check");
  $("#run-content").innerHTML=`<div class="stats-grid">${stat("Processos concluídos",num(c.completed),`${total ? decimal((c.completed || 0)/total*100) : 0}% das execuções`,"check","green")}${stat("Execuções com falha",num(c.failed),"Consulte os motivos abaixo","alert","orange")}${stat("Duração do run",duration(elapsed),"Intervalo entre início e término","clock","purple")}${stat("Workers",num(r.policy?.workers || 1),"Paralelismo da receita congelada","cpu")}</div>
    <div class="two-col">${panel("O caminho de cada execução",`${num(total)} execuções registradas`,progress(c,total,true)+`<div class="legend"><span><i class="green"></i> ${num(c.completed)} concluídas</span><span><i class="orange"></i> ${num(c.failed)} com falha</span><span><i class="blue"></i> ${num(c.running)} em execução</span><span>${num(c.pending)} pendentes</span></div><div class="section-caption">${icon("info")} Estado do processo, não da solução produzida.</div>`)}${panel("Tempo de parede","Distribuição entre processos concluídos",histogram)}</div>
    <div class="two-col">${panel("Sobre este run","O contexto preservado no momento da execução",definition([["Receita",esc(m.recipe_path || "—")],["Iniciado em",esc(dateTime(state.started_at))],["Finalizado em",esc(dateTime(state.ended_at))],["Computador",esc(m.host?.hostname || "—")],["Sementes",m.seeds ? `${num(m.seeds.count)} · meta-seed <code>${esc(m.seeds.meta_seed)}</code>` : "Sem sementes declaradas"],["Tempo médio",`${duration(times.mean)} <span class="muted">por processo concluído</span>`]]))}${panel("Ocorrências","Motivos registrados pelo executor",occurrence)}</div>`;
  $("#run-content").insertAdjacentHTML("beforeend",panel("Parâmetros do experimento","Valores usados para construir as combinações",Object.keys(params).length ? definition(Object.entries(params).map(([k,v])=>[k,(Array.isArray(v) ? v : [v]).map(x=>`<span class="chip">${esc(typeof x === "object" ? JSON.stringify(x) : x)}</span>`).join("")])) : '<p class="muted"><small>Esta receita não declara uma varredura de parâmetros.</small></p>'));
}

function renderEnvironment(d) {
  const m=d.metadata, env=m.recipe?.environment || {}, host=m.host || {};
  const apps=env.apps || {};
  $("#run-content").innerHTML=notice("Estes dados vêm do manifesto congelado, não da configuração atual do projeto.")+`<div class="two-col">${panel("Computador","Ambiente registrado pelo Ygor",definition(Object.entries(host).map(([k,v])=>[k,esc(v)])))}${panel("Execução","Recursos e versões",definition([["Versão do Ygor",esc(m.tool_version || "—")],["Imagem",`<code>${esc(env["base-image"] || "Sem contêiner")}</code>`],["CPU",esc(env.resources?.cpu || "—")],["Memória",esc(env.resources?.memory || "—")],["Timeout",esc(m.recipe?.policy?.["timeout-per-run"] || "Sem limite declarado")]]))}</div>`+panel("Aplicativos e fontes","Commits, binários e dependências preservados",definition(Object.entries(apps).map(([k,v])=>[k,typeof v === "object" ? `<code>${esc(JSON.stringify(v,null,2))}</code>` : esc(v)])))+`<br>`+panel("Datasets","Fontes utilizadas na execução",definition(Object.entries(m.recipe?.datasets || {}).map(([k,v])=>[k,`<code>${esc(JSON.stringify(v,null,2))}</code>`])));
}

function yamlHighlight(text) {
  return text.split("\n").map(line=>{
    const safe=esc(line);
    if (/^\s*#/.test(line)) return `<span class="yaml-comment">${safe}</span>`;
    return safe.replace(/^(\s*)([\w.-]+)(:)/, '$1<span class="yaml-key">$2</span>$3');
  }).join("\n");
}
async function renderRecipe(d,token) {
  const file=await api(runApi(d.id,"recipe"));
  if (token!==epoch) return;
  $("#run-content").innerHTML=notice("Esta é a <strong>receita usada neste run</strong>, preservada em <code>recipe.lock.yaml</code>. Mudanças posteriores na receita original não aparecem aqui.")+`<section class="panel"><div class="code-toolbar"><span>${icon("code")} recipe.lock.yaml</span><div class="actions"><button class="button" id="copy-recipe">${icon("copy")} Copiar</button><a class="button" href="${download(d.id,"recipe.lock.yaml")}" download>${icon("download")} Baixar</a></div></div>${file.empty ? empty("Receita vazia","O arquivo foi encontrado, mas não contém dados.","file") : `<pre class="code-view"><code>${yamlHighlight(file.text)}</code></pre>`}</section>`;
  $("#copy-recipe").onclick=()=>copy(file.text);
}

function renderExecutions(d) {
  const state=executionStates.get(d.id) || {q:"",status:"",sort:"id",desc:false,offset:0,token:0};
  executionStates.set(d.id,state);
  $("#run-content").innerHTML=notice("Uma execução concluída pelo Ygor pode conter uma saída incompleta do algoritmo. Abra a execução para inspecionar o resultado e os logs.")+`<section class="panel"><div class="panel-heading"><div><h2>Execuções</h2><p>Cada instância, combinação e semente, em detalhe.</p></div><a class="button" href="${download(d.id,"executions.csv")}" download>${icon("download")} CSV</a></div><div class="toolbar"><label class="search-field">${icon("search")}<input id="execution-search" type="search" aria-label="Buscar execuções" placeholder="Instância, parâmetro, semente ou ID…" value="${esc(state.q)}"></label><div class="filters"><select id="execution-status" aria-label="Filtrar execuções por estado"><option value="">Todos os estados</option>${Object.entries(executionStatus).map(([v,l])=>`<option value="${v}">${l}</option>`).join("")}</select><select id="execution-sort" aria-label="Ordenar execuções"><option value="id">Número da execução</option><option value="slow">Mais demoradas primeiro</option><option value="fast">Mais rápidas primeiro</option></select></div></div><div id="execution-table">${loading()}</div></section>`;
  $("#execution-status").value=state.status;
  $("#execution-sort").value=state.sort==="id" ? "id" : state.desc ? "slow" : "fast";
  let timer;
  $("#execution-search").oninput=e=>{state.q=e.target.value;state.offset=0;clearTimeout(timer);timer=setTimeout(()=>fetchExecutions(d.id,state),220);};
  $("#execution-status").onchange=e=>{state.status=e.target.value;state.offset=0;fetchExecutions(d.id,state);};
  $("#execution-sort").onchange=e=>{state.sort=e.target.value==="id" ? "id" : "duration";state.desc=e.target.value==="slow";state.offset=0;fetchExecutions(d.id,state);};
  fetchExecutions(d.id,state);
}
async function fetchExecutions(id,state) {
  const token=++state.token;
  const args=new URLSearchParams({q:state.q,status:state.status,sort:state.sort,desc:state.desc ? "1" : "0",offset:state.offset,limit:50});
  try {
    const result=await api(runApi(id,"executions")+"?"+args);
    if (currentRun!==id || currentTab!=="executions" || token!==state.token) return;
    $("#execution-table").innerHTML=(result.items.length ? `<div class="table-wrap"><table><thead><tr><th>Execução / instância</th><th>Estado</th><th>Tempo de parede</th><th>Semente</th><th></th></tr></thead><tbody>${result.items.map(e=>`<tr class="exec-row"><td><button class="exec-button" data-exec="${e.exec_id}">#${num(e.exec_id)} · ${esc(e.instance || "Sem instância")}<small class="muted">${Object.entries(e.params).filter(([k])=>k!=="instance").slice(0,4).map(([k,v])=>`<span class="chip">${esc(k)}: ${esc(v)}</span>`).join("")}</small></button></td><td>${badge(e.status,true)}</td><td>${duration(e.duration_sec)}</td><td class="muted mono">${esc(e.seed ?? "—")}</td><td><button class="icon-button" data-exec="${e.exec_id}" aria-label="Inspecionar execução ${e.exec_id}">${icon("right")}</button></td></tr>`).join("")}</tbody></table></div>` : empty("Nenhuma execução encontrada","Tente buscar por outra instância ou mude o filtro de estado.","activity"))+`<div class="table-footer"><span>${result.total ? `${num(state.offset+1)}–${num(Math.min(state.offset+50,result.total))} de ${num(result.total)} execuções` : "Nenhum resultado"}</span><div class="pagination"><button class="icon-button" id="exec-prev" aria-label="Página anterior de execuções" ${state.offset===0 ? "disabled" : ""}>${icon("left")}</button><button class="icon-button" id="exec-next" aria-label="Próxima página de execuções" ${state.offset+50>=result.total ? "disabled" : ""}>${icon("right")}</button></div></div>`;
    $("#execution-table").querySelectorAll("[data-exec]").forEach(b=>b.onclick=()=>inspectExecution(id,Number(b.dataset.exec)));
    $("#exec-prev").onclick=()=>{state.offset-=50;fetchExecutions(id,state);}; $("#exec-next").onclick=()=>{state.offset+=50;fetchExecutions(id,state);};
  } catch(error) { if (token===state.token && $("#execution-table")) $("#execution-table").innerHTML=errorView(error); }
}

function closeDrawer() { drawerEpoch++; $("#drawer-layer").innerHTML=""; document.body.classList.remove("drawer-open"); drawerReturnFocus?.focus(); }
function openDrawer(title) {
  const token=++drawerEpoch;
  drawerReturnFocus=document.activeElement;
  $("#drawer-layer").innerHTML=`<div class="drawer-backdrop" id="drawer-backdrop"></div><section class="drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title"><div class="drawer-header"><h2 id="drawer-title">${esc(title)}</h2><button class="icon-button" id="drawer-close" aria-label="Fechar detalhes">${icon("close")}</button></div><div class="drawer-body" id="drawer-body">${loading()}</div></section>`;
  $("#drawer-close").onclick=closeDrawer; $("#drawer-backdrop").onclick=closeDrawer; $("#drawer-close").focus(); document.body.classList.add("drawer-open");
  return token;
}
function localPath(value,fallback) {
  if (!value) return fallback;
  if (!value.startsWith("/")) return value;
  for (const root of ["outputs","streams","logs"]) { const i=value.indexOf("/"+root+"/"); if(i>=0) return value.slice(i+1); }
  return fallback;
}
async function inspectExecution(id,execId) {
  const drawerToken=openDrawer(`Execução #${num(execId)}`);
  try {
    const e=await api(runApi(id,`executions/${execId}`));
    if (!$("#drawer-body") || drawerToken!==drawerEpoch) return;
    const folder=`exec_${String(execId).padStart(4,"0")}`;
    const output=localPath(e.output_dir,`outputs/${folder}`);
    const stdout=localPath(e.stdout_path,`streams/${folder}/stdout.log`), stderr=localPath(e.stderr_path,`streams/${folder}/stderr.log`), log=localPath(e.log_path,`logs/${folder}.log`);
    $("#drawer-body").innerHTML=`<div>${badge(e.status,true)}</div><p class="muted"><small>${esc(e.instance || "Sem instância declarada")}</small></p><div class="stats-grid">${stat("Tempo de parede",duration(e.duration_sec),"Registrado pelo executor","clock")}${stat("Código de saída",e.exit_code ?? "—",e.failure_reason || "Do processo executado","terminal")}${stat("Semente",e.seed ?? "—","Desta repetição","leaf")}</div>${e.failure_reason ? notice(esc(e.failure_reason),true) : ""}<details><summary>Parâmetros e horários</summary>${definition([["Início",esc(dateTime(e.started_at))],["Término",esc(dateTime(e.ended_at))],...Object.entries(e.params).map(([k,v])=>[k,`<code>${esc(typeof v==="object" ? JSON.stringify(v) : v)}</code>`])])}</details><nav class="tabs" aria-label="Arquivos da execução"><button class="tab active" data-source="outputs">${icon("folder")} Saídas</button><button class="tab" data-source="stdout">stdout</button><button class="tab" data-source="stderr">stderr</button><button class="tab" data-source="log">Log</button></nav><div id="execution-source">${loading()}</div>`;
    let sourceToken=0;
    const load=async source=>{
      const token=++sourceToken;
      document.querySelectorAll("[data-source]").forEach(b=>b.classList.toggle("active",b.dataset.source===source));
      const target=$("#execution-source"); if(!target) return;
      target.innerHTML=loading();
      try {
        if(source==="outputs") await fileList(id,output,0,target,()=>token===sourceToken && drawerToken===drawerEpoch);
        else { const file=await api(runApi(id,"preview")+"?"+new URLSearchParams({path:{stdout,stderr,log}[source],tail:"1"})); if(token===sourceToken && drawerToken===drawerEpoch && target.isConnected) showPreview(id,file,target); }
      } catch(error) { if(token===sourceToken && drawerToken===drawerEpoch && target.isConnected) target.innerHTML=errorView(error); }
    };
    document.querySelectorAll("[data-source]").forEach(b=>b.onclick=()=>load(b.dataset.source));
    await load("outputs");
  } catch(error) { if($("#drawer-body") && drawerToken===drawerEpoch) $("#drawer-body").innerHTML=errorView(error); }
}

function showPreview(id,file,target,back) {
  const metrics=file.json && !Array.isArray(file.json) ? Object.entries(file.json).filter(([,v])=>typeof v==="number" || typeof v==="boolean").slice(0,12) : [];
  const warning=(file.empty ? notice("Arquivo encontrado, mas <strong>vazio</strong>. O término do processo não garante que a saída foi gravada.",true) : file.truncated ? notice(`Prévia limitada a 512 KB ${file.tail ? "finais" : "iniciais"}. Baixe o arquivo para consultar todo o conteúdo.`) : "")+(file.json?.completed===false ? notice("O algoritmo registrou <code>completed: false</code>. Consulte os limitantes e os logs; não interprete esta saída como uma execução completa do algoritmo.",true) : "");
  target.innerHTML=`${back ? `<button class="button preview-back">${icon("left")} Voltar aos arquivos</button><br><br>` : ""}${warning}<section class="panel"><div class="code-toolbar"><span title="${esc(file.path)}">${esc(file.name)} <small class="muted">· ${size(file.size)}</small></span><div class="actions">${!file.binary && !file.image ? `<button class="button copy-file" aria-label="Copiar conteúdo">${icon("copy")}</button>` : ""}<a class="button" href="${download(id,file.path)}" download aria-label="Baixar ${esc(file.name)}">${icon("download")} Baixar</a></div></div>${metrics.length ? `<div class="metric-grid">${metrics.map(([k,v])=>`<div><small>${esc(k)}</small><strong>${typeof v==="boolean" ? (v ? "true" : "false") : decimal(v,5)}</strong></div>`).join("")}</div>` : ""}${file.image ? `<div class="preview-frame"><img class="file-preview-image" src="${runApi(id,"image")}?path=${encodeURIComponent(file.path)}" alt="Prévia de ${esc(file.name)}"></div>` : file.binary ? empty("Arquivo binário","Baixe o arquivo para abrir no aplicativo apropriado.","file") : `<pre class="code-view"><code>${esc(file.text || "(arquivo vazio)")}</code></pre>`}</section>`;
  if($(".copy-file",target)) $(".copy-file",target).onclick=()=>copy(file.text);
  if(back) $(".preview-back",target).onclick=back;
}
async function fileList(id,path,offset,target,valid=()=>true) {
  const result=await api(runApi(id,"files")+"?"+new URLSearchParams({path,offset}));
  if(!valid() || !target.isConnected) return;
  const parts=path ? path.split("/") : [];
  target.innerHTML=`<div class="code-toolbar"><span class="file-path"><a href="#" data-folder="">Run</a>${parts.map((part,i)=>` / <a href="#" data-folder="${esc(parts.slice(0,i+1).join("/"))}">${esc(part)}</a>`).join("")}</span><small class="muted">${num(result.total)} itens</small></div>${result.items.length ? result.items.map(f=>`<button class="file-row" data-file="${esc(f.path)}" data-directory="${f.directory ? "1" : "0"}">${icon(f.directory ? "folder" : "file")}<span>${esc(f.name)}</span><small>${size(f.size)}</small>${icon("right")}</button>`).join("") : empty("Pasta vazia","Não há arquivos nesta pasta.","folder")}<div class="table-footer"><span>${result.total ? `${offset+1}–${Math.min(offset+result.limit,result.total)} de ${num(result.total)}` : "Sem arquivos"}</span><div class="pagination"><button class="icon-button file-prev" aria-label="Página anterior de arquivos" ${offset===0 ? "disabled" : ""}>${icon("left")}</button><button class="icon-button file-next" aria-label="Próxima página de arquivos" ${offset+result.limit>=result.total ? "disabled" : ""}>${icon("right")}</button></div></div>`;
  const navigate=async(p,o=0)=>{try{await fileList(id,p,o,target,valid);}catch(error){if(valid() && target.isConnected)target.innerHTML=errorView(error);}};
  target.querySelectorAll("[data-folder]").forEach(b=>b.onclick=e=>{e.preventDefault();navigate(b.dataset.folder);});
  target.querySelectorAll("[data-file]").forEach(b=>b.onclick=async()=>{
    if(b.dataset.directory==="1") return navigate(b.dataset.file);
    try{const file=await api(runApi(id,"preview")+"?path="+encodeURIComponent(b.dataset.file));if(valid() && target.isConnected)showPreview(id,file,target,()=>navigate(path,offset));}catch(error){if(valid() && target.isConnected)target.innerHTML=errorView(error);}
  });
  $(".file-prev",target).onclick=()=>navigate(path,offset-result.limit); $(".file-next",target).onclick=()=>navigate(path,offset+result.limit);
}
async function renderFiles(id,path,offset,token) {
  $("#run-content").innerHTML=notice("Explore somente os artefatos deste run. HTML e SVG são mostrados como texto; nenhum arquivo é executado.")+`<section class="panel" id="run-files">${loading()}</section>`;
  await fileList(id,path,offset,$("#run-files"),()=>token===epoch);
}

async function route() {
  const token=++epoch; closeDrawer();
  const pieces=location.hash.replace(/^#\/?/,"").split("/").map(decodeURIComponent);
  if(pieces[0]==="run" && pieces[1]) await renderRun(pieces[1],pieces[2],token); else renderList();
}
function readThemePreference() {
  try{return localStorage.getItem("ygor-theme") || localStorage.getItem("xlab-theme") || "light";}
  catch{return "light";}
}
function setTheme(theme) {
  document.documentElement.classList.toggle("dark",theme==="dark");
  $("#theme-button").innerHTML=icon(theme==="dark" ? "sun" : "moon");
  $("#theme-button").setAttribute("aria-label",theme==="dark" ? "Usar tema claro" : "Usar tema escuro");
  try{localStorage.setItem("ygor-theme",theme);}catch{}
}
async function refresh() {
  $("#refresh-button").disabled=true;
  try { data=await api("/api/runs"); detailCache.clear(); renderSidebar(); await route(); }
  catch(error) { $("#main").innerHTML=errorView(error)+`<a class="button" href="/">${icon("refresh")} Tentar novamente</a>`; }
  finally { $("#refresh-button").disabled=false; }
}
$("#workspace-icon").innerHTML=icon("flask"); $("#runs-icon").innerHTML=icon("grid"); $("#readonly-icon").innerHTML=icon("lock"); $("#refresh-button").innerHTML=icon("refresh");
setTheme(readThemePreference());
$("#theme-button").onclick=()=>setTheme(document.documentElement.classList.contains("dark") ? "light" : "dark");
$("#refresh-button").onclick=refresh;
$("#all-runs-nav").onclick=e=>{e.preventDefault();Object.assign(list,{q:"",status:"",recipe:"",page:0});goList();};
window.addEventListener("hashchange",()=>route().catch(error=>{$("#main").innerHTML=errorView(error);}));
document.addEventListener("keydown",e=>{
  if(e.key==="Escape") closeDrawer();
  if(e.key==="/" && !["INPUT","TEXTAREA","SELECT"].includes(e.target.tagName)) {const search=$("#run-search") || $("#execution-search");if(search){e.preventDefault();search.focus();}}
  const dialog=$(".drawer");
  if(e.key==="Tab" && dialog) {const focusable=[...dialog.querySelectorAll('button:not(:disabled),a[href],input,select,summary,[tabindex="0"]')];const first=focusable[0],last=focusable.at(-1);if(e.shiftKey && document.activeElement===first){e.preventDefault();last.focus();}else if(!e.shiftKey && document.activeElement===last){e.preventDefault();first.focus();}}
});
refresh();
