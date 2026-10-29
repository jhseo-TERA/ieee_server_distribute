const {test} = require('node:test');
const assert = require('node:assert/strict');
const {explore} = require('../web/static/connection-lineage.js');
const nodes = Array.from({length:7},(_,i)=>({id:'W'+(i+1),title:'Paper '+(i+1),citation_count:i}));
const edges = [{source:'W2',target:'W1'},{source:'W3',target:'W2'},{source:'W4',target:'W3'},{source:'W1',target:'W5'},{source:'W5',target:'W6'},{source:'W6',target:'W7'}];
const ids = rows => rows.map(row=>row.id).sort();
test('depth follows direct citations in each direction and expands levels one at a time',()=>{
  const one=explore({nodes,edges,root:'W1'}),two=explore({nodes,edges,root:'W1',depth:2}),three=explore({nodes,edges,root:'W1',depth:3});
  assert.deepEqual(ids(one.nodes),['W1','W2','W5']);
  assert.deepEqual(ids(two.nodes),['W1','W2','W3','W5','W6']);
  assert.deepEqual(ids(three.nodes),['W1','W2','W3','W4','W5','W6','W7']);
  assert.equal(three.prior.find(row=>row.id==='W4').prior_level,3);
  assert.equal(three.derivative.find(row=>row.id==='W7').derivative_level,3);
});
test('prior and derivative are relative to the selected root, not the original map seed',()=>{
  const prior=explore({nodes,edges,root:'W5',depth:2,direction:'prior'});
  assert.deepEqual(ids(prior.nodes),['W1','W2','W5']); assert.equal(prior.derivative.length,0);
  const after=explore({nodes,edges,root:'W5',depth:2,direction:'derivative'});
  assert.deepEqual(ids(after.nodes),['W5','W6','W7']); assert.equal(after.prior.length,0);
});
test('cycles terminate, duplicates collapse and the shortest citation path wins',()=>{
  const result=explore({nodes,edges:[...edges,{source:'W3',target:'W1'},{source:'W1',target:'W3'},{source:'W2',target:'W1'}],root:'W1',depth:3});
  assert.equal(result.prior.find(row=>row.id==='W3').prior_level,1);
  assert.equal(result.nodes.find(row=>row.id==='W1').lineage_level,0);
  assert.ok(!result.prior.some(row=>row.id==='W1'));
  assert.equal(result.edges.length,new Set(result.edges.map(edge=>JSON.stringify(edge))).size);
});
test('limits prioritize nearer levels and always keep the root even when filtered out',()=>{
  const result=explore({nodes,edges,root:'W1',depth:3,limit:3});
  assert.deepEqual(ids(result.nodes),['W1','W2','W5']); assert.equal(result.limited,true);
  const filtered=explore({nodes,edges,root:'W1',depth:3,filter:paper=>paper.id==='W4'});
  assert.deepEqual(ids(filtered.nodes),['W1','W4']); assert.equal(filtered.prior[0].prior_level,3);
  assert.equal(filtered.edges.length,0); // Missing intermediate records never create a shortcut.
});
test('unknown endpoints and matching titles never manufacture paths',()=>{
  const result=explore({nodes,edges:[{source:'unknown',target:'W1'}],root:'W1',depth:3});
  assert.deepEqual(ids(result.nodes),['W1']); assert.deepEqual(result.edges,[]);
  assert.equal(explore({nodes,edges,root:'unknown'}).nodes.length,0);
});
test('input order is deterministic and source records are never mutated',()=>{
  const snapshot=JSON.stringify({nodes,edges});
  const left=explore({nodes,edges,root:'W1',depth:3,limit:4});
  const right=explore({nodes:[...nodes].reverse(),edges:[...edges].reverse(),root:'W1',depth:3,limit:4});
  assert.deepEqual(left,right); assert.equal(JSON.stringify({nodes,edges}),snapshot);
});


test('a crowded first level cannot starve deeper levels or the opposite direction',()=>{
  const crowd=Array.from({length:70},(_,i)=>({id:'P'+i,citation_count:1000-i}));
  const all=[{id:'root'},...crowd,{id:'old2'},{id:'old3'},{id:'new1'},{id:'new2'},{id:'new3'}];
  const links=[...crowd.map(p=>({source:p.id,target:'root'})),
    {source:'old2',target:'P0'},{source:'old3',target:'old2'},
    {source:'root',target:'new1'},{source:'new1',target:'new2'},{source:'new2',target:'new3'}];
  const result=explore({nodes:all,edges:links,root:'root',depth:3,limit:30});
  const shown=new Set(ids(result.nodes));
  for(const key of ['root','P0','old2','old3','new1','new2','new3']) assert.ok(shown.has(key),key);
  assert.equal(result.nodes.length,30); assert.equal(result.limited,true);
  assert.ok(result.edges.some(edge=>edge.source==='old3'&&edge.target==='old2'));
});

test('direct citation provenance survives traversal without copying arbitrary fields',()=>{
  const result=explore({nodes:[{id:'seed'},{id:'prior'}],edges:[{source:'prior',target:'seed',reference_source:'pdf',reference_number:5,reference_page:13,secret:'omit'}],root:'seed'});
  assert.deepEqual(result.edges,[{source:'prior',target:'seed',reference_source:'pdf',reference_number:5,reference_page:13}]);
});
