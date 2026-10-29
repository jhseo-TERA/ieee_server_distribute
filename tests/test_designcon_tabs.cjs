const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const html = fs.readFileSync(path.join(__dirname, '../web/templates/index.html'), 'utf8');
const handlers = html.slice(html.indexOf('function syncLibraryTabs(){'), html.indexOf('// ---- 즐겨찾기 토글 ----'));
const query = html.slice(html.indexOf('function buildQuery(){'), html.indexOf('async function loadPapers(){'));

function setup(){
  const fields = new Map(), tabs = ['all','designcon','designcon-favorites'].map(collection=>({
    dataset:{collection}, active:false, attributes:{},
    classList:{toggle(name,value){ this.owner.active=value; }},
    setAttribute(name,value){ this.attributes[name]=value; }, addEventListener(){}
  }));
  tabs.forEach(tab=>tab.classList.owner=tab);
  const field = id=>{
    if(!fields.has(id)) fields.set(id,{value:'',hidden:false});
    return fields.get(id);
  };
  field('f-size').value='100';
  const requests = [];
  const context=vm.createContext({URLSearchParams, clearTimeout(){}, deb:undefined,
    document:{getElementById:field,querySelectorAll:()=>tabs},
    state:{page:9,size:100},loadPapers(){ requests.push(vm.runInContext('buildQuery()',context)); }
  });
  vm.runInContext(query + handlers, context);
  return {run:code=>vm.runInContext(code,context),field,tabs,requests};
}

test('DesignCon tabs clear incompatible filters and request the selected personal list',()=>{
  const ui=setup();
  for(const id of ['q','f-year','f-source','f-type','f-pdf']) ui.field(id).value='old-filter';
  ui.run("selectLibraryTab('designcon-favorites')");
  const query=new URLSearchParams(ui.requests[0]);
  assert.equal(query.get('publisher'),'designcon');
  assert.equal(query.get('fav_status'),'favorite');
  assert.equal(query.get('page'),'1');
  assert.equal(query.get('size'),'100');
  for(const key of ['q','year','source','type','pdf_status']) assert.equal(query.has(key),false);
  assert.equal(ui.tabs[2].attributes['aria-pressed'],'true');
  assert.equal(ui.field('reco-panel').hidden,true);
  assert.equal(ui.field('designcon-note').hidden,false);
  ui.run("selectLibraryTab('designcon')");
  assert.equal(new URLSearchParams(ui.requests[1]).has('fav_status'),false);
  assert.equal(ui.tabs[1].active,true);
});

test('manual publisher filters synchronize the tabs and all papers restores recommendations',()=>{
  const ui=setup();
  ui.field('f-publisher').value='designcon'; ui.field('f-fav').value='favorite';
  ui.run('applyFilters()');
  assert.equal(ui.tabs[2].active,true);
  ui.run("selectLibraryTab('all')");
  assert.equal(ui.tabs[0].attributes['aria-pressed'],'true');
  assert.equal(ui.field('reco-panel').hidden,false);
  assert.equal(ui.field('designcon-note').hidden,true);
  assert.equal(new URLSearchParams(ui.requests[1]).has('publisher'),false);
  assert.equal(new URLSearchParams(ui.requests[1]).has('fav_status'),false);
});
