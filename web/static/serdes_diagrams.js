/* Original PDF thumbnails only; no model runs on hover. */
(function(root){
  'use strict';
  function create(container, resize, fetcher=fetch){
    const cache=new Map(), pending=new Map();
    let generation=0;
    const element=(tag,text)=>{const node=document.createElement(tag);if(text)node.textContent=text;return node;};
    function message(text){container.replaceChildren(element('p',text));resize();}
    function load(article,measurement){
      const key=JSON.stringify([article,measurement||null]);
      const cached=cache.get(key);
      if(cached && Date.now()-cached.at < (cached.data.status==='available'?300000:30000))return Promise.resolve(cached.data);
      if(pending.has(key))return pending.get(key);
      const request=fetcher(`/api/serdes/papers/${encodeURIComponent(article)}/block-diagram${measurement?`?measurement_id=${encodeURIComponent(measurement)}`:''}`)
        .then(async response=>{if(!response.ok)throw new Error('figure lookup failed');const data=await response.json();
          cache.delete(key);cache.set(key,{data,at:Date.now()});if(cache.size>128)cache.delete(cache.keys().next().value);return data;})
        .finally(()=>pending.delete(key));
      pending.set(key,request);return request;
    }
    async function show(article,measurement){
      const current=++generation;
      if(!article){message('논문 미연결 · 블록도 없음');return;}
      message('원본 블록도 확인 중…');
      try{
        const data=await load(String(article),measurement);
        if(current!==generation)return;
        if(data.status!=='available'&&data.status!=='reference_only'){
          const labels={missing_pdf:'PDF 미보유 · 블록도 없음',not_prepared:'블록도 미준비 · 사전 추출 필요',not_found:'대표 블록도를 자동으로 확인하지 못했습니다.',review_required:'블록도 검토 필요 · 구현/패널을 확정하지 않았습니다.'};
          let label=labels[data.status]||'블록도를 표시할 수 없습니다.';
          if(data.status==='no_top_diagram')label='원문 전체 확인 · 탑/시스템 블록도 없음';
          if(data.review_reasons?.includes('source_visual_review_pending'))label='블록도 후보 추출 완료 · 원문/패널 검증 대기';
          if(data.review_reasons?.includes('different_implementation'))label='이 성능점의 구현에 연결된 블록도는 아직 검증되지 않았습니다.';
          if(data.review_reasons?.includes('scope_mismatch'))label='성능점과 블록도의 회로 범위가 달라 원문 확인이 필요합니다.';
          if(data.review_reasons?.includes('crop_contamination'))label='추출 영역에 주변 내용이 포함되어 블록도 범위 검토가 필요합니다.';
          if(data.review_reasons?.includes('subblock_not_top_level'))label='하위 회로 그림만 확인되어 탑/시스템 블록도 검토가 필요합니다.';
          message(label);
          if(data.status==='no_top_diagram'&&typeof data.review_note==='string')container.append(element('p',data.review_note));
          if(typeof data.pdf_url==='string'&&data.pdf_url.startsWith(`/pdf/${encodeURIComponent(article)}#page=`)){
            const pdf=element('a','원본 PDF에서 확인 ↗');pdf.href=data.pdf_url;pdf.target='_blank';pdf.rel='noopener';container.append(pdf);resize();
          }
          return;
        }
        // Use only URLs of this paper's authenticated image/PDF endpoints.
        const prefix=`/api/serdes/papers/${encodeURIComponent(article)}/block-diagram/image?v=`;
        if(!data.image_url?.startsWith(prefix)||!/^\d+$/.test(String(data.page)))throw new Error('invalid figure');
        const reference=data.status==='reference_only';
        const figure=element('figure'),heading=element('b',reference?'논문 참고 블록도 · 성능점 전체 범위와 구분':'대표 블록도'),link=element('a'),img=element('img');
        link.href=data.image_url;link.target='_blank';link.rel='noopener';link.title='블록도 원본 크기로 보기';
        img.alt=`Fig. ${data.figure_number}: ${data.caption}`;img.decoding='async';img.width=data.width;img.height=data.height;
        img.onload=()=>{if(current===generation)resize();};
        img.onerror=()=>{if(current===generation){cache.delete(JSON.stringify([String(article),measurement||null]));message('블록도를 불러오지 못했습니다. 원본 PDF를 확인해 주세요.');}};
        link.append(img);
        const verification=data.verified?'원문 대조 완료':'자동 선정 · 미검증';
        const mapping=data.measurement_link==='reviewed'?'성능점 연결 확인':data.measurement_link==='implementation'?'구현 단위 연결':data.measurement_link==='paper_level'?'논문 단위 연결':reference?'성능점 직접 연결 아님':'';
        const caption=element('figcaption',data.caption),note=element('small',[verification,mapping,data.scope&&data.scope!=='unknown'?`범위: ${data.scope.toUpperCase()}`:'','이미지 클릭으로 확대'].filter(Boolean).join(' · ')),pdf=element('a',`PDF ${data.page}페이지 ↗`);
        pdf.href=`/pdf/${encodeURIComponent(article)}#page=${data.page}`;pdf.target='_blank';pdf.rel='noopener';
        figure.append(heading,link,caption,note,pdf);
        if(reference)figure.append(element('p',data.scope_note||'이 그림은 논문 참고용이며 성능점 전체 범위와 동일하지 않습니다.'));
        container.replaceChildren(figure);img.src=data.image_url;resize();
      }catch(error){if(current===generation)message('블록도를 불러오지 못했습니다. 다시 선택해 주세요.');}
    }
    return {show, clear(){++generation;container.replaceChildren();}};
  }
  root.SerdesDiagrams={create};
  if(typeof module!=='undefined')module.exports={create};
})(typeof window!=='undefined'?window:globalThis);
