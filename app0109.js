"use strict";

const ICON = {
  val_codegen:'<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h10M4 18h13"/></svg>',
  val_execute:'<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  rec_propose:'<svg viewBox="0 0 24 24"><path d="M12 3v6"/><path d="M9 6l3-3 3 3"/><path d="M4 12h16v8H4z"/></svg>',
  rec_review:'<svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h9"/><path d="M17 17l2 2 3-4"/></svg>',
  rec_codegen:'<svg viewBox="0 0 24 24"><path d="M4 7h16M4 17h16"/><path d="M9 4l-4 3 4 3"/><path d="M15 14l4 3-4 3"/></svg>',
  rec_execute:'<svg viewBox="0 0 24 24"><path d="M12 3v18"/><path d="M5 8h14"/><path d="M5 16h14"/></svg>',
  // source node -> target node: the clearest of the set, geometry nudged so the
  // arrowhead no longer collides with the connector
  mapping:'<svg viewBox="0 0 24 24"><circle cx="5" cy="12" r="2.4"/><circle cx="19" cy="12" r="2.4"/><path d="M7.4 12h8"/><path d="M13.2 9.4l2.6 2.6-2.6 2.6"/></svg>',
  // a checklist, not a shield: this agent checks rules against real rows, it
  // does not protect anything — a shield is the convention for security
  validation:'<svg viewBox="0 0 24 24"><path d="M3 6h10"/><path d="M3 11h10"/><path d="M3 16h7"/><path d="M13.5 16.5l2.6 2.6 4.9-5.4"/></svg>',
  // a flag, not an eye: review is where a human DECIDES, and the queue holds
  // only the items raised for them — an eye reads as passive observation
  review:'<svg viewBox="0 0 24 24"><path d="M6 21V4"/><path d="M6 5h11.5l-2.4 3.4L17.5 12H6"/></svg>',
  // braces, not chevrons — "< >" was near-identical to legacy_expert's "</>"
  codegen:'<svg viewBox="0 0 24 24"><path d="M9.5 4C7.6 4 7.2 5 7.2 6.9v1.9c0 1.5-1 2.4-2.4 2.9 1.4.5 2.4 1.4 2.4 2.9v1.9C7.2 18.4 7.6 20 9.5 20"/><path d="M14.5 4c1.9 0 2.3 1 2.3 2.9v1.9c0 1.5 1 2.4 2.4 2.9-1.4.5-2.4 1.4-2.4 2.9v1.9c0 1.6-.4 3.2-2.3 3.2"/></svg>',
  execute:'<svg viewBox="0 0 24 24"><path d="M7 4.5l12 7.5-12 7.5z"/></svg>',
};
const AGENTS = [
  {id:"mapping",       label:"Mapping Agent",      out:"mapping spec",        role:"Aligns source to target, builds transforms",working:"Building the mapping…"},
  {id:"validation",    label:"Validation Agent",   out:"validation report",   role:"Materialises target, checks the whole spec",working:"Validating the spec…"},
  {id:"review",        label:"Human Review",       out:"review queue",        role:"Flags only what needs a human",             working:"Assembling the queue…"},
  {id:"codegen",       label:"Code Generation",    out:"executable ETL",      role:"Compiles the certified mapping into code",  working:"Generating the ETL…"},
  {id:"execute",       label:"Execution",          out:"target dataset",      role:"Runs the ETL over the source file(s)",      working:"Running the transformation…"},
  {id:"val_codegen",   label:"Rule Generation",    out:"validation script",   role:"Derives the check families from spec + dictionary", working:"Deriving validation rules…"},
  {id:"val_execute",   label:"Validation",         out:"validation report",   role:"Executes every check over the delivered data",     working:"Running the checks…"},
  {id:"rec_propose",   label:"Rule Generation",    out:"proposed controls",   role:"Derives control totals and business controls",     working:"Deriving controls from the spec and source…"},
  {id:"rec_review",    label:"Human Review",       out:"certified controls",  role:"A reviewer decides which controls apply",           working:"Awaiting review…"},
  {id:"rec_codegen",   label:"Script Generation",  out:"recon script",        role:"Generates the script from the certified controls", working:"Generating the script…"},
  {id:"rec_execute",   label:"Reconciliation",     out:"recon report",        role:"Reconciles source against delivered data",         working:"Running reconciliation…"},
];
const VERDICT = {certified:["Certified","spec ready to load"], needs_review:["Needs review","items await a human"], blocked:["Blocked","hard failures present"], // local-only state: every item decided, nothing sent to the server yet
                 ready:["Ready to certify","decisions not yet submitted"]};
const $ = s => document.querySelector(s);
const AGENT_SETS = {b:["mapping","validation","review"], t:["codegen","execute"],
                    val:["val_codegen","val_execute"],
                    rec:["rec_propose","rec_review","rec_codegen","rec_execute"]};
const RUN_LABEL = {b:"Run mapping"};
const state = {artifacts:{}, queue:[], draining:false, dwell:1500, uiMode:"auto", mode:"auto",
               stepResolve:null, tab:"a", railAgents:[], kg:null,
               decisions:{}, certifiedDone:false, finalSpec:null};
const sleep = ms => new Promise(r=>setTimeout(r,ms));

/* ---------- rail (rendered per flow: tab 1 = A agents, tab 3 = B agents) ---------- */
function initRail(ids, keepArtifacts, passive){
  state.railAgents = (ids || AGENT_SETS.b).map(id => AGENTS.find(a=>a.id===id));
  if(!keepArtifacts) state.railAgents.forEach(a=> delete state.artifacts[a.id]);
  $("#rail").innerHTML = state.railAgents.map((a,i)=>`
    <div class="station" data-id="${a.id}" data-i="${i}">
      <span class="st-step">0${i+1}</span>
      <div class="dot">${ICON[a.id]}</div>
      <div class="st-name">${a.label}</div>
      <div class="st-role">${a.role}</div>
      <div class="st-out">&rarr; <b>${a.out}</b></div>
    </div>`).join("");
  document.querySelectorAll(".station").forEach(s=>{
    s.onclick = ()=>{
      // Review is deliberately NOT an inspector agent. Its output is the
      // human-in-the-loop section itself — the full review queue with
      // drill-down cards and the certify action. Rendering an input/output
      // summary above it would just duplicate what the section already shows,
      // so clicking Review takes you straight there.
      if(s.dataset.id==="review"){ hideInspector(); goToVerdict(); return; }
      if(state.artifacts[s.dataset.id]) selectStation(+s.dataset.i);
    };
  });
  if(!passive) setActive(0);
}
const station = i => document.querySelector(`.station[data-i="${i}"]`);
function setActive(i){
  document.querySelectorAll(".station").forEach(s=>s.classList.remove("active"));
  const s = station(i); if(!s) return;
  s.classList.add("active"); s.querySelector(".st-role").textContent = state.railAgents[i].working;
  state.activeIdx = i;
  showWorking(i);
}
// While an agent runs, show WHAT IT IS WORKING ON. The input is known before
// the agent starts (it describes the feed, not the result), so leaving the
// panel empty until the node returned made a slow agent look hung. The output
// pane states what is being produced instead of staying blank.
function showWorking(i){
  const meta = state.railAgents[i]; if(!meta) return;
  const id = meta.id;
  if(state.artifacts[id]) return;          // already finished: keep the real output
  const labels = (state.inputLabels||{})[id];
  if(!labels) return;                      // nothing to show; leave as-is
  $("#inspector").hidden = false;
  document.querySelectorAll(".station").forEach(s=>s.classList.remove("sel"));
  station(i)?.classList.add("sel");
  const rail = (state.railAgents||[]).map(a=>a.id);
  const total = rail.length || 2;
  $("#insp-step").textContent = "Mapping · stage " + (i+1) + " of " + total;
  $("#insp-title").textContent = meta.label;
  $("#insp-sub").textContent = meta.role;
  $("#insp-input").innerHTML = `<ul>${labels.map(l=>`<li>${esc(l)}</li>`).join("")}</ul>`;
  $("#insp-output").innerHTML =
    `<div class="insp-working"><span class="insp-spin"></span>${esc(meta.working)}</div>`;
}
function setDone(i){
  const s = station(i); if(!s) return;
  s.classList.remove("active"); s.classList.add("done");
  s.querySelector(".st-role").textContent = state.railAgents[i].role;
}
/* Passive rails (transformation, validation, reconciliation) have no streaming
   pipeline behind them — they mark progress from the two things the user
   actually does on those tabs: generate the script, then run it. */
function markRail(doneCount, activeIdx){
  document.querySelectorAll(".station").forEach((s, i)=>{
    s.classList.toggle("done", i < doneCount);
    s.classList.toggle("active", i === activeIdx);
    const meta = state.railAgents[i];
    if(meta) s.querySelector(".st-role").textContent =
      (i === activeIdx) ? meta.working : meta.role;
  });
}

function selectStation(i){
  $("#inspector").hidden = false;
  document.querySelectorAll(".station").forEach(s=>s.classList.remove("sel"));
  station(i)?.classList.add("sel"); renderInspector(state.railAgents[i].id);
}
function hideInspector(){ $("#inspector").hidden = true; }
function restoreRail(){
  let last = -1;
  state.railAgents.forEach((a,i)=>{ if(state.artifacts[a.id]){ setDone(i); last = i; } });
  // review has no inspector view, so never restore a selection onto it
  if(last >= 0 && state.railAgents[last].id !== "review"){ selectStation(last); }
  else if(last < 0){
    $("#inspector").hidden = false;
    $("#insp-step").textContent = "Stage";
    $("#insp-title").textContent = "Press Run mapping";
    $("#insp-sub").textContent = "Maps your uploaded artefacts to the target dictionary, validates, and queues exceptions.";
    $("#insp-input").innerHTML = ""; $("#insp-output").innerHTML = "";
  }
}
function goToVerdict(){
  const el = $("#review-section");
  if(el && !el.hidden) el.scrollIntoView({behavior:"smooth", block:"start"});
}

/* ---------- run + streaming + pacing ---------- */
async function run(decisions, fast){
  state.lastDecisions = decisions || null;
  const certifying = !!decisions;
  state.certifying = certifying;
  if(certifying){
    // Certifying replaces the review artifact with the post-certify queue, which
    // is empty by design (the loop has converged). Snapshot what the reviewer
    // actually decided so the certified mapping stays INSPECTABLE and AMENDABLE
    // afterwards — otherwise certifying erased the record of what was signed off
    // and left no way back in.
    state.decidedQueue = (state.artifacts.review && state.artifacts.review.artifact) || null;
    state.decidedBy = Object.assign({}, state.decisions || {});
    const pv = state.artifacts.validation && state.artifacts.validation.artifact;
    if(pv && !state.preCertifyChecks) state.preCertifyChecks = pv.checks;
  }
  // a previous step-mode run may be PARKED awaiting Next — release it and
  // flush its remaining events, or the new run's events jam behind it and
  // the first station spins forever
  state.queue.length = 0;
  if(state.stepResolve){ const r = state.stepResolve; state.stepResolve = null; r(); }
  for(let i = 0; i < 50 && state.draining; i++) await sleep(20);
  $("#run").disabled = true; $("#certify").disabled = true; $("#next").hidden = true;
  if(certifying){
    state.mode = "auto"; state.dwell = 0;
    $("#certify").textContent = "Applying your decisions…";
    const ov = $("#certify-overlay");
    ov.hidden = false;
    $("#certify-status").textContent = "Applying your decisions…";
    $("#certify-substatus").textContent = "Applies your choices to the reviewed mapping, then re-validates — the mapping isn't recomputed.";
    ov.scrollIntoView({behavior:"smooth", block:"center"});
  } else {
    state.mode = fast ? "auto" : state.uiMode;
    state.dwell = fast ? 650 : (state.mode==="step" ? 350 : 1500);
    $("#inspector").hidden = false;
    $("#insp-step").textContent = "Running"; $("#insp-title").textContent = "Pipeline running…";
    $("#insp-sub").textContent = state.mode==="step" ? "Advance one agent at a time." : "Each agent reveals as it completes.";
    $("#insp-input").innerHTML = ""; $("#insp-output").innerHTML = "";
  }

  if(!certifying){
    // a fresh run supersedes anything certified earlier — never let a stale
    // finalSpec become the base of the next certify
    state.finalSpec = null; state.certifiedDone = false; state.decisions = {};
    state.decidedQueue = null; state.decidedBy = null; state.reviewLocked = false;
    state.amending = false; state.preCertifyChecks = null;
    $("#amend").hidden = true; $("#accept-suggested").hidden = false;
    initRail(AGENT_SETS.b);
  }
  const url = certifying ? "/api/mapping/certify" : "/api/mapping/run";
  // Certify sends back the exact spec the reviewer saw, so decisions apply to
  // THAT spec instead of a freshly regenerated one.
  // INCREMENTAL: prefer state.finalSpec — the spec as it stands after the last
  // run OR the last certify. Using the frozen mapping-node artifact instead
  // made every certify replay the ENTIRE decision history from scratch, so
  // revising one item silently reverted every other decision that was not
  // re-sent. Building on the latest spec means each certify applies only the
  // NEW decisions, which is what the reviewer expects.
  const reviewedSpec = certifying
    ? state.finalSpec
      || (state.artifacts.mapping && state.artifacts.mapping.artifact)
      || null : null;
  if(certifying && !reviewedSpec){
    $("#certify-overlay").hidden = true;
    $("#certify").disabled = false; $("#certify").textContent = "Certify mapping →";
    $("#review-sub").innerHTML = `<span class="warn-text">Couldn't find the reviewed mapping to certify — run the mapping again, then certify.</span>`;
    return;
  }
  const body = certifying ? {spec: reviewedSpec, decisions: decisions||null}
             : {decisions: decisions||null};
  state.terminal = false;
  try{
    const res = await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},
      body:JSON.stringify(body)});
    const reader = res.body.getReader(); const dec = new TextDecoder(); let buf="";
    while(true){
      const {value,done} = await reader.read(); if(done) break;
      buf += dec.decode(value,{stream:true}); let idx;
      while((idx=buf.indexOf("\n\n"))>=0){
        const chunk = buf.slice(0,idx); buf = buf.slice(idx+2);
        if(chunk.startsWith("data: ")) enqueue(JSON.parse(chunk.slice(6)));
      }
    }
  }catch(e){ enqueue({type:"error", message:String(e)}); }
  await new Promise(res=>{ const t=setInterval(()=>{
    if(!state.queue.length && !state.draining){ clearInterval(t); res(); } }, 200); });
  if(!state.terminal){
    $("#certify-overlay").hidden = true;
    $("#run").disabled = false; $("#certify").disabled = false; updateRunButton();
    $("#foot-roi").textContent = "The run ended unexpectedly — check the server log. Inputs and knowledge are unaffected; run again.";
  }
}
function enqueue(ev){ state.queue.push(ev); if(!state.draining) drain(); }
async function drain(){
  state.draining = true;
  while(state.queue.length){
    const ev = state.queue.shift();
    if(ev.type==="start"){
      // input labels arrive up front so an agent's INPUT can be shown while it
      // is still working — previously both panes stayed blank for the whole
      // run, which on a slow agent looked like nothing was happening
      // the rail is built (and agent 0 marked active) BEFORE this event lands,
      // so re-render the working view now that the labels are known
      if(ev.inputs){ state.inputLabels = ev.inputs; showWorking(state.activeIdx ?? 0); }
    } else if(ev.type==="node"){
      const i = state.railAgents.findIndex(a=>a.id===ev.node);
      state.artifacts[ev.node] = {input:ev.input, artifact:ev.artifact};
      if(state.certifying){
        const STAGE = {validation: "Validating your decided mapping…",
                       review: "Preparing the certified report…"};
        if(STAGE[ev.node]) $("#certify-status").textContent = STAGE[ev.node];
        continue;
      }
      setActive(i); await sleep(state.dwell);
      setDone(i);
      if(ev.node==="review"){
        hideInspector();          // the review SECTION is this agent's output
      } else {
        selectStation(i);
        if(state.mode==="step" && i < state.railAgents.length-1){
          const nextAgent = state.railAgents[i+1];
          await waitNext(`Next: ${nextAgent.label} &rarr;`);
        }
      }
    } else if(ev.type==="complete"){
      state.terminal = true;
      $("#certify-overlay").hidden = true;
      await sleep(250); finalize(ev);
    } else if(ev.type==="error"){
      state.terminal = true;
      $("#certify-overlay").hidden = true;
      $("#insp-step").textContent = "Stopped";
      $("#insp-title").textContent = "Can't run yet";
      $("#insp-sub").innerHTML = `<span class="warn-text">${ev.message}</span>`;
      $("#run").disabled = false; updateRunButton();
      if(state.certifying){ $("#certify").disabled = false; $("#certify").textContent = "Certify mapping →"; }
    }
  }
  state.draining = false;
}
function waitNext(label){
  const n = $("#next");
  n.innerHTML = label || "Next agent &rarr;";
  n.hidden = false;
  return new Promise(r=>{ state.stepResolve = r; });
}
$("#next").onclick = ()=>{ if(state.stepResolve){ const n=$("#next"); n.hidden = true; n.innerHTML = "Next agent &rarr;"; const r=state.stepResolve; state.stepResolve=null; r(); } };

/* ---------- inspector renderers ---------- */
function renderInspector(id){
  const meta = AGENTS.find(a=>a.id===id); const data = state.artifacts[id];
  if(!meta||!data) return;
  // agents with no RENDER entry (review) have no inspector view by design
  if(!RENDER[id]) return;
  const rail = (state.railAgents||[]).map(a=>a.id);
  const pos = rail.indexOf(id);
  const total = rail.length || 3;
  $("#insp-step").textContent = "Mapping · stage " + ((pos>=0?pos:0)+1) + " of " + total;
  $("#insp-title").textContent = meta.label; $("#insp-sub").textContent = meta.role;
  $("#insp-input").innerHTML = `<ul>${data.input.map(l=>`<li>${l}</li>`).join("")}</ul>`;
  const out = $("#insp-output"); out.classList.remove("fade-in"); void out.offsetWidth; out.classList.add("fade-in");
  out.innerHTML = RENDER[id](data.artifact);
}
const pct = c => Math.round((c||0)*100)+"%";
const bar = c => `<div class="confbar"><i style="width:${pct(c)}"></i></div>`;
const chips = o => Object.entries(o||{}).slice(0,4).map(([k,v])=>`<span class="chip">${k}=${v}</span>`).join("") + (Object.keys(o||{}).length>4?` <span class="chip">+${Object.keys(o).length-4}</span>`:"");

const RENDER = {
  mapping:(s)=>{
    // The two things a reviewer most wants from this table were missing: WHICH
    // FILE each source column came from, and WHAT the transformation does. Both
    // are on the spec (`source_files`, `transformation_note` / `_sql`) and were
    // simply not rendered.
    const files = new Set();
    s.mappings.forEach(m=>(m.source_files||[]).forEach(f=>files.add(f)));
    // only show a file column when more than one file is in play — on a
    // single-source run it would repeat the same name on every row
    const multi = files.size > 1;
    // A cross-file attribute is only trustworthy if you can see HOW its file
    // reaches the driving file. Saying "employer_name · EMPNM · ESCH0009" tells
    // a reviewer the value came from somewhere else but not on what key, which
    // is the one thing that could make it wrong. Surface the join per row.
    const joins = {};
    (s.join_plan||[]).forEach(j=>{ if(j && j.table) joins[j.table] = j; });
    const joinText = j => `${j.table}.${j.on} = ${j.to_table}.${j.to_column}`;
    // the note already ends with "Sourced from joined file X." — drop that once
    // the file is shown structurally, rather than saying it twice
    const ruleOf = m => (m.transformation_note || m.transformation_sql || "")
      .replace(/\s*Sourced from (joined )?file [^.]*\.\s*/i, " ").trim();

    const fileCell = m => {
      const fs_ = m.source_files || [];
      if(!fs_.length) return '<span class="muted">—</span>';
      return fs_.map(f=>{
        const j = joins[f];
        if(!j) return esc(f);
        // joined file: name plus the key it joins on, with the discovery
        // evidence on hover
        return `${esc(f)}<span class="map-join" title="${esc(j.evidence||"")}">`
             + `&#8904; ${esc(joinText(j))}`
             + (j.cardinality?` <span class="chip">${esc(j.cardinality)}</span>`:"")
             + `</span>`;
      }).join(", ");
    };

    // The mapping table shows the DECISION, not the score. Confidence and the
    // gate are decided on different grounds — a model proposal, a close rival,
    // an unmatched code or a multi-field rule all send a mapping to a human
    // regardless of how well it scores — so showing a bare percentage next to
    // the gate read as arbitrary (86% accepted, 88% reviewed). The full
    // reasoning, including the scores, is on the review card where the
    // reviewer actually decides. The certified table below shows no score
    // either, so the two now agree.
    const rows = s.mappings.map(m=>{
      const rule = ruleOf(m);
      return `<tr>
      <td><b>${m.target_attribute}</b></td>
      <td class="mono">${m.source_attributes.join(", ") || '<span class="muted">—</span>'}</td>
      ${multi?`<td class="mono map-file">${fileCell(m)}</td>`:""}
      <td class="map-rule" title="${esc(m.transformation_sql||"")}">${esc(rule)||'<span class="muted">—</span>'}</td>
      <td><span class="chip">${m.cardinality}</span></td>
      <td><span class="badge g-${m.gate}">${m.gate.replace("_"," ")}</span></td></tr>`;
    }).join("");

    const joinSummary = (s.join_plan||[]).length
      ? " · joined on " + (s.join_plan||[]).map(joinText).join("; ") : "";
    return `<div class="muted" style="margin-bottom:10px">${s.stats.mapped}/${s.stats.target_attributes} target attributes mapped`
      + (multi?` · across ${files.size} source files${esc(joinSummary)}`:"")
      + `</div>
      <table class="dt"><tr><th>Target</th><th>← Source</th>${multi?"<th>File</th>":""}`
      + `<th>Transformation</th><th>Card.</th><th>Gate</th></tr>${rows}</table>`;
  },
  validation:(r)=>{
    // Exceptions are GROUPED by the problem they represent, not listed one per
    // line. Three attributes with no source used to repeat the same 12-word
    // label three times, burying the only thing that differed — the attribute
    // name — mid-sentence inside quotes. Grouping states the problem and its
    // consequence once, then lists the attributes as a scannable column.
    const GROUP = {
      // required_present covers TWO different problems; the status tells them
      // apart, and they need different remedies, so they must not be merged
      required_nosource:  {t:"No source field found",
                           n:"A default value is applied at load time."},
      required_nulls:     {t:"Required attribute still empty",
                           n:"Rows are still NULL after the transform runs."},
      reconciliation:     {t:"Values lost in transform",
                           n:"The source held a value that became NULL — usually a legacy code with no target equivalent."},
      crossfield:         {t:"Business rule violated",
                           n:"Rows contradict a rule decoded from the legacy system."},
      key_identified:     {t:"Primary key not identified",
                           n:"No target attribute could be confirmed as the key."},
      key_unique_not_null:{t:"Primary key not unique",
                           n:"Duplicate or null key values in the target."},
      row_count_preserved:{t:"Row count changed",
                           n:"The target row count differs from the source."},
      no_duplicate_targets:{t:"Target written more than once",
                           n:"Two mappings write the same target attribute."},
      source_refs_exist:  {t:"Source column missing",
                           n:"A mapping references a column that is not in the source."},
      transforms_compile: {t:"Transform will not compile",
                           n:"The generated SQL is not valid."},
      transforms_execute: {t:"Transform failed to execute",
                           n:"The SQL compiled but errored against real data."},
      transform_runtime:  {t:"Transform runtime error",
                           n:"The transform raised an error while running."},
    };
    const keyOf = c => {
      const fam = (c.name||"").split(":")[0];
      if(fam === "required_present") return c.status==="fail" ? "required_nulls" : "required_nosource";
      return GROUP[fam] ? fam : (c.category || "other");
    };
    const fmtSamples = s => {
      const seen=new Set(), out=[];
      for(const row of (s||[])){
        const k=JSON.stringify(row); if(seen.has(k)) continue; seen.add(k);
        if(row && typeof row==="object"){
          // a sample whose every value is null carries no information — on a
          // "still empty" check the sample is null BY DEFINITION, so printing
          // "e.g. policy_number=null" only adds noise
          const ent = Object.entries(row).filter(([,v])=> v!==null && v!==undefined && v!=="");
          if(!ent.length) continue;
          out.push(ent.map(([kk,vv])=>`${kk}=${vv}`).join(", "));
        }else{
          if(row===null || row===undefined || row==="") continue;
          out.push(String(row));
        }
        if(out.length>=2) break;
      }
      return out;
    };

    const groupsOf = checks => {
      const buckets = new Map();
      checks.filter(c=>c.status!=="pass").forEach(c=>{
        const k=keyOf(c); if(!buckets.has(k)) buckets.set(k,[]); buckets.get(k).push(c); });
      // failures before warnings, so the blocking problems read first
      return [...buckets.entries()].sort((a,b)=>{
        const f = g => g.some(c=>c.status==="fail") ? 0 : 1;
        return f(a[1]) - f(b[1]);
      });
    };
    const renderGroups = order => order.map(([k, cs])=>{
      const g = GROUP[k] || {t:(k||"Check").replace(/_/g," "), n:""};
      const worstFail = cs.some(c=>c.status==="fail");
      const ic = worstFail ? "✗" : "⚠";
      // some checks are table-level (row count, duplicate targets) and name no
      // attribute — counting those as "attributes" would be plainly wrong
      const named = cs.filter(c=>c.target_attribute).length;
      const noun = named === cs.length
        ? (cs.length===1 ? "attribute" : "attributes")
        : (cs.length===1 ? "issue" : "issues");
      const items = cs.map(c=>{
        // the attribute name leads — it is the thing a reviewer scans for
        const bits = [];
        if(c.offending_rows) bits.push(`${c.offending_rows} row${c.offending_rows===1?"":"s"}`);
        const s = fmtSamples(c.sample);
        if(s.length) bits.push(`e.g. ${s.join("; ")}`);
        if(!c.target_attribute) return `<li><span class="muted">${c.detail}</span></li>`;
        return `<li><span class="mono vchk-attr">${c.target_attribute}</span>`+
               (bits.length?` <span class="muted">${bits.join(" · ")}</span>`:"")+`</li>`;
      }).join("");
      return `<div class="vchk-group">
        <div class="vchk-head"><span class="vchk-ic ${worstFail?"err":"warn"}">${ic}</span>
          <b>${g.t}</b> <span class="vchk-count">${cs.length} ${noun}</span></div>
        ${g.n?`<div class="vchk-note">${g.n}</div>`:""}
        <ul class="vchk-items">${items}</ul>
      </div>`;
    }).join("");

    const ex = renderGroups(groupsOf(r.checks));

    // AUDIT RECORD. Certifying re-runs validation against the decided spec, so
    // findings the reviewer resolved (an unmapped target that now has a default,
    // an edited transform) legitimately disappear — the certified report is all
    // green and says nothing about what was flagged. That erases exactly the
    // history a migration needs to defend. Keep the pre-certification findings
    // visible, marked as resolved, alongside the certified result.
    let resolved = "";
    if(state.certifiedDone && state.preCertifyChecks){
      const stillBad = new Set(r.checks.filter(c=>c.status!=="pass").map(c=>c.name));
      const gone = state.preCertifyChecks.filter(c=>c.status!=="pass" && !stillBad.has(c.name));
      if(gone.length){
        resolved = `<div class="io-label" style="margin-top:16px">`
          + `Resolved at certification — ${gone.length} finding${gone.length===1?"":"s"}</div>`
          + `<div class="muted vchk-note" style="margin-left:0">Flagged before sign-off; `
          + `cleared by the decisions applied.</div>`
          + `<div class="vchk-resolved">${renderGroups(groupsOf(gone))}</div>`;
      }
    }

    return `<div style="margin-bottom:10px"><span class="badge g-${r.verdict==='certified'?'auto_accept':r.verdict==='blocked'?'reject':'review'}">${(VERDICT[r.verdict]||[r.verdict])[0]}</span>
      <span class="muted"> · ${r.stats.passed} passed · ${r.stats.warnings} warning · ${r.stats.failures} failure</span></div>
      <div class="io-label">Checks needing attention</div>
      ${ex||`<div class="muted">All ${r.stats.checks} checks passed.</div>`}
      ${resolved}`;
  },
  // NOTE: there is deliberately no `review` renderer. Review is the
  // human-in-the-loop stage — its output IS the review section below the
  // rail (queue, drill-down cards, certify), not an input/output summary
  // in the inspector. renderInspector() guards on RENDER[id] so a missing
  // entry is a no-op rather than an error.
};

/* ---------- finalize ---------- */
function finalize(ev){
  const summary = ev.summary || ev;
  const v = summary.verdict, ms = summary.mapping_stats;
  const card = $("#verdict-card"); card.dataset.verdict = v;
  $("#verdict-value").textContent = (VERDICT[v]||[v])[0];
  $("#verdict-sub").textContent = (VERDICT[v]||["",""])[1];
  $("#t-mapped").textContent = ms.mapped + "/" + ms.target_attributes;
  $("#t-auto").textContent = ms.auto_accept; $("#t-review").textContent = ms.review; $("#t-reject").textContent = ms.reject;
  // keep the machine's own numbers so the live "to review" tile can be
  // recomputed as the reviewer works without losing the original record
  state.mappingStats = ms;
  state.serverVerdict = v;
  setFootNote("b", `One opaque table → validated mapping in seconds, fully audited. ` +
    `At scale, the same flow turns a multi-week manual mapping effort per table into minutes of review.`);
  $("#run").disabled = false; updateRunButton(); $("#next").hidden = true;

  if(ev.final_spec){ state.finalSpec = ev.final_spec; state.sourceTable = ev.source_table || "EFAS0042"; }
  const wasCertify = !!(state.lastDecisions && Object.keys(state.lastDecisions).length);

  if(v === "certified" && state.finalSpec){
    $("#review-section").hidden = false;
    $("#certify").disabled = true; $("#certify").textContent = "Certified ✓";
    $("#accept-suggested").hidden = true;
    // The queue that came back is authoritative and now empty. Repaint against
    // it and drop the spent decisions — leaving the pre-certify cards on screen
    // meant the next click re-rendered a DIFFERENT queue underneath the user
    // and the item they touched vanished.
    state.certifiedDone = true;
    // Keep the decided cards on screen, showing what was decided, so a reviewer
    // can see what they certified and reopen it. The post-certify queue is
    // empty, so render the snapshot taken before the certify call.
    if(state.decidedQueue){
      state.artifacts.review = {input: (state.artifacts.review||{}).input || [],
                                artifact: state.decidedQueue};
      state.decisions = Object.assign({}, state.decidedBy || {});
    }else{
      state.decisions = {};
    }
    $("#amend").hidden = false;
    renderQueueList("All items resolved — nothing outstanding.");
    setReviewLocked(true);
    $("#review-sub").innerHTML =
      `<span class="ok-text">Certified.</span> These are the decisions that were applied. `
      + `Press “Amend decisions” to change any of them and certify again.`;
    renderFinalMapping(state.finalSpec);
    window.scrollTo({top:document.body.scrollHeight, behavior:"smooth"});
  } else if(wasCertify){
    // certify ran but the result isn't certifiable — say why, don't hide silently
    // renderReview() resets state.decisions and repaints from the NEW queue:
    // the decisions just sent are already baked into state.finalSpec, so only
    // what is still outstanding needs a fresh decision.
    renderReview();
    const blockers = blockingChecks();
    const stillReview = (state.artifacts.review?.artifact?.items||[]).filter(needsDecision).length;
    $("#review-sub").innerHTML = blockers.length
      ? `<span class="warn-text">Not yet certifiable — ${blockers.map(c=>c.detail).join(" · ")} Revise the affected item(s), then certify again.</span>`
      : stillReview
        ? `<span class="warn-text">${stillReview} item(s) still need a decision — resolve them, then certify again.</span>`
        : `<span class="warn-text">The mapping validated as “${(VERDICT[v]||[v])[0]}”. Review the flagged checks below, then certify again.</span>`;
    goToVerdict();
  } else {
    renderReview();
    goToVerdict();
  }
}

function blockingChecks(){
  const checks = state.artifacts.validation?.artifact?.checks || [];
  return checks.filter(c => c.status==="fail" || (c.status==="warn" && c.category!=="completeness"));
}

/* ---------- review queue (per-item human decisions) ---------- */
const esc = s => (s==null?"":String(s)).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
// Items that MUST be resolved before certifying: only true mapping reviews.
// Unmapped targets are non-blocking — they carry an editable load-time default
// (pre-filled with a suggestion) that the reviewer MAY change; if left alone
// they default at load exactly as before, so they never gate certification.
const needsDecision = it => it.kind === "mapping_review";
const isUnmappedDefault = it => it.kind === "unmapped_target";
const reviewItem = attr => state.artifacts.review.artifact.items.find(x=>x.target_attribute===attr);
function decisionLabel(d){
  if(!d) return "";
  if(d.action==="accept") return "✓ Accepted as-is";
  if(d.action==="reject") return "⊘ Excluded — not migrated";
  if(d.action==="edit"){
    if(d._label && /^Default:/.test(d._label)) return "◇ " + d._label;
    return "✎ " + (d._label || "Revised mapping");
  }
  return d.action;
}

function renderReview(){
  const q = state.artifacts.review?.artifact; if(!q) return;
  state.decisions = {};
  state.certifiedDone = false;      // a fresh queue is outstanding again
  $("#review-section").hidden = false;
  $("#final-section").hidden = true;
  $("#accept-suggested").hidden = false;
  $("#certify-overlay").hidden = true;
  $("#certify").textContent = "Certify mapping →";
  const need = q.items.filter(needsDecision).length;
  $("#review-title").textContent = `Review queue — ${need} to resolve`;
  $("#review-sub").textContent = `${q.stats.auto_accepted} mappings auto-accepted and flowed through. Accept, revise, or exclude each item below — then certify.`;
  rerenderList();
}
// Single place that paints #review-list. ALWAYS renders the queue currently in
// state.artifacts.review — never a remembered one — so what is on screen and
// what the server considers outstanding cannot drift apart.
// A certified queue stays VISIBLE but read-only: the reviewer can see exactly
// what was signed off and drill into the reasoning, without being able to
// change it by accident. "Amend decisions" unlocks it deliberately.
function setReviewLocked(locked){
  state.reviewLocked = locked;
  const list = $("#review-list"); if(!list) return;
  list.classList.toggle("locked", !!locked);
  list.querySelectorAll("[data-act]").forEach(b=> b.disabled = !!locked);
  $("#accept-suggested").hidden = !!locked;
}

function amendDecisions(){
  state.certifiedDone = false;
  state.reviewLocked = false;
  $("#amend").hidden = true;
  $("#accept-suggested").hidden = false;
  state.amending = true;
  $("#certify").disabled = false;
  // decisions are applied on top of the CERTIFIED spec (state.finalSpec), so
  // changing one item does not discard the rest
  renderQueueList();
  setReviewLocked(false);
  $("#review-sub").innerHTML =
    `<span class="warn-text">Amending a certified mapping.</span> `
    + `Change any decision below, then re-certify — the mapping stays certified until you do.`;
  $("#review-section").scrollIntoView({behavior:"smooth", block:"start"});
}

function renderQueueList(emptyMsg){
  const q = state.artifacts.review && state.artifacts.review.artifact;
  const list = $("#review-list");
  if(!q){ list.innerHTML = ""; return; }
  list.innerHTML = q.items.length
    ? q.items.map(card).join("")
    : `<div class="empty-queue">${esc(emptyMsg || "Nothing outstanding.")}</div>`;
  list.querySelectorAll(".toggle").forEach(t=> t.onclick = ()=>{ t.classList.toggle("closed"); t.closest(".card").querySelector(".lineage").classList.toggle("open"); });
  list.querySelectorAll("[data-act]").forEach(b=> b.onclick = ()=> handleAct(b));
  if(state.reviewLocked){
    list.classList.add("locked");
    list.querySelectorAll("[data-act]").forEach(b=> b.disabled = true);
  }
  updateProgress();
}
function rerenderList(){ renderQueueList(); }
function updateProgress(){
  const q = state.artifacts.review && state.artifacts.review.artifact;
  if(!q) return;
  const need = q.items.filter(needsDecision);
  const done = need.filter(it=> state.decisions[it.target_attribute]).length;
  $("#review-progress").textContent = need.length
    ? `${done} of ${need.length} resolved` : "nothing outstanding";
  refreshVerdict(need.length, done);
  if(state.certifiedDone){          // settled: the button must stay settled
    $("#certify").disabled = true; $("#certify").textContent = "Certified ✓";
    return;
  }
  const all = done === need.length;
  $("#certify").disabled = !all;
  $("#certify").textContent = all
    ? (state.amending ? "Re-certify mapping →" : "Certify mapping →")
    : `Resolve ${need.length-done} more to certify`;
}

// The verdict card used to be written ONCE, from the server's snapshot at the
// end of the run, and never moved again. So a reviewer could resolve every
// item — or press "Accept all suggested" — and the largest number on screen
// still read "13 to review", which made a working action look broken and made
// the card assert something untrue about the current state.
//
// The tile now counts what is genuinely still OUTSTANDING, and the verdict
// gains an explicit local state: decisions are made in the browser and are not
// submitted until Certify, so the card says "Ready to certify" — never
// "Certified", which only the server can declare.
function refreshVerdict(need, done){
  const card = $("#verdict-card"); if(!card) return;
  const outstanding = Math.max(0, need - done);
  if(state.certifiedDone || state.serverVerdict === "certified"){
    $("#t-review").textContent = state.mappingStats ? 0 : "—";
    return;                                    // the server's verdict stands
  }
  $("#t-review").textContent = outstanding;
  if(need && outstanding === 0){
    card.dataset.verdict = "ready";
    $("#verdict-value").textContent = "Ready to certify";
    $("#verdict-sub").textContent =
      `${done} decision${done===1?"":"s"} made — not yet submitted. Press “Certify mapping”.`;
  }else{
    const v = state.serverVerdict || "needs_review";
    card.dataset.verdict = v;
    $("#verdict-value").textContent = (VERDICT[v]||[v])[0];
    $("#verdict-sub").textContent = done
      ? `${done} of ${need} resolved — ${outstanding} still awaiting a decision.`
      : (VERDICT[v]||["",""])[1];
  }
}
function setDecision(attr, d){ state.decisions[attr]=d; rerenderList(); }
function clearDecision(attr){ delete state.decisions[attr]; rerenderList(); }

function handleAct(b){
  const cardEl = b.closest(".card"); const attr = cardEl.dataset.attr; const act = b.dataset.act;
  if(act==="accept")   setDecision(attr,{action:"accept"});
  else if(act==="exclude") setDecision(attr,{action:"reject"});
  else if(act==="suggest") setDecision(attr,{action:"edit",transformation_sql:b.dataset.sql,_label:"Accepted suggested fix"});
  else if(act==="alt"){
    const it = reviewItem(attr); const from = it.source_attributes[0]; const to = b.dataset.src;
    const sql = (it.transformation_sql||"").split(from).join(to);
    setDecision(attr,{action:"edit",source_attributes:[to],transformation_sql:sql,_label:`Source → ${to}`});
  }
  else if(act==="edit"){ cardEl.querySelector(".editor").hidden = false; b.hidden = true; cardEl.querySelector("textarea").focus(); }
  else if(act==="cancel"){ cardEl.querySelector(".editor").hidden = true; rerenderList(); }
  else if(act==="save"){
    const sql = cardEl.querySelector("textarea").value.trim();
    if(sql) setDecision(attr,{action:"edit",transformation_sql:sql,_label:"Revised mapping"});
  }
  else if(act==="change") clearDecision(attr);
  else if(act==="set-default"){
    const it = reviewItem(attr);
    const input = cardEl.querySelector(".default-input");
    const raw = input ? input.value : "";
    const sql = defaultToSql(raw, it.target_type);
    const shown = sql === "NULL" ? "NULL" : (input ? input.value.trim() : "");
    setDecision(attr, {action:"edit", transformation_sql:sql,
      _label: sql==="NULL" ? "Default: NULL" : `Default: ${shown}`});
  }
  else if(act==="leave-null"){
    setDecision(attr, {action:"edit", transformation_sql:"NULL", _label:"Default: NULL"});
  }
}

const GATE_LABEL = { review: "Decision needed", reject: "Map or exclude" };
function situation(it){
  if(it.kind === "unmapped_target")
    return "No source field feeds this attribute — set a value to apply at load.";
  if(it.ambiguous)
    return "Two source fields fit equally well — confirm which one is correct.";
  if(it.gate === "reject")
    return "We couldn't confirm this mapping from the data or legacy code — map it yourself, or exclude it from the migration.";
  if(/unmapped|no exact target value/i.test(it.reason || ""))
    return "Some source values aren't covered by the mapping — apply the suggested fix, or revise it.";
  return "This attribute is derived by a rule — confirm the proposed logic, or write your own.";
}

function card(it){
  const attr = it.target_attribute;
  const unmapped = it.kind === "unmapped_target";
  const mapping = it.kind === "mapping_review";
  const d = state.decisions[attr];
  const ln = mapping ? `
    <div class="lineage">
      ${it.data_patterns?.length?`<div class="ln"><b>Data pattern (analyst)</b>${it.data_patterns.join("; ")}</div>`:""}
      ${it.upstream_evidence?.length?`<div class="ln"><b>Evidence (legacy expert)</b>${it.upstream_evidence.slice(0,4).join("; ")}</div>`:""}
      ${it.validator_exceptions?.length?`<div class="ln"><b>Validator</b>${it.validator_exceptions.join("; ")}</div>`:""}
      ${it.offending_rows?.length?`<div class="ln"><b>Offending rows</b>${it.offending_rows.map(r=>JSON.stringify(r)).join(", ")}</div>`:""}
    </div>` : "";

  let actions;
  if(unmapped){
    actions = unmappedActions(it, d);
  } else if(d){
    actions = `<div class="row resolved-row"><span class="resolved-chip ${d.action}">${decisionLabel(d)}</span>
      ${d.transformation_sql?`<code class="mono dchip">${esc(d.transformation_sql)}</code>`:""}
      <button class="btn-sm" data-act="change">Change</button></div>`;
  } else {
    const alts = (it.alternatives||[]).map(a=>`<button class="btn-sm alt" data-act="alt" data-src="${a.source}">Use ${a.source}</button>`).join("");
    const suggest = it.suggested_sql ? `<button class="btn-sm accept" data-act="suggest" data-sql="${esc(it.suggested_sql)}">Accept suggested fix</button>` : "";
    const acceptAsIs = it.gate!=="reject" ? `<button class="btn-sm accept" data-act="accept">Accept as-is</button>` : "";
    const exclude = `<button class="btn-sm exclude" data-act="exclude">${it.gate==="reject"?"Exclude — don't migrate":"Exclude"}</button>`;
    actions = `<div class="row wrap">${suggest}${acceptAsIs}${alts}<button class="btn-sm" data-act="edit">✎ Write mapping</button>${exclude}</div>
      <div class="editor" hidden>
        <div class="io-label">Revised transform SQL</div>
        <textarea spellcheck="false">${esc(it.suggested_sql||it.transformation_sql||"")}</textarea>
        <div class="row"><button class="btn-sm accept" data-act="save">Save mapping</button>
        <button class="btn-sm" data-act="cancel">Cancel</button></div>
      </div>`;
  }

  return `<div class="card k-${it.kind} ${d?'resolved':''}" data-gate="${it.gate}" data-attr="${attr}">
    <h3>${attr} <span class="badge g-${it.gate}" style="float:right">${GATE_LABEL[it.gate]||it.gate.replace("_"," ")}</span></h3>
    <p class="situation">${situation(it)}</p>
    <p class="why"><b>Detail:</b> ${it.reason.replace(/\|/g," · ")}</p>
    ${it.source_business_names?.length?`<div class="kv"><b>Source field:</b> ${it.source_attributes.map((a,i)=>a+" ("+(it.source_business_names[i]||a)+")").join(", ")}</div>`:""}
    ${it.alternatives?.length?`<div class="kv alt"><b>⇄ Competing source${it.alternatives.length>1?"s":""}:</b> ${it.alternatives.map(a=>a.source+" ("+(a.business_name||a.source)+")").join(", ")} — equally plausible, you must choose</div>`:""}
    ${it.transformation_sql?`<div class="kv rule-label"><b>Transformation rule</b></div><div class="kv mono" style="color:var(--muted);font-size:11px">${esc(it.transformation_sql)}</div>`:""}
    ${it.suggested_resolution && !unmapped?`<div class="sugg"><b>What to do:</b> ${it.suggested_resolution}</div>`:""}
    ${mapping?`<span class="toggle closed">Show reasoning</span>`:""}
    ${ln}
    ${actions}
  </div>`;
}

/* ---------- unmapped-target default editor ---------- */
// The target has no source field. Offer a load-time default: a type-appropriate
// value is pre-filled (server-suggested), which the reviewer accepts, changes,
// or overrides with "Leave NULL". Resolved state shows the chosen literal.
function unmappedActions(it, d){
  if(d){
    return `<div class="row resolved-row"><span class="resolved-chip ${d.action}">${decisionLabel(d)}</span>
      ${d.transformation_sql?`<code class="mono dchip">${esc(d.transformation_sql)}</code>`:""}
      <button class="btn-sm" data-act="change">Change</button></div>`;
  }
  const t = it.target_type ? `<span class="chip mono">${esc(it.target_type)}</span>` : "";
  const hasSuggestion = it.suggested_default != null && it.suggested_default !== "";
  const prefill = hasSuggestion ? it.suggested_default : "";
  const hint = hasSuggestion
    ? `Suggested default for this ${it.target_type||"field"} — accept it, or type your own.`
    : `No safe default to suggest for this ${it.target_type||"field"} — enter a value to load, or leave it NULL.`;
  return `<div class="default-editor">
      <div class="io-label" style="display:flex;align-items:center;gap:8px">Load-time default ${t}</div>
      <p class="muted" style="margin:2px 0 8px">${hint}</p>
      <div class="row wrap default-row">
        <input class="default-input" type="text" spellcheck="false"
               placeholder="value applied to every row" value="${esc(prefill)}">
        <button class="btn-sm accept" data-act="set-default">${hasSuggestion?"Use this default":"Set default"}</button>
        <button class="btn-sm" data-act="leave-null">Leave NULL</button>
      </div>
    </div>`;
}

// Turn a plain user-entered value into a safe SQL literal for the given type.
function defaultToSql(value, ttype){
  const v = (value==null?"":String(value)).trim();
  if(v === "" ) return "NULL";
  if(/^null$/i.test(v)) return "NULL";
  if(ttype === "boolean"){
    if(/^(true|t|1|y|yes)$/i.test(v)) return "true";
    if(/^(false|f|0|n|no)$/i.test(v)) return "false";
    return "NULL";
  }
  if(["decimal","number","numeric","integer"].includes(ttype)){
    return /^-?\d+(\.\d+)?$/.test(v) ? v : "NULL";
  }
  // string / enum / date / timestamp -> quoted literal (dates load as text and
  // are cast by the target schema); escape single quotes for SQL safety.
  return "'" + v.replace(/'/g, "''") + "'";
}

/* ---------- bulk + certify ---------- */
const decisionFor = it => {
  if(it.kind === "unmapped_target"){
    const sql = it.suggested_sql || "NULL";
    const shown = sql === "NULL" ? "NULL" : (it.suggested_default!=null ? it.suggested_default : sql);
    return {action:"edit", transformation_sql:sql, _label:`Default: ${shown}`};
  }
  return it.gate==="reject" ? {action:"reject"}
    : it.suggested_sql ? {action:"edit",transformation_sql:it.suggested_sql,_label:"Accepted suggested fix"}
    : {action:"accept"};
};
$("#amend").onclick = amendDecisions;
$("#accept-suggested").onclick = ()=>{
  const items = state.artifacts.review.artifact.items.filter(needsDecision);
  items.forEach(it=> state.decisions[it.target_attribute]=decisionFor(it));
  rerenderList();
  // this action is LOCAL ONLY (nothing sent to the server yet) -- make that
  // unmistakable: scroll to where the cards visibly flipped to "resolved",
  // and draw the eye to Certify, which is the button that actually submits.
  $("#review-list").scrollIntoView({behavior:"smooth", block:"start"});
  const btn = $("#certify");
  btn.classList.remove("btn-flash"); void btn.offsetWidth; btn.classList.add("btn-flash");
};
$("#certify").onclick = ()=>{ if(!$("#certify").disabled) run(state.decisions, true); };

/* ---------- final consolidated mapping + downloads ---------- */
function resolutionOf(m){
  if(m.gate==="reject") return ["excluded","g-reject"];
  // a promoted unmapped target has no source column — it is a load-time
  // default, not a mapping the matcher earned
  if(!(m.source_attributes||[]).length) return ["defaulted at load","g-review"];
  const r = m.rationale||"";
  if(/edited by reviewer/.test(r)) return ["revised","g-review"];
  if(/accepted by reviewer/.test(r)) return ["accepted","g-auto_accept"];
  return ["auto-accepted","g-auto_accept"];
}
function renderFinalMapping(spec){
  const multi = (spec.source_tables||[]).length > 1;
  const fileCell = m => {
    if(!multi) return "";
    const files = m.source_files||[];
    const cross = files.length > 1;
    const joined = files.length && spec.source_tables && files.some(f=>f!==spec.source_tables[0]);
    return `<td>${files.map(f=>`<span class="chip mono">${f}</span>`).join(" ")||"—"}${
      cross?` <span class="badge g-review" title="combines columns from more than one source file via the discovered join">cross-file</span>`
      :joined?` <span class="badge g-auto_accept" title="sourced from a joined file via the discovered relationship">joined</span>`:""}</td>`;
  };
  const rows = spec.mappings.map(m=>{
    const [label,cls] = resolutionOf(m);
    const src = (m.source_attributes||[]).join(", ") || "—";
    const sql = m.gate==="reject" ? "<span style='color:var(--faint)'>— not migrated —</span>" : esc(m.transformation_sql||"");
    return `<tr class="${m.gate==='reject'?'excluded':''}"><td class="mono">${m.target_attribute}</td>
      <td class="mono">${src}</td>${fileCell(m)}<td class="mono" style="font-size:12px">${sql}</td>
      <td><span class="badge ${cls}">${label}</span></td></tr>`;
  }).join("");
  const dflt = (spec.unmapped_target||[]).map(u=>`<tr><td class="mono">${u.attribute}</td><td class="mono">—</td>${multi?"<td>—</td>":""}
      <td style="color:var(--faint)">default at load</td><td><span class="badge g-review">defaulted</span></td></tr>`).join("");
  const migrated = spec.mappings.filter(m=>m.gate!=="reject").length;
  const excluded = spec.mappings.filter(m=>m.gate==="reject").length;
  const nCross = (spec.stats||{}).cross_file_mappings || 0;
  $("#final-section").hidden = false;
  $("#final-sub").textContent = `${migrated} attribute(s) migrating, ${excluded} excluded, ${(spec.unmapped_target||[]).length} defaulted at load.`
    + (multi?` Sources: ${spec.source_tables.join(" + ")}${nCross?` · ${nCross} mapping(s) drawn beyond the primary file`:""}.`:"")
    + ` Download the certified mapping below.`;
  $("#final-table").innerHTML = `<table class="dt"><thead><tr><th>Target</th><th>Source</th>${multi?"<th>Source file</th>":""}<th>Transform</th><th>Resolution</th></tr></thead>
    <tbody>${rows}${dflt}</tbody></table>`;
}

function buildSQL(spec){
  const live = spec.mappings.filter(m=>m.gate!=="reject");
  const cols = live.map(m=>`  ${m.transformation_sql} AS ${m.target_attribute}`).join(",\n");
  const excl = spec.mappings.filter(m=>m.gate==="reject")
    .map(m=>`--   ${m.target_attribute}  (excluded — source ${m.source_attributes.join(",")||"none"}, pending manual handling)`).join("\n");
  const dflt = (spec.unmapped_target||[]).map(u=>`--   ${u.attribute}  (defaulted at load)`).join("\n");
  return `-- Certified source → target mapping for "${spec.target_table}"\n`+
    `-- generated by datamap on ${new Date().toISOString()}\n`+
    (excl?`-- Excluded:\n${excl}\n`:"")+(dflt?`-- Defaulted at load:\n${dflt}\n`:"")+`\n`+
    `CREATE TABLE ${spec.target_table} AS\nSELECT\n${cols}\nFROM ${state.sourceTable};\n`;
}
function buildMD(spec){
  const head = `# Certified mapping — ${spec.target_table}\n\nSource table: \`${state.sourceTable}\`  ·  generated ${new Date().toISOString()}\n\n`+
    `| Target | Source | Cardinality | Transform | Resolution |\n|---|---|---|---|---|\n`;
  const body = spec.mappings.map(m=>{
    const [label] = resolutionOf(m);
    const sql = m.gate==="reject" ? "— not migrated —" : (m.transformation_sql||"").replace(/\|/g,"\\|");
    return `| ${m.target_attribute} | ${(m.source_attributes||[]).join(", ")||"—"} | ${m.cardinality} | \`${sql}\` | ${label} |`;
  }).join("\n");
  const dflt = (spec.unmapped_target||[]).map(u=>`| ${u.attribute} | — | — | default at load | defaulted |`).join("\n");
  return head + body + (dflt?"\n"+dflt:"") + "\n";
}
function download(name, text, mime){
  const blob = new Blob([text],{type:mime}); const url = URL.createObjectURL(blob);
  const a = document.createElement("a"); a.href=url; a.download=name; document.body.appendChild(a); a.click();
  a.remove(); setTimeout(()=>URL.revokeObjectURL(url), 1000);
}
document.querySelectorAll(".dl").forEach(b=> b.onclick = ()=>{
  const s = state.finalSpec; if(!s) return; const t = s.target_table;
  if(b.dataset.fmt==="json") download(`${t}_mapping.json`, JSON.stringify(s,null,2), "application/json");
  else if(b.dataset.fmt==="sql") download(`${t}_mapping.sql`, buildSQL(s), "text/plain");
  else download(`${t}_mapping.md`, buildMD(s), "text/markdown");
});

/* ---------- file content viewer (modal) ---------- */
async function openRaw(id){
  $("#modal-title").textContent = "Loading…";
  $("#modal-body").textContent = "";
  $("#modal").hidden = false;
  try{
    const res = await fetch(`/api/raw?id=${encodeURIComponent(id)}`);
    if(!res.ok){
      $("#modal-title").textContent = "Couldn't open file";
      $("#modal-body").textContent = `The server returned ${res.status}. If you just updated the app, restart the server (uvicorn) and reload the page.`;
      return;
    }
    const r = await res.json();
    if(!r || !("content" in r)){
      $("#modal-title").textContent = "Couldn't open file";
      $("#modal-body").textContent = "Unexpected response from the server — restart the server (uvicorn) and reload the page.";
      return;
    }
    $("#modal-title").textContent = r.name || "file";
    $("#modal-body").textContent = (r.content && r.content.length) ? r.content : "(empty file)";
  }catch(e){
    $("#modal-title").textContent = "Couldn't open file";
    $("#modal-body").textContent = String(e);
  }
}
$("#modal-close").onclick = ()=> $("#modal").hidden = true;
$("#modal").onclick = e => { if(e.target.id==="modal") $("#modal").hidden = true; };

/* ---------- mode toggle + run ---------- */
document.querySelectorAll(".seg-btn").forEach(b=> b.onclick = ()=>{
  document.querySelectorAll(".seg-btn").forEach(x=>x.classList.remove("active"));
  b.classList.add("active"); state.uiMode = b.dataset.mode;
});
$("#run").onclick = ()=> run(null, false);

/* ---------- tabs ---------- */
/* The footer line is TAB-SCOPED. It carried a mapping-throughput claim that
   stayed on screen under the validation and reconciliation reports, where it
   was simply not what the reader was looking at. */
function setFootNote(tab, text){
  FOOT_NOTES[tab] = text;
  const el = document.querySelector("#foot-roi");
  if (el && state.tab === tab) el.textContent = text;
}
const FOOT_NOTES = {};

function showTab(tab){
  state.tab = tab;
  document.querySelectorAll(".tab-btn").forEach(b=> b.classList.toggle("active", b.dataset.tab===tab));
  document.querySelectorAll(".tabsec").forEach(s=> s.hidden = !s.classList.contains("tab-"+tab));
  const foot = document.querySelector("#foot-roi");
  if (foot) foot.textContent = FOOT_NOTES[tab] || "";
  // val/rec/t drive no header Run button — val and rec are read-only views of
  // the last mapping run, t drives its own Run button
  $("#run").hidden = (tab==="val" || tab==="rec" || tab==="t");
  $("#seg").style.visibility = (tab==="val" || tab==="rec" || tab==="t") ? "hidden" : "visible";
  if(tab==="b"){ initRail(AGENT_SETS.b, true); restoreRail(); }
  if(tab==="t"){ initRail(AGENT_SETS.t, true, true); hideInspector(); }
  if(tab==="val"){ initRail(AGENT_SETS.val, true, true); hideInspector(); }
  if(tab==="rec"){ initRail(AGENT_SETS.rec, true, true); hideInspector(); }

  if(tab==="b"){
    $("#review-section").hidden = !(state.artifacts.review && state.artifacts.review.artifact);
    renderMapPanel();
  }
  if(tab==="val") renderValidationTab();
  if(tab==="rec") renderReconciliationTab();
  if(tab==="t" && window.mountTransform) window.mountTransform();
  updateRunButton();
}
document.querySelectorAll(".tab-btn").forEach(b=> b.onclick = ()=> showTab(b.dataset.tab));

function updateRunButton(){
  $("#run").textContent = RUN_LABEL[state.tab] || "Run";
  const m = state.inputs || {};
  $("#run").disabled = !(m.sources && m.sources.length
                        && m.enricheds && m.enricheds.length
                        && m.targets && m.targets.length);
}
/* ============================================================
   MAPPING WORKSPACE INPUT SCREEN
   Every artefact the mapping agent (and the validation it triggers) reads,
   uploaded manually here instead of being computed by an upstream flow:
     A. enriched source dictionary (required)  -> `enriched`
     B. target dictionary (required)           -> `target_dict`
     C. source data (required)                 -> `warehouse` + `source_table`
     D. LLM assistance (status only)           -> optional cross-vocabulary
                                                   recovery in the mapping agent
   The source insight validation uses for its key-integrity / crossfield
   checks is NOT collected here: the server derives it from C (the source
   data) and caches it, so there is no artefact for the user to supply.
   plus a preflight readiness strip. Every field is served by an endpoint
   that already exists (/api/inputs, /api/mode, /api/raw) — no bespoke
   backend surface needed for the panel itself.
   ============================================================ */
// state.inputs.sources/enricheds/... only carry {id, name} (see
// api/server.py:inputs_manifest) — the staged table name is always the
// filename stem, same convention the transform-tab module uses below.
function mapTable(f){ return (f.name || "").replace(/\.[^.]+$/, ""); }

async function renderMapEnrichedBlock(){
  const el = $("#map-enriched-preview"); if(!el) return;
  const m = state.inputs || {};
  const enr = (m.enricheds||[]).find(e=>e.id===m.active_enriched) || (m.enricheds||[])[0];
  if(!enr){ el.innerHTML = ""; return; }
  state.mapEnrichedCache = state.mapEnrichedCache || {};
  let parsed = state.mapEnrichedCache[enr.id];
  if(!parsed){
    try{
      const r = await (await fetch(`/api/raw?id=${enr.id}`)).json();
      parsed = JSON.parse(r.content);
      state.mapEnrichedCache[enr.id] = parsed;
    }catch(e){ el.innerHTML = `<span class="warn-text">Couldn't parse ${esc(enr.name)} as JSON.</span>`; return; }
  }
  const cols = parsed.columns || [];
  const nCalc = cols.filter(c=> c && c.derivation_cobol).length;
  const nDecode = cols.filter(c=> c && c.value_decode && Object.keys(c.value_decode).length).length;
  const table = parsed.table || "?";
  // sanity check: the mapping agent validates SQL and pulls sample values
  // against the STAGED SOURCE table using the column names in this
  // dictionary — a table-name mismatch is the single most likely upload
  // error, so surface it before the run fails opaquely.
  const src = (m.sources||[]).find(s=>s.id===m.active_source) || (m.sources||[])[0];
  const srcTable = src ? mapTable(src) : null;
  const mismatch = srcTable && table !== "?" && table !== srcTable;
  el.innerHTML = `table: <b class="mono">${esc(table)}</b> &middot; ${cols.length} column(s)` +
    ` &middot; ${nCalc} calculated &middot; ${nDecode} with value decodes` +
    (mismatch ? `<br><span class="warn-text">⚠ declares table "${esc(table)}" but the loaded ` +
      `source is "${esc(srcTable)}" — the column NAMES still need to match the source CSV's ` +
      `header row for the mapping agent's SQL to run.</span>` : "");
}

async function renderMapTargetPreview(){
  const el = $("#map-target-preview"); if(!el) return;
  const m = state.inputs || {};
  const tgt = (m.targets||[]).find(t=>t.id===m.active_target) || (m.targets||[])[0];
  if(!tgt){ el.innerHTML = ""; return; }
  state.mapTargetCache = state.mapTargetCache || {};
  let parsed = state.mapTargetCache[tgt.id];
  if(!parsed){
    try{
      const r = await (await fetch(`/api/raw?id=${tgt.id}`)).json();
      parsed = JSON.parse(r.content);
      state.mapTargetCache[tgt.id] = parsed;
    }catch(e){ el.innerHTML = `<span class="warn-text">Couldn't parse ${esc(tgt.name)} as JSON.</span>`; return; }
  }
  const attrs = parsed.attributes || parsed.columns || parsed.fields || [];
  const nEnum = attrs.filter(a=> a && (a.type==="enum" || a.allowed_values)).length;
  const nNull = attrs.filter(a=> a && a.nullable).length;
  const table = parsed.table || parsed.target_table || parsed.name || "target";
  el.innerHTML = `table: <b class="mono">${esc(table)}</b> &middot; ${attrs.length} attribute(s)` +
    ` &middot; ${nEnum} enum &middot; ${nNull} nullable`;
}

function renderMapPreflight(){
  const el = $("#map-preflight"); if(!el) return;
  const m = state.inputs || {};
  const checks = [
    {ok: !!(m.enricheds && m.enricheds.length),
      label: (m.enricheds && m.enricheds.length) ? "Enriched dictionary loaded" : "Enriched dictionary missing"},
    {ok: !!(m.targets && m.targets.length),
      label: (m.targets && m.targets.length) ? "Target dictionary loaded" : "Target dictionary missing"},
    {ok: !!(m.sources && m.sources.length),
      label: (m.sources && m.sources.length) ? `Source: ${m.sources.map(mapTable).join(" + ")}` : "No source file loaded"},
    {ok: !!(state.llmMode && state.llmMode.live),
      label: (state.llmMode && state.llmMode.live) ? "LLM live" : "LLM offline (rule-based only)"},
  ];
  el.innerHTML = checks.map(c=>
    `<span class="map-check ${c.ok?"ok":"warn"}">${c.ok?"✓":"⚠"} ${c.label}</span>`).join("") +
    `<span class="muted map-check-hint">press <b>Run mapping</b> above when ready</span>`;
}

function renderMapPanel(){
  renderMapEnrichedBlock();
  renderMapTargetPreview();
  renderMapPreflight();
}

/* Validation Workspace (tab 3) and Reconciliation Workspace (tab 4) are their
   own generate/run modules, further down (mountValidate / mountReconcile) —
   both check the DELIVERED transform output (tab 2's CSV), not the mapping-
   time report. See those modules for the check-card rendering. */


const modeChip = $("#mode");
modeChip.style.cursor = "pointer";
modeChip.title = "click to run the LLM connectivity check";
modeChip.onclick = async ()=>{
  const was = modeChip.textContent; modeChip.textContent = "checking…";
  try{
    const r = await (await fetch("/api/llm/check")).json();
    const txt = r.steps.map(s=>`${s.ok?"✓":"✗"} ${s.step}: ${s.detail}` + (s.ok||!s.hint?"":`\n   → ${s.hint}`)).join("\n\n");
    alert("LLM connectivity check\n\n"+txt);
  } finally { modeChip.textContent = was; }
};

$("#ip-reset").onclick = async ()=>{
  if(!confirm("Reset the workspace?\n\nThis deletes ALL uploaded artefacts, for a fresh start.")) return;
  await fetch("/api/inputs/reset",{method:"POST"});
  state.artifacts = {};
  state.finalSpec = null; state.certifiedDone = false; state.decisions = {};
  state.decidedQueue = null; state.decidedBy = null; state.reviewLocked = false;
  $("#final-section").hidden = true;
  hideInspector();
  $("#review-section").hidden = true;
  $("#certify-overlay").hidden = true;
  await loadInputs();
  initRail(AGENT_SETS.b); restoreRail();
  $("#foot-roi").textContent = "";
};

showTab("b");

/* ---------- input selection ---------- */
function renderInputs(m){
  state.inputs = m;
  const chip = (role, f) => {
    return `<span class="ip-chip" data-id="${f.id}">` +
      `<button class="ip-name ip-view" data-id="${f.id}" title="view contents">${f.name}</button>` +
      `<button class="ip-rm" data-role="${role}" data-id="${f.id}" title="remove">×</button></span>`;
  };
  const render = (elId, role, list) => {
    const placeholder = {
      source:   "Upload source data file",
      target:   "Upload target data dictionary",
      enriched: "Upload one dictionary per source file",
    }[role];
    const el = $(elId); if(!el) return;
    el.innerHTML = (list||[]).length ? list.map(f=>chip(role,f)).join("")
                                     : `<span class="ip-empty">${placeholder}</span>`;
  };
  render("#ip-source","source",m.sources);
  render("#ip-target","target",m.targets);
  render("#ip-enriched","enriched",m.enricheds);

  document.querySelectorAll('.ip-view').forEach(b=> b.onclick = ()=> openRaw(b.dataset.id));
  document.querySelectorAll('.ip-rm').forEach(b=> b.onclick = (e)=>{ e.preventDefault(); removeInput(b.dataset.role, b.dataset.id); });

  if(state.tab==="b") renderMapPanel();
  updateRunButton();
}
async function loadInputs(){ try{ renderInputs(await (await fetch("/api/inputs")).json()); }catch(e){} }
async function uploadFiles(fileList, role){
  if(!fileList || !fileList.length) return;
  const fd = new FormData(); [...fileList].forEach(f=> fd.append("files", f));
  if(role) fd.append("role", role);
  try{
    const m = await (await fetch("/api/inputs/upload",{method:"POST",body:fd})).json();
    renderInputs(m);
  }catch(e){ loadInputs(); }
}
async function removeInput(role, id){
  renderInputs(await (await fetch("/api/inputs/remove",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({role,id})})).json());
}
document.querySelectorAll(".ip-add").forEach(inp=> inp.onchange = (e)=>{
  uploadFiles(e.target.files, inp.dataset.role); e.target.value = ""; });
const panel = $("#target-panel");
["dragover","dragenter"].forEach(ev=> panel.addEventListener(ev, e=>{ e.preventDefault(); panel.classList.add("drop"); }));
["dragleave","drop"].forEach(ev=> panel.addEventListener(ev, e=>{ e.preventDefault(); panel.classList.remove("drop"); }));
panel.addEventListener("drop", e=> uploadFiles(e.dataTransfer.files));
loadInputs();

(async ()=>{
  try{
    const m = await (await fetch("/api/mode")).json();
    state.llmMode = m;
    const pill = $("#mode");
    if(m.live){ pill.textContent = m.label || "live"; pill.classList.add("live"); }
    else { pill.textContent = "rule-based"; }
    if(state.tab==="b"){  renderMapPreflight(); }
  }catch(e){}
})();

/* ============================================================
   TAB 4 · TRANSFORMATION WORKSPACE (additive module)
   Consumes the certified mapping spec (state.finalSpec, or the
   mapping agent's artifact), generates an ETL, runs it on the
   server against the loaded source files, shows the target grid
   and exports CSV. Self-contained; never mutates tabs 1–3 state.
   ============================================================ */
(function(){
  "use strict";
  const $ = s => document.querySelector(s);
  const T = { spec:null, csv:null, columns:null, rows:null, target:null,
              code:null, imported:null, importedName:"", generated:false };
  // exposed so the Validation (tab 3) and Reconciliation (tab 4) workspaces can
  // read the delivered transform output (T.csv) without duplicating tab 4's state
  window.TF = T;

  // Flow C runs whatever spec the reviewer points it at. Normally that is the
  // spec certified on tab 3 (state.finalSpec). An IMPORTED spec — one certified
  // in an earlier session and exported — takes precedence while it is loaded,
  // because tab 4 is deliberately stateless: it consumes a spec, it never
  // reaches into the knowledge store.
  function currentSpec(){
    if(T.imported && T.imported.mappings) return T.imported;
    if(typeof state !== "undefined" && state){
      if(state.finalSpec && state.finalSpec.mappings) return state.finalSpec;
      const m = state.artifacts && state.artifacts.mapping;
      if(m && m.artifact && m.artifact.mappings) return m.artifact;
    }
    return null;
  }

  function specOrigin(){
    if(T.imported) return {kind:"imported", label:T.importedName || "imported spec"};
    if(typeof state !== "undefined" && state && state.finalSpec) return {kind:"certified", label:"certified on tab 1"};
    return {kind:"draft", label:"draft mapping — not yet certified"};
  }

  // Identity of a spec for cache purposes. The old key was
  // target_table + mapping COUNT, so two different specs with the same target
  // and the same number of mappings shared generated code — an edited transform
  // would silently keep the stale script. Fingerprint what actually matters.
  function specKey(spec){
    if(!spec) return "";
    const parts = (spec.mappings||[]).map(m =>
      `${m.target_attribute}|${m.gate}|${m.transformation_sql}`);
    parts.push("T:" + spec.target_table);
    parts.push("S:" + (spec.source_tables||[spec.source_table]).join(","));
    parts.push("J:" + JSON.stringify(spec.join_plan||[]));
    parts.push("U:" + (spec.unmapped_target||[]).map(u=>u.attribute).join(","));
    const s = parts.join("\n");
    let h = 5381;
    for(let i=0;i<s.length;i++) h = ((h*33) ^ s.charCodeAt(i)) >>> 0;
    return h.toString(36) + ":" + s.length;
  }

  function esc(s){ return String(s==null?"":s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

  /* ---------- input feed cards ---------- */
  // `title` is OPTIONAL. A card should only carry one when it states something
  // the rest of the card does not: the file list, the kind label and the meta
  // line already say a great deal, and a title that merely restates them is
  // noise on a panel three cards tall.
  function feedCard(kind, title, files, meta, opts){
    opts = opts || {};
    const loaded = files && files.length && !opts.missing;
    const badge = opts.badge || (loaded
      ? `<span class="badge g-auto_accept">loaded</span>`
      : `<span class="badge g-review">not loaded</span>`);
    const fileRows = (files||[]).map(f=>{
      const miss = f.missing ? " miss" : "";
      const click = f.id ? ` data-view="${f.id}"` : (f.spec ? ` data-view-spec="1"` : "");
      return `<div class="tf-feed-file"><span class="tf-dot${miss}"></span>`+
        `<button${click} title="view contents">${esc(f.name)}</button></div>`;
    }).join("") || `<div class="tf-feed-empty">${esc(opts.empty||"—")}</div>`;
    return `<div class="tf-feed ${loaded?"loaded":""}">
      <div class="tf-feed-head">
        <span class="tf-feed-kind">${esc(kind)}</span>
        <span class="tf-feed-badge">${badge}</span>
      </div>
      ${title ? `<div class="tf-feed-title">${esc(title)}</div>` : ""}
      <div class="tf-feed-files">${fileRows}</div>
      <div class="tf-feed-meta">${esc(meta||"")}</div>
      ${opts.action ? `<div class="tf-feed-action">${opts.action}</div>` : ""}
    </div>`;
  }

  function renderFeeds(spec){
    const inputs = (typeof state !== "undefined" && state.inputs) || {sources:[],targets:[],codes:[]};
    const specTables = (spec && (spec.source_tables ||
      (spec.source_table && spec.source_table!=="__workset" ? [spec.source_table] : []))) || [];
    // which loaded sources back this spec
    const loadedByStem = {};
    (inputs.sources||[]).forEach(f=> loadedByStem[f.name.replace(/\.[^.]+$/,"")] = f);
    const srcFiles = specTables.length
      ? specTables.map(t => loadedByStem[t]
          ? {name: loadedByStem[t].name, id: loadedByStem[t].id}
          : {name: t+".csv (not loaded)", missing:true})
      : (inputs.sources||[]).map(f=>({name:f.name, id:f.id}));
    const anyMissing = srcFiles.some(f=>f.missing);

    const tgt = (inputs.targets||[]).map(f=>({name:f.name, id:f.id}));
    const join = spec && spec.join_plan && spec.join_plan.length
      ? `Joined workset · ${spec.join_plan.length} relationship`+(spec.join_plan.length>1?"s":"") : "Single-file source";

    const st = spec ? spec.stats||{} : {};
    // The knowledge store's own certification status used to be appended here
    // ("· knowledge draft"). It was reporting a DIFFERENT object's status on
    // the spec's card, directly beneath a badge reading "certified on tab 3" —
    // so one card asserted both certified and draft. The knowledge core is
    // built and certified elsewhere; here it is simply an ancestor of the spec,
    // and its status belongs on tab 2, not on an execution input card. The
    // spec JSON still carries kg_version/kg_status for audit.
    const specMeta = spec
      ? `${st.mapped??spec.mappings.length} mapped · ${st.unmapped_target??0} defaulted`
      : "Run & certify a mapping on tab 1";

    // Demand-driven upload: source data and the target dictionary are OWNED by
    // tabs 1 and 3 — a second upload point here would let the executed files
    // drift from the ones Flow A fingerprinted, quietly invalidating the spec's
    // provenance. The one exception is a spec that names a file this workspace
    // does not have (typically an imported spec): offer to add exactly that.
    const missingNames = srcFiles.filter(f=>f.missing)
      .map(f=>f.name.replace(/ \(not loaded\)$/,""));
    const srcMeta = anyMissing
      ? `Missing ${missingNames.join(", ")} — the spec cannot run without it`
      : `${join} · keeps primary row grain`;

    $("#tf-feeds").innerHTML =
      // no title: it read "EFAS0042 + ESCH0009", which is just the file list
      // below it with the extensions stripped off
      feedCard("Source data", "",
               srcFiles, srcMeta,
               {missing:anyMissing, empty:"No source files loaded",
                action: anyMissing
                  ? `<button class="btn-sm" data-add-source="1">Add missing file(s)</button>` : ""}) +
      // no title: the dictionary file is named for the target it describes
      // (policy.json → policy), so a "policy" heading above "policy.json" only
      // repeats the filename underneath it
      feedCard("Target dictionary", "",
               tgt,
               // only claim documented attributes when a dictionary is actually
               // present — with an imported spec and no dictionary loaded, the
               // count describes the spec, not a file on this workspace
               tgt.length ? `${spec?spec.mappings.length:0} target attributes documented`
                          : "Load the dictionary this spec was built against",
               {empty:"No target dictionary loaded",
                badge: tgt.length?`<span class="badge g-auto_accept">loaded</span>`
                                 :`<span class="badge g-review">not loaded</span>`}) +
      // no title: it read "__workset → policy" on a joined run, leaking the
      // engine's internal view name into the UI. Source → target is the whole
      // premise of the tool, so restating it here bought nothing anyway.
      feedCard("Mapping specification", "",
               spec?[{name: T.importedName || `${spec.target_table}_mapping.json`, spec:true}]:[],
               specMeta,
               {missing:!spec, empty:"Not certified yet",
                badge: spec
                  ? `<span class="badge ${specOrigin().kind==='draft'?'g-review':'g-auto_accept'}">${esc(specOrigin().label)}</span>`
                  : `<span class="badge g-review">pending</span>`});

    // wire viewers
    $("#tf-feeds").querySelectorAll("[data-view]").forEach(b=>
      b.onclick = ()=> (typeof openRaw==="function") && openRaw(b.getAttribute("data-view")));
    $("#tf-feeds").querySelectorAll("[data-view-spec]").forEach(b=>
      b.onclick = ()=> showSpecModal(spec));
    $("#tf-feeds").querySelectorAll("[data-add-source]").forEach(b=>
      b.onclick = ()=> $("#tf-add-source").click());
  }

  function showSpecModal(spec){
    if(!spec) return;
    const el = document.querySelector("#modal");
    document.querySelector("#modal-title").textContent = `${spec.target_table}_mapping.json`;
    document.querySelector("#modal-body").textContent = JSON.stringify(spec, null, 2);
    el.hidden = false;
  }

  /* ---------- naive python syntax highlighter ---------- */
  const PY_KW = /\b(import|from|as|def|return|con|print|f)\b/g;
  function highlight(code){
    // work line by line so we can treat triple-quoted SQL/doc blocks specially
    const lines = code.split("\n");
    let inTriple = false, tripleKind = "";
    const out = lines.map(raw=>{
      let line = esc(raw);
      const t = raw.trim();
      // toggle triple-quoted blocks
      const triples = (raw.match(/"""/g)||[]).length;
      if(inTriple){
        const cls = tripleKind;
        if(triples){ inTriple=false; }
        return `<span class="${cls}">${line}</span>`;
      }
      if(triples===1){
        inTriple = true;
        // sql heredocs are assigned to *_sql; docstrings otherwise
        tripleKind = /_sql\s*=/.test(raw) || /transform_sql|workset_sql/.test(raw) ? "sql" : "c";
        return `<span class="${tripleKind}">${line}</span>`;
      }
      if(t.startsWith("#")) return `<span class="c">${line}</span>`;
      // strings
      line = line.replace(/(&quot;[^&]*?&quot;|&#39;[^&]*?&#39;|'[^']*'|"[^"]*")/g, m=>`<span class="s">${m}</span>`);
      // numbers
      line = line.replace(/\b(\d+\.?\d*)\b/g, `<span class="n">$1</span>`);
      // builtins/functions
      line = line.replace(/\b(pd|duckdb|read_csv|register|execute|connect|to_csv|df|len)\b/g, `<span class="f">$1</span>`);
      // keywords
      line = line.replace(PY_KW, `<span class="k">$&</span>`);
      return line;
    });
    return out.join("\n");
  }

  function renderCode(code){
    const n = code.split("\n").length;
    $("#tf-gutter").innerHTML = Array.from({length:n}, (_,i)=>`<span>${i+1}</span>`).join("");
    $("#tf-code").innerHTML = highlight(code);
  }

  // Rail feedback — tab 4 reuses the same station strip as tabs 1 and 3, so
  // the pipeline visibly ADVANCES here instead of arriving pre-finished.
  function railIdx(id){ return (state.railAgents||[]).findIndex(a=>a.id===id); }
  function railActive(id){ const i = railIdx(id); if(i>=0) setActive(i); }
  function railDone(id){ const i = railIdx(id); if(i>=0) setDone(i); }

  function readyBriefing(spec){
    const tables = (spec.source_tables||[spec.source_table]).filter(x=>x&&x!=="__workset");
    const attrs = (spec.mappings||[]).length + (spec.unmapped_target||[]).length;
    const joins = (spec.join_plan||[]).length;
    $("#tf-gutter").innerHTML = "";
    $("#tf-code").innerHTML =
      `<span class="tf-ready">Ready to generate\n\n`
      + `  target      ${esc(spec.target_table||"target")}\n`
      + `  attributes  ${attrs}\n`
      + `  source(s)   ${esc(tables.join(", ")||"—")}\n`
      + `  join(s)     ${joins}\n\n`
      + `Press “Generate ETL” to compile the certified mapping\ninto a runnable pandas + DuckDB script.</span>`;
  }

  async function loadCodegen(spec){
    const btn = $("#tf-generate");
    btn.disabled = true; btn.classList.add("running");
    railActive("codegen");
    $("#tf-gutter").innerHTML = "";
    $("#tf-code").innerHTML = `<span class="tf-ready">Generating the ETL from the certified mapping…</span>`;
    try{
      const r = await (await fetch("/api/transform/codegen",{method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({spec})})).json();
      if(r.error){
        $("#tf-code").textContent = "# "+r.error; $("#tf-gutter").innerHTML="";
        btn.disabled = false; btn.classList.remove("running");
        return false;
      }
      T.code = r.code; T.generated = true; T._codeKey = specKey(spec);
      renderCode(r.code);
      railDone("codegen");
      const miss = (r.missing_files||[]);
      $("#tf-code-sub").innerHTML = `Compiled from the mapping specification · `+
        `<b>${esc((r.source_tables||[]).join(" + ")||"source")}</b> → <b>${esc(r.target_table)}</b>`+
        (miss.length?` · <span class="warn-text">missing: ${esc(miss.join(", "))}</span>`:"");
      $("#tf-copy").disabled = false; $("#tf-dl-code").disabled = false;
      $("#tf-run").disabled = miss.length > 0;
      $("#tf-console-line").textContent = miss.length
        ? `Cannot run — source file(s) not loaded: ${miss.join(", ")}`
        : `Ready. Press “Run transformation” to execute.`;
      btn.textContent = "Regenerate";
      return true;
    }catch(e){
      $("#tf-code").textContent = "# codegen failed: "+e;
      return false;
    }finally{
      btn.disabled = false; btn.classList.remove("running");
    }
  }

  /* ---------- run ---------- */
  function setStat(id,val){ $(id).textContent = val; }
  function consoleLine(html, cls){
    const c = $("#tf-console");
    const span = document.createElement("span");
    span.className = "runline "+(cls||"");
    span.innerHTML = html;
    c.appendChild(span); c.scrollTop = c.scrollHeight;
  }

  async function runTransform(){
    const spec = T.spec || currentSpec();
    if(!spec) return;
    const btn = $("#tf-run");
    btn.classList.add("running"); btn.disabled = true;
    railActive("execute");
    $("#tf-console").innerHTML = `<span class="tf-prompt">$</span> <span class="muted">python etl_${spec.target_table}.py</span>`;
    consoleLine(`loading source file(s)…`, "muted");
    const t0 = performance.now();
    try{
      const r = await (await fetch("/api/transform/run",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({spec, preview_limit:200})})).json();
      const ms = Math.max(1, Math.round(performance.now()-t0));
      if(r.error){
        consoleLine(`✗ ${esc(r.error)}`, "err");
        setStat("#tf-stat-time", ms+"ms");
        btn.classList.remove("running"); btn.disabled = false;
        return;
      }
      T.csv = r.csv; T.columns = r.columns; T.rows = r.rows; T.target = r.target_table;
      const s = r.stats||{};
      setStat("#tf-stat-rows", (s.row_count??r.rows.length).toLocaleString());
      setStat("#tf-stat-cols", s.column_count??r.columns.length);
      setStat("#tf-stat-src", (spec.source_tables||[spec.source_table]).filter(x=>x&&x!=="__workset").length||1);
      setStat("#tf-stat-time", ms+"ms");
      consoleLine(`✓ executed transform · applied ${s.migrating_columns} column transform(s)`, "ok");
      if(s.defaulted_columns) consoleLine(`  ${s.defaulted_columns} target column(s) defaulted at load`, "muted");
      consoleLine(`✓ materialised <b>${s.row_count}</b> row(s) into <b>${r.target_table}</b>`, "ok");
      if(s.returned < s.row_count) consoleLine(`  preview shows first ${s.returned} rows — CSV export has all ${s.row_count}`, "muted");
      renderGrid(r.columns, r.rows, spec);
      railDone("execute");
      $("#tf-out-panel").hidden = false;
      $("#tf-dl-csv").disabled = false;
      $("#tf-filter").hidden = false;
      $("#tf-out-title").textContent = `${r.target_table} — transformed target dataset`;
      $("#tf-out-sub").textContent =
        `${s.row_count} row(s) · ${s.column_count} column(s) · generated ${new Date().toLocaleString()}`;
      $("#tf-out-panel").scrollIntoView({behavior:"smooth", block:"nearest"});
    }catch(e){
      consoleLine(`✗ ${esc(String(e))}`, "err");
    }finally{
      btn.classList.remove("running"); btn.disabled = false;
    }
  }

  /* ---------- output grid ---------- */
  function typeOf(spec, attr){
    return null; // reserved: could look up target dict types
  }
  function cellHtml(v){
    if(v===null || v===undefined) return `<span class="tf-null">NULL</span>`;
    if(typeof v==="boolean") return `<span class="tf-bool">${v}</span>`;
    if(typeof v==="number") return `<span class="tf-num">${v}</span>`;
    const s = String(v);
    if(/^-?\d+\.?\d*$/.test(s)) return `<span class="tf-num">${esc(s)}</span>`;
    return esc(s);
  }
  function renderGrid(columns, rows, spec){
    const head = `<tr><th class="tf-rownum">#</th>` +
      columns.map(c=>`<th>${esc(c)}</th>`).join("") + `</tr>`;
    const body = rows.map((r,i)=>`<tr><td class="tf-rownum">${i+1}</td>`+
      r.map(v=>`<td>${cellHtml(v)}</td>`).join("")+`</tr>`).join("");
    $("#tf-grid-wrap").innerHTML =
      `<table class="tf-dt"><thead>${head}</thead><tbody>${body}</tbody></table>`;
    T._all = rows;
  }
  function applyFilter(q){
    if(!T._all) return;
    q = (q||"").toLowerCase();
    const rows = !q ? T._all : T._all.filter(r=> r.some(v=> String(v==null?"":v).toLowerCase().includes(q)));
    renderGrid(T.columns, rows);
  }

  /* ---------- downloads ---------- */
  function downloadText(name, text, mime){
    const blob = new Blob([text],{type:mime}); const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href=url; a.download=name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 1000);
  }

  /* ---------- mount ---------- */
  window.mountTransform = function(){
    const spec = currentSpec();
    T.spec = spec;
    $("#tf-inputs-panel").hidden = false;
    renderFeeds(spec);
    // "Use certified spec" is only meaningful when there IS one to go back to.
    // Importing a spec without having run Flow B in this session left the
    // button visible, and clicking it dropped straight back to the "certify a
    // mapping first" gate — it looked like it was asking you to import again.
    // It cannot be removed outright, though: T.imported survives tab switches,
    // so with no way to clear it a user who imports after certifying is stuck
    // on the imported spec until they reload the page.
    $("#tf-import-clear").hidden = !(T.imported && state && state.finalSpec);
    if(!spec){
      $("#tf-gate").hidden = false;
      $("#tf-grid").hidden = true;
      $("#tf-out-panel").hidden = true;
      return;
    }
    $("#tf-gate").hidden = true;
    $("#tf-grid").hidden = false;

    // The spec drives everything downstream, so its IDENTITY decides whether
    // previously generated code is still valid. Landing straight on generated
    // code hid the generation step entirely — arrive on a briefing instead and
    // let the reviewer trigger it.
    const key = specKey(spec);
    if(T._codeKey !== key){
      T.code = null; T.generated = false; T._codeKey = null;
      $("#tf-generate").textContent = "Generate ETL";
      $("#tf-copy").disabled = true; $("#tf-dl-code").disabled = true;
      $("#tf-run").disabled = true;
      readyBriefing(spec);
      $("#tf-code-sub").textContent = "Compiles the certified mapping into a runnable script.";
      $("#tf-out-panel").hidden = true; $("#tf-dl-csv").disabled = true;
      ["#tf-stat-rows","#tf-stat-cols","#tf-stat-src","#tf-stat-time"].forEach(id=>$(id).textContent="—");
      $("#tf-console").innerHTML = `<span class="tf-prompt">$</span> <span id="tf-console-line" class="muted">Generate the ETL first, then run it.</span>`;
    }else{
      if(T.code) railDone("codegen");
      if(T.rows) railDone("execute");
    }
  };

  /* ---------- import a previously certified spec ---------- */
  // Tab 4 is server-stateless: /api/transform/* consume whatever spec the
  // client posts. So running a spec certified in an EARLIER session against a
  // fresh extract needs no re-run of Flows A and B — it just needs the file.
  async function importSpecFile(file){
    if(!file) return;
    try{
      const text = await file.text();
      const doc = JSON.parse(text);
      if(!doc || !Array.isArray(doc.mappings) || !doc.mappings.length){
        alert("That file isn't a mapping specification — expected a JSON object with a 'mappings' array.");
        return;
      }
      T.imported = doc; T.importedName = file.name;
      T._codeKey = null;
      window.mountTransform();
      $("#tf-inputs-panel").scrollIntoView({behavior:"smooth", block:"nearest"});
    }catch(e){
      alert("Couldn't read that spec: " + e);
    }
  }

  /* ---------- wire controls (once) ---------- */
  document.addEventListener("DOMContentLoaded", wire);
  if(document.readyState!=="loading") wire();
  function wire(){
    const gen = $("#tf-generate"); if(gen && !gen._wired){ gen._wired=true;
      gen.onclick = ()=>{ const s = T.spec || currentSpec(); if(s) loadCodegen(s); }; }
    const imp = $("#tf-import"); if(imp && !imp._wired){ imp._wired=true;
      imp.onclick = ()=> $("#tf-import-file").click(); }
    const impf = $("#tf-import-file"); if(impf && !impf._wired){ impf._wired=true;
      impf.onchange = ()=>{ importSpecFile(impf.files && impf.files[0]); impf.value=""; }; }
    const impc = $("#tf-import-clear"); if(impc && !impc._wired){ impc._wired=true;
      impc.onclick = ()=>{ T.imported=null; T.importedName=""; T._codeKey=null;
                           window.mountTransform(); }; }
    // targeted upload for the file(s) a spec names but the workspace lacks
    const addsrc = $("#tf-add-source"); if(addsrc && !addsrc._wired){ addsrc._wired=true;
      addsrc.onchange = async ()=>{
        if(addsrc.files && addsrc.files.length && typeof uploadFiles === "function"){
          await uploadFiles(addsrc.files);
          T._codeKey = null;
          window.mountTransform();
        }
        addsrc.value = "";
      }; }
    const run = $("#tf-run"); if(run && !run._wired){ run._wired=true; run.onclick = runTransform; }
    const dl = $("#tf-dl-csv"); if(dl && !dl._wired){ dl._wired=true;
      dl.onclick = ()=>{ if(T.csv) downloadText(`${T.target||"target"}.csv`, T.csv, "text/csv"); }; }
    const cp = $("#tf-copy"); if(cp && !cp._wired){ cp._wired=true;
      cp.onclick = async ()=>{ if(!T.code) return;
        try{ await navigator.clipboard.writeText(T.code); cp.textContent="Copied ✓";
             setTimeout(()=>cp.textContent="Copy",1200);}catch(e){} }; }
    const dc = $("#tf-dl-code"); if(dc && !dc._wired){ dc._wired=true;
      dc.onclick = ()=>{ if(T.code) downloadText(`etl_${T.target||(T.spec&&T.spec.target_table)||"transform"}.py`, T.code, "text/x-python"); }; }
    const flt = $("#tf-filter"); if(flt && !flt._wired){ flt._wired=true;
      flt.oninput = ()=> applyFilter(flt.value); }
  }
})();

/* ============================================================
   TAB 3 · VALIDATION WORKSPACE (additive module)
   Checks the DELIVERED transform output (window.TF.csv, tab 2's CSV)
   against the certified spec + target dictionary — generate a script,
   review it, run it on demand, see the results. Self-contained; never
   mutates tabs 1/2 state.
   ============================================================ */
(function(){
  "use strict";
  const $ = s => document.querySelector(s);
  const VAL = { code:null, _csvRef:null, result:null };

  function gutterRender(prefix, code){
    const n = code.split("\n").length;
    $(`#${prefix}-gutter`).innerHTML = Array.from({length:n}, (_,i)=>`<span>${i+1}</span>`).join("");
    $(`#${prefix}-code`).textContent = code;
  }
  function downloadText(name, text, mime){
    const blob = new Blob([text],{type:mime}); const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href=url; a.download=name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 1000);
  }
  function statusIcon(s){ return s==="fail" ? "✗" : s==="warn" ? "⚠" : "✓"; }
  function checkCard(c){
    const sample = (c.sample||[]).slice(0,2).map(row=>{
      if(row && typeof row==="object"){
        const ent = Object.entries(row).filter(([,v])=> v!==null && v!==undefined && v!=="");
        return ent.map(([k,v])=>`${k}=${v}`).join(", ");
      }
      return String(row);
    }).filter(Boolean);
    return `<div class="ccard ccard-${c.status}">
      <div class="ccard-h"><span class="ccard-ic">${statusIcon(c.status)}</span><b>${esc(c.name)}</b></div>
      <div class="ccard-cat muted">${esc(c.category)}${c.target_attribute?" · "+esc(c.target_attribute):""}</div>
      <div class="ccard-detail">${esc(c.detail)}</div>
      ${sample.length?`<div class="ccard-sample">e.g. ${sample.map(esc).join("; ")}</div>`:""}
    </div>`;
  }

  // one dashboard tile + one grouped section per check family, in the order
  // the validation script runs them — the whole outcome, not just failures
  const VAL_CAT_LABELS = {wellformed:"Wellformed", grain:"Grain", key_integrity:"Key",
                          duplicates:"Duplicates", type:"Data type", transform:"Transform rule",
                          completeness:"Completeness", domain:"Domain"};
  function renderValDashboard(r){
    const cats = {};
    r.checks.forEach(c=>{
      const k = c.category;
      (cats[k] || (cats[k] = {pass:0,warn:0,fail:0,checks:[]}));
      cats[k][c.status] = (cats[k][c.status]||0) + 1;
      cats[k].checks.push(c);
    });
    const order = Object.keys(VAL_CAT_LABELS).filter(k=>cats[k])
      .concat(Object.keys(cats).filter(k=>!VAL_CAT_LABELS[k]));
    const tiles = order.map(k=>{
      const s = cats[k]; const total = s.checks.length;
      const cls = s.fail ? "ccard-fail" : s.warn ? "ccard-warn" : "ccard-pass";
      return `<div class="valcat-tile ${cls}">
        <div class="valcat-name">${esc(VAL_CAT_LABELS[k]||k)}</div>
        <div class="valcat-count">${s.pass||0}/${total} passed</div>
        ${s.fail?`<div class="valcat-fail">${s.fail} failed</div>`:""}
        ${s.warn?`<div class="valcat-warn">${s.warn} warning</div>`:""}
      </div>`;
    }).join("");
    const groups = order.map(k=>{
      const s = cats[k];
      // failures and warnings first — the outcome that needs a look — but every
      // check for the family is shown, passes included
      const sorted = [...s.checks].sort((a,b)=> (a.status==="pass") - (b.status==="pass"));
      return `<div class="valcat-group">
        <div class="valcat-h">${esc(VAL_CAT_LABELS[k]||k)}</div>
        <div class="cards">${sorted.map(checkCard).join("")}</div>
      </div>`;
    }).join("");
    return {tiles, groups};
  }

  /* One card per CHECK FAMILY — the family name is the card heading, so a
     separate group header above a single card is redundant. Cards sit in a
     grid (see #val-rules in styles.css) so eight families do not become a
     scroll before the reader reaches the script. */
  function renderRulePreview(rules){
    const cats = {};
    rules.forEach(rl=>{ (cats[rl.category] || (cats[rl.category]=[])).push(rl); });
    const order = Object.keys(VAL_CAT_LABELS).filter(k=>cats[k])
      .concat(Object.keys(cats).filter(k=>!VAL_CAT_LABELS[k]));
    return order.map(k=>{
      const rs = cats[k];
      const scope = rs.map(r=>r.scope).filter(Boolean).join(" · ");
      return `<div class="rulecard">
        <div class="rulecard-name">${esc(VAL_CAT_LABELS[k]||k)}</div>
        ${scope?`<div class="rulecard-scope">${esc(scope)}</div>`:""}
        <div class="rulecard-detail">${rs.map(r=>esc(r.detail)).join(" ")}</div>
      </div>`;
    }).join("");
  }



  /* ---- (1) inputs: what this workspace is validating against --------
     Uses the SAME .tf-feed markup as the transformation workspace so the two
     panels read identically. Deliberately does NOT hide itself when the spec
     is missing: a panel that vanishes when something is absent is exactly the
     silent-omission problem the results table exists to avoid — it says
     "not loaded" instead. */
  function valFeedCard(kind, title, lines, meta, loaded, action){
    const rows = (lines || []).length
      ? lines.map(l => `<div class="tf-feed-file"><span class="tf-dot${l.miss?" miss":""}"></span>`
                     + (l.act ? `<button ${l.act}>${esc(l.text)}</button>`
                              : `<span class="mono">${esc(l.text)}</span>`)
                     + `</div>`).join("")
      : `<div class="tf-feed-empty">not loaded</div>`;
    return `<div class="tf-feed ${loaded?"loaded":""}">
      <div class="tf-feed-head"><span class="tf-feed-kind">${esc(kind)}</span></div>
      ${title?`<div class="tf-feed-title">${esc(title)}</div>`:""}
      <div class="tf-feed-files">${rows}</div>
      <div class="tf-feed-meta">${esc(meta||"")}</div>
      ${action?`<div class="tf-feed-action">${action}</div>`:""}
    </div>`;
  }

  function valSpec(){
    const T = window.TF || {};
    if (T.spec && T.spec.mappings) return T.spec;
    if (typeof state !== "undefined" && state){
      if (state.finalSpec && state.finalSpec.mappings) return state.finalSpec;
      const m = state.artifacts && state.artifacts.mapping;
      if (m && m.artifact && m.artifact.mappings) return m.artifact;
    }
    return null;
  }

  function renderValInputs(){
    const el = $("#val-feeds"); if(!el) return;
    const panel = $("#val-inputs-panel"); if(panel) panel.hidden = false;
    const T = window.TF || {};
    const spec = valSpec();
    const files = (spec && spec.source_tables) || [];
    const nMap = spec ? (spec.mappings||[]).length : 0;
    const nDflt = spec ? (spec.unmapped_target||[]).length : 0;

    // Filenames are the clickable item, exactly as the transformation
    // workspace does it. Titles were invented labels ("policy",
    // "joined workset") that leaked internal names and told the reader
    // nothing the filename does not.
    const specName = (window.TF && TF.importedName)
                     || (spec ? `${spec.target_table || "target"}_mapping.json` : "");
    const csvName  = VAL.importedName || `${T.target || "target"}.csv`;
    el.innerHTML =
      valFeedCard("Certified mapping", "",
        spec ? [{text: specName, act:'data-val-view-spec class="mono"'}] : [],
        spec ? `${nMap} mapping(s)`
               + (nDflt ? `, ${nDflt} defaulted` : "")
               + " — certified on 1 · Mapping workspace"
             : "run and certify a mapping first",
        !!spec)
    + valFeedCard("Transformed target data", "",
        VAL.csvRows!=null ? [{text: csvName, act:'data-val-view-csv class="mono"'}] : [],
        (VAL.csvRows!=null ? `${VAL.csvRows} row(s) × ${VAL.csvCols} column(s) — ` : "")
        + (VAL.imported ? "imported, overrides the transformation output"
                        : "produced by 2 · Transformation workspace"),
        VAL.csvRows!=null,
        // same escape hatch tab 2 gives for the spec: validate a file someone
        // else produced, without having to run the transform here first
        VAL.imported
          ? `<button class="btn-sm" data-val-unimport>Use transformation output</button>`
          : `<label class="btn-sm upload-btn">Import target data…<input type="file"
               accept=".csv" hidden data-val-import></label>`)
    + valFeedCard("Source data", "",
        files.map(f=>({text:f, act:`data-val-view-src="${esc(f)}" class="mono"`})),
        (files.length>1 ? `${files.length} files joined — ` : "")
        + "used to compare delivered grain and re-execute the certified transforms",
        !!files.length);

    // view links. showSpecModal lives in tab 2's module scope, so render the
    // spec here rather than reaching across IIFEs for it.
    el.querySelectorAll("[data-val-view-spec]").forEach(b=>
      b.onclick = ()=> showTextModal("Certified mapping spec",
                                     JSON.stringify(valSpec(), null, 2)));
    el.querySelectorAll("[data-val-view-csv]").forEach(b=>
      b.onclick = ()=> showTextModal("Transformed target data", valCsv()));
    el.querySelectorAll("[data-val-view-src]").forEach(b=>
      b.onclick = async ()=>{
        const t = b.getAttribute("data-val-view-src");
        try{
          const man = await (await fetch("/api/inputs")).json();
          const rec = (man.sources||[]).find(f => f.name.replace(/\.[^.]+$/,"") === t);
          if(!rec) return showTextModal(t, "source file not found in the registry");
          const raw = await (await fetch("/api/raw?id="+encodeURIComponent(rec.id))).json();
          showTextModal(raw.name || t, raw.content || "");
        }catch(e){ showTextModal(t, "could not load: "+e); }
      });
    const imp = el.querySelector("[data-val-import]");
    if(imp) imp.onchange = async e =>{
      const f = e.target.files && e.target.files[0]; if(!f) return;
      VAL.imported = await f.text(); VAL.importedName = f.name;
      VAL.result = null; VAL._csvRef = null;
      renderValidationTab(); renderValInputs();
    };
    const unimp = el.querySelector("[data-val-unimport]");
    if(unimp) unimp.onclick = ()=>{
      VAL.imported = null; VAL.importedName = "";
      VAL.result = null; VAL._csvRef = null;
      renderValidationTab(); renderValInputs();
    };
  }

  // the CSV under test: an imported file wins over tab 2's output
  function valCsv(){ return VAL.imported || (window.TF && TF.csv) || ""; }

  function showTextModal(title, text){
    let m = document.getElementById("val-text-modal");
    if(!m){
      m = document.createElement("div");
      m.id = "val-text-modal"; m.className = "modal";
      m.innerHTML = `<div class="modal-card"><div class="modal-head">
          <b id="val-text-title"></b><button class="btn-sm" id="val-text-x">Close</button>
        </div><pre class="mono modal-body" id="val-text-body"></pre></div>`;
      document.body.appendChild(m);
      m.onclick = e =>{ if(e.target===m) m.hidden = true; };
      m.querySelector("#val-text-x").onclick = ()=> m.hidden = true;
    }
    m.querySelector("#val-text-title").textContent = title;
    m.querySelector("#val-text-body").textContent = (text||"").split("\n").slice(0,200).join("\n");
    m.hidden = false;
  }

  /* ---- (4)+(5) results table with evidence ---------------------------
     Rows are TARGET ATTRIBUTES, not checks — a card per check does not
     survive a 50-attribute target. Attributes with no executed test still
     get a row (that is the no-silent-skips invariant), so coverage is
     something the reader observes rather than a number we assert.
     Every cell expands to the SQL that ran, the population it scanned and
     sample offending rows: a green tick with its SQL is evidence, a green
     tick alone is a claim. */
  const VERDICT_LABEL = {certified:"Certified", blocked:"Blocked",
                         needs_review:"Needs review"};
  // Plain English, because "blocked" on its own tells a reader nothing about
  // what to do next. Severity is what separates the two failure verdicts: a
  // HARD failure means the delivered data is wrong or unusable (a required
  // column empty, the row count changed, a transform not reproducing), so the
  // load must not proceed. A SOFT failure is a discrepancy worth a human
  // decision but not automatically disqualifying.
  const VERDICT_WHY = {
    certified: "Every applicable check passed. The delivered data matches the certified spec.",
    blocked: "At least one hard check failed — the delivered data is wrong or unusable, so it should not be loaded. Fix the mapping or the transform and re-run.",
    needs_review: "A soft check failed. Nothing disqualifying, but a person should look before this is loaded."};

  const ATTR_CATS = ["transform","completeness","domain","type","key_integrity"];
  const TABLE_CATS = ["wellformed","grain","duplicates"];
  const CELL_CLS = {pass:"vc-pass", fail:"vc-fail", warn:"vc-warn",
                    skipped:"vc-skip", absent:"vc-absent"};
  const CELL_TXT = {pass:"pass", fail:"fail", warn:"warn",
                    skipped:"n/a", absent:"—"};

  /* Offending records. A failure the reviewer cannot SEE is only half an
     answer: "1 of 50 values differ" tells you something is wrong, not which
     record to go and look at. Samples arrive as row dicts, so render them as a
     real table (keyed by the business key where one is mapped) rather than a
     k=v soup, say how many of the total are shown, and let the reviewer take
     them away as CSV. */
  const EV_LABEL = {source:"Source", delivered:"Delivered", ties:"Agrees",
                    source_total:"Source total", delivered_total:"Delivered total",
                    expected_records:"Expected", delivered_records:"Delivered",
                    rows_scanned:"Rows", group:"Group", measure:"Measure",
                    difference:"Difference", mandatory:"Mandatory"};

  function sampleTable(rows){
    if(!rows || !rows.length) return "";
    const cols = Object.keys(rows[0]);
    const head = `<tr>${cols.map(c=>`<th>${esc(EV_LABEL[c]||c)}</th>`).join("")}</tr>`;
    const cell = (v)=>{
      if(v === true)  return `<td><span class="vtc-verdict vtc-v-pass">YES</span></td>`;
      if(v === false) return `<td><span class="vtc-verdict vtc-v-fail">NO</span></td>`;
      if(v === null || v === undefined || v === "")
        return `<td class="mono vev-null">∅</td>`;
      if(typeof v === "number")
        return `<td class="mono num">${v.toLocaleString(undefined,
          {maximumFractionDigits:2})}</td>`;
      return `<td class="mono">${esc(String(v))}</td>`;
    };
    const body = rows.map(r=>`<tr>${cols.map(c=>cell(r[c])).join("")}</tr>`).join("");
    return `<div class="tbl-wrap"><table class="tbl vev-tbl">${head}${body}</table></div>`;
  }

  function sampleCsv(rows){
    const cols = Object.keys(rows[0]);
    const q = v => {
      const t = (v===null||v===undefined) ? "" : String(v);
      return /[",\n]/.test(t) ? '"' + t.replace(/"/g,'""') + '"' : t;
    };
    return [cols.join(","), ...rows.map(r=>cols.map(c=>q(r[c])).join(","))].join("\n");
  }

  function evidenceHtml(c){
    if(!c) return "";
    if(c.status==="skipped")
      return `<div class="vev"><div class="vev-why">${esc(c.detail)}</div>
               <div class="vev-note muted">No SQL ran for this attribute.</div></div>`;
    const rows = c.sample || [];
    const n = c.offending_rows || 0;
    let offending = "";
    if(n && rows.length){
      const more = n > rows.length ? ` — showing the first ${rows.length}` : "";
      offending = `<div class="vev-sample">
        <div class="vev-sample-h">
          <b>Offending record${n===1?"":"s"}</b>
          <span class="muted">${n} found${more}</span>
          <button class="btn-xs" data-ev-dl>Download .csv</button>
        </div>
        ${sampleTable(rows)}
      </div>`;
      EV_SAMPLES[c.name] = rows;
    } else if(n){
      offending = `<div class="vev-note muted">${n} offending record(s) — no sample
        could be captured for this check.</div>`;
    }
    return `<div class="vev" data-ev-name="${esc(c.name)}">
      <div class="vev-why">${esc(c.detail)}</div>
      <div class="vev-stats mono">${c.rows_scanned
          ? `${n} violation(s) across ${c.rows_scanned} row(s) scanned`
          : `${n} violation(s) — structural check, no row values read`}</div>
      ${c.sql?`<pre class="vev-sql mono">${esc(c.sql)}</pre>`:""}
      ${offending}
    </div>`;
  }

  const EV_SAMPLES = {};

  function wireEvidenceDownloads(root){
    root.querySelectorAll("[data-ev-dl]").forEach(b=>{
      if(b._wired) return; b._wired = true;
      b.onclick = ()=>{
        const name = b.closest(".vev").getAttribute("data-ev-name");
        const rows = EV_SAMPLES[name];
        if(rows && rows.length)
          downloadText(name.replace(/[^\w.-]+/g,"_") + "_offending.csv",
                       sampleCsv(rows), "text/csv");
      };
    });
  }

  /* (6) The assurance statement.
     "Values examined 1,150 of 1,150" is an input metric — it says how much work
     was done, not what the work FOUND. What earns confidence is the conjunction
     of three things stated in one breath: every value was independently
     re-derived and compared (completeness), the comparison would have caught a
     defect (sensitivity — demonstrated by the ones it did catch), and any
     exception is named (traceability). So the headline is a sentence a
     non-technical reviewer can read aloud, with the exception count as the
     number that carries meaning — not the volume. */
  const VERDICT_WORD = {pass:"PASS", fail:"FAIL", warn:"CHECK", skipped:"N/A"};

  /* The one number each whole-table check is really about, pulled out of the
     sentence so it can be read without reading it. */
  function headlineFigure(c){
    const m = (c.detail || "").match(/([\d,]+)\s*(?:source )?row\(s\)\s*->\s*([\d,]+)/i);
    if (m) return `${m[1]} → ${m[2]} rows`;
    if (c.category === "duplicates")
      return c.offending_rows ? `${c.offending_rows} duplicate group(s)` : "0 duplicates";
    if (c.category === "wellformed"){
      const w = (c.detail || "").match(/all (\d+) certified/);
      return w ? `${w[1]}/${w[1]} columns present`
               : (c.offending_rows ? `${c.offending_rows} missing` : "all present");
    }
    return c.offending_rows ? `${c.offending_rows} issue(s)` : "";
  }

  const FAMILY_ORDER = ["transform","type","completeness","domain",
                       "key_integrity","duplicates","grain","wellformed"];

  /* (1) "63 checks" is meaningless on its own. What answers "63 of what?" is
     the COMPOSITION — 20 transform + 23 data type + 10 completeness + ... —
     which is also more useful than the total. */
  function familyBreakdown(r){
    const ran = {}, skipped = {};
    (r.checks||[]).forEach(c=>{
      const bag = c.status === "skipped" ? skipped : ran;
      bag[c.category] = (bag[c.category]||0) + 1;
    });
    const order = FAMILY_ORDER.filter(k=>ran[k]).concat(
      Object.keys(ran).filter(k=>!FAMILY_ORDER.includes(k)));
    return {ran, skipped, order,
            total: Object.values(ran).reduce((a,b)=>a+b,0),
            nSkipped: Object.values(skipped).reduce((a,b)=>a+b,0)};
  }

  /* (3) The verdict line. Previously four overlapping sentences that between
     them never mentioned the other check families. Now: what was compared,
     what else was checked, what was found — once each. */
  function assuranceLine(r){
    const cc = r.coverage_cells || {};
    const fb = familyBreakdown(r);
    const bad = (r.checks||[]).filter(c=>c.status==="fail")
                              .reduce((n,c)=>n+(c.offending_rows||0),0);
    const failing = (r.checks||[]).filter(c=>c.status==="fail");
    const others = fb.order.filter(k=>k!=="transform")
                           .map(k=>(VAL_CAT_LABELS[k]||k).toLowerCase());
    const scope =
      `<b>${(cc.cells_examined||0).toLocaleString()}</b> values re-derived from source `
      + `and compared, plus <b>${(fb.total - (fb.ran.transform||0))}</b> rule checks `
      + `across ${others.join(", ")}.`;
    const outcome = bad === 0
      ? `<b class="ok">Nothing failed.</b>`
      : `<b class="bad">${bad.toLocaleString()} value${bad===1?"":"s"} did not match</b> in `
        + failing.map(c=>`<span class="mono">${esc(c.target_attribute||c.name)}</span>`).join(", ")
        + `, listed below with the offending record.`;
    return {html: outcome + " " + scope, bad, fb};
  }

  function renderValSummary(r){
    const cc = r.coverage_cells || {}, st = r.stats || {};
    const a = assuranceLine(r), fb = a.fb;
    const vClass = r.verdict==="certified" ? "ok" : r.verdict==="blocked" ? "bad" : "warn";
    const matched = (cc.cells_examined||0) - a.bad;
    const chips = fb.order.map(k=>
      `<span class="fam"><b>${fb.ran[k]}</b> ${esc(VAL_CAT_LABELS[k]||k)}</span>`).join("");
    $("#val-summary").innerHTML = `
      <div class="vs-assure ${a.bad?"has-bad":"clean"}">
        <div class="vs-assure-verdict ${vClass}">${esc(VERDICT_LABEL[r.verdict]||r.verdict||"—")}</div>
        <div class="vs-assure-text">${a.html}</div>
      </div>
      <div class="val-tiles">
        <div class="vs-card"><div class="vs-k">Values matched</div>
          <div class="vs-v ok">${matched.toLocaleString()}</div>
          <div class="vs-s">of ${(cc.cells_examined||0).toLocaleString()} compared —
            ${(cc.rows||0).toLocaleString()} rows × ${cc.columns_examined||0} columns</div></div>
        <div class="vs-card"><div class="vs-k">Discrepancies</div>
          <div class="vs-v ${a.bad?"bad":"ok"}">${a.bad.toLocaleString()}</div>
          <div class="vs-s">${a.bad?"named below, with the offending record":"nothing to investigate"}</div></div>
        <div class="vs-card"><div class="vs-k">Checks run</div>
          <div class="vs-v">${fb.total}</div>
          <div class="vs-s fam-list">${chips}</div></div>
      </div>
      ${fb.nSkipped?`<div class="vs-foot muted">${fb.nSkipped} further rule(s) had nothing to
        assert on their attribute — a domain rule needs an allowed-value list, a completeness
        rule needs a non-nullable column. They appear as <b>n/a</b> in the table and are never
        counted as passes.</div>`:""}`;
  }

  function renderValResults(r){
    renderValSummary(r);
    // index checks by attribute + category
    const byAttr = {}, tableLevel = [];
    (r.checks||[]).forEach(c=>{
      if(TABLE_CATS.includes(c.category) || !c.target_attribute){ tableLevel.push(c); return; }
      (byAttr[c.target_attribute] || (byAttr[c.target_attribute] = {}))[c.category] = c;
    });
    // every declared attribute gets a row, in target-dictionary order
    const order = (window.TF && TF.targetAttrNames && TF.targetAttrNames.length)
      ? TF.targetAttrNames : Object.keys(byAttr);
    Object.keys(byAttr).forEach(a=>{ if(!order.includes(a)) order.push(a); });
    VAL.rows = order.map(a=>{
      const cells = byAttr[a] || {};
      const statuses = ATTR_CATS.map(k=> cells[k] ? cells[k].status : "absent");
      return {attribute:a, cells,
              worst: statuses.includes("fail") ? "fail"
                   : statuses.includes("warn") ? "warn"
                   : statuses.includes("pass") ? "pass" : "untested"};
    });

    // (7) Lead with the VERDICT and the figure, not the sentence. These three
    // are whole-table facts a reviewer scans in a second; the prose and the SQL
    // stay, one click away, to support the verdict rather than carry it.
    $("#val-table-checks").innerHTML = tableLevel.map(c=>`
      <div class="vtc vtc-${c.status}" data-ev="${esc(c.name)}">
        <span class="vtc-verdict vtc-v-${c.status}">${esc(VERDICT_WORD[c.status]||c.status)}</span>
        <div class="vtc-body">
          <div class="vtc-h"><b>${esc(VAL_CAT_LABELS[c.category]||c.category)}</b>
            <span class="vtc-fig">${esc(headlineFigure(c))}</span></div>
          <div class="vtc-detail muted">${esc(c.detail)}</div>
        </div>
        <button class="btn-xs vtc-more">evidence</button>
        <div class="vtc-ev" hidden>${evidenceHtml(c)}</div>
      </div>`).join("");

    paintValMatrix();
  }

  function paintValMatrix(){
    const q = ($("#val-filter") && $("#val-filter").value || "").trim().toLowerCase();
    const mode = VAL.filter || "all";
    const rows = (VAL.rows||[]).filter(r=>{
      if(q && !r.attribute.toLowerCase().includes(q)) return false;
      if(mode==="issues")   return r.worst==="fail" || r.worst==="warn";
      if(mode==="untested") return r.worst==="untested";
      return true;
    });
    const head = `<tr><th>Target attribute</th>${
      ATTR_CATS.map(k=>`<th>${esc(VAL_CAT_LABELS[k]||k)}</th>`).join("")}<th></th></tr>`;
    const body = rows.map((r,i)=>`
      <tr class="vrow vrow-${r.worst}" data-i="${i}">
        <td class="mono vrow-a">${esc(r.attribute)}</td>
        ${ATTR_CATS.map(k=>{
          const c = r.cells[k]; const st = c ? c.status : "absent";
          return `<td><button class="vcell ${CELL_CLS[st]}" data-i="${i}" data-k="${k}"
                    ${c?"":"disabled"}>${CELL_TXT[st]}</button></td>`;}).join("")}
        <td class="vrow-x muted">${r.worst==="untested"?"no test applies":""}</td>
      </tr>
      <tr class="vdet" data-i="${i}" hidden><td colspan="${ATTR_CATS.length+2}"><div class="vdet-in"></div></td></tr>`).join("");
    $("#val-matrix").innerHTML = rows.length
      ? `<div class="tbl-wrap"><table class="tbl val-tbl">${head}${body}</table></div>`
      : `<div class="muted" style="padding:10px">No attribute matches this filter.</div>`;
    const shown = rows.length, total = (VAL.rows||[]).length;
    const untested = (VAL.rows||[]).filter(r=>r.worst==="untested").length;
    $("#val-out-sub").textContent =
      `${total} target attribute(s), each with every check that applies to it`
      + (untested?` · ${untested} have no applicable test`:"")
      + (shown!==total?` · showing ${shown}`:"");
  }

  function valResultsCsv(){
    const lines = [["target_attribute", ...ATTR_CATS, "violations"].join(",")];
    (VAL.rows||[]).forEach(r=>{
      const v = ATTR_CATS.reduce((n,k)=> n + ((r.cells[k]||{}).offending_rows||0), 0);
      lines.push([r.attribute, ...ATTR_CATS.map(k=>(r.cells[k]||{}).status||"absent"), v].join(","));
    });
    return lines.join("\n");
  }

  window.renderValidationTab = function(){
    const T = window.TF;
    const gate = $("#val-gate"), grid = $("#val-grid"), out = $("#val-out-panel");
    if(!gate || !grid) return;
    if(!T || (!T.csv && !VAL.imported)){
      gate.hidden = false; grid.hidden = true; out.hidden = true;
      // still show the inputs panel — it explains WHY the gate is up
      VAL.csvRows = VAL.csvCols = null;
      renderValInputs();
      return;
    }
    gate.hidden = true; grid.hidden = false;
    const _c = valCsv();
    VAL.csvRows = _c.trim() ? (_c.trim().split("\n").length - 1) : null;
    VAL.csvCols = _c.trim() ? (_c.trim().split("\n")[0].split(",").length) : null;
    renderValInputs();
    if(VAL._csvRef !== valCsv()){
      VAL._csvRef = valCsv(); VAL.code = null; VAL.result = null;
      markRail(0, -1);
      $("#val-generate").textContent = "Generate script";
      $("#val-copy").disabled = true; $("#val-dl-code").disabled = true;
      $("#val-run").disabled = true;
      $("#val-gutter").innerHTML = ""; $("#val-code").textContent = "";
      $("#val-rules-panel").hidden = true; $("#val-rules").innerHTML = "";
      $("#val-summary").innerHTML = "";
      $("#val-table-checks").innerHTML = ""; $("#val-matrix").innerHTML = "";
      VAL.rows = null;
      out.hidden = true;
      ["#val-stat-checks","#val-stat-pass","#val-stat-fail","#val-stat-time"].forEach(id=>$(id).textContent="—");
      $("#val-console").innerHTML = `<span class="tf-prompt">$</span> <span id="val-console-line" class="muted">Generate the script first, then run it.</span>`;
    }
  };

  async function generateVal(){
    const T = window.TF;
    if(!T || !T.spec) return;
    const btn = $("#val-generate");
    btn.disabled = true; btn.classList.add("running");
    markRail(0, 0);
    try{
      const r = await (await fetch("/api/validate/codegen",{method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({spec:T.spec})})).json();
      if(r.error){ $("#val-code").textContent = "# "+r.error; return; }
      VAL.code = r.code;
      markRail(1, -1);
      gutterRender("val", r.code);
      $("#val-copy").disabled = false; $("#val-dl-code").disabled = false;
      $("#val-run").disabled = false;
      const rules = r.rules || [];
      $("#val-rules-panel").hidden = !rules.length;
      $("#val-rules-sub").textContent = `${rules.length} rule${rules.length===1?"":"s"} the script checks against the delivered output.`;
      $("#val-rules").innerHTML = renderRulePreview(rules);
      const line = $("#val-console-line"); if(line) line.textContent = "Ready. Press “Run validation” to execute.";
      btn.textContent = "Regenerate";
    }catch(e){
      $("#val-code").textContent = "# codegen failed: "+e;
    }finally{
      btn.disabled = false; btn.classList.remove("running");
    }
  }

  function consoleLine(prefix, html, cls){
    const c = $(`#${prefix}-console`);
    const span = document.createElement("span");
    span.className = "runline "+(cls||"");
    span.innerHTML = html;
    c.appendChild(span); c.scrollTop = c.scrollHeight;
  }

  async function runVal(){
    const T = window.TF;
    if(!T || !T.spec || !valCsv()) return;
    VAL.csvRows = valCsv().trim() ? (valCsv().trim().split("\n").length - 1) : null;
    VAL.csvCols = valCsv().trim() ? (valCsv().trim().split("\n")[0].split(",").length) : null;
    const btn = $("#val-run");
    btn.disabled = true; btn.classList.add("running");
    $("#val-console").innerHTML = `<span class="tf-prompt">$</span> <span class="muted">python validate_${esc(T.target||"target")}.py</span>`;
    const t0 = performance.now();
    markRail(1, 1);
    try{
      const r = await (await fetch("/api/validate/run",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({spec:T.spec, csv:valCsv()})})).json();
      const ms = Math.max(1, Math.round(performance.now()-t0));
      if(r.error){
        consoleLine("val", `✗ ${esc(r.error)}`, "err");
        $("#val-stat-time").textContent = ms+"ms";
        return;
      }
      VAL.result = r;
      markRail(2, -1);
      // target-dictionary order, so the table reads like the dictionary
      TF.targetAttrNames = (r.checks||[]).reduce((acc,c)=>{
        if(c.target_attribute && !acc.includes(c.target_attribute)) acc.push(c.target_attribute);
        return acc; }, []);
      $("#val-stat-checks").textContent = r.stats.checks;
      $("#val-stat-pass").textContent = r.stats.passed;
      $("#val-stat-fail").textContent = r.stats.failures;
      $("#val-stat-time").textContent = ms+"ms";
      consoleLine("val", `✓ ${r.stats.checks} check(s) run — verdict: ${esc(r.verdict)}`, r.verdict==="blocked"?"err":"ok");
      $("#val-out-panel").hidden = false;

      renderValResults(r);
      $("#val-out-panel").scrollIntoView({behavior:"smooth", block:"nearest"});
    }catch(e){
      consoleLine("val", `✗ ${esc(String(e))}`, "err");
    }finally{
      btn.disabled = false; btn.classList.remove("running");
    }
  }

  document.addEventListener("DOMContentLoaded", wire);
  if(document.readyState!=="loading") wire();
  function wire(){
    const gen = $("#val-generate"); if(gen && !gen._wired){ gen._wired=true; gen.onclick = generateVal; }
    const run = $("#val-run"); if(run && !run._wired){ run._wired=true; run.onclick = runVal; }
    const cp = $("#val-copy"); if(cp && !cp._wired){ cp._wired=true;
      cp.onclick = async ()=>{ if(!VAL.code) return;
        try{ await navigator.clipboard.writeText(VAL.code); cp.textContent="Copied ✓";
             setTimeout(()=>cp.textContent="Copy",1200); }catch(e){} }; }
    const fl = $("#val-filter"); if(fl && !fl._wired){ fl._wired=true;
      fl.oninput = ()=> paintValMatrix(); }
    document.querySelectorAll("button.vf").forEach(b=>{ if(b._wired) return; b._wired=true;
      b.onclick = ()=>{ VAL.filter = b.dataset.vf;
        document.querySelectorAll("button.vf").forEach(x=>x.classList.toggle("on", x===b));
        paintValMatrix(); }; });
    const rd = $("#val-dl-results"); if(rd && !rd._wired){ rd._wired=true;
      rd.onclick = ()=>{ if(VAL.rows) downloadText("validation_results.csv", valResultsCsv(), "text/csv"); }; }
    // evidence expands on click — rendered lazily, so a 50-attribute table
    // does not build 150 evidence blocks it will never show
    const mx = $("#val-matrix"); if(mx && !mx._wired){ mx._wired=true;
      mx.addEventListener("click", ev=>{
        const btn = ev.target.closest(".vcell"); if(!btn || btn.disabled) return;
        const i = btn.dataset.i, k = btn.dataset.k;
        const det = mx.querySelector(`tr.vdet[data-i="${i}"]`); if(!det) return;
        const cell = (VAL.rows[i]||{}).cells[k];
        const openK = det.dataset.k;
        if(!det.hidden && openK===k){ det.hidden = true; det.dataset.k=""; return; }
        det.querySelector(".vdet-in").innerHTML =
          `<div class="vdet-h">${esc(VAL_CAT_LABELS[k]||k)} · ${esc(VAL.rows[i].attribute)}</div>`
          + evidenceHtml(cell);
        det.hidden = false; det.dataset.k = k;
        wireEvidenceDownloads(det);
      }); }
    const tc = $("#val-table-checks"); if(tc && !tc._wired){ tc._wired=true;
      tc.addEventListener("click", ev=>{
        const b = ev.target.closest(".vtc-more"); if(!b) return;
        const box = b.parentElement.querySelector(".vtc-ev"); box.hidden = !box.hidden;
        b.textContent = box.hidden ? "evidence" : "hide";
        if(!box.hidden) wireEvidenceDownloads(box); }); }
    const dc = $("#val-dl-code"); if(dc && !dc._wired){ dc._wired=true;
      dc.onclick = ()=>{ if(VAL.code) downloadText(`validate_${(window.TF&&window.TF.target)||"target"}.py`, VAL.code, "text/x-python"); }; }
  }

  /* ---- shared with the reconciliation workspace ----------------------
     These three are defined in this module but used by the reconciliation
     module below, which is a separate IIFE and cannot see this scope.
     Exporting beats duplicating: `valCsv` in particular decides WHICH csv is
     under test (an imported file overrides the transformation output), and two
     copies of that rule would eventually disagree about what was validated
     versus what was reconciled. */
  window.valCsv = valCsv;
  window.valFeedCard = valFeedCard;
  window.showTextModal = showTextModal;
  // the reconciliation module renders the same evidence block, so it shares the
  // renderer rather than growing a second copy that can drift from this one
  window.sampleTable = sampleTable;
  window.sampleCsv = sampleCsv;
  window.wireEvidenceDownloads = wireEvidenceDownloads;
  window.VERDICT_WORD = VERDICT_WORD;
})();

/* ============================================================
   TAB 4 · RECONCILIATION WORKSPACE (additive module)
   Technical (row counts, per-field value loss, numeric aggregate diffs)
   + business-rule (cross-field dependencies, calculated-field
   derivations) reconciliation against the DELIVERED transform output.
   Same generate / display / run-on-instruction pattern as tab 3.
   ============================================================ */
(function(){
  "use strict";
  const $ = s => document.querySelector(s);
  const REC = { code:null, _csvRef:null, result:null };
  // Deliberate ORDER, not just membership: control totals lead the technical
  // side because they are the figures a reviewer checks first, and category
  // profiles lead the business side for the same reason. Checks are grouped
  // under their family rather than arriving in emission order, which
  // interleaved grain, value-loss and aggregate cards unpredictably.
  // value_loss, aggregate and derivation were removed from the backend:
  // subsumed by validation's transform check, demoted to a reported control
  // total, and structurally dead respectively. Grain folded into control
  // totals — it was the one check duplicated with the validation workspace.
  const TECH_ORDER = ["control_total"];
  // aggregate_by was missing, so a business-authored control ran, produced a
  // check, and was then filtered out of the display by groupedChecks — the
  // reviewer's own rule appeared nowhere in the results.
  const BIZ_ORDER  = ["category_profile","crossfield","aggregate_by"];
  const TECH_CATS = new Set(TECH_ORDER);
  const BIZ_CATS  = new Set(BIZ_ORDER);
  const REC_CAT_LABELS = {
    control_total:"Control totals", category_profile:"Category counts",
    crossfield:"Cross-field rules", aggregate_by:"Business aggregates"};

  const REC_SAMPLES = {};   // this module's own evidence store — VAL keeps its

  /* (4) The reconciliation dashboard deliberately does NOT copy validation's.
     Validation answers "was every value examined and did any differ?".
     Reconciliation answers "do the totals tie out?" — so the summary is a
     source-vs-delivered ledger, which is what a control sheet carries and what
     a reviewer checks first. */
  /* The summary answers "does it reconcile?" — it must not restate the
     figures. The first version was a ledger of rows/columns/keys/fill/sums,
     every one of which appears verbatim in the technical cards directly below
     it, so the reader paid for the same information twice.

     Counts alone would be thin (the same weakness as "63 checks run"), so the
     verdict word leads and the counts qualify it — the same shape as the
     validation summary, so the two workspaces read alike. */
  const REC_VERDICT_LABEL = {certified:"Reconciled", blocked:"Not reconciled",
                             needs_review:"Needs review"};
  const REC_VERDICT_WHY = {
    certified: "Every control total ties out and every business control reconciles.",
    blocked: "At least one control does not reconcile — the delivered data and the source disagree.",
    needs_review: "A soft control did not reconcile. Worth a look before this is signed off."};

  function renderRecDashboard(r){
    const checks = r.checks || [];
    const fam = (cats) => {
      const set = checks.filter(c => cats.includes(c.category));
      return {n: set.length,
              passed: set.filter(c => c.status === "pass").length,
              failed: set.filter(c => c.status === "fail").length,
              warned: set.filter(c => c.status === "warn").length};
    };
    const tech = fam(TECH_ORDER), biz = fam(BIZ_ORDER);
    const discrepancies = checks.filter(c => c.status === "fail")
                                .reduce((n, c) => n + (c.offending_rows || 0), 0);
    const failing = checks.filter(c => c.status === "fail");
    const vClass = r.verdict === "certified" ? "ok"
                 : r.verdict === "blocked" ? "bad" : "warn";

    const tile = (k, f, note) => `
      <div class="vs-card"><div class="vs-k">${esc(k)}</div>
        <div class="vs-v ${f.failed?"bad":"ok"}">${f.passed}<span class="vs-of">/${f.n}</span></div>
        <div class="vs-s">${esc(note)}${f.failed?` · <b class="bad">${f.failed} failed</b>`:""}${
          f.warned?` · ${f.warned} warning(s)`:""}</div></div>`;

    $("#rec-dash").innerHTML = `
      <div class="vs-assure ${failing.length?"has-bad":"clean"}">
        <div class="vs-assure-verdict ${vClass}">${esc(REC_VERDICT_LABEL[r.verdict]||r.verdict||"—")}</div>
        <div class="vs-assure-text">${
          failing.length
            ? `<b class="bad">${failing.length} control(s) did not reconcile</b> — `
              + failing.map(c=>`<span class="mono">${esc(c.name)}</span>`).join(", ")
              + `. Detail and evidence below.`
            : `<b class="ok">Everything reconciles.</b> Source and delivered agree on every
               control total and every business control.`}</div>
      </div>
      <div class="val-tiles">
        ${tile("Technical controls", tech, "control totals")}
        ${tile("Business controls", biz, "category counts, cross-field rules")}
        <div class="vs-card"><div class="vs-k">Discrepancies</div>
          <div class="vs-v ${discrepancies?"bad":"ok"}">${discrepancies.toLocaleString()}</div>
          <div class="vs-s">${discrepancies
            ? "records or buckets that do not agree — listed below"
            : "nothing to investigate"}</div></div>
      </div>`;
    $("#rec-dash-panel").hidden = false;
  }

  function wireRecEvidence(){
    ["#rec-tech-checks", "#rec-biz-checks"].forEach(sel=>{
      const root = $(sel); if(!root || root._wired) return; root._wired = true;
      root.addEventListener("click", ev=>{
        const b = ev.target.closest(".ccard-more"); if(!b) return;
        const box = b.parentElement.querySelector(".ccard-ev");
        box.hidden = !box.hidden;
        b.textContent = box.hidden ? "evidence" : "hide";
        if(!box.hidden) wireRecDownloads(box);
      });
    });
  }

  function wireRecDownloads(root){
    root.querySelectorAll("[data-ev-dl]").forEach(b=>{
      if(b._wired) return; b._wired = true;
      b.onclick = ()=>{
        const name = b.closest(".vev").getAttribute("data-ev-name");
        const rows = REC_SAMPLES[name];
        if(rows && rows.length)
          downloadText(name.replace(/[^\w.-]+/g,"_") + "_evidence.csv",
                       sampleCsv(rows), "text/csv");
      };
    });
  }

  function groupedChecks(checks, order){
    const by = {};
    checks.forEach(c=> (by[c.category] || (by[c.category]=[])).push(c));
    return order.filter(k=>by[k]).map(k=>{
      const fails = by[k].filter(c=>c.status!=="pass").length;
      return `<div class="rec-group">
        <div class="rec-group-h">${esc(REC_CAT_LABELS[k]||k)}
          <span class="muted">${by[k].length} check(s)${fails?` · ${fails} need attention`:""}</span>
        </div>
        <div class="cards">${by[k].map(checkCard).join("")}</div>
      </div>`;
    }).join("");
  }

  function gutterRender(prefix, code){
    const n = code.split("\n").length;
    $(`#${prefix}-gutter`).innerHTML = Array.from({length:n}, (_,i)=>`<span>${i+1}</span>`).join("");
    $(`#${prefix}-code`).textContent = code;
  }
  function downloadText(name, text, mime){
    const blob = new Blob([text],{type:mime}); const url = URL.createObjectURL(blob);
    const a = document.createElement("a"); a.href=url; a.download=name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 1000);
  }
  function statusIcon(s){ return s==="fail" ? "✗" : s==="warn" ? "⚠" : "✓"; }
  function checkCard(c){
    const rows = c.sample || [];
    const body = `<div class="vev" data-ev-name="${esc(c.name)}">
        <div class="vev-stats mono">${c.rows_scanned
            ? `${c.offending_rows||0} discrepanc${(c.offending_rows||0)===1?"y":"ies"} across ${c.rows_scanned} row(s) scanned`
            : `${c.offending_rows||0} discrepanc${(c.offending_rows||0)===1?"y":"ies"} — aggregate check`}</div>
        ${c.sql?`<pre class="vev-sql mono">${esc(c.sql)}</pre>`:""}
        ${rows.length?`<div class="vev-sample">
            <div class="vev-sample-h"><b>${c.status==="pass"?"Figures compared":"Offending records"}</b>
              <span class="muted">${rows.length} shown</span>
              <button class="btn-xs" data-ev-dl>Download .csv</button></div>
            ${sampleTable(rows)}</div>`:""}
      </div>`;
    if (rows.length) REC_SAMPLES[c.name] = rows;
    return `<div class="ccard ccard-${c.status}">
      <div class="ccard-h">
        <span class="vtc-verdict vtc-v-${c.status}">${esc(VERDICT_WORD[c.status]||c.status)}</span>
        <b>${esc(c.name)}</b>
      </div>
      <div class="ccard-detail">${esc(c.detail)}</div>
      <button class="btn-xs ccard-more">evidence</button>
      <div class="ccard-ev" hidden>${body}</div>
    </div>`;
  }
  function ruleCard(rule){
    return `<div class="rulecard">
      <div class="rulecard-name">${esc(rule.name)}</div>
      <div class="rulecard-detail">${esc(rule.detail)}</div>
    </div>`;
  }
  function renderRulePreview(rules){
    // one card per FAMILY, side by side — a rule-per-attribute preview was
    // dozens of near-identical cards on a realistic target
    const famCard = (k, rs)=>{
      const scope = rs.map(r=>r.scope).filter(Boolean).join(" · ");
      // A rule family may carry ITEMISED rules. Rendering five of them inside
      // one semicolon-joined sentence was unreadable and got worse as the
      // source's complexity grew — they belong in a table.
      const items = rs.flatMap(r=>r.items||[]);
      const table = items.length ? `<table class="rule-items">${items.map(it=>`
          <tr><td class="mono">${esc(it.attribute)}</td>
              <td class="muted">only when</td>
              <td class="mono">${esc(it.driver)}</td>
              <td class="rule-eq">=</td>
              <td>${(it.values||[]).map(v=>`<span class="chip">${esc(v)}</span>`).join(" ")}</td>
          </tr>`).join("")}</table>` : "";
      return `<div class="rulecard">
        <div class="rulecard-name">${esc(REC_CAT_LABELS[k]||k)}</div>
        ${scope?`<div class="rulecard-scope">${esc(scope)}</div>`:""}
        <div class="rulecard-detail">${rs.map(r=>esc(r.detail)).join(" ")}</div>
        ${table}
      </div>`;
    };
    const build = (order)=>{
      const by = {};
      rules.forEach(rl=> (by[rl.category] || (by[rl.category]=[])).push(rl));
      return order.filter(k=>by[k]).map(k=>famCard(k, by[k])).join("");
    };
    const tech = build(TECH_ORDER), biz = build(BIZ_ORDER);
    let html = "";
    if(tech) html += `<div class="rule-group-h">Technical</div><div class="rulegrid">${tech}</div>`;
    if(biz) html += `<div class="rule-group-h">Business rule</div><div class="rulegrid">${biz}</div>`;
    return html;
  }

  /* Reconciliation inputs — the same three feeds as tabs 2 and 3, using the
     shared valFeedCard so all three panels stay identical. */

  /* ---- reconciliation controls: propose -> certify -> generate -> run ----
     The reviewer decides which controls apply and may add the business's own.
     A user control is entered STRUCTURALLY — attribute and driver come from the
     target dictionary, values from that driver's declared domain — so the form
     cannot express a rule that names a column which does not exist or a value
     outside the domain. There is no natural-language step and therefore nothing
     to mistranslate into SQL. */
  const RULES = {proposed: [], attributes: [], excluded: new Set(),
                 added: [], certified: null};

  const RULE_FAMILY = {control_total:"Control totals",
                       category_profile:"Category counts",
                       crossfield:"Cross-field rules",
                       aggregate_by:"Business aggregates"};

  async function proposeRecRules(){
    const T = window.TF; if(!T || !T.spec) return;
    const btn = $("#rec-propose");
    btn.disabled = true; btn.classList.add("running");
    markRail(0, 0);
    try{
      const r = await (await fetch("/api/reconcile/rules",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({spec:T.spec})})).json();
      if(r.error){ $("#rec-rules-sub").textContent = r.error; return; }
      RULES.proposed = r.rules || [];
      RULES.attributes = r.attributes || [];
      // A fresh proposal starts uncertified: these controls were just derived,
      // so nobody has approved them. Carrying a previous certification forward
      // greyed out the whole panel the moment "Propose controls" was pressed.
      RULES.certified = (r.certified || []).length ? r.certified : null;
      RULES.excluded.clear();
      RULES.added.length = 0;
      markRail(1, -1);
      renderRecRules();
    }catch(e){ $("#rec-rules-sub").textContent = "Could not propose controls: "+e; }
    finally{ btn.classList.remove("running"); }
  }

  /* Before anything is proposed the panel explains the step rather than sitting
     empty — the derivation used to happen silently on render, so a reader never
     saw that controls are proposed from the specification at all. */
  function renderRecRulesEmpty(){
    $("#rec-rules-sub").textContent =
      "Controls are derived from the certified mapping, the target dictionary "
      + "and the source data. Propose them, then decide which apply.";
    $("#rec-rules-list").innerHTML =
      `<div class="rules-empty muted">No controls proposed yet.
         Press <b>Propose controls</b> to derive them.</div>`;
    $("#rec-propose").disabled = false;
    $("#rec-propose").textContent = "Propose controls";
    $("#rec-add-toggle").disabled = true;
    $("#rec-certify").disabled = true;
    $("#rec-rules-review").hidden = false;
  }

  function ruleSummary(rule){
    const p = rule.params || {};
    if(rule.kind === "crossfield")
      return `<span class="mono">${esc(p.attribute)}</span> populated only when
              <span class="mono">${esc(p.driver)}</span> is
              ${(p.values||[]).map(v=>`<span class="chip">${esc(v)}</span>`).join(" ")}`;
    if(rule.kind === "aggregate_by"){
      const p2 = rule.params || {};
      return `<span class="mono">${esc(p2.function)}</span> of
              <span class="mono">${esc(p2.column || "records")}</span>`
             + ((p2.group_by||[]).length
                 ? ` for each <span class="mono">${(p2.group_by||[]).map(esc).join(" × ")}</span>`
                 : ` across the whole file`)
             + ` reconciles source vs delivered`;
    }
    if(rule.kind === "category_profile")
      return `record counts per value of <span class="mono">${esc(p.attribute)}</span>
              reconcile between source and delivered`;
    return esc(rule.title || rule.kind);
  }

  function renderRecRules(){
    const all = RULES.proposed.concat(RULES.added);
    const fams = {};
    all.forEach(r=>{ (fams[r.category||r.kind] ||= []).push(r); });
    const order = Object.keys(RULE_FAMILY).filter(k=>fams[k]);
    const included = all.length - RULES.excluded.size;

    $("#rec-rules-sub").innerHTML = RULES.certified
      ? `<b class="ok">${RULES.certified.length} control(s) certified.</b>
         The script and the report below both run exactly these.`
      : `${all.length} control(s) proposed — ${included} selected.
         Deselect any that do not apply, add the business's own, then certify.`;

    $("#rec-rules-list").innerHTML = order.map(k=>`
      <div class="rec-group">
        <div class="rec-group-h">${esc(RULE_FAMILY[k]||k)}
          <span class="muted">${fams[k].length} control(s)</span></div>
        ${fams[k].map(r=>{
          const id = r.id || `${r.kind}:${(r.params||{}).attribute||""}`;
          const off = RULES.excluded.has(id);
          const mine = r.origin === "user_added";
          return `<label class="rulerow ${off?"off":""}">
            <input type="checkbox" data-rule="${esc(id)}" ${off?"":"checked"}
                   ${RULES.certified?"disabled":""}>
            <span class="rulerow-t">${ruleSummary(r)}</span>
            ${mine?`<span class="chip origin">added by the business</span>`:""}
          </label>`;
        }).join("")}
      </div>`).join("");

    $("#rec-certify").disabled = !!RULES.certified;
    $("#rec-certify").textContent = RULES.certified ? "Controls certified ✓" : "Certify controls →";
    $("#rec-add-toggle").disabled = !!RULES.certified;
    $("#rec-propose").disabled = !!RULES.certified;
    $("#rec-propose").textContent = RULES.proposed.length ? "Re-propose" : "Propose controls";
    $("#rec-rules-review").hidden = false;
  }

  /* Business-authored controls are AGGREGATES, not cross-field conditions.
     "does the total sum assured for each product still agree?" is the control a
     business reviewer actually asks for; cross-field conditions are mined from
     the data already, so asking a human to re-enter them added nothing.
     Everything is chosen from the target dictionary — function, column,
     breakdown — so an invalid control cannot be expressed. */
  const AGG_FUNCS = [
    ["sum", "Sum of", true], ["avg", "Average of", true],
    ["min", "Minimum of", true], ["max", "Maximum of", true],
    ["count", "Count of records", false],
    ["count_distinct", "Distinct count of", false],
  ];

  function renderAddForm(){
    const NUMERIC = new Set(["number","integer","int","float","decimal","numeric"]);
    const numeric = RULES.attributes.filter(a=>NUMERIC.has((a.type||"").toLowerCase()));
    const groupable = RULES.attributes.filter(a=>!NUMERIC.has((a.type||"").toLowerCase()));
    $("#rec-add-form").innerHTML = `
      <div class="addrule">
        <div class="addrule-h">Add a control
          <span class="muted">— reconcile an aggregate between the source and the delivered data</span></div>
        <div class="addrule-row">
          <select id="ar-fn">${AGG_FUNCS.map(([v,l])=>`<option value="${v}">${esc(l)}</option>`).join("")}</select>
          <select id="ar-col">${RULES.attributes.map(a=>`<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("")}</select>
          <span class="muted">broken down by</span>
          <select id="ar-group" multiple size="5">${groupable.map(a=>
            `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("")}</select>
          <button class="btn-sm" id="ar-add">Add control</button>
        </div>
        <div class="muted addrule-n" id="ar-preview"></div>
      </div>`;

    const syncCols = ()=>{
      const fn = $("#ar-fn").value;
      const needsNumeric = ["sum","avg","min","max"].includes(fn);
      const pool = needsNumeric ? numeric : RULES.attributes;
      $("#ar-col").innerHTML = pool.map(a=>
        `<option value="${esc(a.name)}">${esc(a.name)}</option>`).join("");
      $("#ar-col").disabled = (fn === "count");
      preview();
    };
    const chosen = ()=> Array.from($("#ar-group").selectedOptions).map(o=>o.value);
    const preview = ()=>{
      const fn = $("#ar-fn").value, col = $("#ar-col").value, g = chosen();
      const label = AGG_FUNCS.find(f=>f[0]===fn);
      $("#ar-preview").innerHTML =
        `Will check: <b>${esc(label?label[1]:fn)}${fn==="count"?"":" "+esc(col)}</b>`
        + (g.length?` for each <b>${g.map(esc).join(" × ")}</b>`:"")
        + ` reconciles between the source and the delivered file.`
        + ` <span class="muted">Leave the breakdown empty for a whole-file total.</span>`;
    };
    $("#ar-fn").onchange = syncCols;
    $("#ar-col").onchange = preview;
    $("#ar-group").onchange = preview;
    syncCols();

    $("#ar-add").onclick = ()=>{
      const fn = $("#ar-fn").value;
      const col = fn === "count" ? null : $("#ar-col").value;
      const groups = chosen();
      // never submit a half-built control: the server would reject it, but the
      // reviewer should not have to discover that at certification time
      if(!fn || (fn !== "count" && !col)){
        $("#ar-preview").innerHTML =
          `<b class="bad">Choose a function and a column first.</b>`;
        return;
      }
      const label = AGG_FUNCS.find(f=>f[0]===fn);
      const id = `aggregate_by:${fn}:${col||"*"}${groups.length?"~"+groups.join("+"):""}`;
      if(RULES.added.some(r=>r.id===id)) { $("#rec-add-form").hidden = true; return; }
      RULES.added.push({kind:"aggregate_by", origin:"user_added",
        category:"aggregate_by", severity:"hard", id,
        title:`${label?label[1]:fn}${col?" "+col:" records"}`
              + (groups.length?` by ${groups.join(", ")}`:""),
        params:{function:fn, column:col, group_by:groups}});
      $("#rec-add-form").hidden = true;
      renderRecRules();
    };
  }

  function gateRecScript(){
    const gen = $("#rec-generate"); if(!gen) return;
    const ready = !!RULES.certified;
    gen.disabled = !ready;
    gen.title = ready ? "" : "Certify the controls above first";
    const line = $("#rec-console-line");
    if(line && !REC.code)
      line.textContent = ready
        ? "Controls certified. Press “Generate script”."
        : "Certify the reconciliation controls above, then generate the script.";
    const sub = $("#rec-code-sub");
    if(sub) sub.textContent = ready
      ? `Generated from the ${RULES.certified.length} certified control(s) — the same rules the report executes.`
      : "Generated from the certified controls above — the same rules the report executes.";
  }

  async function certifyRecRules(){
    const T = window.TF; if(!T || !T.spec) return;
    const btn = $("#rec-certify"); btn.disabled = true;
    const decisions = {};
    RULES.excluded.forEach(id=>{ decisions[id] = {action:"reject"}; });
    try{
      const r = await (await fetch("/api/reconcile/certify",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({spec:T.spec, decisions,
          added: RULES.added.filter(a=>!RULES.excluded.has(a.id))})})).json();
      if(r.error){ $("#rec-rules-sub").textContent = r.error; btn.disabled = false; return; }
      RULES.certified = r.certified || [];
      // a control the reviewer asked for that could not be executed must be
      // SHOWN, never silently dropped
      $("#rec-rules-rejected").innerHTML = (r.rejected||[])
        .filter(x=>x.reason && x.reason !== "excluded by the reviewer")
        .map(x=>`<div class="rule-reject"><b>Not certified:</b>
            ${esc(JSON.stringify(x.params||{}))} — ${esc(x.reason)}</div>`).join("");
      renderRecRules();
      gateRecScript();
      markRail(2, -1);
      const grid = $("#rec-grid");
      if (grid && grid.scrollIntoView) grid.scrollIntoView({behavior:"smooth", block:"nearest"});
    }catch(e){ $("#rec-rules-sub").textContent = "Certify failed: "+e; btn.disabled = false; }
  }

  function renderRecInputs(){
    const el = $("#rec-feeds"); if(!el) return;
    const panel = $("#rec-inputs-panel"); if(panel) panel.hidden = false;
    const T = window.TF || {};
    const spec = (T.spec && T.spec.mappings) ? T.spec : null;
    const files = (spec && spec.source_tables) || [];
    const csv = valCsv();
    const rows = csv.trim() ? csv.trim().split("\n").length - 1 : null;
    const cols = csv.trim() ? csv.trim().split("\n")[0].split(",").length : null;
    const specName = T.importedName || (spec ? `${spec.target_table||"target"}_mapping.json` : "");
    el.innerHTML =
      valFeedCard("Certified mapping", "",
        spec ? [{text: specName, act:'data-rec-view-spec class="mono"'}] : [],
        spec ? `${(spec.mappings||[]).length} mapping(s) — certified on 1 · Mapping workspace`
             : "run and certify a mapping first", !!spec)
    + valFeedCard("Transformed target data", "",
        rows!=null ? [{text:`${T.target||"target"}.csv`, act:'data-rec-view-csv class="mono"'}] : [],
        rows!=null ? `${rows} row(s) × ${cols} column(s) — the delivered side of the reconciliation`
                   : "produced by 2 · Transformation workspace", rows!=null)
    + valFeedCard("Source data", "",
        files.map(f=>({text:f, act:`data-rec-view-src="${esc(f)}" class="mono"`})),
        (files.length>1 ? `${files.length} files joined — ` : "")
        + "the source side: control totals and category counts are recomputed from it",
        !!files.length);
    el.querySelectorAll("[data-rec-view-spec]").forEach(b=>
      b.onclick = ()=> showTextModal("Certified mapping spec", JSON.stringify(spec, null, 2)));
    el.querySelectorAll("[data-rec-view-csv]").forEach(b=>
      b.onclick = ()=> showTextModal("Transformed target data", csv));
    el.querySelectorAll("[data-rec-view-src]").forEach(b=>
      b.onclick = async ()=>{
        const t = b.getAttribute("data-rec-view-src");
        try{
          const man = await (await fetch("/api/inputs")).json();
          const rec = (man.sources||[]).find(f => f.name.replace(/\.[^.]+$/,"") === t);
          if(!rec) return showTextModal(t, "source file not found in the registry");
          const raw = await (await fetch("/api/raw?id="+encodeURIComponent(rec.id))).json();
          showTextModal(raw.name || t, raw.content || "");
        }catch(e){ showTextModal(t, "could not load: "+e); }
      });
  }

  window.renderReconciliationTab = function(){
    const T = window.TF;
    const gate = $("#rec-gate"), grid = $("#rec-grid"), out = $("#rec-out-panel"), biz = $("#rec-biz-panel");
    if(!gate || !grid) return;
    if(!T || !valCsv()){
      gate.hidden = false; grid.hidden = true; out.hidden = true; biz.hidden = true;
      renderRecInputs();
      return;
    }
    gate.hidden = true;
    renderRecInputs();
    // The script step stays VISIBLE and is gated by disabling its action —
    // hiding the panel removed the step from the workflow entirely, so a
    // first-time reader could not see that a script gets generated at all.
    grid.hidden = false;
    gateRecScript();
    if(!RULES.proposed.length) renderRecRulesEmpty(); else renderRecRules();
    if(REC._csvRef !== valCsv()){
      REC._csvRef = valCsv(); REC.code = null; REC.result = null;
      RULES.certified = null; RULES.excluded.clear(); RULES.added.length = 0;
      RULES.proposed = [];
      markRail(0, -1);
      $("#rec-generate").textContent = "Generate script";
      $("#rec-copy").disabled = true; $("#rec-dl-code").disabled = true;
      $("#rec-run").disabled = true;
      $("#rec-gutter").innerHTML = ""; $("#rec-code").textContent = "";
      out.hidden = true; biz.hidden = true;
      ["#rec-stat-tech","#rec-stat-biz","#rec-stat-fail","#rec-stat-time"].forEach(id=>$(id).textContent="—");
      $("#rec-console").innerHTML = `<span class="tf-prompt">$</span> <span id="rec-console-line" class="muted">Generate the script first, then run it.</span>`;
    }
  };

  async function generateRec(){
    const T = window.TF;
    if(!T || !T.spec) return;
    const btn = $("#rec-generate");
    btn.disabled = true; btn.classList.add("running");
    markRail(2, 2);
    try{
      const r = await (await fetch("/api/reconcile/codegen",{method:"POST",
        headers:{"Content-Type":"application/json"}, body:JSON.stringify({spec:T.spec})})).json();
      if(r.error){ $("#rec-code").textContent = "# "+r.error; return; }
      REC.code = r.code;
      markRail(3, -1);
      gutterRender("rec", r.code);
      $("#rec-copy").disabled = false; $("#rec-dl-code").disabled = false;
      $("#rec-run").disabled = false;
      // No "rules this script checks" panel here: the certified controls panel
      // above IS that list, and showing it twice invited the two to disagree.
      const line = $("#rec-console-line"); if(line) line.textContent = "Ready. Press “Run reconciliation” to execute.";
      btn.textContent = "Regenerate";
    }catch(e){
      $("#rec-code").textContent = "# codegen failed: "+e;
    }finally{
      btn.disabled = false; btn.classList.remove("running");
    }
  }

  function consoleLine(prefix, html, cls){
    const c = $(`#${prefix}-console`);
    const span = document.createElement("span");
    span.className = "runline "+(cls||"");
    span.innerHTML = html;
    c.appendChild(span); c.scrollTop = c.scrollHeight;
  }

  async function runRec(){
    const T = window.TF;
    // valCsv(), not T.csv: an imported target file overrides the transformation
    // output, and this guard silently returned for it — the button appeared to
    // do nothing at all.
    if(!T || !T.spec || !valCsv()) return;
    const btn = $("#rec-run");
    btn.disabled = true; btn.classList.add("running");
    $("#rec-console").innerHTML = `<span class="tf-prompt">$</span> <span class="muted">python reconcile_${esc(T.target||"target")}.py</span>`;
    const t0 = performance.now();
    markRail(3, 3);
    try{
      const r = await (await fetch("/api/reconcile/run",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({spec:T.spec, csv:valCsv()})})).json();
      const ms = Math.max(1, Math.round(performance.now()-t0));
      if(r.error){
        consoleLine("rec", `✗ ${esc(r.error)}`, "err");
        $("#rec-stat-time").textContent = ms+"ms";
        return;
      }
      REC.result = r;
      markRail(4, -1);
      $("#rec-stat-tech").textContent = r.stats.technical;
      $("#rec-stat-biz").textContent = r.stats.business_rule;
      $("#rec-stat-fail").textContent = r.stats.failures;
      $("#rec-stat-time").textContent = ms+"ms";
      consoleLine("rec", `✓ ${r.stats.checks} check(s) run — verdict: ${esc(r.verdict)}`, r.verdict==="blocked"?"err":"ok");
      const techChecks = r.checks.filter(c=>TECH_CATS.has(c.category));
      const bizChecks = r.checks.filter(c=>BIZ_CATS.has(c.category));
      $("#rec-out-panel").hidden = false;
      wireRecEvidence();
      renderRecDashboard(r);
      $("#rec-tech-checks").innerHTML = techChecks.length
        ? groupedChecks(techChecks, TECH_ORDER)
        : `<div class="muted">No technical checks ran.</div>`;
      $("#rec-biz-panel").hidden = false;
      $("#rec-biz-checks").innerHTML = bizChecks.length
        ? groupedChecks(bizChecks, BIZ_ORDER)
        : `<div class="muted">No business-rule checks ran (load an enriched dictionary on tab 1, and check that the source data yields dependency patterns).</div>`;
      $("#rec-out-panel").scrollIntoView({behavior:"smooth", block:"nearest"});
    }catch(e){
      consoleLine("rec", `✗ ${esc(String(e))}`, "err");
    }finally{
      btn.disabled = false; btn.classList.remove("running");
    }
  }

  document.addEventListener("DOMContentLoaded", wire);
  if(document.readyState!=="loading") wire();
  function wire(){
    const gen = $("#rec-generate"); if(gen && !gen._wired){ gen._wired=true; gen.onclick = generateRec; }
    const pp = $("#rec-propose"); if(pp && !pp._wired){ pp._wired=true; pp.onclick = proposeRecRules; }
    const cy = $("#rec-certify"); if(cy && !cy._wired){ cy._wired=true; cy.onclick = certifyRecRules; }
    const at = $("#rec-add-toggle"); if(at && !at._wired){ at._wired=true;
      at.onclick = ()=>{ const f = $("#rec-add-form");
        if(f.hidden) renderAddForm();
        f.hidden = !f.hidden; }; }
    const rl = $("#rec-rules-list"); if(rl && !rl._wired){ rl._wired=true;
      rl.addEventListener("change", ev=>{
        const cb = ev.target.closest("[data-rule]"); if(!cb) return;
        const id = cb.getAttribute("data-rule");
        if(cb.checked) RULES.excluded.delete(id); else RULES.excluded.add(id);
        renderRecRules();
      }); }
    const run = $("#rec-run"); if(run && !run._wired){ run._wired=true; run.onclick = runRec; }
    const cp = $("#rec-copy"); if(cp && !cp._wired){ cp._wired=true;
      cp.onclick = async ()=>{ if(!REC.code) return;
        try{ await navigator.clipboard.writeText(REC.code); cp.textContent="Copied ✓";
             setTimeout(()=>cp.textContent="Copy",1200); }catch(e){} }; }
    const dc = $("#rec-dl-code"); if(dc && !dc._wired){ dc._wired=true;
      dc.onclick = ()=>{ if(REC.code) downloadText(`reconcile_${(window.TF&&window.TF.target)||"target"}.py`, REC.code, "text/x-python"); }; }
  }
})();
