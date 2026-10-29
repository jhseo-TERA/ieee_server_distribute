/* Cached provenance only. Parser candidates never become benchmark values here. */
(function(root){
  'use strict';
  const FIELDS={reported_rate_gbps:['보고 속도','Gb/s'],reported_rate_min_gbps:['최저 속도','Gb/s'],reported_rate_max_gbps:['최고 속도','Gb/s'],lane_rate_gbps:['레인 속도','Gb/s'],lane_count:['레인 수',''],aggregate_rate_gbps:['합산 속도','Gb/s'],symbol_rate_gbaud:['심볼 속도','GBd'],throughput_density_gbps_per_mm:['대역폭 밀도','Gb/s/mm'],power_mw:['전력','mW'],energy_pj_bit:['에너지','pJ/bit'],energy_loss_normalized_pj_bit_db:['손실 정규화 에너지','pJ/bit/dB'],process_nm:['공정','nm'],channel_loss_db:['채널 손실','dB'],loss_frequency_ghz:['손실 측정 주파수','GHz'],ber:['BER','']};
  const CONDITIONS={modulation:'변조',supply_voltage:'공급 전압',temperature:'온도',fec:'FEC',excluded_components:'제외 회로',measurement_type:'측정/시뮬레이션'};
  const FLAGS={requires_human_review:'원문 대조 필요',implementation_attribution_unconfirmed:'해당 구현의 수치인지 확인 필요',multiple_values_in_same_fragment:'동일 구간에 복수 수치',not_confirmed_measured:'실측 여부 확인 필요',table_cell_requires_operating_point_review:'표의 동작 조건 확인 필요',flash_native_parse_requires_layout_review:'레이아웃 확인 필요',internal_or_parallel_rate_scope_requires_review:'내부 신호·병렬 출력 속도인지 확인 필요'};
  const WARNINGS={table_spans_require_manual_review:'병합 셀이 있는 표 · 헤더와 수치를 원문에서 확인',table_requires_unique_this_work_label:'This Work 행/열을 특정하지 못한 표 · 구현 귀속 확인',table_this_work_position_ambiguous:'This Work 위치가 모호한 표 · 구현 귀속 확인',table_has_no_html_cells:'표 구조를 읽지 못함 · 원문 확인',prior_work_sentence_excluded:'이전 연구 인용 구간을 추출 후보에서 제외'};
  const SCOPES={tx:'TX',rx:'RX',trx:'TX+RX',tx_rx:'TX+RX',full_link:'전체 링크',adc:'ADC',dsp:'DSP',cdr:'CDR',equalizer:'Equalizer',unknown:'범위 미확인'};
  const list=value=>Array.isArray(value)?value:[];
  function element(tag,text,className){
    const node=document.createElement(tag);
    if(text!==undefined&&text!==null)node.textContent=String(text);
    if(className)node.className=className;
    return node;
  }
  function externalUrl(value){
    if(typeof value!=='string'||!value)return '';
    try{const url=new URL(value,root.location?.origin||'http://localhost');return ['https:','http:'].includes(url.protocol)?url.href:'';}catch{return '';}
  }
  function link(text,url){const node=element('a',text);node.href=url;node.target='_blank';node.rel='noopener';return node;}
  function badge(text,pending){return element('span',text,'evidence-status'+(pending?' pending':''));}
  function evidenceItem(row){
    const node=element('div',null,'evidence-item'),head=element('div',null,'evidence-item-head');
    head.append(element('b',FIELDS[row.field]?.[0]||row.field||'근거'));
    const status=String(row.review_status||'');
    const reviewed=['verified','reviewed'].includes(status);
    const statusLabel=reviewed?'원문 검토 완료':status==='curated_reference'?'참고자료 정리':status==='calculated'?'계산 근거':status==='rejected'?'반려':status==='invalid'?'무효':'검토 필요';
    head.append(badge(statusLabel,!reviewed));node.append(head);
    const source=element('div',null,'evidence-meta'),url=externalUrl(row.source_url);
    source.append(url?link((row.source_label||row.source_kind||'출처')+' ↗',url):element('span',row.source_label||row.source_kind||'출처 미기재'));
    if(row.locator)source.append(element('span',' · '+row.locator));node.append(source);
    if(row.measurement_id!==undefined)node.append(element('div',`성능점 #${row.measurement_id}${row.operating_point_key?' · '+row.operating_point_key:''}`,'evidence-meta'));
    node.append(element('p',row.text||'','evidence-quote'));
    if(row.extraction_method)node.append(element('div',row.extraction_method+(row.extractor_version?' · '+row.extractor_version:''),'evidence-meta'));
    return node;
  }
  function candidateItem(candidate,article){
    const node=element('div',null,'evidence-item evidence-candidate'),head=element('div',null,'evidence-item-head');
    head.append(element('b',candidate.block_type==='table'?'표에서 추출한 후보':'본문에서 추출한 후보'),badge('검토 필요',true));node.append(head);
    const locator=element('div',null,'evidence-meta');
    const page=Number(candidate.page),pdfUrl=`/pdf/${encodeURIComponent(article)}#page=${page}`;
    if(Number.isInteger(page)&&page>0&&candidate.pdf_url===pdfUrl)locator.append(link(`원문 p.${page} ↗`,pdfUrl));
    else locator.append(element('span',`p.${candidate.page||'—'}`));
    if(candidate.table_row&&candidate.table_column)locator.append(element('span',` · 표 ${candidate.table_row}행 ${candidate.table_column}열`));
    locator.append(element('span',' · '+(candidate.attribution==='this_work'?'This Work 표기/자기 연구 문맥':'구현 귀속 확인 필요')));node.append(locator);
    const metrics=Object.entries(candidate.performance||{}).filter(([key,value])=>FIELDS[key]&&typeof value==='number'&&Number.isFinite(value));
    if(metrics.length)node.append(element('div',metrics.map(([key,value])=>`${FIELDS[key][0]} ${value}${FIELDS[key][1]?' '+FIELDS[key][1]:''}`).join(' · '),'evidence-values'));
    node.append(element('p',candidate.text||'','evidence-quote'));
    const conditions=Object.entries(candidate.conditions||{}).filter(([key])=>CONDITIONS[key]).map(([key,value])=>`${CONDITIONS[key]}: ${list(value).join(', ')}`);
    if(candidate.energy_component_scope)conditions.push('전력 범위 단서: '+(SCOPES[candidate.energy_component_scope]||candidate.energy_component_scope));
    if(conditions.length)node.append(element('div',conditions.join(' · '),'evidence-meta'));
    if(candidate.caption)node.append(element('div','캡션: '+candidate.caption,'evidence-meta'));
    if(candidate.footnotes)node.append(element('div','각주: '+candidate.footnotes,'evidence-meta'));
    const flags=list(candidate.flags).filter(flag=>flag!=='requires_human_review').map(flag=>FLAGS[flag]||flag);
    if(flags.length)node.append(element('div',flags.join(' · '),'evidence-review-note'));
    return node;
  }
  function appendItems(container,items,renderer){
    const grid=element('div',null,'evidence-list');items.slice(0,16).forEach(item=>grid.append(renderer(item)));container.append(grid);
    if(items.length>16){
      const details=element('details',null,'evidence-more'),remaining=element('div',null,'evidence-list');
      details.append(element('summary',`나머지 ${items.length-16}개 보기`));
      items.slice(16).forEach(item=>remaining.append(renderer(item)));details.append(remaining);container.append(details);
    }
  }
  function render(container,data){
    container.replaceChildren();
    const rows=list(data.evidence),section=element('section',null,'evidence-section');
    section.append(element('h4',`등록된 성능 근거 · ${rows.length}개`));
    if(rows.length)appendItems(section,rows,evidenceItem);
    else section.append(element('p','이 논문에 연결된 등록 근거가 없습니다.','evidence-empty'));
    if(data.evidence_may_have_more)section.append(element('p',`검토 완료 근거를 우선하여 최대 ${data.evidence_limit||80}개를 표시합니다.`,'evidence-meta'));
    container.append(section);
    const report=data.structured_evidence||{},structured=element('section',null,'evidence-section');
    structured.append(element('h4','MinerU 구조화 근거'));
    if(report.status!=='available'){
      structured.append(element('p',report.status==='missing_pdf'?'로컬 PDF가 없어 구조화 근거를 표시할 수 없습니다.':'구조화 근거가 아직 준비되지 않았습니다. 사전 분석을 실행한 뒤 표시됩니다.','evidence-empty'));
      container.append(structured);return;
    }
    structured.append(element('p',`분석 ${report.pages??'—'}페이지 · 후보 ${report.candidate_count??0}개 · 서로 다른 값이 있는 항목 ${report.conflict_count??0}개`,'evidence-meta'));
    structured.append(element('p','자동 추출 후보입니다. 원문과 측정 조건을 확인하기 전에는 성능 비교·차트의 확정값으로 반영하지 않습니다.','evidence-review-note'));
    if(report.tier==='flash')structured.append(element('p','텍스트 기반 분석 결과로, 표와 다단 레이아웃을 추가 확인해야 합니다.','evidence-review-note'));
    if(list(report.warnings).some(warning=>String(warning).includes(':table_')))structured.append(element('p','일부 표는 구조 또는 구현 귀속을 확인해야 하므로 수치 자동 추출에서 제외됐습니다. 아래 확인 항목의 원문 링크에서 검토할 수 있습니다.','evidence-review-note'));
    if(list(report.conflicts).length){
      const conflicts=element('div',null,'evidence-conflicts');
      conflicts.append(element('b','값 차이 검토 · 서로 다른 동작 조건 또는 추출 오류일 수 있습니다.'));
      list(report.conflicts).forEach(conflict=>conflicts.append(element('div',`${FIELDS[conflict.field]?.[0]||conflict.field}: ${list(conflict.values).join(' / ')}${FIELDS[conflict.field]?.[1]?' '+FIELDS[conflict.field][1]:''}`)));
      structured.append(conflicts);
    }
    const candidates=list(report.candidates);
    if(candidates.length)appendItems(structured,candidates,item=>candidateItem(item,String(data.article_number||'')));
    else structured.append(element('p','분석 구간에서 성능 수치 후보를 확인하지 못했습니다.','evidence-empty'));
    if(report.candidate_count>candidates.length)structured.append(element('p',`후보 ${report.candidate_count}개 중 ${candidates.length}개를 표시합니다. 전체 후보는 저장된 분석 보고서에서 확인할 수 있습니다.`,'evidence-meta'));
    if(list(report.warnings).length){
      const details=element('details',null,'evidence-more');details.append(element('summary','추출 시 확인이 필요한 항목'));
      list(report.warnings).forEach(warning=>{
        const raw=String(warning),split=raw.lastIndexOf(':'),code=raw.slice(split+1),line=element('div',WARNINGS[code]?raw.slice(0,split)+' · '+WARNINGS[code]:raw,'evidence-meta');
        const page=raw.match(/^p([1-9]\d*)-b/);
        if(page&&/^[A-Za-z0-9._-]+$/.test(String(data.article_number||'')))line.append(link(' · 원문 p.'+page[1]+' ↗',`/pdf/${encodeURIComponent(data.article_number)}#page=${page[1]}`));
        details.append(line);
      });structured.append(details);
    }
    structured.append(element('div',[report.adapter_version,report.producer_version&&'MinerU '+report.producer_version,report.created_at].filter(Boolean).join(' · '),'evidence-meta'));
    container.append(structured);
  }
  root.SerdesEvidence={render};
  if(typeof module!=='undefined'&&module.exports)module.exports={render};
})(typeof globalThis!=='undefined'?globalThis:this);
