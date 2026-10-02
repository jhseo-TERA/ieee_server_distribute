/* Survey jobs use server-captured metrics; text from papers/models is never HTML. */
(() => {
  "use strict";
  const el = id => document.getElementById(id);
  const selected = new Map();
  const controls = {rate_mode:"rate-mode", evidence_tier:"performance-source-mode", medium:"performance-medium-mode", subtype:"performance-subtype-mode", energy_scope:"performance-scope-mode", pareto_mode:"performance-pareto-mode"};
  let currentJob = null, polling = null, generation = 0, busy = false, sending = false;
  const labels = {survey_analyze:"현재 조건 분석", survey_compare:"선택 논문 비교"};
  const states = {queued:"대기 중", running:"분석 중", complete:"완료", failed:"실패", cancelled:"취소됨"};
  const node = (tag, text, className="") => { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(className)n.className=className; return n; };
  const filters = () => Object.fromEntries(Object.entries(controls).map(([key,id])=>[key,el(id).value]));
  const describe = f => Object.entries(controls).map(([key,id])=>[...el(id).options].find(o=>o.value===f[key])?.textContent || f[key]).join(" · ");
  function status(text, error=false) { el("sai-status").textContent=text; el("sai-status").classList.toggle("error",error); }
  async function api(path, payload) {
    const response = await fetch(`/api/ai${path}`, {credentials:"same-origin", cache:"no-store", ...(payload===undefined?{}:{method:"POST", headers:{"Content-Type":"application/json","X-CSRF-Token":CSRF_TOKEN}, body:JSON.stringify(payload)})});
    const data=await response.json().catch(()=>({}));
    if(!response.ok || data.ok===false) throw Error(response.status===401?"로그인이 만료되었습니다. 다시 로그인해 주세요.":data.error||`요청 실패 (${response.status})`);
    return data;
  }
  function syncSelection() {
    el("sai-compare").textContent=`선택 논문 AI 비교 (${selected.size}/5)`;
    el("sai-compare").disabled=sending||busy||selected.size<2;
    el("sai-analyze").disabled=sending||busy;
    el("sai-clear").disabled=!selected.size;
    document.querySelectorAll("[data-survey-select]").forEach(button=>{const on=selected.has(button.dataset.surveySelect);button.setAttribute("aria-pressed",String(on));button.textContent=on?"비교 선택됨 ✓":"비교 선택 +";});
    const tray=el("sai-selected");tray.replaceChildren();
    selected.forEach((title,key)=>{const button=node("button",`${title} ×`);button.type="button";button.title=title;button.setAttribute("aria-label",`${title} 비교 선택 해제`);button.addEventListener("click",()=>{selected.delete(key);syncSelection();});tray.append(button);});
  }
  window.SurveyAI={syncSelection};
  document.addEventListener("click",event=>{const button=event.target.closest("[data-survey-select]");if(!button)return;const key=button.dataset.surveySelect;if(selected.has(key))selected.delete(key);else if(selected.size<5)selected.set(key,button.dataset.title||key);else{status("최대 5편까지 비교할 수 있습니다. 기존 선택을 먼저 해제하세요.",true);el("survey-ai").scrollIntoView({behavior:"smooth"});return;}syncSelection();});
  function renderResult(job) {
    const result=job.result||{}, snap=job.request?.survey_snapshot;
    const data=result.summary?result:snap;
    const root=el("sai-result");root.replaceChildren();root.hidden=!data;
    if(!data)return;
    const header=node("div",undefined,"sai-result-header");header.append(node("h4",`${labels[job.kind]||"Survey 분석"} · ${states[job.status]||job.status}`),node("p",`${data.captured_at||""} · ${describe(data.summary.filters)}`,"sai-help"));root.append(header);
    root.append(node("p","아래 수치는 요청 시점의 서버 집계입니다. 필터를 바꿔도 이 기록은 바뀌지 않습니다. AI 해석과 분리해 확인하세요.","sai-notice"));
    const stats=node("div",undefined,"sai-stats"), counts=data.summary.chart_counts;
    [["연결 논문",data.summary.matched_papers],["속도 · 연도",counts.rate_year],["에너지 · 연도",counts.energy_year],["속도 · 에너지",counts.rate_energy],["Pareto frontier",counts.frontier],["AI 근거 표본",data.sample_count]].forEach(([label,value])=>{const box=node("div",undefined,"sai-stat");box.append(node("b",Number(value||0).toLocaleString()),node("span",label));stats.append(box);});root.append(stats);
    if(data.comparison?.length) {
      const wrap=node("div",undefined,"sai-table-wrap"),table=node("table"),head=node("tr");
      ["선택 논문 / 동작점","속도 (Gb/s)","에너지 (pJ/bit)","범위 / 조건"].forEach(t=>head.append(node("th",t)));const thead=node("thead");thead.append(head);table.append(thead);const tbody=node("tbody");
      data.comparison.forEach(p=>{(p.matching_points.length?p.matching_points:[null]).forEach(point=>{const row=node("tr"),m=point?.performance||{},rateField={lane:"lane_rate_gbps",reported:"reported_rate_gbps",aggregate:"aggregate_rate_gbps"}[data.summary.filters.rate_mode];row.append(node("td",`${p.title}\n#${m.measurement_id||"—"}${p.omitted_points?` · 다른 동작점 ${p.omitted_points}개 생략`:""}`),node("td",m[rateField]??"—"),node("td",m.energy_pj_bit??"—"),node("td",point?`${point.link_medium} / ${m.energy_component_scope||"unknown"} / rate ${m.rate_scope||"unknown"}; ${m.process_nm??"—"} nm; loss ${m.channel_loss_db??"—"} dB; BER ${m.ber??"—"}`:"현재 조건의 대표 성능점 없음"));tbody.append(row);});});table.append(tbody);wrap.append(table);root.append(wrap);
    }
    if(result.answer){
      root.append(node("h4","AI 해석 · 검토 필요"));
      if(result.validation)root.append(node("p",`제공 근거·수치 대조 통과 ${result.validation.accepted}개 · 제외 ${result.validation.rejected}개. 근거는 DB 값 정리이며 PDF 발췌가 아닙니다. 의미·범위는 원문 검토가 필요합니다.`,"sai-help"));
      else root.append(node("p","이전 형식의 분석 기록입니다. 근거 대조 전 결과이므로 다시 분석하는 것이 좋습니다.","sai-notice"));
      const statement=o=>((result.sources||[]).find(s=>s.source_id===o.source_id)?.kind==="survey_statistics"&&o.evidence_index!==undefined)?`서버 집계 · ${o.evidence_quote}`:o.statement;
      const answer=result.observations?.length?result.observations.map(o=>`${statement(o)} [${o.source_id}]`).join("\n\n"):result.answer;
      root.append(node("div",answer,"sai-answer"));
      if(result.observations?.length){const details=node("details");details.append(node("summary","설명별 DB 근거 대조"));result.observations.forEach(o=>{const block=node("div",undefined,"sai-source");block.append(node("b",`[${o.source_id}] ${statement(o)}`),node("pre",o.evidence_quote));details.append(block);});root.append(details);}
    }
    const limitations=result.limitations||data.warnings||[];
    if(limitations.length){const details=node("details"),list=node("ul");details.append(node("summary",`비교 한계 및 주의사항 (${limitations.length})`));limitations.forEach(t=>list.append(node("li",t)));details.append(list);root.append(details);}
    const sources=node("details");sources.append(node("summary",`사용 근거 / 서버 집계 원본 (${data.sources?.length||0})`));
    (data.sources||[]).forEach(source=>{const box=node("div",undefined,"sai-source");box.append(node("b",`[${source.source_id}] ${source.title}`));if(source.article_number){const link=node("a"," 근거 확인 ↗");link.href=`/ai?paper=${encodeURIComponent(source.article_number)}`;box.append(link);}let content=source.text;try{content=JSON.stringify(JSON.parse(content),null,2);}catch(_){}box.append(node("pre",content));sources.append(box);});root.append(sources);
  }
  async function history() {
    try {const data=await api("/jobs"),select=el("sai-history");select.replaceChildren(new Option("최근 20개 작업 중 Survey 기록", ""));(data.items||[]).filter(j=>labels[j.kind]).forEach(job=>select.add(new Option(`${states[job.status]} · ${labels[job.kind]} · ${job.created_at}`,job.id)));select.value=currentJob||"";}
    catch(error){status(error.message,true);}
  }
  async function watch(id) {
    clearTimeout(polling);const ticket=++generation;currentJob=id;
    async function poll() {
      try {const {job}=await api(`/jobs/${encodeURIComponent(id)}`);if(ticket!==generation)return;
        busy=["queued","running"].includes(job.status);syncSelection();el("sai-cancel").hidden=!busy;
        status(job.status==="failed"?job.error||"모델 응답을 완료하지 못했습니다. 다시 시도해 주세요.":job.progress||`${labels[job.kind]} · ${states[job.status]}`,job.status==="failed");
        renderResult(job);if(busy)polling=setTimeout(poll,2500);else history();
      }catch(error){if(ticket!==generation)return;status(`${error.message} 기록 새로고침 후 다시 선택할 수 있습니다.`,true);busy=false;syncSelection();el("sai-cancel").hidden=true;}
    }
    await poll();
  }
  async function submit(kind) {
    if(sending||busy)return;sending=true;syncSelection();status("현재 필터의 성능 집계를 서버에서 확보하고 있습니다…");
    try {const {job}=await api("/survey/jobs",{kind, filters:filters(), question:el("sai-question").value.trim(), article_numbers:kind==="compare"?[...selected.keys()]:[]});await watch(job.id);await history();}
    catch(error){status(error.message,true);}finally{sending=false;syncSelection();}
  }
  el("sai-analyze").addEventListener("click",()=>submit("analyze"));el("sai-compare").addEventListener("click",()=>submit("compare"));
  el("sai-clear").addEventListener("click",()=>{selected.clear();syncSelection();});el("sai-refresh").addEventListener("click",history);
  el("sai-history").addEventListener("change",event=>{if(event.target.value)watch(event.target.value);});
  el("sai-cancel").addEventListener("click",async()=>{if(!currentJob)return;try{await api(`/jobs/${encodeURIComponent(currentJob)}/cancel`,{});await watch(currentJob);}catch(error){status(error.message,true);}});
  el("sai-gaps-load").addEventListener("click",async()=>{
    const button=el("sai-gaps-load");button.disabled=true;el("sai-gaps-status").textContent="충돌·누락 후보를 조회하고 있습니다…";
    try {const data=await api("/survey/gaps"),list=el("sai-gaps");list.replaceChildren();el("sai-gaps-status").textContent=`${data.returned_count} / 후보 ${data.candidate_count}편 · ${data.priority_note}`;
      (data.items||[]).forEach(p=>{const row=node("article",undefined,"sai-gap"),body=node("div"),actions=node("div",undefined,"sai-actions");body.append(node("h4",`${p.priority} · ${p.title}`),node("p",[p.year,p.is_favorite?"★ 즐겨찾기":null,p.pdf_available?"PDF 보유":"PDF 없음",p.flags.join(", "),p.missing_fields.length?`누락: ${p.missing_fields.join(", ")}`:null].filter(Boolean).join(" · ")));const choose=node("button","비교 선택 +");choose.type="button";choose.dataset.surveySelect=p.article_number;choose.dataset.title=p.title;actions.append(choose);const review=node("a","AI 검토 ↗");review.href=`/ai?paper=${encodeURIComponent(p.article_number)}&kind=review`;actions.append(review);if(CAN_EDIT_SHARED&&p.pdf_available){const extract=node("a","PDF 추출·승인 ↗");extract.href=`/ai?paper=${encodeURIComponent(p.article_number)}&kind=extract`;actions.append(extract);}row.append(body,actions);list.append(row);});if(!data.items?.length)list.append(node("p","현재 후보가 없습니다.","sai-help"));syncSelection();
    }catch(error){el("sai-gaps-status").textContent=error.message;}finally{button.disabled=false;}
  });
  function updateFilters(){el("sai-filters").textContent=`분석 요청 조건: ${describe(filters())}`;}
  Object.values(controls).forEach(id=>el(id).addEventListener("change",updateFilters));
  window.addEventListener("pagehide",()=>{++generation;clearTimeout(polling);});
  updateFilters();syncSelection();history();
})();
