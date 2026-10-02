/* Research Desk: all model and PDF text is rendered as text, never HTML. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const CAN_EDIT = document.body.dataset.canEdit === "true";
  const CSRF = document.querySelector('meta[name="csrf-token"]').content;
  const DEFAULT_TEXT = "gpt-oss:20b";
  const DEFAULT_VISION = "qwen3.6:35b-a3b-mtp-q4_K_M";
  const state = {kind: "ask", papers: [], selected: new Map(), models: [], runtimeReady: false,
    currentJob: null, polling: null, submitting: false, searchVersion: 0, answer: "", toast: null,
    proposals: [], selectedProposals: new Set()};
  const descriptions = {
    ask: "논문을 선택해 질문하거나 2–5편을 비교하세요. 논문 미선택 시 검색 범위와 질문을 바탕으로 관련 자료를 찾습니다.",
    review: "논문의 현재 성능값과 원문 근거를 함께 검토합니다. 전력 포함 범위, 동작점, 단위와 비교 가능성을 확인하세요.",
    extract: "논문 1편의 지정한 PDF 페이지에서 성능값과 측정 조건을 추출합니다. 근거 문장과 함께 저장된 제안을 검토한 뒤 승인하세요."
  };
  const presets = {
    ask: [
      ["선택 논문 비교", "선택한 논문들의 RX 구조, 데이터 속도, 에너지 효율을 비교해줘. 전력 포함 범위와 서로 다른 측정 조건을 구분하고 근거를 인용해줘."],
      ["핵심 기여", "이 논문의 핵심 기여, 기존 연구와의 차이, 검증된 성능과 한계를 원문 근거를 들어 설명해줘."],
      ["비교 조건 확인", "이 논문들의 lane rate와 aggregate rate, RX/TX/TRX 전력 범위, 채널 손실과 BER 조건을 구분해서 정리해줘."]
    ],
    review: [
      ["에너지 범위 검토", "현재 서베이에 기록된 에너지 효율이 RX, TX, TRX 중 어떤 범위인지 검토하고 DSP와 PLL 포함 여부를 근거와 함께 확인해줘."],
      ["동작점·수치 검토", "서베이 성능값의 단위와 동작점을 검토해줘. 최고 속도와 최저 전력이 같은 조건에서 측정되었는지 확인하고 불확실한 항목을 표시해줘."],
      ["분류 검토", "이 논문의 SerDes 관련성, 링크 매체와 회로 분류가 현재 서베이와 일치하는지 확인하고 검토가 필요한 이유를 설명해줘."]
    ],
    extract: [
      ["성능 수치와 조건", "데이터 속도, 전력, 에너지 효율, 공정, 채널 손실 및 BER을 추출해줘. lane/aggregate와 TX/RX/TRX 범위, 동작점, 원문 근거를 함께 기록해줘."],
      ["표·각주 함께 읽기", "성능 비교표와 각주를 함께 읽고 이 논문 자체의 측정값만 추출해줘. 다른 논문의 값은 제외하고 전력에서 제외한 회로와 동작점을 명시해줘."]
    ]
  };
  const statusLabels = {queued: "대기 중", pending: "대기 중", running: "실행 중", processing: "실행 중",
    completed: "완료", complete: "완료", done: "완료", succeeded: "완료", failed: "실패", error: "실패", cancelled: "취소됨", canceled: "취소됨"};
  const fieldLabels = {data_rate_gbps: "데이터 속도", reported_rate_gbps: "보고 전송률", lane_rate_gbps: "Lane rate", aggregate_rate_gbps: "Aggregate rate",
    energy_pj_bit: "에너지 효율", power_mw: "전력", process_nm: "공정", channel_loss_db: "채널 손실", loss_db: "채널 손실",
    loss_frequency_ghz: "손실 측정 주파수", ber: "BER", modulation: "변조", link_medium: "링크 매체", component_scope: "전력 범위"};

  function node(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined && value !== null) element.textContent = String(value);
    return element;
  }
  function plain(value) {
    if (value == null) return "";
    if (typeof value === "string") return value;
    if (Array.isArray(value)) return value.map(plain).filter(Boolean).join("\n");
    if (typeof value === "object") return Object.entries(value).map(([key, item]) => `${key}: ${plain(item)}`).join("\n");
    return String(value);
  }
  const articleKey = (paper) => String(paper.article_number || paper.articleNumber || paper.paper_id || "");
  const paperTitle = (paper) => paper.title || paper.paper_title || paper.canonical_title || articleKey(paper) || "제목 미확인";
  const activeStatus = (status) => ["queued", "pending", "running", "processing"].includes(status);
  const completeStatus = (status) => ["complete", "completed", "done", "succeeded"].includes(status);
  const hasPdf = (paper) => Boolean(Number(paper.pdf_available || paper.has_pdf || 0));
  function toast(message) {
    clearTimeout(state.toast); $("toast").textContent = message; $("toast").hidden = false;
    state.toast = setTimeout(() => { $("toast").hidden = true; }, 5000);
  }
  function notice(message) { $("notice").textContent = message || ""; $("notice").hidden = !message; }
  function errorText(error) { return error && error.message ? error.message : "요청을 처리하지 못했습니다."; }
  async function api(path, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(`/api/ai${path}`, {...options, credentials: "same-origin", signal: controller.signal,
        headers: {"Accept": "application/json", ...(options.body ? {"Content-Type": "application/json", "X-CSRF-Token": CSRF} : {}), ...options.headers}});
      if (response.status === 401) throw new Error("로그인이 만료되었습니다. 페이지를 새로고침해 다시 로그인하세요.");
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        throw new Error(plain(payload.error || payload.message) || `요청 실패 (${response.status})`);
      }
      return payload;
    } catch (error) {
      if (error.name === "AbortError") throw new Error("서버 응답 시간이 초과되었습니다. 최근 작업에서 접수 여부를 확인한 후 다시 시도하세요.");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  const post = (path, payload = {}) => api(path, {method: "POST", body: JSON.stringify(payload)});
  function safeUrl(value) {
    try { const url = new URL(value, location.origin); return ["http:", "https:"].includes(url.protocol) ? url.href : null; }
    catch (_) { return null; }
  }
  function sourceLink(label, value) {
    if (!value) return null;
    const href = safeUrl(value); if (!href) return null;
    const link = node("a", "", label); link.href = href; link.target = "_blank"; link.rel = "noopener noreferrer";
    return link;
  }
  function pdfUrl(article, page) {
    const base = `/pdf/${encodeURIComponent(article)}`;
    return base + (Number.isInteger(Number(page)) && Number(page) > 0 ? `#page=${Number(page)}` : "");
  }
  function readFilters() {
    const values = Object.fromEntries(new FormData($("search-form")).entries());
    Object.keys(values).forEach((key) => { values[key] = values[key].trim(); if (!values[key]) delete values[key]; });
    return values;
  }
  function selectedSummary() {
    const count = state.selected.size;
    $("selection-summary").textContent = count ? `${count}편 선택${state.kind === "extract" ? " · 추출은 1편씩" : count > 1 ? " · 논문 간 비교 가능" : ""}` : (state.kind === "ask" ? "논문 미선택 · 자동 검색" : "왼쪽에서 논문을 선택하세요");
    $("clear-selection").disabled = !count;
  }
  function renderSelected() {
    const basket = $("selected-papers"); basket.replaceChildren();
    state.selected.forEach((paper, key) => {
      const chip = node("div", "selected-chip"); const title = node("span", "", paperTitle(paper)); title.title = paperTitle(paper);
      const remove = node("button", "", "×"); remove.type = "button"; remove.setAttribute("aria-label", `${paperTitle(paper)} 선택 해제`);
      remove.addEventListener("click", () => { state.selected.delete(key); renderSelected(); renderPapers(); });
      chip.append(title, remove); basket.append(chip);
    }); selectedSummary();
  }
  function togglePaper(paper, checked) {
    const key = articleKey(paper); if (!key) return;
    if (checked && !state.selected.has(key) && state.selected.size >= 5) { toast("논문은 최대 5편까지 선택할 수 있습니다."); renderPapers(); return; }
    if (checked) state.selected.set(key, paper); else state.selected.delete(key);
    renderSelected(); renderPapers();
  }
  function renderPapers() {
    const list = $("search-results"); list.replaceChildren();
    if (!state.papers.length) { list.append(node("p", "list-empty", "검색 결과가 없습니다. 키워드 또는 수치 조건을 넓혀보세요.")); return; }
    state.papers.forEach((paper, index) => {
      const key = articleKey(paper); const selected = state.selected.has(key);
      const row = node("article", `paper-card${selected ? " selected" : ""}`);
      const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = selected; checkbox.id = `paper-select-${index}`; checkbox.disabled = !key;
      checkbox.addEventListener("change", () => togglePaper(paper, checkbox.checked));
      const info = node("div", "paper-info"); const title = node("label", "paper-title", paperTitle(paper)); title.htmlFor = checkbox.id;
      const meta = node("div", "paper-meta"); meta.append(node("span", "", [paper.publication_year || paper.year, paper.source_name || paper.venue].filter(Boolean).join(" · ") || key));
      if (hasPdf(paper)) meta.append(node("span", "pdf-label", "PDF"));
      info.append(title, meta);
      const metrics = []; const point = (paper.matching_measurements || [])[0] || paper;
      const scope = paper.requested_rate_scope || "lane";
      const rate = point[`${scope}_rate_gbps`] != null ? point[`${scope}_rate_gbps`] : point.rate_scope === scope ? point.reported_rate_gbps : null;
      if (rate != null) metrics.push(`${rate} Gb/s ${scope}`);
      if (point.energy_pj_bit != null) metrics.push(`${point.energy_pj_bit} pJ/bit${point.energy_component_scope ? ` (${point.energy_component_scope.toUpperCase()})` : ""}`);
      if (point.process_nm != null) metrics.push(`${point.process_nm} nm`);
      if (metrics.length) info.append(node("div", "paper-metrics", metrics.join(" · ")));
      if (hasPdf(paper)) { const preview = node("button", "text-button paper-preview", "원문 열기 ↗"); preview.type = "button"; preview.addEventListener("click", () => openSource({...paper, page: 1, text: "", pdf_available: true})); info.append(preview); }
      row.append(checkbox, info); list.append(row);
    });
  }
  async function searchPapers(event) {
    if (event) event.preventDefault();
    const version = ++state.searchVersion;
    $("search-count").textContent = "검색 중…";
    try {
      const filters = readFilters();
      if (filters.year_from && filters.year_to && Number(filters.year_from) > Number(filters.year_to)) throw new Error("시작 연도는 마지막 연도보다 작거나 같아야 합니다.");
      const data = await api(`/search?${new URLSearchParams(filters)}`);
      if (version !== state.searchVersion) return;
      state.papers = Array.isArray(data.items) ? data.items : [];
      const count = data.total == null ? `${state.papers.length}편 표시` : `${Number(data.total).toLocaleString()}편 중 ${state.papers.length}편 표시`;
      $("search-count").textContent = count; renderPapers();
    } catch (error) {
      if (version !== state.searchVersion) return;
      $("search-count").textContent = "검색 오류"; $("search-results").replaceChildren(node("p", "list-empty", errorText(error)));
    }
  }
  function renderModels(preferred) {
    const selector = $("model-select"); const previous = preferred || selector.value;
    selector.replaceChildren();
    const models = state.models.length ? state.models : [{name: DEFAULT_TEXT, label: "gpt-oss 20B", available: false}, {name: DEFAULT_VISION, label: "Qwen3.6 35B-A3B", vision: true, available: false}];
    models.forEach((model) => {
      const name = model.name || model.model; const option = node("option", "", `${model.label || name}${model.vision ? " · 비전" : ""}${model.available === false ? " · 사용 불가" : ""}`);
      option.value = name; option.disabled = model.available === false; selector.append(option);
    });
    const preferredOption = Array.from(selector.options).find((option) => option.value === previous && !option.disabled);
    if (preferredOption) selector.value = preferredOption.value;
    else { const first = Array.from(selector.options).find((option) => !option.disabled && (state.kind !== "extract" || option.value.startsWith("qwen"))) || Array.from(selector.options).find((option) => !option.disabled); if (first) selector.value = first.value; }
    syncSubmit();
  }
  function syncSubmit() { $("submit-job").disabled = state.submitting || !state.runtimeReady || (state.kind === "extract" && !CAN_EDIT); }
  async function refreshStatus() {
    try {
      const status = await api("/status"); state.models = Array.isArray(status.models) ? status.models : [];
      state.runtimeReady = status.available !== false && status.ollama_available !== false && state.models.some((model) => model.available !== false);
      const queued = Number(status.queue_size || 0); const busy = Boolean(status.busy);
      $("runtime-dot").className = `status-dot ${state.runtimeReady ? (busy ? "busy" : "ready") : "error"}`;
      $("runtime-label").textContent = !state.runtimeReady ? "로컬 모델 연결 필요" : busy ? "로컬 모델 작업 중" : "로컬 모델 준비됨";
      $("runtime-detail").textContent = state.runtimeReady ? `Ollama · 한 번에 1개 실행${queued ? ` · 대기 ${queued}건` : " · 요청 대기 중"}` : "이 PC에서 Ollama 실행 및 모델 설치 상태를 확인하세요.";
      renderModels();
      if (!state.runtimeReady) notice(plain(status.error || status.message) || "Ollama에 연결할 수 없거나 사용 가능한 모델이 없습니다. AI Studio의 Ollama를 실행한 후 연결 상태를 새로고침하세요. 논문 검색은 계속 사용할 수 있습니다.");
      else notice("");
    } catch (error) { state.runtimeReady = false; $("runtime-dot").className = "status-dot error"; $("runtime-label").textContent = "연결 확인 실패"; $("runtime-detail").textContent = errorText(error); syncSubmit(); }
  }
  function setKind(kind, focus = false) {
    state.kind = kind;
    $("thinking").value = kind === "extract" ? "off" : "low";
    $("max-tokens").value = kind === "extract" ? "2048" : "1024";
    document.querySelectorAll(".tab").forEach((tab) => { const active = tab.dataset.kind === kind; tab.classList.toggle("active", active); tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1; if (active && focus) tab.focus(); });
    $("task-panel").setAttribute("aria-labelledby", `tab-${kind}`);
    $("task-description").textContent = descriptions[kind]; $("extract-options").hidden = kind !== "extract";
    $("viewer-note").hidden = kind !== "extract" || CAN_EDIT; $("review-queue-panel").hidden = kind !== "review";
    const area = $("prompt-presets"); area.replaceChildren();
    presets[kind].forEach(([label, question]) => { const button = node("button", "preset", label); button.type = "button"; button.addEventListener("click", () => { $("question").value = question; $("question").focus(); }); area.append(button); });
    $("submit-job").replaceChildren(document.createTextNode(kind === "ask" ? "근거 기반 답변 생성 " : kind === "review" ? "서베이 검토 시작 " : "PDF 추출 제안 생성 "), node("span", "", "↗"));
    renderModels(kind === "extract" ? DEFAULT_VISION : DEFAULT_TEXT); selectedSummary();
    if (kind === "review") refreshReview();
  }
  function readPages() {
    const raw = $("extract-pages").value.trim();
    if (!/^\d+(?:\s*[,\s]\s*\d+)*$/.test(raw)) throw new Error("페이지는 쉼표로 구분한 숫자로 입력하세요. 예: 1, 2, 3");
    const pages = Array.from(new Set(raw.split(/[,\s]+/).filter(Boolean).map(Number)));
    if (!pages.length || pages.length > 3 || pages.some((page) => !Number.isInteger(page) || page < 1 || page > 40)) throw new Error("1–40페이지 중 최대 3페이지를 선택하세요.");
    return pages;
  }
  async function submitJob(event) {
    event.preventDefault(); if (state.submitting) return;
    try {
      const question = $("question").value.trim(); const articles = Array.from(state.selected.keys());
      if (!question && state.kind !== "extract") throw new Error("질문 또는 검토 요청을 입력하세요.");
      if (state.kind === "review" && !articles.length) throw new Error("검토할 논문을 1편 이상 선택하세요.");
      if (state.kind === "extract" && articles.length !== 1) throw new Error("PDF 추출은 논문 1편을 선택해야 합니다.");
      if (state.kind === "extract" && !CAN_EDIT) throw new Error("PDF 추출에는 관리자 권한이 필요합니다.");
      if (state.kind === "extract" && !hasPdf(state.selected.values().next().value)) throw new Error("PDF가 확보된 논문을 선택하세요.");
      const model = $("model-select").value;
      const vision = state.kind === "extract" && $("use-vision").checked;
      if (vision && !state.models.some((item) => (item.name || item.model) === model && item.vision)) throw new Error("이미지 분석은 비전 지원 Qwen 모델을 선택하세요.");
      const filters = readFilters(); const scope = filters.scope || "serdes"; delete filters.scope; delete filters.q;
      // The untouched lane selector is a numeric-search default, not an override
      // of an explicit aggregate-throughput question interpreted by the planner.
      if (!filters.min_rate_gbps && filters.rate_scope === "lane") delete filters.rate_scope;
      const payload = {kind: state.kind, model, question, article_numbers: articles, scope, filters,
        num_ctx: Number($("num-ctx").value), max_tokens: Number($("max-tokens").value), thinking: $("thinking").value, vision};
      if (state.kind === "extract") payload.pages = readPages();
      state.submitting = true; syncSubmit();
      const response = await post("/jobs", payload); const job = response.job || response;
      if (!job.id) throw new Error("작업 ID를 받지 못했습니다. 최근 작업을 확인하세요.");
      await selectJob(job.id, job); toast("작업이 접수되었습니다. 대기 중인 작업부터 순서대로 실행합니다.");
      refreshHistory(); refreshStatus();
    } catch (error) { toast(errorText(error)); }
    finally { state.submitting = false; syncSubmit(); }
  }
  function statusBadge(status) { return node("span", `status-badge${completeStatus(status) ? " complete" : ["failed", "error"].includes(status) ? " failed" : ""}`, statusLabels[status] || status || "대기"); }
  function renderSources(sources) {
    const list = $("source-cards"); list.replaceChildren(); $("sources-section").hidden = !sources.length;
    $("sources-count").textContent = `${sources.length}개 근거`;
    sources.forEach((source, index) => {
      const card = node("article", "source-card");
      const page = source.page || source.page_number;
      card.append(node("div", "source-kicker", `${source.source_id || `[${index + 1}]`}${page ? ` · PDF p.${page}` : " · 메타데이터/저장 근거"}`), node("h4", "", paperTitle(source)));
      const excerpt = source.text || source.quote || source.content;
      if (excerpt) card.append(node("p", "", plain(excerpt)));
      const links = node("div", "source-links"); const view = node("button", "text-button", "근거 확인 ↗"); view.type = "button"; view.addEventListener("click", () => openSource(source)); links.append(view);
      const external = sourceLink("출처 링크", source.source_url || source.url); if (external) links.append(external);
      card.append(links); list.append(card);
    });
  }
  function openSource(source) {
    const dialog = $("source-dialog"); const article = articleKey(source); const page = source.page || source.page_number;
    $("source-dialog-title").textContent = paperTitle(source);
    const meta = $("source-dialog-meta"); meta.replaceChildren();
    if (article) meta.append(node("span", "", `논문 ${article}`));
    if (page) meta.append(node("span", "", `PDF 파일 기준 ${page}페이지`));
    const isPdf = article && (page || hasPdf(source) || String(source.source_url || "").includes("/pdf/"));
    if (isPdf) { const link = sourceLink("PDF 새 탭으로 열기 ↗", pdfUrl(article, page)); if (link) meta.append(link); }
    const external = sourceLink("원 출처 ↗", source.source_url || source.url); if (external) meta.append(external);
    $("source-dialog-quote").textContent = plain(source.text || source.quote || source.content || "");
    $("source-dialog-quote").hidden = !$("source-dialog-quote").textContent;
    $("pdf-preview").hidden = !isPdf; $("pdf-preview-note").hidden = !isPdf;
    $("pdf-preview").src = isPdf ? pdfUrl(article, page) : "about:blank";
    if (!dialog.open) dialog.showModal();
  }
  function renderProposals(proposals) {
    state.proposals = proposals; state.selectedProposals.clear();
    if ($("confirm-visual")) $("confirm-visual").checked = false;
    if ($("visual-confirm-label")) $("visual-confirm-label").hidden = !proposals.some((proposal) => proposal.validation_status === "visual_review" && (proposal.status || "pending") === "pending");
    const list = $("proposal-cards"); list.replaceChildren(); $("proposals-section").hidden = !proposals.length;
    proposals.forEach((proposal) => {
      const field = proposal.field || proposal.field_name || proposal.metric || "측정값";
      const value = proposal.value != null ? proposal.value : proposal.proposed_value;
      const status = proposal.status || "pending";
      const card = node("article", "proposal-card");
      const top = node("div", "proposal-topline"); const heading = node("label", "checkbox-label proposal-field");
      if (CAN_EDIT && status === "pending" && proposal.id != null) {
        const check = node("input"); check.type = "checkbox"; check.dataset.proposalId = String(proposal.id);
        check.addEventListener("change", () => { if (check.checked) state.selectedProposals.add(proposal.id); else state.selectedProposals.delete(proposal.id); syncProposalControls(); }); heading.append(check);
      }
      heading.append(document.createTextNode(fieldLabels[field] || field));
      top.append(heading, node("span", "status-badge", ({approved: "승인됨", rejected: "반려됨", pending: "검토 대기"})[status] || status));
      const number = node("div", "proposal-value", plain(value)); if (proposal.unit) number.append(node("small", "", proposal.unit));
      const context = node("div", "proposal-context");
      if (articleKey(proposal)) context.append(node("span", "", `논문 ${articleKey(proposal)}`));
      if (proposal.page) context.append(node("span", "", `PDF p.${proposal.page}`));
      const operating = proposal.operating_point || proposal.operating_point_key;
      if (operating) context.append(node("span", "", `동작점: ${plain(operating)}`));
      if (proposal.component_scope || proposal.rate_scope) context.append(node("span", "", [proposal.component_scope, proposal.rate_scope].filter(Boolean).join(" · ")));
      card.append(top, number, context);
      if (proposal.quote || proposal.evidence_text) card.append(node("blockquote", "", plain(proposal.quote || proposal.evidence_text)));
      const validation = plain(proposal.validation_message) || plain(proposal.validation_errors) || plain(proposal.validation_reasons) || ({valid: "텍스트 검증 통과 · 원문 확인 필요", visual_review: "이미지 근거 직접 확인 후 승인 가능", invalid: "검증 실패 · 승인 불가", needs_review: "추가 검토 필요 · 승인 불가"})[proposal.validation_status] || proposal.validation_status;
      if (validation) card.append(node("p", "proposal-validation", `검증: ${plain(validation)}`));
      const actions = node("div", "proposal-actions");
      if (articleKey(proposal) && proposal.page) { const preview = node("button", "text-button", "근거 페이지 확인 ↗"); preview.type = "button"; preview.addEventListener("click", () => openSource({...proposal, title: proposal.title || fieldLabels[field] || field, text: proposal.quote || proposal.evidence_text})); actions.append(preview); }
      if (CAN_EDIT && ["pending", "proposed", "needs_review"].includes(status) && proposal.id != null) {
        const approve = node("button", "button secondary", "승인 · 서베이에 반영"); approve.type = "button";
        approve.dataset.approveId = String(proposal.id);
        approve.disabled = !canApprove(proposal);
        approve.addEventListener("click", () => decideProposal(proposal, "approve", card));
        const reject = node("button", "text-button danger", "반려"); reject.type = "button"; reject.addEventListener("click", () => decideProposal(proposal, "reject", card));
        actions.append(approve, reject);
      } else if (!CAN_EDIT && status === "pending") actions.append(node("span", "mini-label", "관리자 승인 필요"));
      card.append(actions); list.append(card);
    }); syncProposalControls();
  }
  function canApprove(proposal) { return proposal.validation_status === "valid" || proposal.validation_status === "visual_review" && Boolean($("confirm-visual") && $("confirm-visual").checked); }
  function syncProposalControls() {
    if (!CAN_EDIT) return;
    const selected = state.proposals.filter((proposal) => state.selectedProposals.has(proposal.id));
    $("proposal-selection-count").textContent = `${selected.length}개 선택`;
    $("approve-proposals").disabled = !selected.length || selected.some((proposal) => !canApprove(proposal));
    $("reject-proposals").disabled = !selected.length;
    document.querySelectorAll("[data-approve-id]").forEach((button) => { const proposal = state.proposals.find((item) => String(item.id) === button.dataset.approveId); button.disabled = !proposal || !canApprove(proposal); });
  }
  async function decideSelected(decision) {
    const proposals = state.proposals.filter((proposal) => state.selectedProposals.has(proposal.id));
    if (!proposals.length) return;
    if (decision === "approve" && proposals.some((proposal) => !canApprove(proposal))) { toast("검증 상태와 이미지 근거 확인 여부를 확인하세요."); return; }
    const label = decision === "approve" ? "승인" : "반려";
    const summary = proposals.map((proposal) => `${fieldLabels[proposal.field || proposal.field_name] || proposal.field || proposal.field_name}: ${plain(proposal.value)} ${proposal.unit || ""} · p.${proposal.page || "?"} · ${plain(proposal.operating_point || "")}`).join("\n");
    if (!window.confirm(`${summary}\n\n선택한 ${proposals.length}개 제안을 ${label}할까요?${decision === "approve" ? " 동일 동작점의 값만 함께 묶여 새 항목으로 추가됩니다." : ""}`)) return;
    $("approve-proposals").disabled = true; $("reject-proposals").disabled = true;
    try { await post("/proposals/decision", {ids: proposals.map((proposal) => Number(proposal.id)), decision, confirm_visual: Boolean($("confirm-visual").checked)}); toast(`${proposals.length}개 제안을 ${label}했습니다.`); await refreshProposals(state.currentJob); }
    catch (error) { toast(errorText(error)); syncProposalControls(); }
  }
  async function decideProposal(proposal, decision, card) {
    const label = decision === "approve" ? "승인" : "반려";
    const name = fieldLabels[proposal.field || proposal.field_name] || proposal.field || proposal.field_name || "측정값";
    if (!window.confirm(`${name}: ${plain(proposal.value != null ? proposal.value : proposal.proposed_value)} ${proposal.unit || ""}\n${proposal.page ? `PDF ${proposal.page}페이지 · ` : ""}${plain(proposal.operating_point || "")}\n\n이 제안을 ${label}할까요?${decision === "approve" ? " 원문과 동작점을 확인한 경우에만 승인하세요." : ""}`)) return;
    const controls = Array.from(card.querySelectorAll("button")); controls.forEach((button) => { button.disabled = true; });
    try { await post("/proposals/decision", {ids: [Number(proposal.id)], decision, confirm_visual: Boolean($("confirm-visual") && $("confirm-visual").checked)}); toast(`제안을 ${label}했습니다.`); await refreshProposals(state.currentJob); }
    catch (error) { toast(errorText(error)); controls.forEach((button) => { button.disabled = false; }); syncProposalControls(); }
  }
  async function refreshProposals(id) {
    if (!id || !CAN_EDIT) return;
    try { const data = await api(`/proposals?job_id=${encodeURIComponent(id)}&status=all`); if (state.currentJob === id) renderProposals(Array.isArray(data.items) ? data.items : []); }
    catch (error) { if (state.currentJob === id) toast(`제안 목록: ${errorText(error)}`); }
  }
  function renderJob(job) {
    const status = job.status || "queued"; const result = job.result || {};
    const badge = statusBadge(status); $("job-status").className = badge.className; $("job-status").textContent = badge.textContent; $("job-status").hidden = false;
    $("cancel-job").hidden = !activeStatus(status);
    const progress = job.error ? `작업 실패: ${plain(job.error)}` : job.progress || result.progress || (activeStatus(status) ? "작업이 실행 중입니다. 모델을 처음 불러올 때는 시간이 더 걸릴 수 있습니다." : "");
    $("job-progress").textContent = plain(progress); $("job-progress").hidden = !progress;
    state.answer = plain(result.answer || result.summary || result.review || "");
    $("answer-content").textContent = state.answer; $("answer-content").hidden = !state.answer;
    $("answer-empty").hidden = activeStatus(status) || Boolean(state.answer) || Boolean(job.error) || Array.isArray(result.proposals) && result.proposals.length > 0;
    if (completeStatus(status) && !state.answer && !(result.proposals || []).length) { $("answer-empty").hidden = true; $("job-progress").hidden = false; $("job-progress").textContent = "작업이 완료되었습니다. 생성된 제안과 근거를 확인하세요. 제안이 없다면 PDF 페이지 또는 질문을 조정해 다시 시도할 수 있습니다."; }
    $("copy-answer").hidden = !state.answer;
    const caveats = [plain(result.warnings), plain(result.caveats), plain(result.limitations)].filter(Boolean);
    if (result.grounding === "needs_review") caveats.unshift("근거 인용 검증이 충분하지 않습니다. 아래 답변은 확인이 필요한 초안이며, 검증된 결론으로 사용하지 마세요.");
    const discarded = Array.isArray(result.discarded_candidates) ? result.discarded_candidates.length : Number(result.discarded_candidates || 0);
    if (discarded) caveats.unshift(`수치 후보 ${discarded}개는 형식·근거 검증을 통과하지 못해 저장 또는 승인 대상에서 제외되었습니다.`);
    $("answer-caveats").textContent = caveats.join("\n"); $("answer-caveats").hidden = !caveats.length;
    const findings = Array.isArray(result.findings) ? result.findings : []; $("findings-section").hidden = !findings.length; $("finding-cards").replaceChildren();
    findings.forEach((finding) => {
      const card = node("article", "proposal-card"); const header = node("div", "proposal-topline");
      header.append(node("span", "proposal-field", fieldLabels[finding.field] || finding.field), node("span", "status-badge", ({info: "참고", review: "검토 필요", conflict: "조건·근거 충돌"})[finding.severity] || "검토"));
      card.append(header, node("p", "finding-issue", finding.issue), node("p", "help", `검토 제안: ${finding.recommendation}`));
      const links = node("div", "source-links finding-links"); (finding.source_ids || []).forEach((id) => { const source = (result.sources || []).find((item) => item.source_id === id); if (source) { const button = node("button", "text-button", `[${id}] 근거 확인`); button.type = "button"; button.addEventListener("click", () => openSource(source)); links.append(button); } }); card.append(links); $("finding-cards").append(card);
    });
    const usage = result.usage || {}; const details = [job.model || result.model];
    if (usage.eval_count != null) details.push(`출력 ${usage.eval_count} tokens`);
    else if (usage.completion_tokens != null) details.push(`출력 ${usage.completion_tokens} tokens`);
    if (usage.tokens_per_second != null) details.push(`${Number(usage.tokens_per_second).toFixed(1)} tok/s`);
    if (usage.duration_seconds != null) details.push(`${Number(usage.duration_seconds).toFixed(1)}초`);
    $("answer-usage").textContent = details.filter(Boolean).join(" · "); $("answer-usage").hidden = !details.filter(Boolean).length;
    renderSources(Array.isArray(result.sources) ? result.sources : []);
    renderProposals(Array.isArray(result.proposals) ? result.proposals : []);
  }
  async function selectJob(id, initial) {
    clearTimeout(state.polling); state.currentJob = id;
    $("answer-content").hidden = true; $("answer-empty").hidden = true; $("sources-section").hidden = true; $("proposals-section").hidden = true;
    if (initial) renderJob(initial); else { $("job-progress").hidden = false; $("job-progress").textContent = "작업 결과를 불러오는 중…"; }
    await pollJob(id);
  }
  async function pollJob(id) {
    if (state.currentJob !== id) return;
    try {
      const data = await api(`/jobs/${encodeURIComponent(id)}`); if (state.currentJob !== id) return;
      const job = data.job || data; renderJob(job);
      if (activeStatus(job.status)) state.polling = setTimeout(() => pollJob(id), 1800);
      else { await refreshProposals(id); refreshHistory(); refreshStatus(); }
    } catch (error) { if (state.currentJob !== id) return; $("job-progress").textContent = errorText(error); $("job-progress").hidden = false; state.polling = setTimeout(() => pollJob(id), 6000); }
  }
  async function cancelJob() {
    if (!state.currentJob || !window.confirm("이 작업을 취소할까요? 이미 승인된 제안에는 영향을 주지 않습니다.")) return;
    const id = state.currentJob; $("cancel-job").disabled = true;
    try { await post(`/jobs/${encodeURIComponent(id)}/cancel`); toast("취소를 요청했습니다."); clearTimeout(state.polling); await pollJob(id); refreshHistory(); }
    catch (error) { toast(errorText(error)); } finally { $("cancel-job").disabled = false; }
  }
  async function refreshHistory() {
    try {
      const data = await api("/jobs"); const items = Array.isArray(data.items) ? data.items : []; const list = $("job-history"); list.replaceChildren();
      if (!items.length) { list.append(node("p", "help", "아직 실행한 작업이 없습니다. 질문을 보내면 이곳에 기록됩니다.")); return; }
      items.slice(0, 8).forEach((job) => {
        const row = node("button", `history-row${state.currentJob === job.id ? " current" : ""}`); row.type = "button";
        const main = node("span", "history-main"); main.append(node("b", "", job.question || (job.request || {}).question || ({ask: "논문 Q&A", review: "서베이 검토", extract: "PDF 수치 추출"})[job.kind] || "AI 작업"));
        const time = job.created_at ? new Date(job.created_at).toLocaleString("ko-KR", {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"}) : "";
        main.append(node("small", "", [time, job.model].filter(Boolean).join(" · ")));
        row.append(node("span", "history-kind", ({ask: "Q&A", review: "검토", extract: "추출"})[job.kind] || "AI"), main, statusBadge(job.status));
        row.addEventListener("click", () => selectJob(job.id)); list.append(row);
      });
    } catch (error) { $("job-history").replaceChildren(node("p", "help", errorText(error))); }
  }
  async function refreshReview() {
    const list = $("review-queue"); list.replaceChildren(node("p", "help", "검토 대상을 불러오는 중…"));
    try {
      const data = await api("/review-queue"); const items = Array.isArray(data.items) ? data.items : []; list.replaceChildren();
      if (!items.length) { list.append(node("p", "help", "현재 검토 대기 항목이 없습니다. 논문을 직접 선택해 검토할 수도 있습니다.")); return; }
      items.slice(0, 30).forEach((item) => {
        const paper = item.paper || item; const row = node("article", "review-row"); const body = node("div");
        body.append(node("h3", "", paperTitle(paper)), node("p", "", plain(item.reason || item.reasons || item.review_reason || item.issue || item.category || item.status || "원문과 성능값 대조 필요")));
        row.append(body);
        if (articleKey(paper)) { const choose = node("button", "text-button", "검토 선택 →"); choose.type = "button"; choose.addEventListener("click", () => { state.selected.clear(); state.selected.set(articleKey(paper), paper); renderSelected(); renderPapers(); $("question").value = presets.review[1][1]; $("question").focus(); }); row.append(choose); }
        else { const find = node("button", "text-button", "논문 찾기 →"); find.type = "button"; find.addEventListener("click", () => { $("search-scope").value = "repo"; $("search-q").value = paperTitle(paper); searchPapers(); }); row.append(find); }
        list.append(row);
      });
    } catch (error) { list.replaceChildren(node("p", "help", errorText(error))); }
  }
  $("search-form").addEventListener("submit", searchPapers);
  $("clear-selection").addEventListener("click", () => { state.selected.clear(); renderSelected(); renderPapers(); });
  $("job-form").addEventListener("submit", submitJob);
  $("refresh-status").addEventListener("click", refreshStatus);
  $("refresh-history").addEventListener("click", refreshHistory);
  $("refresh-review").addEventListener("click", refreshReview);
  $("cancel-job").addEventListener("click", cancelJob);
  if (CAN_EDIT) {
    $("confirm-visual").addEventListener("change", syncProposalControls);
    $("select-proposals").addEventListener("click", () => { state.selectedProposals.clear(); state.proposals.forEach((proposal) => { if (proposal.status === "pending" && ["valid", "visual_review"].includes(proposal.validation_status)) state.selectedProposals.add(proposal.id); }); document.querySelectorAll("[data-proposal-id]").forEach((check) => { check.checked = state.selectedProposals.has(Number(check.dataset.proposalId)); }); syncProposalControls(); });
    $("approve-proposals").addEventListener("click", () => decideSelected("approve"));
    $("reject-proposals").addEventListener("click", () => decideSelected("reject"));
  }
  $("close-source").addEventListener("click", () => $("source-dialog").close());
  $("source-dialog").addEventListener("close", () => { $("pdf-preview").src = "about:blank"; });
  $("copy-answer").addEventListener("click", async () => { try { await navigator.clipboard.writeText(state.answer); toast("답변을 복사했습니다."); } catch (_) { toast("복사가 허용되지 않았습니다. 답변 텍스트를 선택해 복사하세요."); } });
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => setKind(tab.dataset.kind));
    tab.addEventListener("keydown", (event) => { const kinds = ["ask", "review", "extract"]; const direction = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0; if (direction) { event.preventDefault(); setKind(kinds[(kinds.indexOf(state.kind) + direction + kinds.length) % kinds.length], true); } });
  });
  $("question").addEventListener("keydown", (event) => { if ((event.ctrlKey || event.metaKey) && event.key === "Enter" && !$("submit-job").disabled) { event.preventDefault(); $("job-form").requestSubmit(); } });
  const statusInterval = setInterval(() => { if (!document.hidden) refreshStatus(); }, 12000);
  window.addEventListener("pagehide", () => { clearTimeout(state.polling); clearInterval(statusInterval); });
  setKind("ask"); renderSelected();
  Promise.allSettled([refreshStatus(), searchPapers(), refreshHistory()]);
  const linkedParams = new URLSearchParams(location.search);
  const linkedPaper = linkedParams.get("paper");
  const linkedKind = linkedParams.get("kind");
  if (["ask", "review", "extract"].includes(linkedKind) && (linkedKind !== "extract" || CAN_EDIT)) setKind(linkedKind);
  if (linkedPaper && /^[A-Za-z0-9._-]{1,180}$/.test(linkedPaper)) api(`/papers/${encodeURIComponent(linkedPaper)}`).then((data) => { if (data.paper) { state.selected.set(articleKey(data.paper), data.paper); renderSelected(); renderPapers(); } }).catch((error) => toast(errorText(error)));
})();
