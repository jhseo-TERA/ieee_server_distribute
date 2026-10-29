(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const csrf = document.querySelector('meta[name="csrf-token"]').content;
  const CAN_WRITE = document.body.dataset.canWriteReviews === 'true';
  const labels = {pending:'미완료', reading:'미완료', completed:'리뷰 완료'};
  const editors = new Map();
  let dailyData = null, historyPage = 1, historyRequest = 0, pendingSaves = 0;
  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const setStatus = (node, message, error = false) => {
    node.textContent = message;
    node.classList.toggle('error', error);
  };
  const dirty = editor => editor.completed.checked !== (editor.item.status === 'completed');
  const hasUnsaved = () => pendingSaves > 0 || [...editors.values()].some(dirty);
  async function api(path, options = {}) {
    const response = await fetch(path, options);
    let data;
    try { data = await response.json(); } catch { throw new Error('응답을 확인할 수 없습니다. 잠시 후 다시 시도하세요.'); }
    if (!response.ok || !data.ok) {
      throw new Error(response.status === 401 ? '로그인이 만료되었습니다. 다시 로그인한 뒤 시도하세요.' :
        data.error || '요청을 처리할 수 없습니다. 잠시 후 다시 시도하세요.');
    }
    return data;
  }
  function addLink(container, title, href) {
    const a = element('a', '', title);
    a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; container.append(a);
  }
  function summary() {
    if (!dailyData) return;
    $('review-date').textContent = dailyData.date + ' · 오늘의 읽기';
    const done = dailyData.items.filter(item => item.status === 'completed').length;
    $('today-progress').replaceChildren(document.createTextNode(String(done)),
      element('small', '', ` / ${dailyData.items.length}편 완료`));
    $('remaining-count').textContent = `미완료 즐겨찾기 ${dailyData.remaining.toLocaleString()}편`;
  }
  function renderEditor(item, label) {
    const card = $('paper-template').content.firstElementChild.cloneNode(true);
    card.querySelector('.paper-label').textContent = label;
    const badge = card.querySelector('.status-badge');
    const updateBadge = () => { badge.textContent = labels[item.status]; badge.className = 'status-badge ' + item.status; };
    updateBadge();
    card.querySelector('.paper-title').textContent = item.title;
    card.querySelector('.paper-meta').textContent = [item.source_name, item.year].filter(Boolean).join(' · ');
    card.querySelector('.paper-authors').textContent = item.authors || '저자 정보 없음';
    const links = card.querySelector('.paper-links');
    if (item.pdf_available) addLink(links, 'PDF 읽기 ↗', '/pdf/' + encodeURIComponent(item.article_number));
    else links.append(element('span', '', '저장된 PDF 없음'));
    try {
      const url = new URL(item.url);
      if (['http:', 'https:'].includes(url.protocol)) addLink(links, '출판사 원문 ↗', url.href);
    } catch { /* Missing or malformed source URL. */ }
    addLink(links, 'Connection 지도 ↗', '/connection?article_number=' + encodeURIComponent(item.article_number));
    const form = card.querySelector('form'), completed = form.elements.completed;
    const button = form.querySelector('button[type="submit"]'), status = form.querySelector('.save-status');
    completed.checked = item.status === 'completed'; completed.disabled = !CAN_WRITE;
    button.hidden = !CAN_WRITE; button.disabled = !CAN_WRITE;
    const editor = {item, card, completed};
    editors.set(item.article_number, editor);
    const savedLabel = () => !CAN_WRITE ? '완료 체크는 admin 계정만 가능합니다.' : item.version ? '상태 저장됨 · ' + new Date(item.updated_at).toLocaleString('ko-KR', {timeZone:'Asia/Seoul'}) : '완료 전까지 추천 후보에 남습니다.';
    const prepareNote = form.querySelector('.prepare-note'), noteStatus = form.querySelector('.note-status');
    const openNote = form.querySelector('.open-note');
    const showNote = url => {
      openNote.href = url; openNote.hidden = false; prepareNote.hidden = true;
      setStatus(noteStatus, '리뷰는 Obsidian에 저장됩니다. 웹에서 완료 상태를 바꿔도 노트는 유지됩니다.');
    };
    if (prepareNote) {
      if(item.obsidian_url) showNote(item.obsidian_url);
      prepareNote.addEventListener('click', async () => {
        if (!CAN_WRITE || prepareNote.disabled) return;
        prepareNote.disabled = true; pendingSaves++; setStatus(noteStatus, '리뷰 노트를 준비하는 중…');
        try {
          const result = await api('/api/daily-review/obsidian-note', {method:'POST',
            headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
            body:JSON.stringify({article_number:item.article_number})});
          showNote(result.obsidian_sync.url);
          window.location.href = result.obsidian_sync.url;
        } catch (error) { setStatus(noteStatus, error.message, true); }
        finally { prepareNote.disabled = false; pendingSaves--; }
      });
    }
    if(item.notes){ card.querySelector('.legacy-review').hidden=false; card.querySelector('.legacy-notes').textContent=item.notes; }
    const changed = () => setStatus(status, dirty(editor) ? '완료 상태를 저장해 주세요.' : savedLabel());
    changed();
    completed.addEventListener('change', changed);
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (!CAN_WRITE || button.disabled) return;
      button.disabled = completed.disabled = true;
      pendingSaves++;
      setStatus(status, '저장 중…');
      try {
        const result = await api('/api/daily-review/review', {method:'PUT',
          headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},
          body:JSON.stringify({article_number:item.article_number,
            status:completed.checked ? 'completed' : 'reading', version:item.version})});
        Object.assign(item, result.item);
        summary();
        updateBadge(); setStatus(status, savedLabel());
        loadHistory();
        refreshSummary();
      } catch (error) { setStatus(status, error.message, true); }
      finally { pendingSaves--; button.disabled = completed.disabled = false; }
    });
    return card;
  }
  async function refreshSummary() {
    try {
      const current = await api('/api/daily-review');
      if (dailyData && current.date === dailyData.date) { dailyData.remaining = current.remaining; summary(); }
    } catch { /* Review save already succeeded. A later refresh updates the count. */ }
  }
  async function loadToday() {
    if (hasUnsaved()) {
      setStatus($('page-status'), '변경한 완료 상태를 먼저 저장하세요.', true);
      $('page-status').hidden = false; return;
    }
    $('retry-today').hidden = true;
    $('page-status').hidden = false;
    setStatus($('page-status'), '추천을 불러오는 중…');
    try {
      const data = await api('/api/daily-review');
      editors.clear(); $('today-papers').replaceChildren();
      dailyData = data; summary();
      data.items.forEach((item, index) => $('today-papers').append(renderEditor(item, `TODAY'S PAPER ${String(index + 1).padStart(2, '0')}`)));
      $('page-status').hidden = data.items.length === 2;
      if (!data.items.length) setStatus($('page-status'), data.remaining ?
        '오늘 선택된 논문이 없습니다. 새로 추가한 즐겨찾기는 내일 추천에 반영됩니다.' :
        '미완료 즐겨찾기가 없습니다. 논문을 즐겨찾기에 추가하거나 지난 리뷰의 완료 체크를 해제해 저장하면 다음 날 추천 후보가 됩니다.');
      else if (data.items.length < 2) setStatus($('page-status'), '오늘 선택할 수 있는 논문이 1편입니다. 미완료 논문은 다음 날에도 다시 추천될 수 있습니다.');
      $('rollover').hidden = true;
      loadHistory();
    } catch (error) {
      setStatus($('page-status'), error.message, true); $('retry-today').hidden = false;
    }
  }
  function openReview(item) {
    const existing = editors.get(item.article_number);
    if (existing) {
      existing.card.scrollIntoView({behavior:'smooth', block:'center'}); existing.completed.focus(); return;
    }
    $('dialog-paper').replaceChildren(renderEditor(item, 'PREVIOUSLY RECOMMENDED'));
    $('review-dialog').showModal();
  }
  function closeDialog() {
    const active = [...editors.values()].find(editor => $('dialog-paper').contains(editor.card));
    if (pendingSaves) {
      if (active) setStatus(active.card.querySelector('.save-status'), '처리가 끝난 뒤 닫을 수 있습니다.');
      return;
    }
    if (active && dirty(active) && !window.confirm('저장하지 않은 완료 상태 변경을 버리고 닫을까요?')) return;
    if (active) editors.delete(active.item.article_number);
    $('review-dialog').close();
  }
  async function loadHistory() {
    const requestId = ++historyRequest;
    $('history-status').textContent = '기록을 불러오는 중…';
    try {
      const data = await api('/api/daily-review/history?page=' + historyPage);
      if (requestId !== historyRequest) return;
      $('history-list').replaceChildren();
      for (const item of data.items) {
        const row = element('article', 'history-item'), info = element('div', 'history-info');
        info.append(element('div', 'history-meta', `${item.last_recommended} 추천 · ${labels[item.status]} · ${item.source_name || ''}`), element('h3', '', item.title));
        const button = element('button', 'button secondary', '논문 열기'); button.type = 'button';
        button.addEventListener('click', () => openReview(item)); row.append(info, button); $('history-list').append(row);
      }
      $('history-status').textContent = data.total ? `추천받은 논문 ${data.total.toLocaleString()}편 · 같은 논문에 쓴 리뷰는 하나로 이어집니다.` : '첫 추천을 받으면 이곳에 읽기 기록이 쌓입니다.';
      $('history-page').textContent = `${historyPage} / ${Math.max(1, Math.ceil(data.total / data.page_size))}`;
      $('history-prev').disabled = historyPage <= 1;
      $('history-next').disabled = historyPage * data.page_size >= data.total;
    } catch (error) { if (requestId === historyRequest) $('history-status').textContent = error.message; }
  }
  $('history-prev').addEventListener('click', () => { historyPage--; loadHistory(); });
  $('history-next').addEventListener('click', () => { historyPage++; loadHistory(); });
  $('refresh-history').addEventListener('click', loadHistory);
  $('retry-today').addEventListener('click', loadToday);
  $('reload-day').addEventListener('click', loadToday);
  $('close-dialog').addEventListener('click', closeDialog);
  $('review-dialog').addEventListener('cancel', event => { event.preventDefault(); closeDialog(); });
  window.addEventListener('beforeunload', event => { if (hasUnsaved()) { event.preventDefault(); event.returnValue = ''; } });
  const checkDate = () => {
    if (!dailyData) return;
    const currentDate = new Intl.DateTimeFormat('sv-SE', {timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());
    if (currentDate !== dailyData.date) $('rollover').hidden = false;
  };
  document.addEventListener('visibilitychange', () => { if (!document.hidden) checkDate(); });
  setInterval(checkDate, 60000);
  loadToday();
})();
