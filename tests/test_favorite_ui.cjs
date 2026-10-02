const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

// Execute the actual page handlers, with only startup network calls omitted.
const html = fs.readFileSync(path.join(__dirname, '../web/templates/index.html'), 'utf8');
const script = html.split('<script>')[1].split('// ---- init ----')[0]
  .replace('{{ csrf_token|tojson }}', '"test-csrf"')
  .replace('{{ can_edit_favorites|tojson }}', 'true');

function element(on=false){
  const classes = new Set(on ? ['on'] : []);
  let markup='';
  return {dataset:{}, textContent:'', children:[], parentNode:null, scrollLeft:0, scrollTop:0,
    get innerHTML(){return this.children.length ? this.children.map(row=>row.outerHTML).join('') : markup;},
    set innerHTML(value){
      this.children.forEach(row=>{row.parentNode=null;}); this.children=[]; markup=value;
      for(const match of value.matchAll(/<tr\b[^>]*>[\s\S]*?<\/tr>/g)){
        const row=element(); row.outerHTML=match[0]; row.parentNode=this;
        row.dataset.recoId=match[0].match(/data-reco-id="([^"]*)"/)?.[1]; this.children.push(row);
      }
      if(this.children.length) markup='';
    },
    get firstElementChild(){return this.children[0]||null;},
    get isConnected(){return !!this.parentNode;},
    remove(){if(this.parentNode){const siblings=this.parentNode.children;siblings.splice(siblings.indexOf(this),1);this.parentNode=null;}},
    insertBefore(row,before){row.remove(); const index=before ? this.children.indexOf(before) : this.children.length;this.children.splice(index,0,row);row.parentNode=this;},
    replaceWith(row){const parent=this.parentNode, index=parent.children.indexOf(this);row.remove();parent.children[index]=row;row.parentNode=parent;this.parentNode=null;},
    querySelectorAll(){return [];},
    disabled:false, hidden:false, value:'match',
    classList:{contains:c=>classes.has(c), toggle(c,on){on ? classes.add(c) : classes.delete(c);}},
    setAttribute(){}, addEventListener(){}};
}
function ui(){
  const elements = new Map(), calls = [], timers = new Map(), stars = [element(),element()];
  let sequence = 0;
  const get = id=>{if(!elements.has(id)) elements.set(id,element()); return elements.get(id);};
  get('stat-fav').dataset.n = '10';
  const context = vm.createContext({console, URLSearchParams, CSS:{escape:s=>s}, clicked:stars[0],
    document:{getElementById:get, createElement:()=>element(), body:element(), querySelector:()=>null,
      querySelectorAll:s=>s.includes('data-art="p"') ? stars : []},
    setTimeout(fn,ms){const id=++sequence;timers.set(id,{fn,ms});return id;},
    clearTimeout(id){timers.delete(id);},
    fetch(url,options){return new Promise((resolve,reject)=>calls.push({url,options,resolve,reject}));}
  });
  vm.runInContext(script,context);
  const run = code=>vm.runInContext(code,context);
  const data = {items:[{article_number:'p',title:'Paper P',source_name:'JSSC'},
                     {article_number:'q',title:'Paper Q',source_name:'JSSC'}],
    groups:[{name:'JSSC',type:'journal',count:2}], fav_count:10,source_count:1,per_source:20,ai:{},refresh:{status:'ready'}};
  run(`lastRecoData = ${JSON.stringify(data)}; renderRecoItems();`);
  const reply = (i,body,ok=true)=>calls[i].resolve({ok,json:async()=>body});
  return {run,get,calls,reply,timers,stars,data,context};
}

test('stars and rows change before network response; duplicate clicks send no extra request', async()=>{
  const u=ui();
  const pending=u.run("toggleFav('p',clicked)");
  assert.equal(u.calls.length,1);
  assert.equal(u.calls[0].url,'/api/favorite');
  assert.deepEqual(JSON.parse(u.calls[0].options.body),{article_number:'p',favorite:true});
  for(const star of u.stars){assert.equal(star.classList.contains('on'),true);assert.equal(star.disabled,true);}
  assert.equal(Number(u.get('stat-fav').dataset.n),11);
  assert.doesNotMatch(u.get('reco-tbody').innerHTML,/Paper P/);
  assert.match(u.get('reco-tabs').innerHTML,/JSSC/);
  assert.match(u.get('reco-tabs').innerHTML,/>1<\/small>/);
  await u.run("toggleFav('p',clicked)");
  assert.equal(u.calls.length,1);
  u.reply(0,{ok:true,is_favorite:1,favorite_delta:1}); await pending;
  assert.equal(u.stars[0].disabled,false);
  assert.equal(u.calls.length,1); // No synchronous recommendation reload.
  assert.equal([...u.timers.values()][0].ms,1000);
});

test('feedback excludes a row only after persistence, with restore and no favorite change', async()=>{
  const u=ui();
  const pending=u.run("saveRecoFeedback('p','dismissed',clicked)");
  assert.match(u.get('reco-tbody').innerHTML,/Paper P/);
  await u.run("saveRecoFeedback('p','reviewed',clicked)");
  assert.equal(u.calls.length,1);
  assert.equal(u.calls[0].url,'/api/recommendations/feedback');
  assert.deepEqual(JSON.parse(u.calls[0].options.body),{article_number:'p',action:'dismissed'});
  u.reply(0,{ok:true}); await pending;
  assert.doesNotMatch(u.get('reco-tbody').innerHTML,/Paper P/);
  assert.equal(Number(u.get('stat-fav').dataset.n),10);
  const restored=u.run("saveRecoFeedback('p','restore',clicked)");
  u.reply(1,{ok:true}); await restored;
  assert.match(u.get('favorite-status').textContent,/복원/);
});

test('feedback failure leaves rows and favorite counts untouched', async()=>{
  const u=ui(), pending=u.run("saveRecoFeedback('p','reviewed',clicked)");
  u.reply(0,{error:'save failed'},false); await pending;
  assert.match(u.get('reco-tbody').innerHTML,/Paper P/);
  assert.equal(Number(u.get('stat-fav').dataset.n),10);
  assert.equal(u.stars[0].disabled,false);
});

test('HTTP and network errors restore stars, count, rows and controls', async()=>{
  for(const network of [false,true]){
    const u=ui(), pending=u.run("toggleFav('p',clicked)");
    if(network) u.calls[0].reject(new Error('offline'));
    else u.reply(0,{ok:false,error:'permission denied'},false);
    await pending;
    assert.equal(Number(u.get('stat-fav').dataset.n),10);
    assert.equal(u.stars[0].classList.contains('on'),false);
    assert.equal(u.stars[0].disabled,false);
    assert.match(u.get('reco-tbody').innerHTML,/Paper P/);
    assert.match(u.get('favorite-status').textContent,/화면을 복원/);
  }
});

test('bulk action sends one request, uses actual changed count and restores on failure', async()=>{
  const u=ui(), pending=u.run('addAllRecommendations()');
  assert.equal(u.calls.length,1);
  assert.equal(u.calls[0].url,'/api/favorites/bulk');
  assert.deepEqual(JSON.parse(u.calls[0].options.body),{article_numbers:['p','q']});
  assert.equal(Number(u.get('stat-fav').dataset.n),12);
  u.reply(0,{ok:true,changed_count:1,favorite_delta:1});await pending;
  assert.equal(Number(u.get('stat-fav').dataset.n),11);
  assert.match(u.get('favorite-status').textContent,/1편 추가 완료/);
  assert.equal(u.calls.length,1);
  const f=ui(), failed=f.run('addAllRecommendations()');
  f.reply(0,{ok:false,error:'not found'},false);await failed;
  assert.equal(Number(f.get('stat-fav').dataset.n),10);
  assert.equal(f.get('btn-reco-addall').disabled,false);
  assert.match(f.get('reco-tbody').innerHTML,/Paper P/);
  assert.match(f.get('reco-tbody').innerHTML,/Paper Q/);
});

test('old recommendation response cannot resurrect an optimistically added row', async()=>{
  const u=ui();
  const loading=u.run('loadRecommendations()');
  const saving=u.run("toggleFav('p',clicked)");
  u.reply(0,u.data);await loading;
  assert.doesNotMatch(u.get('reco-tbody').innerHTML,/Paper P/);
  u.reply(1,{ok:true,favorite_delta:1});await saving;
  const reload=u.run('loadRecommendations()');
  u.reply(2,u.data);await reload; // A cached server response also stays filtered.
  assert.doesNotMatch(u.get('reco-tbody').innerHTML,/Paper P/);
});

test('recommendation errors keep the previous list and rapid changes debounce one refresh',async()=>{
  const u=ui();
  const loading=u.run('loadRecommendations()');
  u.calls[0].reject(new Error('offline'));await loading;
  assert.match(u.get('reco-tbody').innerHTML,/Paper P/);
  assert.match(u.get('reco-sub').textContent,/기존 목록은 유지/);
  for(const art of ['p','q']){
    const saving=u.run(`saveFavoriteChanges([{art:'${art}',before:false,on:true}])`);
    u.reply(u.calls.length-1,{ok:true,favorite_delta:1});await saving;
  }
  assert.equal(u.timers.size,1);
});

test('cold SQL generation polls and displays progress instead of an empty/error result',async()=>{
  const u=ui(), loading=u.run('loadRecommendations()');
  u.reply(0,{...u.data,items:[],groups:[],refresh:{status:'refreshing'}});await loading;
  assert.match(u.get('reco-tbody').innerHTML,/백그라운드에서 준비/);
  assert.equal([...u.timers.values()][0].ms,2000);
});

test('paper pagination renders page numbers in blocks of ten',()=>{
  const u=ui();
  const labels=()=>[...u.get('pager').innerHTML.matchAll(/<button[^>]*>(.*?)<\/button>/g)].map(match=>match[1]);

  u.run('renderPager({page:1,pages:27})');
  assert.deepEqual(labels(),['«','‹','1','2','3','4','5','6','7','8','9','10','›','»']);
  assert.match(u.get('pager').innerHTML,/title="다음 10페이지"[^>]*onclick="gotoPage\(11\)"/);

  u.run('renderPager({page:11,pages:27})');
  assert.deepEqual(labels(),['«','‹','11','12','13','14','15','16','17','18','19','20','›','»']);
  assert.match(u.get('pager').innerHTML,/title="이전 10페이지"[^>]*onclick="gotoPage\(10\)"/);
  assert.match(u.get('pager').innerHTML,/title="다음 10페이지"[^>]*onclick="gotoPage\(21\)"/);

  u.run('renderPager({page:27,pages:27})');
  assert.deepEqual(labels(),['«','‹','21','22','23','24','25','26','27','›','»']);
});

test('recommendations use twenty-item journal and conference tabs',async()=>{
  const u=ui();
  const journalItems=Array.from({length:22},(_,i)=>({article_number:`j-${i}`,title:`Journal ${i}`,source_name:'JSSC'}));
  const conferenceItems=[{article_number:'c-1',title:'Conference 1',source_name:'ISSCC'}];
  const data={...u.data,items:[...journalItems,...conferenceItems],groups:[
    {name:'JSSC',type:'journal',count:22},{name:'ISSCC',type:'conference',count:1}
  ],source_count:2,per_source:20};
  u.run(`lastRecoData=${JSON.stringify(data)}; selectedRecoSource=''; renderRecoItems()`);

  assert.match(u.get('reco-tabs').innerHTML,/저널/);
  assert.match(u.get('reco-tabs').innerHTML,/학회/);
  assert.equal((u.get('reco-tbody').innerHTML.match(/<tr\b/g)||[]).length,20);
  assert.match(u.get('reco-tbody').innerHTML,/Journal 0/);
  assert.doesNotMatch(u.get('reco-tbody').innerHTML,/Conference 1/);

  u.run(`selectRecoSource('ISSCC')`);
  assert.match(u.get('reco-tbody').innerHTML,/Conference 1/);
  assert.doesNotMatch(u.get('reco-tbody').innerHTML,/Journal 0/);

  const loading=u.run('loadRecommendations()');
  assert.match(u.calls[0].url,/per_source=20/);
  u.reply(0,data);await loading;
});

test('tab ordering is stable and journals/conferences never interleave after reranking',()=>{
  const u=ui();
  const groups=[{name:'OFC',type:'conference'},{name:'SSCL',type:'journal'},
    {name:'ISSCC',type:'conference'},{name:'JSSC',type:'journal'}];
  const items=groups.map(g=>({article_number:g.name,title:g.name,source_name:g.name}));
  u.run(`lastRecoData=${JSON.stringify({...u.data,groups,items})};renderRecoItems()`);
  const names=()=>[...u.get('reco-tabs').innerHTML.matchAll(/data-source="([^"]+)"/g)].map(m=>m[1]);
  assert.deepEqual(names(),['JSSC','SSCL','ISSCC','OFC']);
  u.run(`lastRecoData.groups.reverse();lastRecoData.items.reverse();renderRecoItems()`);
  assert.deepEqual(names(),['JSSC','SSCL','ISSCC','OFC']);
  assert.equal((u.get('reco-tabs').innerHTML.match(/reco-tab-kind/g)||[]).length,2);
});

test('saving the final paper keeps its empty selected tab even when API omits it',async()=>{
  const u=ui();
  const data={...u.data,items:[u.data.items[0],{article_number:'c',title:'Conference',source_name:'OFC'}],
    groups:[{name:'JSSC',type:'journal'},{name:'OFC',type:'conference'}]};
  u.run(`lastRecoData=${JSON.stringify(data)};renderRecoItems()`);
  u.get('reco-tabs').scrollLeft=135;
  const saving=u.run("toggleFav('p',clicked)");
  assert.equal(u.run('selectedRecoSource'),'JSSC');
  assert.doesNotMatch(u.get('reco-tbody').innerHTML,/Conference/);
  assert.equal(u.get('reco-tabs').scrollLeft,135);
  u.reply(0,{ok:true,favorite_delta:1});await saving;
  const loading=u.run('loadRecommendations()');
  u.reply(1,{...data,items:[data.items[1]],groups:[data.groups[1]]});await loading;
  assert.equal(u.run('selectedRecoSource'),'JSSC');
  assert.match(u.get('reco-tbody').innerHTML,/이 탭에 남은 추천이 없습니다/);
  assert.equal(u.get('btn-reco-addall').disabled,true);
  u.run("selectRecoSource('OFC')");
  assert.match(u.get('reco-tbody').innerHTML,/Conference/);
});

test('successful save and polling reuse every surviving row node',async()=>{
  const u=ui(), row=u.get('reco-tbody').children[1];
  row.userExpandedDetails=true;
  const saving=u.run("toggleFav('p',clicked)");
  assert.equal(u.get('reco-tbody').children[0],row);
  u.reply(0,{ok:true,favorite_delta:1});await saving;
  assert.equal(u.get('reco-tbody').children[0],row);
  const loading=u.run('loadRecommendations()');
  u.reply(1,u.data);await loading;
  assert.equal(u.get('reco-tbody').children[0],row);
  assert.equal(row.userExpandedDetails,true);
});

test('failed save restores the removed row without rebuilding surviving rows or changing tabs',async()=>{
  const u=ui(), row=u.get('reco-tbody').children[1];
  const saving=u.run("toggleFav('p',clicked)");
  u.reply(0,{error:'failed'},false);await saving;
  assert.equal(u.get('reco-tbody').children[1],row);
  assert.equal(u.run('selectedRecoSource'),'JSSC');
  assert.match(u.get('reco-tbody').innerHTML,/Paper P/);
});

test('removing a preceding row preserves the viewport position of the surviving visible row',()=>{
  const u=ui(), main={scrollTop:120,getBoundingClientRect:()=>({top:0,bottom:300})};
  u.context.document.querySelector=selector=>selector==='.main' ? main : null;
  const rows=u.get('reco-tbody').children;
  rows.forEach(row=>{row.getBoundingClientRect=()=>{
    const top=row.parentNode.children.indexOf(row)*100-main.scrollTop;
    return {top,bottom:top+100};
  };});
  const remaining=rows[1], before=remaining.getBoundingClientRect().top;
  u.run("favoriteOverrides.set('p',true);renderRecoItems()");
  assert.equal(remaining.getBoundingClientRect().top,before);
  assert.equal(main.scrollTop,20);
});

test('showing save status above the table does not shift the visible row',()=>{
  const u=ui(), main={scrollTop:20,getBoundingClientRect:()=>({top:0,bottom:300})};
  const status=u.get('favorite-status');status.hidden=true;
  u.context.document.querySelector=selector=>selector==='.main' ? main : null;
  u.get('reco-tbody').children.forEach(row=>{row.getBoundingClientRect=()=>{
    const top=row.parentNode.children.indexOf(row)*100+(status.hidden?0:30)-main.scrollTop;
    return {top,bottom:top+100};
  };});
  const row=u.get('reco-tbody').children[0], before=row.getBoundingClientRect().top;
  u.run("favoriteStatus('Saving')");
  assert.equal(row.getBoundingClientRect().top,before);
  assert.equal(main.scrollTop,50);
});

test('deterministic recommendation mode is labeled without AI controls',async()=>{
  const u=ui(),loading=u.run('loadRecommendations()');
  u.reply(0,{...u.data,engine:'sql',total_limit:180,modes:{match:'관심사 일치'},ai:{status:'disabled'}});await loading;
  assert.match(u.get('reco-sub').innerHTML,/규칙 기반 관심사 일치/);
  assert.match(u.get('reco-sub').innerHTML,/전체 최대 180편/);
  assert.doesNotMatch(u.get('reco-sub').innerHTML,/AI 평가/);
});
