const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.listeners = {}; this.attributes = {}; this.style = {}; this.dataset = {}; this.className = ''; this.textContent = ''; this.value = ''; this.hidden = false; this.classList = { add: (value) => this.toggle(value, true), remove: (value) => this.toggle(value, false), toggle: (value, on) => this.toggle(value, on) }; }
  toggle(value, on) { const classes = new Set(this.className.split(' ').filter(Boolean)); const active = on ?? !classes.has(value); if (active) classes.add(value); else classes.delete(value); this.className = [...classes].join(' '); }
  set innerHTML(_) { throw new Error('Untrusted paper data must never be rendered as HTML'); }
  get childNodes() { return this.children; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = value; if (key === 'class') this.className = value; }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; delete this[key]; }
  addEventListener(event, fn) { (this.listeners[event] ||= []).push(fn); }
  focus() { this.focused = true; }
  dispatch(event, detail = {}) { for (const fn of this.listeners[event] || []) fn({ preventDefault() {}, ...detail }); }
  querySelectorAll(selector) { return walk(this).slice(1).filter((node) => selector[0] === '.' && node.className.split(' ').includes(selector.slice(1))); }
}
function walk(node) { return [node, ...node.children.flatMap(walk)]; }
const flush = () => new Promise(setImmediate);
function createApp(query = '', fakeTimers = false) {
  const template = fs.readFileSync(path.join(__dirname, '../web/templates/connection.html'), 'utf8');
  const elements = Object.fromEntries([...template.matchAll(/id="([^"]+)"/g)].map((match) => [match[1], new Element()]));
  elements['map-limit'].value = '30'; const requests = []; const location = { href: `https://papers.example/connection${query}`, search: query }; const history = { replaceState(_, __, url) { location.href = String(url); location.search = url.search; } };
  const document = { getElementById(id) { assert.ok(elements[id], `Missing template element ${id}`); return elements[id]; }, createElement: (tag) => new Element(tag), createElementNS: (_, tag) => new Element(tag) };
  const window = new Element(); const timers = new Map(); let timerId = 0;
  const schedule = fakeTimers ? (fn, ms) => { const id = ++timerId; timers.set(id, { fn, ms }); return id; } : setTimeout;
  const cancel = fakeTimers ? (id) => timers.delete(id) : clearTimeout;
  const fetch = (url, options) => new Promise((resolve, reject) => requests.push({ url, options, reject, respond(data, status = 200) { resolve({ status, ok: status < 400, redirected: false, headers: { get: () => 'application/json' }, json: async () => data }); } }));
  const context = vm.createContext({ document, window, fetch, location, history, URL, URLSearchParams, AbortController, setTimeout: schedule, clearTimeout: cancel });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/static/connection-lineage.js'), 'utf8'), context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../web/static/connection.js'), 'utf8'), context);
  return { elements, requests, location, runTimers(ms) { for (const [id, timer] of [...timers]) if (timer.ms <= ms) { timers.delete(id); timer.fn(); } } };
}
function paper(id, extra = {}) { return { article_number: id, id, title: `Paper ${id}`, year: 2025, source_name: 'JSSC', authors: 'Researcher', pdf_available: false, similarity: .5, shared_terms: ['equalizer'], ...extra }; }
function graph(seed, others = []) { return { seed_id: seed.article_number, nodes: [seed, ...others], edges: others.map((item) => ({ source: seed.article_number, target: item.article_number, weight: .4 })), meta: { candidate_count: 100, abstract_count: 1 } }; }

test('search and graph render untrusted text safely and construct only safe source links', async () => {
  const app = createApp('?mode=local&article_number=A%2FB'); const malicious = paper('A/B', { title: '<img src=x onerror=alert(1)>', doi: 'javascript:alert(1)', pdf_available: true });
  assert.equal(app.requests.length, 2); assert.match(app.requests[1].url, /article_number=A%2FB/);
  app.requests[0].respond({ papers: [malicious] }); app.requests[1].respond(graph(malicious)); await flush();
  assert.equal(app.elements['detail-title'].textContent, malicious.title); assert.equal(app.elements['detail-doi'].hidden, true); assert.equal(app.elements['detail-pdf'].href, '/pdf/A%2FB');
  assert.equal(app.elements['map-nodes'].children.length, 1); assert.equal(app.elements['node-list'].children.length, 1); assert.match(app.elements['map-status'].textContent, /유사한 논문을 찾지 못했습니다/);
  assert.ok(walk(app.elements['search-results']).every((node) => node.tag !== 'img'));
});

test('late graph responses cannot replace a newer selection', async () => {
  const app = createApp('?mode=local'); const a = paper('a'), b = paper('b'); app.requests[0].respond({ papers: [a, b] }); await flush();
  app.elements['search-results'].children[0].dispatch('click'); app.elements['search-results'].children[1].dispatch('click');
  assert.equal(app.requests[1].options.signal.aborted, true);
  app.requests[2].respond(graph(b)); await flush(); app.requests[1].respond(graph(a)); await flush();
  assert.equal(app.elements['detail-title'].textContent, 'Paper b'); assert.match(app.location.search, /article_number=b/); assert.equal(app.elements['map-stage'].getAttribute('aria-busy'), 'false');
});

test('search responses stay matched to the newest query even if abort is ignored', async () => {
  const app = createApp(); app.elements['paper-query'].value = 'equalizer'; app.elements['search-form'].dispatch('submit');
  app.requests[1].respond({ papers: [paper('new')], has_more: false }); await flush(); app.requests[0].respond({ papers: [paper('old')] }); await flush();
  assert.equal(app.elements['search-results'].children[0].dataset.paperId, 'new'); assert.equal(app.elements['search-status'].textContent, '검색 결과 1편');
});

test('selecting a related node changes details and re-explores its own graph', async () => {
  const app = createApp('?mode=local&article_number=a'); const related = paper('b', { doi: 'https://doi.org/10.1234/example', abstract_available: true }); app.requests[0].respond({ papers: [] }); app.requests[1].respond(graph(paper('a'), [related])); await flush();
  app.elements['node-list'].children[1].dispatch('click'); assert.equal(app.elements['detail-title'].textContent, 'Paper b'); assert.equal(app.elements['detail-doi'].href, 'https://doi.org/10.1234/example'); assert.equal(app.elements['detail-basis'].textContent, '제목 + 저장된 초록'); assert.equal(app.elements['recenter-paper'].disabled, false);
  app.elements['recenter-paper'].dispatch('click'); assert.match(app.requests[2].url, /article_number=b/); app.requests[2].respond(graph(related)); await flush(); assert.equal(app.elements['recenter-paper'].disabled, true);
});

test('server errors leave a retry path and retry uses the failed seed', async () => {
  const app = createApp('?article_number=missing'); app.requests[0].respond({ papers: [] }); app.requests[1].respond({ error: '논문을 찾을 수 없습니다.' }, 404); await flush();
  assert.equal(app.elements['map-state-description'].textContent, '논문을 찾을 수 없습니다.'); assert.equal(app.elements['map-retry'].hidden, false); assert.equal(app.elements['map-stage'].getAttribute('aria-busy'), 'false');
  app.elements['map-retry'].dispatch('click'); assert.match(app.requests[2].url, /article_number=missing/); app.requests[2].respond(graph(paper('missing'))); await flush(); assert.equal(app.elements['map-overlay'].hidden, true);
});

function citationGraph() {
  const seed = paper('local-a', { id: 'W100', in_library: true, citation_count: 60, abstract: 'Seed abstract' });
  const related = paper(null, { id: 'W200', title: '<script>external title</script>', doi: '10.1234/external', in_library: false, citation_count: null, year: null, abstract: '<img src=x> This is text.', related_by: { shared_references: 3, co_citations: 2, direct_citation: true } });
  const prior = paper(null, { id: 'W300', in_library: false, doi: '10.1234/prior', relation_count: 2, abstract: 'Prior abstract', citation_count: 0 });
  return { source: 'citation_similarity', seed_id: 'W100', nodes: [seed, related], edges: [{ source: 'W100', target: 'W200', weight: .5, shared_references: 3, co_citations: 2, direct_citation: true }], citation_edges: [{source:'W100',target:'W200'}], lineage_nodes: [seed, related, prior], lineage_edges: [{source:'W300',target:'W100'}, {source:'W100',target:'W200'}], prior_works: [prior], derivative_works: [], meta: { provider: 'OpenAlex', coverage_note: 'Limited collected sample', candidate_count: 40 } };
}

test('citation mode preserves provider IDs, renders evidence, and re-seeds external DOI safely', async () => {
  const app = createApp('?article_number=local-a'); assert.match(app.requests[1].url, /mode=citation/);
  app.requests[0].respond({ papers: [] }); app.requests[1].respond(citationGraph()); await flush();
  assert.equal(app.elements['map-edges'].children.length, 1); assert.equal(app.elements['detail-id'].textContent, 'local-a');
  app.elements['node-list'].children[1].dispatch('click');
  assert.equal(app.elements['detail-citations'].textContent, '정보 없음'); assert.equal(app.elements['detail-year'].textContent, '정보 없음'); assert.equal(app.elements['detail-pdf'].hidden, true);
  assert.match(app.elements['detail-relation'].textContent, /공통 참고문헌 3편 · 수집 범위 내 동시 인용 2편 · 직접 인용/);
  assert.equal(app.elements['detail-abstract'].textContent, '<img src=x> This is text.'); assert.equal(app.elements['terms-section'].hidden, true);
  assert.ok(walk(app.elements['map-nodes']).some((node) => node.className.includes('unknown-citations')));
  app.elements['recenter-paper'].dispatch('click'); assert.match(app.requests[2].url, /doi=10.1234%2Fexternal/); assert.doesNotMatch(app.requests[2].url, /article_number/);
  const next = citationGraph(); next.seed_id = 'W200'; app.requests[2].respond(next); await flush(); assert.match(app.location.search, /doi=10.1234%2Fexternal/); assert.match(app.location.search, /root=W200/);
});

test('partial citation data shows a retry notice without exposing provider warning details', async () => {
  const app = createApp('?article_number=local-a'); const data = citationGraph(); data.meta.warnings = ['internal provider detail'];
  app.requests[0].respond({ papers: [] }); app.requests[1].respond(data); await flush();
  assert.match(app.elements['coverage-note'].textContent, /일부 데이터를 가져오지 못했습니다. 5분 후 다시 조회하면 재시도합니다./);
  assert.doesNotMatch(app.elements['coverage-note'].textContent, /internal provider detail/);
  assert.ok(walk(app.elements['map-legend']).some((node) => node.textContent === '점선 = 직접 인용'));
});

test('seed-only citation maps explain missing links, keep a visible seed and offer SQL similarity explicitly', async () => {
  const app = createApp('?article_number=local-a&root=W100&depth=1&direction=both&hierarchy_limit=30');
  const data = citationGraph();
  data.nodes = [data.nodes[0]]; data.edges = []; data.citation_edges = [];
  data.nodes[0].citation_count = 0;
  data.lineage_nodes = data.nodes; data.lineage_edges = []; data.prior_works = [];
  data.meta = { connected_candidate_count: 0, reference_total: 0, citing_total: 0, cache_ttl_days: 1 };
  app.requests[0].respond({papers:[]}); app.requests[1].respond(data); await flush();
  assert.equal(app.elements['citation-empty'].hidden, false);
  assert.match(app.elements['citation-empty-description'].textContent, /OpenAlex/);
  assert.match(app.elements['citation-empty-description'].textContent, /24시간/);
  assert.equal(app.elements['map-overlay'].hidden, true);
  assert.equal(app.elements['map-nodes'].children.length, 1);
  assert.doesNotMatch(JSON.stringify(walk(app.elements['map-nodes']).map(node => node.attributes)), /NaN|Infinity/);
  assert.equal(app.elements['detail-title'].textContent, 'Paper local-a');
  assert.equal(app.elements['use-local-empty'].hidden, false);
  assert.equal(app.requests.length, 2);
  app.elements['use-local-empty'].dispatch('click');
  assert.equal(app.elements['citation-empty'].hidden, true);
  assert.match(app.requests[2].url, /mode=local/); assert.match(app.requests[2].url, /article_number=local-a/);
  app.requests[2].respond(graph(paper('local-a'),[paper('b')])); await flush();
  assert.equal(app.elements['citation-empty'].hidden, true);
});

test('empty external citation maps do not offer an unavailable SQL fallback', async () => {
  const app = createApp('?doi=10.1234/external'); const data = citationGraph();
  data.nodes = [paper(null,{id:'W100',doi:'10.1234/external',in_library:false})];
  data.edges = []; data.citation_edges = []; data.lineage_nodes = data.nodes; data.lineage_edges = [];
  data.meta = { connected_candidate_count:0 };
  app.requests[0].respond({papers:[]}); app.requests[1].respond(data); await flush();
  assert.equal(app.elements['citation-empty'].hidden,false);
  assert.equal(app.elements['use-local-empty'].hidden,true);
  assert.equal(app.requests.length,2);
});

test('supplemental citations expose provenance, verified reference coverage and IEEE deferral without refetching', async () => {
  const app = createApp('?article_number=local-a&view=timeline'); const data = citationGraph();
  data.meta = {source_labels:['OpenAlex','보유 PDF','IEEE'],reference_origin:'pdf',reference_total:32,reference_fetched:20,reference_unresolved:12,ieee:{status:'deferred'}};
  data.nodes[1].related_by.direct_citation_evidence=[{source:'pdf',number:5,page:13}];
  data.lineage_edges[1] = {...data.lineage_edges[1],reference_source:'pdf',reference_number:5,reference_page:13};
  app.requests[0].respond({papers:[]}); app.requests[1].respond(data); await flush();
  assert.match(app.elements['graph-provider'].textContent,/보유 PDF/);
  assert.match(app.elements['coverage-note'].textContent,/32개 중 20개 연결 · 12개 미확인/);
  assert.match(app.elements['coverage-note'].textContent,/IEEE 후속 인용 보완/);
  assert.ok(walk(app.elements['timeline-edges']).some(node=>node.textContent.includes('보유 PDF 13쪽 [5]')));
  app.elements['node-list'].children[1].dispatch('click');
  assert.match(app.elements['detail-relation'].textContent,/보유 PDF 13쪽 \[5\]/);
  assert.equal(app.requests.length,2);
});

test('prior works show details independently of graph nodes and preserve a zero citation count', async () => {
  const app = createApp('?mode=citation&doi=10.1234%2Fseed'); app.requests[0].respond({ papers: [] }); app.requests[1].respond(citationGraph()); await flush();
  app.elements['tab-prior'].dispatch('click'); assert.equal(app.elements['view-map'].hidden, true); assert.equal(app.elements['view-prior'].hidden, false);
  app.elements['prior-list'].children[0].dispatch('click'); assert.equal(app.elements['detail-title'].textContent, 'Paper null'); assert.equal(app.elements['detail-citations'].textContent, '0'); assert.equal(app.elements['detail-abstract'].textContent, 'Prior abstract'); assert.equal(app.elements['similarity-section'].hidden, false);
  app.elements['tab-prior'].dispatch('keydown', { key: 'ArrowRight' }); assert.equal(app.elements['tab-derivative'].getAttribute('aria-selected'), 'true'); assert.equal(app.elements['tab-derivative'].focused, true);
});

test('building graphs poll the same target and abort polling when a newer paper is selected', async () => {
  const app = createApp('?article_number=local-a', true); app.requests[0].respond({ papers: [paper('b')] }); app.requests[1].respond({ status: 'building', retry_after: 2, message: 'Collecting references' }, 202); await flush();
  assert.equal(app.elements['map-stage'].getAttribute('aria-busy'), 'true'); assert.equal(app.elements['map-state-description'].textContent, 'Collecting references');
  app.runTimers(2000); await flush(); assert.equal(app.requests[2].url, app.requests[1].url);
  app.requests[2].respond({ status: 'building', retry_after: 2 }, 202); await flush();
  app.elements['search-results'].children[0].dispatch('click'); assert.equal(app.requests[2].options.signal.aborted, true);
  app.requests[3].respond({ ...citationGraph(), seed_id: 'W100' }); await flush(); app.runTimers(2000); await flush();
  assert.equal(app.requests.length, 4); assert.match(app.location.search, /article_number=b/); assert.equal(app.elements['map-stage'].getAttribute('aria-busy'), 'false');
});

test('citation failures offer an explicit local fallback with the original SQL seed', async () => {
  const app = createApp('?article_number=local-a'); app.requests[0].respond({ papers: [] }); app.requests[1].respond({ error: 'Provider unavailable' }, 503); await flush();
  assert.equal(app.requests.length, 2); assert.equal(app.elements['use-local'].hidden, false); assert.equal(app.elements['map-state-description'].textContent, 'Provider unavailable');
  app.elements['use-local'].dispatch('click'); assert.match(app.requests[2].url, /mode=local/); assert.match(app.requests[2].url, /article_number=local-a/);
  app.requests[2].respond(graph(paper('local-a'))); await flush(); assert.match(app.elements['coverage-note'].textContent, /인용 데이터는 사용하지 않습니다/); assert.equal(app.elements['tab-prior'].disabled, true);
});

test('pending generation times out cleanly and external seeds cannot silently use SQL mode', async () => {
  const app = createApp('?doi=10.1234%2Fseed', true); app.requests[0].respond({ papers: [] }); app.requests[1].respond({ status: 'building', retry_after: 2 }, 202); await flush();
  app.runTimers(120000); await flush(); assert.equal(app.elements['map-stage'].getAttribute('aria-busy'), 'false'); assert.match(app.elements['map-state-description'].textContent, /시간이 더 필요/); assert.equal(app.elements['use-local'].hidden, true);
  app.elements['graph-mode'].value = 'local'; app.elements['graph-mode'].dispatch('change'); assert.equal(app.requests.length, 2); assert.match(app.elements['map-state-title'].textContent, /저장소 논문을 선택/); assert.doesNotMatch(app.location.search, /doi=/);
});


test('timeline uses directed citation records, spaces publication years and isolates unknown years without fetching', async () => {
  const app = createApp('?article_number=local-a&view=timeline'); const data = citationGraph();
  data.nodes[0].year = 2024;
  data.nodes.push(paper(null, { id: 'W400', year: 2020, in_library: false, title: 'Prior circuit' }));
  data.citation_edges = [{source:'W400',target:'W100',cited:'W400',citing:'W100'}];
  data.lineage_nodes = data.nodes; data.lineage_edges = [...data.citation_edges, {source:'W100',target:'W200'}];
  app.requests[0].respond({papers:[]}); app.requests[1].respond(data); await flush();
  assert.equal(app.elements['view-timeline'].hidden, false);
  assert.equal(app.elements['timeline-edges'].children.length, 2);
  const edge = app.elements['timeline-edges'].children.find(node => node.getAttribute('data-source') === 'W400');
  assert.equal(edge.getAttribute('data-source'), 'W400'); assert.equal(edge.getAttribute('data-target'), 'W100');
  assert.equal(edge.getAttribute('marker-end'), 'url(#citation-arrow)');
  const nodes = app.elements['timeline-nodes'].children;
  const x = (id) => Number(nodes.find(n => n.getAttribute('data-node-id') === id).getAttribute('transform').match(/translate\(([^,]+)/)[1]);
  assert.ok(x('W400') < x('W100')); assert.ok(x('W200') > x('W100'));
  assert.equal(nodes.find(n => n.getAttribute('data-node-id') === 'W200').getAttribute('data-year'), 'unknown');
  app.elements['tab-list'].dispatch('click'); app.elements['tab-timeline'].dispatch('click');
  assert.equal(app.requests.length, 2);
  nodes[0].dispatch('click'); assert.equal(app.elements['detail-title'].textContent, 'Prior circuit');
});

test('timeline never infers arrows from undirected similarity and handles same-year or seed-only data', async () => {
  const app = createApp('?article_number=local-a'); const data = citationGraph();
  data.nodes[1].year = 2025; data.citation_edges = [];
  data.lineage_nodes = data.nodes; data.lineage_edges = [];
  app.requests[0].respond({papers:[]}); app.requests[1].respond(data); await flush();
  assert.equal(app.elements['timeline-edges'].children.length, 0);
  assert.match(app.elements['timeline-status'].textContent, /직접 인용 기록이 없습니다/);
  assert.equal(app.elements['timeline-nodes'].children.length, 1);
});

test('filters update graph, timeline, paper list and table together while preserving seed and excluding dangling edges', async () => {
  const app = createApp('?article_number=local-a'); const data = citationGraph();
  data.nodes[1].year = 2026; data.nodes[1].citation_count = 5;
  data.citation_edges = [{source:'W100',target:'W200'}];
  data.lineage_nodes = data.nodes; data.lineage_edges = data.citation_edges;
  app.requests[0].respond({papers:[]}); app.requests[1].respond(data); await flush();
  assert.equal(app.elements['graph-paper-list'].children.length, 2);
  app.elements['filter-citations'].value = '10'; app.elements['filter-citations'].dispatch('input');
  for(const id of ['map-nodes','timeline-nodes','table-papers','graph-paper-list']) assert.equal(app.elements[id].children.length, 1);
  assert.equal(app.elements['map-edges'].children.length, 0); assert.equal(app.elements['timeline-edges'].children.length, 0);
  assert.match(app.elements['graph-count'].textContent, /1 \/ 2/);
  app.elements['filter-query'].value = 'does not match seed'; app.elements['filter-query'].dispatch('input');
  assert.equal(app.elements['map-nodes'].children.length, 1);
  app.elements['reset-filters'].dispatch('click'); assert.equal(app.elements['map-nodes'].children.length, 2);
  app.elements['filter-library'].value = 'external'; app.elements['filter-library'].dispatch('change');
  assert.equal(app.elements['map-nodes'].children.length, 2);
  app.elements['filter-pdf'].checked = true; app.elements['filter-pdf'].dispatch('change');
  assert.equal(app.elements['map-nodes'].children.length, 1);
  assert.equal(app.requests.length, 2);
});

test('invalid filter ranges retain the last valid display and show an actionable message', async () => {
  const app = createApp('?article_number=local-a'); app.requests[0].respond({papers:[]}); app.requests[1].respond(citationGraph()); await flush();
  app.elements['filter-year-from'].value = '2026'; app.elements['filter-year-to'].value = '2020'; app.elements['filter-year-to'].dispatch('input');
  assert.match(app.elements['filter-status'].textContent, /시작 연도/);
  assert.equal(app.elements['map-nodes'].children.length, 2);
  app.elements['filter-year-from'].value = '-100'; app.elements['filter-year-from'].dispatch('input');
  assert.match(app.elements['filter-status'].textContent, /정수/);
});

test('paper list and list view select details without rebuilding the graph, and search stays available', async () => {
  const app = createApp('?article_number=local-a'); app.requests[0].respond({papers:[]}); app.requests[1].respond(citationGraph()); await flush();
  assert.equal(app.elements['graph-paper-panel'].hidden, false);
  app.elements['graph-paper-list'].children[1].dispatch('click');
  assert.equal(app.elements['detail-title'].textContent, '<script>external title</script>');
  assert.ok(walk(app.elements['table-papers']).every(node => node.tag !== 'script'));
  app.elements['list-mode-search'].dispatch('click'); assert.equal(app.elements['library-search-panel'].hidden, false);
  app.elements['list-mode-graph'].dispatch('click'); assert.equal(app.elements['graph-paper-panel'].hidden, false);
  app.elements['tab-list'].dispatch('click'); assert.equal(app.elements['view-list'].hidden, false);
  assert.equal(app.requests.length, 2);
});



test('citation counts remain visible on graph nodes and in prior works, including zero and unknown values', async () => {
  const app = createApp('?article_number=local-a'); const data = citationGraph();
  data.nodes[0].citation_source = 'openalex'; data.nodes[0].citation_updated_at = '2026-10-15T00:00:00Z';
  data.prior_works[0].citation_source = 'openalex'; data.prior_works[0].citation_updated_at = '2026-10-15T00:00:00Z';
  app.requests[0].respond({papers:[]}); app.requests[1].respond(data); await flush();
  const labels = walk(app.elements['map-nodes']).filter(node => node.className === 'node-citation-count');
  assert.deepEqual(labels.map(node => node.textContent), ['60', '?']);
  assert.match(app.elements['detail-citation-meta'].textContent, /OpenAlex.*2026-10-15/);
  const priorBadge = walk(app.elements['prior-list']).find(node => node.className === 'citation-badge');
  assert.equal(priorBadge.textContent, '인용 0회');
  assert.match(priorBadge.getAttribute('title'), /OpenAlex.*2026-10-15/);
  assert.equal(app.requests.length, 2);
});

test('SQL-only search and map display saved publisher counts and dates without external calls', async () => {
  const app = createApp('?mode=local&article_number=a');
  const seed = paper('a', {citation_count:0,citation_source:'ieee',citation_updated_at:'2026-10-14T00:00:00Z'});
  const related = paper('b', {citation_count:12,citation_source:'crossref',citation_updated_at:'2026-10-13T00:00:00Z'});
  app.requests[0].respond({papers:[seed]}); app.requests[1].respond(graph(seed,[related])); await flush();
  assert.equal(app.elements['citation-count-row'].hidden, false);
  assert.equal(app.elements['detail-citations'].textContent, '0');
  assert.match(app.elements['detail-citation-meta'].textContent, /IEEE Xplore.*2026-10-14/);
  const badge = walk(app.elements['search-results']).find(node => node.className === 'citation-badge');
  assert.equal(badge.textContent, '인용 0회');
  app.elements['graph-paper-list'].children[1].dispatch('click');
  assert.equal(app.elements['detail-citations'].textContent, '12');
  assert.match(app.elements['detail-citation-meta'].textContent, /Crossref.*2026-10-13/);
  assert.equal(app.elements['table-papers'].children[1].children[2].textContent, '12');
  assert.equal(app.requests.length, 2);
});



test('hierarchy root, direction and depth update prior works and timeline together without new requests', async () => {
  const app=createApp('?article_number=local-a&view=timeline'); const data=citationGraph();
  const second=paper(null,{id:'W400',title:'Older reference',year:2020});
  data.lineage_nodes.push(second); data.lineage_edges.push({source:'W400',target:'W300'});
  app.requests[0].respond({papers:[]}); app.requests[1].respond(data); await flush();
  assert.equal(app.elements['prior-list'].children.length,1);
  app.elements['lineage-depth'].value='2'; app.elements['lineage-depth'].dispatch('change');
  assert.equal(app.elements['prior-list'].children.length,2);
  assert.ok(walk(app.elements['prior-list']).some(node=>node.textContent.includes('기준에서 2단계')));
  assert.ok(app.elements['timeline-nodes'].children.some(node=>node.getAttribute('data-node-id')==='W400'));
  app.elements['node-list'].children[1].dispatch('click');
  assert.equal(app.elements['lineage-use-selected'].disabled,false);
  app.elements['lineage-use-selected'].dispatch('click');
  assert.equal(app.elements['lineage-root-title'].textContent,'<script>external title</script>');
  assert.ok(walk(app.elements['prior-list']).some(node=>node.textContent==='Paper local-a'));
  app.elements['lineage-direction'].value='derivative'; app.elements['lineage-direction'].dispatch('change');
  assert.equal(app.elements['prior-list'].children.length,1); // Empty-state message.
  assert.equal(app.elements['prior-count'].textContent,'0');
  assert.equal(app.elements['timeline-nodes'].children.length,1);
  app.elements['lineage-reset-root'].dispatch('click');
  assert.equal(app.elements['lineage-root-title'].textContent,'Paper local-a');
  assert.equal(app.elements['derivative-count'].textContent,'1');
  assert.equal(app.requests.length,2);
});

test('shared hierarchy URLs restore the selected root and controls while changing limits remains local', async () => {
  const app=createApp('?article_number=local-a&view=timeline&root=W300&depth=3&direction=derivative&hierarchy_limit=60');
  app.requests[0].respond({papers:[]}); app.requests[1].respond(citationGraph()); await flush();
  assert.equal(app.elements['lineage-root-title'].textContent,'Paper null');
  assert.equal(app.elements['detail-title'].textContent,'Paper null');
  assert.equal(app.elements['lineage-depth'].value,'3');
  assert.equal(app.elements['lineage-direction'].value,'derivative');
  assert.equal(app.elements['lineage-limit'].value,'60');
  app.elements['lineage-limit'].value='30'; app.elements['lineage-limit'].dispatch('change');
  assert.match(app.location.search,/root=W300/); assert.match(app.location.search,/hierarchy_limit=30/);
  assert.equal(app.requests.length,2);
});
