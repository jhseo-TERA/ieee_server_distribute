const assert=require('node:assert/strict');
const {test}=require('node:test');
const {render}=require('../web/static/serdes_evidence.js');
function node(tag){return {tag,children:[],textContent:'',append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;},set innerHTML(_){throw new Error('Evidence must render as text, never raw HTML');}};}
global.document={createElement:node};
function walk(root){return [root,...root.children.flatMap(walk)];}
function texts(root){return walk(root).map(item=>item.textContent).join('\n');}
function fixture(overrides={}){return {article_number:'123',evidence:[],structured_evidence:{status:'available',pages:2,candidate_count:1,conflict_count:1,
  candidates:[{page:2,pdf_url:'/pdf/123#page=2',block_type:'table',text:'Energy 1.2 pJ/bit',review_status:'verified',attribution:'this_work',performance:{energy_pj_bit:1.2},conditions:{excluded_components:['excluding CDR']},table_row:2,table_column:3}],
  conflicts:[{field:'energy_pj_bit',values:['1.2','2.4']}],...overrides}};}

test('MinerU candidates show pending status, measurement context and source page',()=>{
  const box=node('div');render(box,fixture());
  assert.match(texts(box),/검토 필요/);
  assert.doesNotMatch(texts(box),/원문 검토 완료/);
  assert.match(texts(box),/성능 비교·차트의 확정값으로 반영하지 않습니다/);
  assert.match(texts(box),/excluding CDR/);
  assert.match(texts(box),/서로 다른 동작 조건 또는 추출 오류/);
  assert.equal(walk(box).find(item=>item.tag==='a').href,'/pdf/123#page=2');
});
test('registered PDF evidence is not implicitly verified and preserves measurement identity',()=>{
  const box=node('div');render(box,{article_number:'123',evidence:[{field:'energy_pj_bit',source_kind:'pdf',review_status:'needs_review',measurement_id:7,operating_point_key:'PAM4 56G',text:'candidate'}]});
  assert.match(texts(box),/검토 필요/);assert.match(texts(box),/성능점 #7 · PAM4 56G/);
  assert.doesNotMatch(texts(box),/원문 검토 완료/);
});
test('all registered evidence is reachable instead of silently dropping rows after 16',()=>{
  const box=node('div');render(box,{evidence:Array.from({length:22},(_,i)=>({field:'lane_rate_gbps',text:'quote-'+i,review_status:'reviewed'}))});
  assert.match(texts(box),/나머지 6개 보기/);assert.match(texts(box),/quote-21/);
  assert.equal(walk(box).filter(item=>item.className==='evidence-item').length,22);
});
test('source content is text-only and unsafe links are never exposed',()=>{
  const box=node('div'),data=fixture();
  data.evidence=[{field:'<script>alert(1)</script>',text:'<img src=x onerror=alert(1)>',source_url:'javascript:alert(1)',source_label:'bad'}];
  data.structured_evidence.candidates[0].text='<table onclick=alert(1)>';
  data.structured_evidence.candidates[0].pdf_url='/pdf/another-paper#page=2';
  render(box,data);
  assert.ok(walk(box).some(item=>item.textContent==='<img src=x onerror=alert(1)>'));
  assert.equal(walk(box).filter(item=>['script','img','table','a'].includes(item.tag)).length,0);
});
test('missing and unprepared caches produce useful empty states',()=>{
  const missing=node('div'),pending=node('div');
  render(missing,fixture({status:'missing_pdf'}));render(pending,fixture({status:'not_prepared'}));
  assert.match(texts(missing),/로컬 PDF가 없어/);assert.match(texts(pending),/사전 분석/);
});
test('papers without registered performance evidence can still open MinerU review candidates',()=>{
  const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
  const template=fs.readFileSync(path.join(__dirname,'../web/templates/serdes.html'),'utf8');
  const start=template.indexOf('function rowHtml(item){'),end=template.indexOf('function renderPager(',start);
  const context={safeUrl:()=>'',metricHtml:()=>'',state:{},esc:value=>String(value??''),CAN_EDIT_FAVORITES:false,CAN_EDIT_SHARED:false,completenessHtml:()=>'',mediumBadge:()=>'',tagHtml:()=>'',surveySelectHtml:()=>''};
  vm.runInNewContext(template.slice(start,end),context);
  assert.match(context.rowHtml({article_number:'123',pdf_available:true,title:'No registered values'}),/data-action="evidence"[^>]*>근거 확인/);
});
test('skipped complex tables disclose the limitation and link to the original page',()=>{
  const box=node('div');render(box,fixture({candidates:[],candidate_count:0,warnings:['p3-b0:table_spans_require_manual_review']}));
  assert.match(texts(box),/수치 자동 추출에서 제외됐습니다/);
  assert.match(texts(box),/병합 셀이 있는 표/);
  assert.ok(walk(box).some(item=>item.tag==='a'&&item.href==='/pdf/123#page=3'));
});
