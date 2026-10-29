(() => {
  'use strict';
  const $=id=>document.getElementById(id);
  const caption={Highlight:'형광펜',Underline:'밑줄',StrikeOut:'취소선',Text:'메모',FreeText:'텍스트 메모',Ink:'펜',Square:'사각형',Circle:'원'};
  let target=null,page=1,pages=1,box=null,start=null,processing=false,annotations=[],epoch=0,annotationRequest=0;
  const setStatus=(node,text,error=false)=>{ node.textContent=text; node.classList.toggle('error',error); };
  const query=()=>'?article_number='+encodeURIComponent(target.item.article_number);
  function insert(editor,text){
    const notes=editor.notes;
    if(notes.value.length+text.length>12000) throw new Error('리뷰가 12,000자를 넘습니다. 일부 주석을 선택하거나 내용을 나눠 넣으세요.');
    notes.setRangeText(text,notes.selectionStart,notes.selectionEnd,'end');
    notes.dispatchEvent(new Event('input',{bubbles:true})); notes.focus();
  }
  async function busy(editor,action){
    editor.onBusy(1);
    try { return await action(); } finally { editor.onBusy(-1); }
  }
  async function uploadImage(editor,file){
    if(file.size>12*1024*1024) throw new Error('그림은 12MB 이내로 첨부하세요.');
    if(editor.notes.value.length>11800) throw new Error('그림을 넣을 공간이 부족합니다. 리뷰 내용을 줄인 뒤 첨부하세요.');
    return busy(editor,async()=>{
      setStatus(editor.status,'그림을 첨부하는 중…');
      const data=new FormData(); data.append('article_number',editor.item.article_number); data.append('image',file);
      const result=await editor.api('/api/daily-review/image',{method:'POST',headers:{'X-CSRF-Token':editor.csrf},body:data});
      insert(editor,'\n\n'+result.item.markdown+'\n\n');
      setStatus(editor.status,'그림을 넣었습니다. 리뷰 저장을 눌러 반영하세요.');
    });
  }
  function chooseBox(value){
    box=value;
    $('crop-selection').hidden=!value;
    $('clip-insert').disabled=!value || processing;
    if(value){ const [x0,y0,x1,y1]=value; Object.assign($('crop-selection').style,{left:x0*100+'%',top:y0*100+'%',width:(x1-x0)*100+'%',height:(y1-y0)*100+'%'}); }
  }
  function showPage(number){
    page=Math.max(1,Math.min(pages,number));
    $('clip-page').value=String(page); $('clip-page').max=String(pages); $('clip-page-count').textContent='/ '+pages;
    $('clip-prev').disabled=page<=1; $('clip-next').disabled=page>=pages;
    chooseBox(null);
    $('crop-page-image').src='/api/daily-review/page'+query()+'&page='+page+'&v='+Date.now();
  }
  async function refreshDocument(){
    const editor=target,requestEpoch=epoch;
    const data=await editor.api('/api/daily-review/document'+query());
    if(editor!==target || requestEpoch!==epoch) return;
    pages=data.pages;
    $('clip-download').href=data.download_url;
    $('clip-view').href=data.viewer_url;
    $('clip-view').textContent=data.working_copy?'주석 PDF 보기':'PDF 보기';
    showPage(page);
  }
  async function openTools(editor){
    if(!editor.canWrite) return;
    target=editor; epoch++; page=1; annotations=[]; $('annotation-panel').hidden=true;
    $('clip-paper-title').textContent=editor.item.title;
    $('clip-dialog').showModal();
    setStatus($('clip-status'),'PDF를 불러오는 중…');
    try { await refreshDocument(); if(target===editor) setStatus($('clip-status'),'그림을 선택하거나 Acrobat에서 주석을 작성하세요.'); }
    catch(error){ setStatus($('clip-status'),error.message,true); $('crop-page-image').removeAttribute('src'); }
  }
  function closeTools(){ if(processing) return; $('clip-dialog').close(); target=null; epoch++; }
  const jsonOptions=body=>({method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':target.csrf},body:JSON.stringify(body)});
  function annotationMarkdown(item){
    const lines=[`**p.${item.page} · ${caption[item.kind] || item.kind}**`];
    if(item.text) lines.push(...item.text.split(/\r?\n/).map(line=>'> '+line));
    if(item.comment) lines.push('', '메모: '+item.comment);
    return lines.join('\n');
  }
  const hasAnnotation=(editor,item)=>editor.notes.value.includes(annotationMarkdown(item)) || editor.notes.value.includes(`<!-- pdf-review:${item.id} -->`);
  async function readAnnotations(){
    const editor=target,requestEpoch=epoch,requestNumber=++annotationRequest;
    if(!editor) return;
    setStatus($('clip-status'),'Acrobat에서 저장한 형광펜·메모를 읽는 중…');
    try {
      const data=await editor.api('/api/daily-review/annotations'+query());
      if(editor!==target || requestEpoch!==epoch || requestNumber!==annotationRequest) return;
      annotations=data.items;
      $('annotation-list').replaceChildren();
      for(const item of annotations){
        const label=document.createElement('label'),checkbox=document.createElement('input'),text=document.createElement('span');
        checkbox.type='checkbox'; checkbox.value=item.id;
        const imported=hasAnnotation(editor,item);
        checkbox.checked=!imported; checkbox.disabled=imported;
        text.textContent=`p.${item.page} · ${caption[item.kind] || item.kind}${imported?' · 이미 가져옴':''}\n${item.text || ''}${item.comment?'\n메모: '+item.comment:''}`;
        label.append(checkbox,text); $('annotation-list').append(label);
      }
      $('annotation-panel').hidden=!annotations.length;
      setStatus($('clip-status'),annotations.length ? `${annotations.length}개 주석을 확인했습니다. 가져올 항목을 선택하세요.` : '저장된 형광펜·메모를 찾지 못했습니다. Acrobat에서 Ctrl+S로 저장한 뒤 다시 읽어 주세요.');
    } catch(error){ setStatus($('clip-status'),error.message,true); }
  }
  $('clip-close').addEventListener('click',closeTools);
  $('clip-dialog').addEventListener('cancel',event=>{ event.preventDefault(); closeTools(); });
  $('clip-prev').addEventListener('click',()=>showPage(page-1));
  $('clip-next').addEventListener('click',()=>showPage(page+1));
  $('clip-page').addEventListener('change',()=>showPage(Number($('clip-page').value)||1));
  $('clip-reload').addEventListener('click',()=>refreshDocument().catch(error=>setStatus($('clip-status'),error.message,true)));
  $('crop-page-image').addEventListener('error',()=>setStatus($('clip-status'),'PDF 페이지를 표시하지 못했습니다. 저장 내용 새로고침을 누르세요.',true));
  $('clip-whole-page').addEventListener('click',()=>{ if($('crop-page-image').naturalWidth) chooseBox([0,0,1,1]); });
  const stage=$('crop-stage');
  const point=event=>{ const rect=$('crop-page-image').getBoundingClientRect(); return [Math.max(0,Math.min(1,(event.clientX-rect.left)/rect.width)),Math.max(0,Math.min(1,(event.clientY-rect.top)/rect.height))]; };
  stage.addEventListener('pointerdown',event=>{ if(!target || processing || !$('crop-page-image').naturalWidth) return; event.preventDefault(); start=point(event); stage.setPointerCapture(event.pointerId); chooseBox(null); });
  stage.addEventListener('pointermove',event=>{ if(!start) return; const end=point(event); chooseBox([Math.min(start[0],end[0]),Math.min(start[1],end[1]),Math.max(start[0],end[0]),Math.max(start[1],end[1])]); });
  stage.addEventListener('pointerup',()=>{ start=null; });
  stage.addEventListener('pointercancel',()=>{ start=null; chooseBox(null); });
  $('clip-insert').addEventListener('click',async()=>{
    if(!target || !box || processing) return;
    if(target.notes.value.length>11800){ setStatus($('clip-status'),'그림을 넣을 공간이 부족합니다. 리뷰 내용을 줄인 뒤 첨부하세요.',true); return; }
    const editor=target; processing=true; $('clip-insert').disabled=true;
    try {
      await busy(editor,async()=>{
        const result=await editor.api('/api/daily-review/clip',jsonOptions({article_number:editor.item.article_number,page,box}));
        insert(editor,'\n\n'+result.item.markdown+'\n\n');
      });
      setStatus(editor.status,'잘라낸 그림을 넣었습니다. 리뷰 저장을 눌러 반영하세요.');
      processing=false; closeTools();
    } catch(error){ setStatus($('clip-status'),error.message,true); }
    finally{ processing=false; $('clip-insert').disabled=!box; }
  });
  $('clip-acrobat').addEventListener('click',async()=>{
    if(!target || processing) return;
    processing=true;
    try {
      await busy(target,()=>target.api('/api/daily-review/acrobat',jsonOptions({article_number:target.item.article_number})));
      setStatus($('clip-status'),'이 PC의 Acrobat 열기를 요청했습니다. 형광펜·메모를 저장한 뒤 주석 읽기를 누르세요.');
    } catch(error){ setStatus($('clip-status'),error.message,true); }
    finally{ processing=false; }
  });
  $('clip-upload-pdf').addEventListener('change',async event=>{
    const file=event.target.files[0]; event.target.value='';
    if(!file || !target || processing) return;
    processing=true;
    try {
      await busy(target,async()=>{
        const data=new FormData(); data.append('article_number',target.item.article_number); data.append('pdf',file);
        await target.api('/api/daily-review/annotated-pdf',{method:'POST',headers:{'X-CSRF-Token':target.csrf},body:data});
        await refreshDocument(); await readAnnotations();
      });
    } catch(error){ setStatus($('clip-status'),error.message,true); }
    finally{ processing=false; }
  });
  $('clip-read-annotations').addEventListener('click',readAnnotations);
  $('insert-annotations').addEventListener('click',()=>{
    const selected=new Set([...$('annotation-list').querySelectorAll('input:checked')].map(input=>input.value));
    const chosen=annotations.filter(item=>selected.has(item.id) && !hasAnnotation(target,item));
    if(!chosen.length){ setStatus($('clip-status'),'새로 가져올 주석을 선택하세요.'); return; }
    try { const editor=target; insert(editor,'\n\n'+chosen.map(annotationMarkdown).join('\n\n')+'\n\n'); setStatus(editor.status,`${chosen.length}개 주석을 넣었습니다. 리뷰 저장을 눌러 반영하세요.`); closeTools(); }
    catch(error){ setStatus($('clip-status'),error.message,true); }
  });
  window.ReviewTools={mount(editor){
    editor.status=editor.card.querySelector('.media-status');
    const preview=editor.card.querySelector('.review-preview');
    let renderTimer;
    const render=()=>{ preview.innerHTML=editor.notes.value ? window.ReviewMarkdown.render(editor.notes.value) : '<p class="preview-empty">그림과 LaTeX 수식이 여기에 표시됩니다.</p>'; };
    render(); editor.notes.addEventListener('input',()=>{ clearTimeout(renderTimer); renderTimer=setTimeout(render,150); });
    if(!editor.canWrite) return;
    editor.card.querySelector('.open-pdf-tools').disabled=!editor.item.pdf_available;
    editor.card.querySelector('.open-pdf-tools').addEventListener('click',()=>openTools(editor));
    const input=editor.card.querySelector('.image-upload');
    editor.card.querySelector('.attach-image').addEventListener('click',()=>input.click());
    input.addEventListener('change',async()=>{ const file=input.files[0]; input.value=''; if(file) try { await uploadImage(editor,file); } catch(error){ setStatus(editor.status,error.message,true); } });
    editor.notes.addEventListener('paste',async event=>{
      const images=[...(event.clipboardData?.files || [])].filter(file=>file.type.startsWith('image/'));
      if(!images.length) return;
      event.preventDefault();
      for(const image of images) try { await uploadImage(editor,image); } catch(error){ setStatus(editor.status,error.message,true); }
    });
    editor.card.querySelector('.insert-math').addEventListener('click',()=>{
      try { insert(editor,'\n\n$$\nH(s) = \\frac{V_{out}(s)}{V_{in}(s)}\n$$\n\n'); }
      catch(error){ setStatus(editor.status,error.message,true); }
    });
  }};
})();
