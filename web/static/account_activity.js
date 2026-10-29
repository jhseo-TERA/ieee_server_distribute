(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const roles = {admin:'관리자',member:'일반',viewer:'읽기 전용'};
  const states = {online:'활동 중',idle:'최근 활동 없음',offline:'종료',unknown:'아직 확인 안 됨',disabled:'비활성',logout:'로그아웃',revoked:'권한 / 비밀번호 변경',expired:'만료'};
  const node = (tag, text, className='') => { const result=document.createElement(tag); result.textContent=text; result.className=className; return result; };
  const time = value => value ? new Date(value).toLocaleString('ko-KR',{timeZone:'Asia/Seoul',hour12:false}) : '–';
  const pill = status => node('span',states[status] || status,'activity-pill '+status);
  const cell = value => { const td=document.createElement('td'); if(typeof value==='string') td.textContent=value; else td.append(value); return td; };
  let loading=false;
  async function load() {
    if(loading) return;
    loading=true; $('refresh-activity').disabled=true;
    try {
      const response=await fetch('/api/account/activity');
      const data=await response.json();
      if(!response.ok || !data.ok) throw new Error(data.error || '로그인 현황을 가져오지 못했습니다.');
      $('account-rows').replaceChildren();
      for(const account of data.accounts){
        const name=node('span',account.username,'activity-name');
        if(account.username==='admin') name.append(node('small','내 계정'));
        const row=document.createElement('tr');
        row.append(cell(name),cell(roles[account.role] || account.role),cell(pill(account.status)),cell(String(account.active_sessions)),cell(time(account.last_login_at)),cell(time(account.last_seen_at)));
        $('account-rows').append(row);
      }
      $('session-rows').replaceChildren();
      for(const session of data.recent_sessions){
        const row=document.createElement('tr');
        row.append(cell(session.username),cell(session.kind==='login'?'로그인':'기존 세션 확인'),cell(time(session.login_at || session.first_seen_at)),cell(time(session.last_seen_at)),cell(pill(session.status)),cell(time(session.ended_at)));
        $('session-rows').append(row);
      }
      if(!data.recent_sessions.length){ const row=document.createElement('tr'), td=cell('아직 기록된 로그인이나 활동이 없습니다.'); td.colSpan=6; row.append(td); $('session-rows').append(row); }
      $('account-count').textContent=String(data.accounts.length);
      $('online-count').textContent=String(data.accounts.filter(item=>item.status==='online').length);
      $('session-count').textContent=String(data.accounts.reduce((sum,item)=>sum+item.active_sessions,0));
      $('activity-status').textContent='30초마다 갱신 · 모든 시각은 한국 시간입니다.';
      $('activity-status').classList.remove('error');
      $('last-refreshed').textContent='갱신 '+time(data.generated_at);
    } catch(error){ $('activity-status').textContent=error.message; $('activity-status').classList.add('error'); }
    finally{ loading=false; $('refresh-activity').disabled=false; }
  }
  $('refresh-activity').addEventListener('click',load);
  document.addEventListener('visibilitychange',()=>{ if(!document.hidden) load(); });
  setInterval(()=>{ if(!document.hidden) load(); },30000);
  load();
})();
