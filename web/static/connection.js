/* Connection research explorer. All data comes from authenticated same-origin APIs. */
(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const svgNS = 'http://www.w3.org/2000/svg';
  const views = ['map', 'timeline', 'list', 'prior', 'derivative'];
  const lineageTraversal = window.ConnectionLineage;
  const state = { graph: null, selected: null, seed: null, target: null, pendingTarget: null, mode: 'citation', view: 'map', requestedView: 'map', visibleNodes: [], visibleEdges: [], filters: {}, searchRequest: 0, graphRequest: 0, searchAbort: null, graphAbort: null, searchTimer: null, transform: { x: 0, y: 0, k: 1 }, positions: new Map(), nodeEls: new Map(), listEls: new Map(), timelineEls: new Map(), timelineEdges: [], paperEls: new Map(), tableEls: new Map(), workEls: [], edgeEls: [], drag: null, suppressClick: false, labels: false };
  state.lineage = null; state.lineageRoot = null; state.lineageConfig = { depth: 1, direction: 'both', limit: 30 }; state.requestedRoot = null;
  const asText = (value, fallback = '') => value == null ? fallback : String(value);
  const titleOf = (paper) => asText(paper.title, '제목 없음') || '제목 없음';
  const idOf = (paper) => asText(paper.id ?? paper.article_number);
  const authorsOf = (paper) => Array.isArray(paper.authors) ? paper.authors.join(', ') : asText(paper.authors);
  const score = (value) => Math.max(0, Math.min(1, Number(value) || 0));
  const scoreText = (value) => `${(score(value) * 100).toFixed(1)}%`;
  const isCitation = () => state.graph?.source === 'citation_similarity';
  const knownCount = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) && Number(value) >= 0;
  const countText = (value) => knownCount(value) ? Number(value).toLocaleString('ko-KR') : '정보 없음';
  const citationSummary = (paper) => knownCount(paper.citation_count) ? `인용 ${countText(paper.citation_count)}회` : '인용 수 미확인';
  function citationMeta(paper) {
    if (!knownCount(paper.citation_count)) return '아직 확인된 인용 수가 없습니다.';
    const sources = { openalex: 'OpenAlex', ieee: 'IEEE Xplore', crossref: 'Crossref' };
    const source = sources[asText(paper.citation_source).toLowerCase()] || asText(paper.citation_source, isCitation() && (paper.metadata_source === 'openalex' || idOf(paper).startsWith('W')) ? 'OpenAlex' : '출처 미확인');
    const date = paper.citation_updated_at ? new Date(paper.citation_updated_at) : null;
    return source + (date && !Number.isNaN(date.getTime()) ? ` · ${date.toISOString().slice(0, 10)} 확인` : ' · 확인일 미상');
  }
  function citationBadge(paper) { const badge = el('span', 'citation-badge', citationSummary(paper)); badge.setAttribute('title', citationMeta(paper)); return badge; }
  const yearOf = (paper) => Number.isInteger(Number(paper.year)) && Number(paper.year) >= 1000 && Number(paper.year) <= 2200 ? Number(paper.year) : null;
  const inLibrary = (paper) => paper.in_library === undefined ? Boolean(paper.article_number) : paper.in_library;
  const targetOf = (paper) => paper.article_number && inLibrary(paper) ? { article_number: asText(paper.article_number) } : doiURL(paper.doi) ? { doi: asText(paper.doi) } : null;
  function el(tag, className, value) { const node = document.createElement(tag); if (className) node.className = className; if (value !== undefined) node.textContent = value; return node; }
  function svg(tag, attrs, value) { const node = document.createElementNS(svgNS, tag); for (const [key, val] of Object.entries(attrs || {})) node.setAttribute(key, String(val)); if (value !== undefined) node.textContent = value; return node; }
  function statusMessage(parent, title, message, retry) { parent.replaceChildren(); const box = el('div', 'inline-empty'); box.append(el('strong', '', title), el('div', '', message)); if (retry) { const button = el('button', 'button secondary', '다시 시도'); button.type = 'button'; button.addEventListener('click', retry); box.append(button); } parent.append(box); }
  async function getJSON(path, signal) {
    const response = await fetch(path, { signal, headers: { Accept: 'application/json' }, credentials: 'same-origin' });
    if (response.status === 401 || response.redirected) throw new Error('로그인 세션을 확인한 뒤 페이지를 새로고침해 주세요.');
    const type = response.headers.get('content-type') || '';
    if (!type.includes('application/json')) throw new Error('서버 응답을 읽을 수 없습니다. 잠시 후 다시 시도해 주세요.');
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.error === 'string' ? data.error : '논문 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.');
    return data;
  }
  function sourceName(paper) { return asText(paper.venue || paper.source_name || paper.source_system, '출처 정보 없음'); }
  function paperButton(paper) {
    const button = el('button', 'paper-result'); button.type = 'button'; button.dataset.paperId = idOf(paper); button.setAttribute('aria-pressed', String(idOf(paper) === state.target?.article_number)); button.setAttribute('aria-label', `${titleOf(paper)} — 이 논문으로 지도 탐색`);
    const eyebrow = el('div', 'result-eyebrow'); eyebrow.append(el('span', 'year', asText(paper.year, '연도 미상')), el('span', '', '·'), el('span', '', sourceName(paper)));
    button.append(eyebrow, el('h3', '', titleOf(paper)), el('p', 'result-authors', authorsOf(paper) || '저자 정보 없음'));
    button.append(citationBadge(paper));
    const badges = el('div', 'result-badges'); if (paper.pdf_available) badges.append(el('span', '', 'PDF')); if (paper.is_favorite) badges.append(el('span', 'favorite', '★ 즐겨찾기')); if (badges.childNodes.length) button.append(badges);
    button.addEventListener('click', () => loadGraph({ article_number: asText(paper.article_number || paper.id) })); return button;
  }
  async function search() {
    clearTimeout(state.searchTimer); state.searchAbort?.abort(); state.searchAbort = new AbortController(); const request = ++state.searchRequest;
    const query = $('paper-query').value.trim(); $('search-status').textContent = '검색 중…'; $('search-results').setAttribute('aria-busy', 'true');
    try {
      const data = await getJSON(`/api/connection/search?${new URLSearchParams({ q: query, limit: '20' })}`, state.searchAbort.signal);
      if (request !== state.searchRequest) return;
      const papers = Array.isArray(data.papers) ? data.papers : []; $('search-results').replaceChildren();
      $('search-status').textContent = query ? `검색 결과 ${papers.length}편${data.has_more ? ' · 일부 표시' : ''}` : `즐겨찾기 · 최근 논문 ${papers.length}편`;
      if (!papers.length) statusMessage($('search-results'), '검색된 논문이 없습니다', query ? '다른 키워드나 짧은 제목으로 검색해 보세요.' : '저장소에 논문이 추가되면 여기서 탐색할 수 있습니다.');
      else papers.forEach((paper) => $('search-results').append(paperButton(paper)));
    } catch (error) {
      if (error.name === 'AbortError' || request !== state.searchRequest) return;
      $('search-status').textContent = '검색을 완료하지 못했습니다'; statusMessage($('search-results'), '저장소 연결을 확인해 주세요', error.message, search);
    } finally { if (request === state.searchRequest) $('search-results').setAttribute('aria-busy', 'false'); }
  }
  function showOverlay(kind, title, description) {
    const overlay = $('map-overlay'); overlay.hidden = false; overlay.className = `map-overlay ${kind}`; $('map-state-title').textContent = title; $('map-state-description').textContent = description; $('map-retry').hidden = kind !== 'error'; $('use-local').hidden = kind !== 'error' || state.mode !== 'citation' || !state.pendingTarget?.article_number;
  }
  function waitForPoll(milliseconds, signal) {
    return new Promise((resolve, reject) => {
      const onAbort = () => { clearTimeout(timer); reject(Object.assign(new Error('Cancelled'), { name: 'AbortError' })); };
      const timer = setTimeout(() => { signal.removeEventListener('abort', onAbort); resolve(); }, milliseconds);
      signal.addEventListener('abort', onAbort, { once: true }); if (signal.aborted) onAbort();
    });
  }
  async function loadGraph(target, updateUrl = true) {
    if (!target || (!target.article_number && !target.doi)) return;
    state.graphAbort?.abort(); const controller = new AbortController(); state.graphAbort = controller; const request = ++state.graphRequest; state.pendingTarget = target;
    const mode = state.mode; const deadline = Date.now() + 120000; let expired = false;
    const desiredView = state.requestedView;
    const deadlineTimer = setTimeout(() => { expired = true; controller.abort(); }, 120000);
    setView('map'); $('map-legend').hidden = true; $('citation-empty').hidden = true; $('use-local-empty').hidden = true;
    state.graph = null; state.selected = null; state.seed = null; state.nodeEls.clear(); state.listEls.clear(); state.workEls = [];
    state.lineage = null; state.lineageRoot = null;
    for (const id of ['graph-paper-list', 'table-papers', 'timeline-axis', 'timeline-edges', 'timeline-nodes']) $(id).replaceChildren();
    state.timelineEls.clear(); state.paperEls.clear(); state.tableEls.clear(); state.timelineEdges = [];
    $('paper-list-count').textContent = '0'; $('tab-timeline').disabled = true; $('tab-list').disabled = true;
    $('map-nodes').replaceChildren(); $('map-edges').replaceChildren(); $('node-list-section').hidden = true; $('paper-details').hidden = true; $('details-empty').hidden = false; $('graph-count').textContent = '…';
    updateMethod();
    for (const name of ['prior', 'derivative']) { $(name + '-list').replaceChildren(); $(name + '-count').textContent = '0'; $('tab-' + name).disabled = true; }
    $('map-stage').setAttribute('aria-busy', 'true'); $('map-stage').classList.add('loading'); $('recenter-paper').disabled = true;
    showOverlay('loading', '연구의 연결점을 찾고 있습니다', mode === 'citation' ? '공개 인용 관계를 수집하고 지도를 구성합니다. 처음에는 시간이 걸릴 수 있습니다.' : '저장소의 제목과 저장된 초록을 비교하는 중입니다.'); $('map-status').textContent = mode === 'citation' ? '인용 데이터를 수집하는 중…' : '유사한 논문을 찾는 중…';
    try {
      const path = `/api/connection/graph?${new URLSearchParams({ ...target, mode, limit: $('map-limit').value })}`;
      let data;
      do {
        data = await getJSON(path, controller.signal);
        if (request !== state.graphRequest) return;
        if (data.status !== 'building') break;
        if (Date.now() >= deadline) throw new Error('지도 준비에 시간이 더 필요합니다. 잠시 후 다시 시도해 주세요.');
        $('map-state-description').textContent = asText(data.message, '인용 데이터를 수집하고 있습니다. 잠시만 기다려 주세요.');
        await waitForPoll(Math.max(1000, Math.min(10000, (Number(data.retry_after) || 2) * 1000)), controller.signal);
      } while (request === state.graphRequest);
      if (request !== state.graphRequest) return;
      if (!Array.isArray(data.nodes) || !data.nodes.length) throw new Error('지도에 표시할 논문이 없습니다. 다른 논문을 선택해 주세요.');
      state.graph = data; state.seed = asText(data.seed_id, target.article_number || target.doi); state.target = target; state.pendingTarget = null;
      state.graph.nodes = data.nodes.map((paper) => ({ ...paper, id: idOf(paper), is_seed: idOf(paper) === state.seed }));
      state.graph.edges = Array.isArray(data.edges) ? data.edges : [];
      state.graph.citation_edges = Array.isArray(data.citation_edges) ? data.citation_edges : [];
      state.graph.lineage_nodes = (Array.isArray(data.lineage_nodes) ? data.lineage_nodes : data.nodes).map((paper) => ({ ...paper, id: idOf(paper), is_seed: idOf(paper) === state.seed }));
      state.graph.lineage_edges = Array.isArray(data.lineage_edges) ? data.lineage_edges : state.graph.citation_edges;
      state.lineageRoot = state.graph.lineage_nodes.some((paper) => paper.id === state.requestedRoot) ? state.requestedRoot : state.seed;
      state.requestedRoot = null;
      state.graph.prior_works = Array.isArray(data.prior_works) ? data.prior_works : []; state.graph.derivative_works = Array.isArray(data.derivative_works) ? data.derivative_works : [];
      $('map-overlay').hidden = true; $('page-title').textContent = titleOf(state.graph.nodes.find((paper) => paper.is_seed) || state.graph.nodes[0]);
      updateMethod(); renderFiltered(); setListMode('graph');
      selectPaper(isCitation() && ['timeline', 'prior', 'derivative'].includes(desiredView) ? state.lineageRoot : state.seed); markSearchSeed();
      const meta = data.meta || {}; const candidates = Number(meta.candidate_count); const abstracts = Number(meta.abstract_count);
      const counts = Number.isFinite(candidates) ? `${candidates.toLocaleString('ko-KR')}편 비교` : '저장소 텍스트 비교';
      $('map-status').textContent = isCitation() ? `${Number.isFinite(candidates) ? `${candidates.toLocaleString('ko-KR')}편 후보 · ` : ''}${data.nodes.length}편 표시 · 인용 관계 기반${data.nodes.length === 1 ? ' · 관련 연구를 찾지 못했습니다' : ''}` : data.nodes.length === 1 ? '기준 논문과 유사한 논문을 찾지 못했습니다. 다른 논문으로 탐색해 보세요.' : `${counts}${Number.isFinite(abstracts) ? ` · 표시 논문 중 초록 ${abstracts.toLocaleString('ko-KR')}편` : ''} · 텍스트 유사성 기반`;
      $('graph-count').textContent = `${data.nodes.length} papers`;
      setView(!isCitation() && ['timeline', 'prior', 'derivative'].includes(desiredView) ? 'map' : desiredView);
      updateVisibleCount();
      if (updateUrl) {
        const url = new URL(location.href); url.searchParams.delete('article_number'); url.searchParams.delete('doi'); url.searchParams.set('mode', mode); url.searchParams.set('view', state.view);
        for (const [key, value] of Object.entries(target)) url.searchParams.set(key, value);
        if (isCitation()) {
          url.searchParams.set('root', state.lineageRoot); url.searchParams.set('depth', state.lineageConfig.depth);
          url.searchParams.set('direction', state.lineageConfig.direction); url.searchParams.set('hierarchy_limit', state.lineageConfig.limit);
        } else for (const key of ['root', 'depth', 'direction', 'hierarchy_limit']) url.searchParams.delete(key);
        history.replaceState(null, '', url);
      }
    } catch (error) {
      if (request !== state.graphRequest || (error.name === 'AbortError' && !expired)) return;
      showOverlay('error', '지도를 불러오지 못했습니다', expired ? '지도 준비에 시간이 더 필요합니다. 잠시 후 다시 시도해 주세요.' : error.message); $('map-status').textContent = '다시 시도하거나 다른 논문을 선택해 주세요.';
    } finally {
      clearTimeout(deadlineTimer);
      if (request === state.graphRequest) { $('map-stage').setAttribute('aria-busy', 'false'); $('map-stage').classList.remove('loading'); $('recenter-paper').disabled = !state.selected || state.selected.is_seed || !targetOf(state.selected); }
    }
  }
  function markSearchSeed() { $('search-results').querySelectorAll('.paper-result').forEach((node) => node.setAttribute('aria-pressed', String(node.dataset.paperId === state.target?.article_number))); }
  function updateMethod() {
    const citation = state.mode === 'citation'; $('graph-mode').value = state.mode;
    const meta = state.graph?.meta || {};
    const labels = Array.isArray(meta.source_labels) && meta.source_labels.length ? meta.source_labels.map(value => asText(value)).join(' / ') : 'OpenAlex';
    $('method-badge').textContent = citation ? '공개 인용 관계 지도' : 'SQL / 제목·초록 유사도';
    $('method-summary').textContent = citation ? 'OpenAlex 인용 관계를 Crossref·보유 PDF 참고문헌과 IEEE 후속 인용으로 보완합니다.' : '기존 SQL의 제목과 저장된 초록을 비교합니다. 저장소에 있는 논문만 표시합니다.';
    $('graph-provider').textContent = citation ? '인용 데이터 · '+labels : '기존 SQL 저장소';
    $('tab-prior').disabled = !citation || !state.graph; $('tab-derivative').disabled = !citation || !state.graph;
    $('tab-timeline').disabled = !citation || !state.graph; $('tab-list').disabled = !state.graph;
    $('lineage-controls').hidden = !citation || !state.graph;
    $('method-note-primary').textContent = citation ? '점의 크기는 인용 수, 색은 발행 연도입니다. 이중 테두리는 기준 논문입니다. 선은 공통 참고문헌, 동시 인용, 직접 인용으로 계산합니다.' : '점은 저장소의 논문, 선은 제목·초록의 공통점을 나타냅니다. 점의 크기는 기준 논문과의 텍스트 유사도입니다.';
    $('method-note-secondary').textContent = citation ? '공개 데이터의 수집 범위에 따라 누락이 있을 수 있습니다. 위치는 탐색을 돕기 위한 배치이며 인용 방향이나 연구 품질을 나타내지 않습니다.' : '초록이 없는 논문은 제목만 비교합니다. 이 모드의 선은 인용 관계를 뜻하지 않습니다.';
    const empty = citation && Boolean(state.graph) && Number(meta.connected_candidate_count ?? state.graph.nodes.length - 1) === 0;
    $('citation-empty').hidden = !empty;
    $('use-local-empty').hidden = !empty || !state.target?.article_number;
    if (empty) $('citation-empty-description').textContent = 'OpenAlex 등 조회한 출처의 연결 정보가 부족해 기준 논문만 표시합니다. 참고문헌·인용 관계는 추후 데이터에 반영될 수 있습니다.' + (Number(meta.cache_ttl_days) === 1 ? ' 24시간 후 다시 열면 연결 정보를 새로 조회합니다.' : '');
    $('coverage-note').textContent = citation ? asText(meta.coverage_note, '공개 인용 데이터의 제한된 범위를 수집한 지도입니다. 연결이 없다고 실제 인용이 없는 것은 아닙니다.') + (meta.fetched_at ? ` · 조회: ${asText(meta.fetched_at)}` : '') : '제목·초록의 텍스트 유사성 기반 · 인용 데이터는 사용하지 않습니다.';
    if (citation && Array.isArray(meta.warnings) && meta.warnings.length) $('coverage-note').textContent += ' 일부 데이터를 가져오지 못했습니다. 5분 후 다시 조회하면 재시도합니다.';
    if (citation && ['crossref','pdf'].includes(meta.reference_origin)) $('coverage-note').textContent += ` · 참고문헌 출처: ${meta.reference_origin === 'pdf' ? '보유 PDF' : 'Crossref'} · ${countText(meta.reference_total)}개 중 ${countText(meta.reference_fetched)}개 연결 · ${countText(meta.reference_unresolved)}개 미확인`;
    if (citation && ['deferred','partial'].includes(meta.ieee?.status)) $('coverage-note').textContent += ' · IEEE 후속 인용 보완은 사용량 제한 또는 일시적 오류로 일부 생략했습니다. 현재 확인된 연결은 유지됩니다.';
    if (citation && Array.isArray(meta.source_notes)) $('coverage-note').textContent += meta.source_notes.map(value => ' · '+asText(value)).join('');
  }
  function setView(view, updateUrl = false) {
    if (!views.includes(view)) view = 'map';
    state.view = view;
    for (const name of views) { $('view-' + name).hidden = name !== view; $('tab-' + name).setAttribute('aria-selected', String(name === view)); $('tab-' + name).setAttribute('tabindex', name === view ? '0' : '-1'); }
    if (updateUrl) { state.requestedView = view; const url = new URL(location.href); url.searchParams.set('view', view); history.replaceState(null, '', url); }
  }
  function setListMode(mode) {
    const graph = mode === 'graph'; $('graph-paper-panel').hidden = !graph; $('library-search-panel').hidden = graph;
    $('list-mode-graph').setAttribute('aria-pressed', String(graph)); $('list-mode-search').setAttribute('aria-pressed', String(!graph));
  }
  function matchesFilters(paper) {
    const f = state.filters, year = yearOf(paper);
    if (f.query && !`${titleOf(paper)} ${authorsOf(paper)}`.toLocaleLowerCase().includes(f.query)) return false;
    if (f.from && (year === null || year < f.from)) return false;
    if (f.to && (year === null || year > f.to)) return false;
    if (f.citations && (!knownCount(paper.citation_count) || Number(paper.citation_count) < f.citations)) return false;
    if (f.library === 'local' && !inLibrary(paper)) return false;
    if (f.library === 'external' && inLibrary(paper)) return false;
    return !f.pdf || Boolean(paper.pdf_available && inLibrary(paper));
  }
  function sortedPapers(papers) {
    const mode = $('paper-sort').value;
    return [...papers].sort((a, b) => Number(Boolean(b.is_seed)) - Number(Boolean(a.is_seed)) ||
      (mode === 'year' ? (yearOf(b) || 0) - (yearOf(a) || 0) : mode === 'citations' ?
        (knownCount(b.citation_count) ? Number(b.citation_count) : -1) - (knownCount(a.citation_count) ? Number(a.citation_count) : -1) : score(b.similarity) - score(a.similarity)) || idOf(a).localeCompare(idOf(b)));
  }
  function readFilters() {
    function integer(id, min, max) { const raw = $(id).value.trim(); if (!raw) return null; if (!/^\d+$/.test(raw) || Number(raw) < min || Number(raw) > max) throw new Error('연도와 인용 수를 유효한 정수로 입력해 주세요.'); return Number(raw); }
    try {
      const from = integer('filter-year-from', 1000, 2200), to = integer('filter-year-to', 1000, 2200), citations = integer('filter-citations', 0, 1000000000);
      if (from && to && from > to) throw new Error('시작 연도는 종료 연도보다 클 수 없습니다.');
      state.filters = { query: $('filter-query').value.trim().toLocaleLowerCase(), from, to, citations, library: $('filter-library').value || 'all', pdf: Boolean($('filter-pdf').checked) };
      $('filter-status').textContent = '수집된 논문에 적용합니다. 기준 논문은 항상 표시합니다.';
      $('filter-count').textContent = String(Object.entries(state.filters).filter(([key, value]) => Boolean(value) && !(key === 'library' && value === 'all')).length);
      renderFiltered();
    } catch (error) { $('filter-status').textContent = error.message; }
  }
  function updateVisibleCount() {
    if (!state.graph) return;
    const shown = state.visibleNodes.length, total = state.graph.nodes.length;
    $('graph-count').textContent = shown === total ? `${total} papers` : `${shown} / ${total} papers`;
    $('paper-list-count').textContent = String(shown);
    $('graph-list-status').textContent = `지도 속 ${total}편 중 ${shown}편 표시 · 기준 논문 포함`;
  }
  function renderFiltered() {
    if (!state.graph) return;
    const previous = state.selected;
    state.visibleNodes = sortedPapers(state.graph.nodes.filter((paper) => paper.is_seed || matchesFilters(paper)));
    const ids = new Set(state.visibleNodes.map(idOf));
    state.visibleEdges = state.graph.edges.filter((edge) => ids.has(asText(edge.source)) && ids.has(asText(edge.target)));
    updateLineage(); drawGraph(); renderWorks(); renderPaperList(); renderTable(); renderTimeline(); updateVisibleCount();
    const available = new Set([...ids, ...(state.lineage?.nodes || []).map(idOf)]);
    selectPaper(previous && (available.has(previous.id) || matchesFilters(previous)) ? previous.id : state.seed, previous);
  }
  function updateLineage() {
    if (!isCitation()) { state.lineage = null; return; }
    state.lineage = lineageTraversal.explore({ nodes: state.graph.lineage_nodes, edges: state.graph.lineage_edges, root: state.lineageRoot, ...state.lineageConfig, filter: matchesFilters });
    const title = state.graph.lineage_nodes.find((paper) => paper.id === state.lineageRoot);
    $('lineage-root-title').textContent = title ? titleOf(title) : '시작 논문';
    $('lineage-depth').value = String(state.lineageConfig.depth); $('lineage-direction').value = state.lineageConfig.direction; $('lineage-limit').value = String(state.lineageConfig.limit);
    $('lineage-reset-root').disabled = state.lineageRoot === state.seed;
    const result = state.lineage;
    $('lineage-status').textContent = `저장된 ${state.graph.lineage_nodes.length}편에서 ${state.lineageConfig.depth}단계까지 · ${result.nodes.length}편 표시 · 선행 ${result.prior.length}편 / 후속 ${result.derivative.length}편${result.limited ? ' · 표시 상한 적용' : ''}. 계보도와 두 목록에 적용하며, 전체 인용망을 뜻하지 않습니다. 추가 외부 조회는 없습니다.`;
  }
  function changeLineage(root) {
    if (!state.graph || !isCitation()) return;
    if (root && state.graph.lineage_nodes.some((paper) => paper.id === root)) state.lineageRoot = root;
    state.lineageConfig = { depth: [1, 2, 3].includes(Number($('lineage-depth').value)) ? Number($('lineage-depth').value) : 1,
      direction: ['both', 'prior', 'derivative'].includes($('lineage-direction').value) ? $('lineage-direction').value : 'both',
      limit: $('lineage-limit').value === '60' ? 60 : 30 };
    updateLineage(); renderWorks(); renderTimeline();
    if (state.selected) selectPaper(state.selected.id, state.selected);
    const url = new URL(location.href); url.searchParams.set('depth', state.lineageConfig.depth); url.searchParams.set('direction', state.lineageConfig.direction); url.searchParams.set('hierarchy_limit', state.lineageConfig.limit); url.searchParams.set('root', state.lineageRoot); history.replaceState(null, '', url);
  }
  function renderPaperList() {
    $('graph-paper-list').replaceChildren(); state.paperEls.clear();
    for (const paper of state.visibleNodes) {
      const button = el('button', 'paper-result'); button.type = 'button'; button.setAttribute('aria-pressed', 'false');
      button.append(el('span', 'result-eyebrow', `${asText(paper.year, '연도 미상')} · ${paper.is_seed ? '기준 논문' : inLibrary(paper) ? '저장소 보유' : '외부 논문'}`), el('h3', '', titleOf(paper)), el('p', 'result-authors', authorsOf(paper)), citationBadge(paper));
      if (!isCitation()) button.append(el('span', 'result-stat', `유사도 ${scoreText(paper.similarity)}`));
      button.addEventListener('click', () => selectPaper(paper.id)); $('graph-paper-list').append(button); state.paperEls.set(paper.id, button);
    }
  }
  function renderTable() {
    $('table-papers').replaceChildren(); state.tableEls.clear();
    for (const paper of state.visibleNodes) {
      const row = el('tr', ''); const title = el('td', 'table-title'); const button = el('button', 'text-button', titleOf(paper)); button.type = 'button'; button.addEventListener('click', () => selectPaper(paper.id)); title.append(button, el('small', '', authorsOf(paper)));
      const citation = el('td', 'citation-cell', countText(paper.citation_count)); citation.setAttribute('title', citationMeta(paper));
      citation.append(el('small', '', citationMeta(paper)));
      row.append(title, el('td', '', asText(paper.year, '미상')), citation, el('td', '', inLibrary(paper) ? paper.pdf_available ? '보유 · PDF' : '보유' : '외부'));
      $('table-papers').append(row); state.tableEls.set(paper.id, row);
    }
    $('table-status').textContent = `${state.visibleNodes.length}편 표시 · 제목을 선택하면 오른쪽에서 논문을 살펴볼 수 있습니다.`;
  }
  function renderTimeline() {
    for (const id of ['timeline-axis', 'timeline-edges', 'timeline-nodes']) $(id).replaceChildren();
    state.timelineEls.clear(); state.timelineEdges = [];
    if (!isCitation()) { $('timeline-status').textContent = '시간축 계보도는 인용 관계 기반 모드에서 사용할 수 있습니다.'; return; }
    const nodes = [...(state.lineage?.nodes || [])].sort((a, b) => (yearOf(a) ?? 9999) - (yearOf(b) ?? 9999) || Number(b.is_lineage_root) - Number(a.is_lineage_root) || a.id.localeCompare(b.id));
    const edges = state.lineage?.edges || [];
    const years = nodes.map(yearOf).filter((year) => year !== null); const minimum = years.length ? Math.min(...years) : null, maximum = years.length ? Math.max(...years) : null;
    const span = minimum === null ? 0 : maximum - minimum; const pitch = span ? Math.min(180, 5500 / span) : 180;
    const knownEnd = 120 + span * pitch, unknownX = minimum === null ? 160 : knownEnd + 220;
    const positions = new Map(), placed = []; let maxRow = 0;
    for (const paper of nodes) {
      const year = yearOf(paper); const x = year === null ? unknownX : 120 + (year - minimum) * pitch;
      const neighbors = edges.filter((edge) => edge.source === paper.id || edge.target === paper.id).map((edge) => positions.get(edge.source === paper.id ? edge.target : edge.source)).filter(Boolean);
      const desired = neighbors.length ? Math.round(neighbors.reduce((sum, point) => sum + point.row, 0) / neighbors.length) : 1;
      const choices = Array.from({ length: nodes.length + 2 }, (_, row) => row).sort((a, b) => Math.abs(a - desired) - Math.abs(b - desired) || a - b);
      const row = choices.find((candidate) => !placed.some((point) => point.row === candidate && Math.abs(point.x - x) < 168));
      const position = { x, y: 120 + row * 92, row }; positions.set(paper.id, position); placed.push(position); maxRow = Math.max(maxRow, row);
    }
    const width = Math.max(900, (nodes.some((paper) => yearOf(paper) === null) ? unknownX : knownEnd) + 120), height = Math.max(450, 200 + maxRow * 92);
    $('timeline-map').setAttribute('width', width); $('timeline-map').setAttribute('height', height); $('timeline-map').setAttribute('viewBox', `0 0 ${width} ${height}`);
    $('timeline-axis').append(svg('line', { x1: 40, y1: 58, x2: width - 35, y2: 58, class: 'timeline-baseline' }));
    if (minimum !== null) {
      const interval = Math.max(1, Math.ceil(75 / pitch)); const ticks = new Set([minimum, maximum]);
      for (let year = minimum; year <= maximum; year += interval) ticks.add(year);
      for (const year of [...ticks].sort((a, b) => a - b)) {
        const x = 120 + (year - minimum) * pitch;
        $('timeline-axis').append(svg('line', { x1: x, x2: x, y1: 58, y2: height - 20, class: 'timeline-grid' }), svg('text', { x, y: 36, class: 'timeline-year' }, String(year)));
      }
    }
    if (nodes.some((paper) => yearOf(paper) === null)) $('timeline-axis').append(svg('text', { x: unknownX, y: 36, class: 'timeline-year' }, '연도 미상'));
    const byId = new Map(nodes.map((paper) => [paper.id, paper]));
    for (const edge of edges) {
      const a = positions.get(asText(edge.source)), b = positions.get(asText(edge.target)); let start, end, c1, c2;
      if (a.x === b.x) { start = a.x + 72; end = b.x + 72; c1 = start + 85; c2 = end + 85; }
      else { const direction = Math.sign(b.x - a.x); start = a.x + direction * 72; end = b.x - direction * 78; const curve = Math.max(45, Math.abs(end - start) * .45); c1 = start + direction * curve; c2 = end - direction * curve; }
      const path = svg('path', { d: `M ${start} ${a.y} C ${c1} ${a.y} ${c2} ${b.y} ${end} ${b.y}`, class: 'timeline-edge', 'marker-end': 'url(#citation-arrow)', 'data-source': edge.source, 'data-target': edge.target });
      path.append(svg('title', {}, `${titleOf(byId.get(asText(edge.source)))} → ${titleOf(byId.get(asText(edge.target)))} (직접 인용)${edge.reference_source ? ' · '+evidenceText({source:edge.reference_source,page:edge.reference_page,number:edge.reference_number}) : ''}`)); $('timeline-edges').append(path); state.timelineEdges.push({ source: asText(edge.source), target: asText(edge.target), el: path });
    }
    for (const paper of nodes) {
      const position = positions.get(paper.id); const group = svg('g', { transform: `translate(${position.x},${position.y})`, class: `timeline-node${paper.is_lineage_root ? ' seed' : ''}`, tabindex: '0', role: 'button', 'aria-label': `${titleOf(paper)}, ${asText(paper.year, '연도 미상')}, ${paper.lineage_level}단계`, 'aria-pressed': 'false', 'data-node-id': paper.id, 'data-year': yearOf(paper) ?? 'unknown', 'data-level': paper.lineage_level });
      group.append(svg('title', {}, titleOf(paper)), svg('rect', { x: -72, y: -29, width: 144, height: 58, rx: 7 }));
      const title = titleOf(paper); group.append(svg('text', { x: -60, y: -9, class: 'timeline-title' }, title.slice(0, 20)), svg('text', { x: -60, y: 5, class: 'timeline-title' }, title.length > 20 ? title.slice(20, 38) + (title.length > 38 ? '…' : '') : ''), svg('text', { x: -60, y: 20, class: 'timeline-meta' }, `${paper.is_lineage_root ? '계층 기준' : paper.lineage_level + '단계'} · ${asText(paper.year, '미상')} · 인용 ${countText(paper.citation_count)}`));
      group.addEventListener('click', () => selectPaper(paper.id)); group.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectPaper(paper.id); } });
      $('timeline-nodes').append(group); state.timelineEls.set(paper.id, group);
    }
    $('timeline-status').textContent = `${state.lineageConfig.depth}단계까지 ${nodes.length}편 · 직접 인용 ${edges.length}개 · 가로·세로로 스크롤해 탐색하세요.${edges.length ? '' : ' 현재 표시 논문 사이에 직접 인용 기록이 없습니다.'} 연도 미상은 별도 열에 표시하며, 필터로 숨긴 논문의 연결은 생략합니다.`;
  }
  function renderWorks() {
    state.workEls = [];
    for (const [name, papers] of [['prior', state.lineage?.prior || []], ['derivative', state.lineage?.derivative || []]]) {
      const list = $(name + '-list'); list.replaceChildren(); $(name + '-count').textContent = String(papers.length);
      if (!papers.length) { statusMessage(list, '표시할 연구가 없습니다', '이번에 수집한 인용 데이터 범위에서 연구를 찾지 못했습니다.'); continue; }
      for (const paper of papers) {
        const row = el('button', 'work-result'); row.type = 'button'; row.setAttribute('aria-pressed', 'false');
        row.append(el('span', 'work-meta', `${asText(paper.year, '연도 미상')} · ${inLibrary(paper) ? '저장소 보유' : '외부 논문'} · 기준에서 ${name === 'prior' ? paper.prior_level : paper.derivative_level}단계`), el('strong', '', titleOf(paper)), el('span', 'work-authors', authorsOf(paper) || '저자 정보 없음'), citationBadge(paper));
        row.addEventListener('click', () => selectPaper(idOf(paper), paper)); list.append(row); state.workEls.push({ id: idOf(paper), el: row });
      }
    }
  }
  function layout(nodes, edges) {
    const positions = new Map(); const others = nodes.filter((node) => !node.is_seed); const maxScore = Math.max(.01, ...others.map((node) => score(node.similarity)));
    nodes.forEach((node) => { const index = others.indexOf(node); const angle = index * 2.3999632297 - .7; const radius = node.is_seed ? 0 : 120 + (1 - score(node.similarity) / maxScore) * 145 + (index % 3) * 18; positions.set(node.id, { x: 450 + Math.cos(angle) * radius, y: 336 + Math.sin(angle) * radius, tx: 450 + Math.cos(angle) * radius, ty: 336 + Math.sin(angle) * radius }); });
    // A short deterministic relaxation separates labels while preserving the seed's vicinity.
    for (let pass = 0; pass < 110; pass++) {
      const shifts = new Map(nodes.map((node) => [node.id, { x: 0, y: 0 }]));
      for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        const a = positions.get(nodes[i].id), b = positions.get(nodes[j].id); const dx = a.x - b.x || .01, dy = a.y - b.y || .01; const distance = Math.max(1, Math.hypot(dx, dy));
        const minimum = nodes[i].is_seed || nodes[j].is_seed ? 105 : 86; if (distance >= minimum) continue;
        const push = (minimum - distance) * .14; const sa = shifts.get(nodes[i].id), sb = shifts.get(nodes[j].id); sa.x += dx / distance * push; sa.y += dy / distance * push; sb.x -= dx / distance * push; sb.y -= dy / distance * push;
      }
      for (const edge of edges) { const a = positions.get(asText(edge.source)), b = positions.get(asText(edge.target)); if (!a || !b) continue; const dx = b.x - a.x, dy = b.y - a.y, distance = Math.hypot(dx, dy); if (distance < 160) continue; const force = .0007 * score(edge.weight); const sa = shifts.get(asText(edge.source)), sb = shifts.get(asText(edge.target)); sa.x += dx * force; sa.y += dy * force; sb.x -= dx * force; sb.y -= dy * force; }
      nodes.forEach((node) => { if (node.is_seed) return; const p = positions.get(node.id), s = shifts.get(node.id); p.x = Math.max(90, Math.min(810, p.x + s.x + (p.tx - p.x) * .009)); p.y = Math.max(100, Math.min(568, p.y + s.y + (p.ty - p.y) * .009)); });
    }
    return positions;
  }
  function shortTitle(title) { return title.length > 30 ? title.slice(0, 28).trimEnd() + '…' : title; }
  function yearColor(paper, minimum, maximum) { const year = yearOf(paper); if (year === null) return '#9da6ac'; const portion = maximum === minimum ? .5 : (year - minimum) / (maximum - minimum); return `hsl(${Math.round(170 + portion * 70)} 48% ${Math.round(73 - portion * 18)}%)`; }
  function renderLegend(years) {
    const legend = $('map-legend'); legend.replaceChildren(); legend.hidden = false;
    const seed = el('span', ''); seed.append(el('i', isCitation() ? 'legend-seed-ring' : 'legend-seed'), el('span', '', '기준 논문')); legend.append(seed);
    if (isCitation()) {
      if (years.length) { const year = el('span', 'year-legend'); if (Math.min(...years) === Math.max(...years)) year.append(el('span', '', `발행 연도 ${years[0]}`)); else year.append(el('span', '', String(Math.min(...years))), el('i', 'year-gradient'), el('span', '', String(Math.max(...years)))); legend.append(year); }
      legend.append(el('span', '', '회색 = 연도 미상'), el('span', '', '크기 = 인용 수 · 점 테두리 점선 = 수 미상'), el('span', 'legend-line', '실선 = 인용 유사성'), el('span', 'legend-line legend-direct', '점선 = 직접 인용'));
    } else legend.append(el('span', 'legend-line', '선 = 텍스트 유사성'));
    legend.append(el('span', '', '점 옆 숫자 = 인용 수 · ? = 미확인'));
  }
  function drawGraph() {
    $('map-edges').replaceChildren(); $('map-nodes').replaceChildren(); $('node-list').replaceChildren(); state.nodeEls.clear(); state.listEls.clear(); state.edgeEls = []; state.positions = layout(state.visibleNodes, state.visibleEdges);
    const years = state.visibleNodes.map(yearOf).filter((year) => year !== null); const minimum = Math.min(...years), maximum = Math.max(...years); const maxCitations = Math.max(1, ...state.visibleNodes.map((paper) => knownCount(paper.citation_count) ? Number(paper.citation_count) : 0));
    $('map-stage').classList.toggle('citation-map', isCitation()); renderLegend(years);
    for (const edge of state.visibleEdges) {
      const source = asText(edge.source), target = asText(edge.target); const a = state.positions.get(source), b = state.positions.get(target); if (!a || !b) continue;
      const line = svg('line', { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: `map-edge${edge.direct_citation ? ' direct-citation' : ''}`, 'stroke-width': .5 + score(edge.weight) * 2.4 }); if (isCitation()) line.append(svg('title', {}, relationText(edge))); $('map-edges').append(line); state.edgeEls.push({ source, target, el: line });
    }
    for (const paper of state.visibleNodes) {
      const position = state.positions.get(paper.id), radius = isCitation() ? knownCount(paper.citation_count) ? 6 + Math.sqrt(Math.log1p(Number(paper.citation_count)) / Math.log1p(maxCitations)) * 14 : 6 : paper.is_seed ? 15 : 5.5 + score(paper.similarity) * 9;
      const group = svg('g', { class: `map-node${paper.is_seed ? ' seed' : ''}`, transform: `translate(${position.x},${position.y})`, tabindex: '0', role: 'button', 'aria-label': `${paper.is_seed ? '기준 논문: ' : ''}${titleOf(paper)}, ${asText(paper.year, '연도 미상')}, ${citationSummary(paper)}`, 'aria-pressed': 'false', 'data-node-id': paper.id });
      const dot = svg('circle', { r: radius, class: `node-dot${isCitation() && !knownCount(paper.citation_count) ? ' unknown-citations' : ''}` }); if (isCitation()) dot.style.fill = yearColor(paper, minimum, maximum);
      group.append(svg('title', {}, `${titleOf(paper)} · ${citationSummary(paper)} · ${citationMeta(paper)}`), svg('circle', { r: radius + 8, class: 'node-halo' }), dot);
      group.append(svg('text', { x: radius + 3, y: -radius + 1, class: 'node-citation-count' }, knownCount(paper.citation_count) ? Number(paper.citation_count).toLocaleString('ko-KR', { notation: 'compact', maximumFractionDigits: 1 }) : '?'));
      if (paper.is_seed && isCitation()) group.append(svg('circle', { r: radius + 4, class: 'seed-ring' }));
      group.append(svg('text', { y: radius + 20, class: 'node-label' }, shortTitle(titleOf(paper))), svg('text', { y: radius + 33, class: 'node-label node-year' }, `${asText(paper.year, '연도 미상')}${paper.is_seed ? ' · 기준 논문' : ''}`));
      group.addEventListener('click', () => { if (!state.suppressClick) selectPaper(paper.id); });
      group.addEventListener('keydown', (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectPaper(paper.id); } });
      $('map-nodes').append(group); state.nodeEls.set(paper.id, group);
      const row = el('button', ''); row.type = 'button'; row.setAttribute('aria-pressed', 'false'); row.append(el('span', '', titleOf(paper)), el('small', '', `${paper.is_seed ? '기준 · ' : ''}${citationSummary(paper)}`)); row.addEventListener('click', () => selectPaper(paper.id)); $('node-list').append(row); state.listEls.set(paper.id, row);
    }
    $('node-list-section').hidden = false; $('node-list-count').textContent = `${state.visibleNodes.length}편`; resetView();
  }
  function doiURL(doi) { const value = asText(doi).trim().replace(/^https?:\/\/(?:dx\.)?doi\.org\//i, ''); return /^10\.\d{4,9}\/\S+$/i.test(value) ? `https://doi.org/${encodeURIComponent(value).replace(/%2F/gi, '/')}` : null; }
  function evidenceText(detail) { const labels={pdf:'보유 PDF',crossref:'Crossref',ieee:'IEEE',openalex:'OpenAlex'}; return (labels[detail.source] || '출처 미확인') + (detail.page ? ` ${detail.page}쪽` : '') + (detail.number ? ` [${detail.number}]` : ''); }
  function relationText(relation) { if (!relation) return '기준 논문과의 상세 연결 근거가 제공되지 않았습니다.'; return `공통 참고문헌 ${countText(relation.shared_references)}편 · 수집 범위 내 동시 인용 ${countText(relation.co_citations)}편${relation.direct_citation ? ' · 직접 인용 관계 있음' : ''}` + (Array.isArray(relation.direct_citation_evidence) ? ' · 근거: '+relation.direct_citation_evidence.map(evidenceText).join(', ') : ''); }
  function selectPaper(id, listedPaper) {
    const paper = state.graph?.nodes.find((node) => node.id === id) || state.graph?.lineage_nodes?.find((node) => node.id === id) || (listedPaper ? { ...listedPaper, id, is_seed: id === state.seed } : null); if (!paper) return; state.selected = paper;
    $('lineage-use-selected').disabled = !isCitation() || paper.id === state.lineageRoot;
    state.nodeEls.forEach((node, key) => { node.classList.toggle('selected', key === id); node.setAttribute('aria-pressed', String(key === id)); }); state.listEls.forEach((node, key) => node.setAttribute('aria-pressed', String(key === id)));
    state.edgeEls.forEach((edge) => edge.el.classList.toggle('selected', edge.source === id || edge.target === id));
    state.workEls.forEach((row) => row.el.setAttribute('aria-pressed', String(row.id === id)));
    for (const collection of [state.timelineEls, state.paperEls]) collection.forEach((node, key) => { node.classList.toggle('selected', key === id); node.setAttribute('aria-pressed', String(key === id)); });
    state.tableEls.forEach((node, key) => node.classList.toggle('selected', key === id));
    state.timelineEdges.forEach((edge) => edge.el.classList.toggle('selected', edge.source === id || edge.target === id));
    $('details-empty').hidden = true; $('paper-details').hidden = false; $('detail-title').textContent = titleOf(paper); $('detail-authors').textContent = authorsOf(paper) || '저자 정보 없음'; $('detail-year').textContent = asText(paper.year, '정보 없음'); $('detail-source').textContent = sourceName(paper); $('detail-id').textContent = paper.article_number || paper.id; $('detail-basis').textContent = isCitation() ? '공개 인용 데이터 · OpenAlex' : paper.abstract_available ? '제목 + 저장된 초록' : '제목만 사용';
    $('detail-tags').replaceChildren(); if (paper.is_seed) $('detail-tags').append(el('span', 'seed-tag', '기준 논문')); $('detail-tags').append(el('span', inLibrary(paper) ? '' : 'external-tag', inLibrary(paper) ? '저장소 보유' : '외부 논문')); if (paper.is_favorite) $('detail-tags').append(el('span', '', '★ 즐겨찾기')); if (paper.pdf_available && inLibrary(paper)) $('detail-tags').append(el('span', '', 'PDF 보유'));
    $('citation-count-row').hidden = false; $('detail-citations').textContent = countText(paper.citation_count); $('detail-citation-meta').textContent = citationMeta(paper);
    if (isCitation()) $('detail-basis').textContent = '인용 관계 · '+(state.graph.meta?.source_labels || ['OpenAlex']).map(value=>asText(value)).join(' / ');
    $('similarity-label').textContent = isCitation() ? '시작 논문과 인용 유사도' : '시작 논문과 텍스트 유사도';
    $('similarity-section').hidden = paper.is_seed || paper.similarity == null; $('detail-score').textContent = scoreText(paper.similarity); $('detail-score-fill').style.width = `${score(paper.similarity) * 100}%`; $('terms-section').hidden = paper.is_seed || isCitation(); $('detail-terms').replaceChildren();
    $('relation-section').hidden = !isCitation() || paper.is_seed; $('detail-relation').textContent = relationText(paper.related_by);
    $('detail-abstract').textContent = asText(paper.abstract).trim() || '이 논문의 초록 데이터가 제공되지 않았습니다.'; $('abstract-section').hidden = !isCitation() && !paper.abstract;
    const terms = Array.isArray(paper.shared_terms) ? paper.shared_terms.slice(0, 10) : []; if (terms.length) terms.forEach((term) => $('detail-terms').append(el('span', '', asText(term)))); else $('detail-terms').append(el('span', 'no-terms', '표시할 공통 키워드가 없습니다.'));
    const target = targetOf(paper); $('recenter-paper').disabled = paper.is_seed || !target || $('map-stage').getAttribute('aria-busy') === 'true'; $('recenter-paper').textContent = paper.is_seed ? '현재 지도의 기준 논문' : !target ? 'DOI가 없어 다시 탐색할 수 없습니다' : '이 논문으로 다시 탐색 ↗';
    const hasPdf = paper.pdf_available && inLibrary(paper) && paper.article_number; $('detail-pdf').hidden = !hasPdf; if (hasPdf) $('detail-pdf').href = `/pdf/${encodeURIComponent(paper.article_number)}`; else $('detail-pdf').removeAttribute('href');
    const doi = doiURL(paper.doi); $('detail-doi').hidden = !doi; if (doi) $('detail-doi').href = doi; else $('detail-doi').removeAttribute('href');
  }
  function applyTransform() { const t = state.transform; $('map-viewport').setAttribute('transform', `translate(${t.x} ${t.y}) scale(${t.k})`); }
  function resetView() { state.transform = { x: 0, y: 0, k: 1 }; applyTransform(); }
  function mapPoint(event) { const matrix = $('research-map').getScreenCTM(); if (!matrix) return { x: 450, y: 350 }; return new DOMPoint(event.clientX, event.clientY).matrixTransform(matrix.inverse()); }
  function zoom(factor, point = { x: 450, y: 350 }) { const t = state.transform, k = Math.max(.5, Math.min(4, t.k * factor)); state.transform = { x: point.x - (point.x - t.x) * k / t.k, y: point.y - (point.y - t.y) * k / t.k, k }; applyTransform(); }
  $('search-form').addEventListener('submit', (event) => { event.preventDefault(); search(); });
  $('paper-query').addEventListener('input', () => { clearTimeout(state.searchTimer); state.searchAbort?.abort(); ++state.searchRequest; state.searchTimer = setTimeout(search, 280); });
  function changeMode(mode) {
    state.mode = mode === 'local' ? 'local' : 'citation'; updateMethod(); setView('map');
    const target = state.pendingTarget || state.target;
    if (state.mode === 'local' && target?.doi) {
      state.graphAbort?.abort(); ++state.graphRequest; state.graph = null; state.selected = null; state.seed = null; state.pendingTarget = null; state.target = null;
      $('map-nodes').replaceChildren(); $('map-edges').replaceChildren(); $('node-list-section').hidden = true; $('paper-details').hidden = true; $('details-empty').hidden = false; $('map-stage').setAttribute('aria-busy', 'false'); $('map-stage').classList.remove('loading'); $('map-legend').hidden = true;
      for (const id of ['graph-paper-list', 'table-papers', 'timeline-axis', 'timeline-edges', 'timeline-nodes']) $(id).replaceChildren();
      state.visibleNodes = []; state.visibleEdges = []; state.timelineEls.clear(); state.paperEls.clear(); state.tableEls.clear(); state.timelineEdges = []; $('paper-list-count').textContent = '0';
      setListMode('search'); updateMethod();
      $('graph-count').textContent = '0 papers'; for (const name of ['prior', 'derivative']) { $(name + '-list').replaceChildren(); $(name + '-count').textContent = '0'; }
      showOverlay('empty', '저장소 논문을 선택해 주세요', '제목·초록 지도는 SQL 저장소의 논문에서 시작합니다. 왼쪽 목록에서 논문을 선택하세요.'); $('map-status').textContent = '저장소 논문을 선택하면 텍스트 지도를 구성합니다.';
      const url = new URL(location.href); url.searchParams.delete('doi'); url.searchParams.delete('article_number'); url.searchParams.set('mode', 'local'); history.replaceState(null, '', url); return;
    }
    if (target) loadGraph(target);
  }
  $('graph-mode').addEventListener('change', () => changeMode($('graph-mode').value));
  $('use-local').addEventListener('click', () => changeMode('local'));
  $('use-local-empty').addEventListener('click', () => changeMode('local'));
  $('map-limit').addEventListener('change', () => { if (state.pendingTarget || state.target) loadGraph(state.pendingTarget || state.target); });
  $('map-retry').addEventListener('click', () => loadGraph(state.pendingTarget || state.target));
  $('recenter-paper').addEventListener('click', () => { if (state.selected && !state.selected.is_seed && !$('recenter-paper').disabled) loadGraph(targetOf(state.selected)); });
  for (const name of views) {
    $('tab-' + name).addEventListener('click', () => { if (!$('tab-' + name).disabled) setView(name, true); });
    $('tab-' + name).addEventListener('keydown', (event) => {
      const names = views.filter((item) => !$('tab-' + item).disabled); const index = names.indexOf(name);
      const next = event.key === 'ArrowRight' ? names[(index + 1) % names.length] : event.key === 'ArrowLeft' ? names[(index + names.length - 1) % names.length] : event.key === 'Home' ? names[0] : event.key === 'End' ? names[names.length - 1] : null;
      if (next) { event.preventDefault(); setView(next, true); $('tab-' + next).focus(); }
    });
  }
  $('zoom-in').addEventListener('click', () => zoom(1.25)); $('zoom-out').addEventListener('click', () => zoom(.8)); $('reset-view').addEventListener('click', resetView);
  $('toggle-labels').addEventListener('click', () => { state.labels = !state.labels; $('map-stage').classList.toggle('hide-labels', !state.labels); $('toggle-labels').setAttribute('aria-pressed', String(state.labels)); });
  $('research-map').addEventListener('wheel', (event) => { if (!state.graph) return; event.preventDefault(); zoom(Math.exp(-Math.max(-100, Math.min(100, event.deltaY)) * .003), mapPoint(event)); }, { passive: false });
  $('research-map').addEventListener('pointerdown', (event) => { if (!state.graph || event.button !== 0) return; const point = mapPoint(event); state.drag = { pointerId: event.pointerId, start: point, x: state.transform.x, y: state.transform.y, moved: false }; state.suppressClick = false; });
  $('research-map').addEventListener('pointermove', (event) => { if (!state.drag || state.drag.pointerId !== event.pointerId) return; const point = mapPoint(event), dx = point.x - state.drag.start.x, dy = point.y - state.drag.start.y; if (!state.drag.moved && Math.hypot(dx, dy) < 5) return; state.drag.moved = true; state.suppressClick = true; $('research-map').setPointerCapture(event.pointerId); $('research-map').classList.add('panning'); state.transform.x = state.drag.x + dx; state.transform.y = state.drag.y + dy; applyTransform(); });
  function endDrag(event) { if (!state.drag || event.pointerId !== state.drag.pointerId) return; state.drag = null; $('research-map').classList.remove('panning'); if ($('research-map').hasPointerCapture(event.pointerId)) $('research-map').releasePointerCapture(event.pointerId); setTimeout(() => { state.suppressClick = false; }, 0); }
  window.addEventListener('pointerup', endDrag); window.addEventListener('pointercancel', endDrag);
  $('research-map').addEventListener('keydown', (event) => { const shifts = { ArrowLeft: [35, 0], ArrowRight: [-35, 0], ArrowUp: [0, 35], ArrowDown: [0, -35] }; if (shifts[event.key]) { event.preventDefault(); state.transform.x += shifts[event.key][0]; state.transform.y += shifts[event.key][1]; applyTransform(); } else if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(1.25); } else if (event.key === '-') { event.preventDefault(); zoom(.8); } else if (event.key === '0') { event.preventDefault(); resetView(); } });
  function readLocation() {
    const params = new URLSearchParams(location.search); state.mode = params.get('mode') === 'local' ? 'local' : 'citation'; state.requestedView = views.includes(params.get('view')) ? params.get('view') : 'map'; updateMethod();
    state.lineageConfig = { depth: [1, 2, 3].includes(Number(params.get('depth'))) ? Number(params.get('depth')) : 1,
      direction: ['both', 'prior', 'derivative'].includes(params.get('direction')) ? params.get('direction') : 'both', limit: params.get('hierarchy_limit') === '60' ? 60 : 30 };
    state.requestedRoot = params.get('root');
    const article = params.get('article_number'), doi = params.get('doi'); const target = article ? { article_number: article } : doi && state.mode === 'citation' ? { doi } : null;
    if (target) loadGraph(target, false);
  }
  window.addEventListener('popstate', readLocation);
  $('list-mode-graph').addEventListener('click', () => setListMode('graph')); $('list-mode-search').addEventListener('click', () => setListMode('search'));
  $('paper-sort').addEventListener('change', renderFiltered);
  $('timeline-focus').addEventListener('click', () => state.timelineEls.get(state.lineageRoot)?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest', inline: 'center' }));
  for (const id of ['lineage-depth', 'lineage-direction', 'lineage-limit']) $(id).addEventListener('change', () => changeLineage());
  $('lineage-use-selected').addEventListener('click', () => { if (state.selected && !$('lineage-use-selected').disabled) changeLineage(state.selected.id); });
  $('lineage-reset-root').addEventListener('click', () => changeLineage(state.seed));
  $('toggle-filters').addEventListener('click', () => { const hidden = !$('filter-panel').hidden; $('filter-panel').hidden = hidden; $('toggle-filters').setAttribute('aria-expanded', String(!hidden)); });
  $('filter-panel').addEventListener('submit', (event) => { event.preventDefault(); readFilters(); });
  for (const id of ['filter-query', 'filter-year-from', 'filter-year-to', 'filter-citations']) $(id).addEventListener('input', readFilters);
  for (const id of ['filter-library', 'filter-pdf']) $(id).addEventListener('change', readFilters);
  $('reset-filters').addEventListener('click', () => { for (const id of ['filter-query', 'filter-year-from', 'filter-year-to', 'filter-citations']) $(id).value = ''; $('filter-library').value = 'all'; $('filter-pdf').checked = false; readFilters(); });
  search(); readLocation();
})();
