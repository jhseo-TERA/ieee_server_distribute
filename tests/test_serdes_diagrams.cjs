const assert=require('node:assert/strict');
const {test}=require('node:test');
const {create}=require('../web/static/serdes_diagrams.js');
function node(tag){return {tag,children:[],textContent:'',append(...items){this.children.push(...items);},replaceChildren(...items){this.children=items;}};}
global.document={createElement:node};
const available=(article)=>({status:'available',page:2,figure_number:'1',caption:'Fig. 1. RX architecture.',width:800,height:400,
  image_url:`/api/serdes/papers/${article}/block-diagram/image?v=abc`});
function fixture(){const container=node('div'),calls=[];const control=create(container,()=>{},url=>new Promise((resolve,reject)=>calls.push({url,resolve,reject})));return{container,control,calls,reply(i,data){calls[i].resolve({ok:true,json:async()=>data});}};}
test('late image response cannot replace a different selected paper',async()=>{
  const f=fixture(),a=f.control.show('a'),b=f.control.show('b');f.reply(1,available('b'));await b;
  f.reply(0,available('a'));await a;
  assert.equal(f.container.children[0].children[1].children[0].src,available('b').image_url);
});
test('same paper deduplicates requests and reuses cache',async()=>{
  const f=fixture(),a=f.control.show('a'),b=f.control.show('a');assert.equal(f.calls.length,1);
  f.reply(0,available('a'));await Promise.all([a,b]);await f.control.show('a');assert.equal(f.calls.length,1);
});
test('different measurements in one paper do not share cached figure verification',async()=>{
  const f=fixture(),a=f.control.show('a',2180);f.reply(0,available('a'));await a;
  const b=f.control.show('a',2181);assert.equal(f.calls.length,2);
  assert.match(f.calls[1].url,/measurement_id=2181/);
  f.reply(1,{status:'review_required',review_reasons:['different_implementation'],pdf_url:'/pdf/a#page=6'});await b;
  assert.match(f.container.children[0].textContent,/이 성능점/);
  assert.equal(f.container.children[1].href,'/pdf/a#page=6');
});
test('source-reviewed image note does not imply every measurement is verified',async()=>{
  const f=fixture(),a=f.control.show('a',2180);
  f.reply(0,{...available('a'),verified:true,measurement_link:'paper_level',scope:'rx'});await a;
  assert.match(f.container.children[0].children[3].textContent,/원문 대조 완료.*논문 단위 연결/);
});
test('source absence is conclusive and keeps a page link without showing a figure',async()=>{
  const f=fixture(),a=f.control.show('a');
  f.reply(0,{status:'no_top_diagram',verified:true,review_note:'부분 회로만 따로 있습니다.',pdf_url:'/pdf/a#page=6'});await a;
  assert.match(f.container.children[0].textContent,/원문 전체 확인.*없음/);
  assert.equal(f.container.children[1].textContent,'부분 회로만 따로 있습니다.');
  assert.deepEqual(f.container.children.map(n=>n.tag),['p','p','a']);
});
test('verified reference is not presented as the complete measurement architecture',async()=>{
  const f=fixture(),a=f.control.show('a',5);
  f.reply(0,{...available('a'),status:'reference_only',verified:true,measurement_link:'reference_only',scope:'afe',
    scope_note:'AFE만 포함하며 CDR은 외부입니다.'});await a;
  const figure=f.container.children[0];
  assert.match(figure.children[0].textContent,/논문 참고.*전체 범위와 구분/);
  assert.match(figure.children[3].textContent,/성능점 직접 연결 아님/);
  assert.equal(figure.children[5].textContent,'AFE만 포함하며 CDR은 외부입니다.');
});
test('closing prevents delayed response from reopening figure',async()=>{
  const f=fixture(),a=f.control.show('a');f.control.clear();f.reply(0,available('a'));await a;assert.equal(f.container.children.length,0);
});
test('missing figure and network errors leave informative non-image placeholders',async()=>{
  const f=fixture(),a=f.control.show('a');f.reply(0,{status:'missing_pdf'});await a;
  assert.match(f.container.children[0].textContent,/PDF 미보유/);
  const b=f.control.show('b');f.calls[1].reject(new Error('offline'));await b;
  assert.match(f.container.children[0].textContent,/다시 선택/);
});
test('newly recovered candidates are distinguished from missing figures without showing an image',async()=>{
  const f=fixture(),a=f.control.show('a');
  f.reply(0,{...available('a'),status:'review_required',verified:false,review_reasons:['source_visual_review_pending'],pdf_url:'/pdf/a#page=2'});await a;
  assert.match(f.container.children[0].textContent,/후보 추출 완료.*검증 대기/);
  assert.deepEqual(f.container.children.map(n=>n.tag),['p','a']);
});
test('captions are text-only and foreign image URLs are rejected',async()=>{
  const f=fixture(),a=f.control.show('a');f.reply(0,{...available('a'),caption:'<script>unsafe</script>'});await a;
  assert.equal(f.container.children[0].children[2].textContent,'<script>unsafe</script>');
  const b=f.control.show('b');f.reply(1,{...available('b'),image_url:'https://foreign.invalid/a.png'});await b;
  assert.match(f.container.children[0].textContent,/다시 선택/);
});

test('moving from a chart point through blank canvas into its popover cancels every close timer',()=>{
  const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
  const template=fs.readFileSync(path.join(__dirname,'../web/templates/serdes.html'),'utf8').replaceAll('\r\n','\n');
  const start=template.indexOf("ZOOMABLE_CHART_IDS.forEach(id=>{\n  const canvas=$(id);\n  canvas.addEventListener('pointermove'");
  const end=template.indexOf("window.addEventListener('resize',positionPointPopover);",start);
  assert.ok(start>=0&&end>start);
  const timers=new Map(),handlers=new Map();let next=0;
  const target=id=>({addEventListener(name,handler){handlers.set(id+':'+name,handler);},getBoundingClientRect(){return{left:0,top:0};}});
  const hit={x:20,y:20,r:5,point:{performance:{measurement_id:123}}};
  const context={ZOOMABLE_CHART_IDS:['chart'],$:target,pointPinned:false,pointHoverKey:null,pointHoverTimer:null,pointLeaveTimer:null,
    performanceState:{drag:null,hits:new Map([['chart',[hit]]])},
    setTimeout(fn,delay){const id=++next;timers.set(id,{fn,delay});return id;},clearTimeout(id){timers.delete(id);},
    showPointPopover(){},hidePointPopover(){throw new Error('Popover should stay open');}};
  vm.runInNewContext(template.slice(start,end),context);
  const move=handlers.get('chart:pointermove');
  move({pointerType:'mouse',buttons:0,clientX:20,clientY:20});
  assert.equal(timers.get(context.pointHoverTimer).delay,180);
  timers.get(context.pointHoverTimer).fn();timers.delete(context.pointHoverTimer);
  move({pointerType:'mouse',buttons:0,clientX:100,clientY:100});
  handlers.get('chart:pointerleave')();
  assert.equal(timers.size,1,'Do not leave an older close timer running');
  handlers.get('point-popover:pointerenter')();
  assert.equal(timers.size,0,'Popover entry keeps it open for image/PDF clicks');
  context.pointPinned=true;
  handlers.get('point-popover:pointerleave')();
  assert.equal(timers.size,0,'Pinned popover stays open');
});
