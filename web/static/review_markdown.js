(function (root) {
  'use strict';
  function createRenderer(markdownit,katex) {
    const md=markdownit({html:false,breaks:true,linkify:false,maxNesting:20});
    const math=(source,display)=>katex.renderToString(source,{displayMode:display,throwOnError:false,
      trust:false,maxExpand:1000,maxSize:15,strict:'ignore'});
    md.block.ruler.before('html_block','review_import_marker',function(state,start,end,silent){
      const line=state.src.slice(state.bMarks[start]+state.tShift[start],state.eMarks[start]).trim();
      if(!/^<!-- pdf-review:[a-f0-9]{24} -->$/.test(line)) return false;
      if(!silent){ const token=state.push('review_import_marker','',0); token.hidden=true; state.line=start+1; }
      return true;
    });
    md.renderer.rules.review_import_marker=()=>'';
    md.inline.ruler.before('escape','review_math',function(state,silent){
      const start=state.pos;
      if(state.src[start]!=='$' || state.src[start+1]==='$') return false;
      let end=start+1;
      while((end=state.src.indexOf('$',end))!==-1){
        if(state.src[end-1]!=='\\') break;
        end++;
      }
      if(end<0 || end===start+1 || state.src.slice(start+1,end).includes('\n')) return false;
      if(!silent){ const token=state.push('review_math','math',0); token.content=state.src.slice(start+1,end); }
      state.pos=end+1; return true;
    });
    md.block.ruler.before('fence','review_math_block',function(state,start,end,silent){
      const first=state.src.slice(state.bMarks[start]+state.tShift[start],state.eMarks[start]).trim();
      if(!first.startsWith('$$')) return false;
      if(silent) return true;
      let content=first.slice(2),next=start+1;
      if(content.endsWith('$$')) content=content.slice(0,-2);
      else {
        let closed=false;
        for(;next<end;next++){
          const line=state.src.slice(state.bMarks[next]+state.tShift[next],state.eMarks[next]);
          if(line.trimEnd().endsWith('$$')){ content+='\n'+line.trimEnd().slice(0,-2); next++; closed=true; break; }
          content+='\n'+line;
        }
        if(!closed) return false;
      }
      const token=state.push('review_math_block','math',0); token.block=true; token.content=content.trim();
      token.map=[start,next]; state.line=next; return true;
    },{alt:['paragraph','reference','blockquote','list']});
    md.renderer.rules.review_math=(tokens,index)=>math(tokens[index].content,false);
    md.renderer.rules.review_math_block=(tokens,index)=>math(tokens[index].content,true);
    const imageRule=md.renderer.rules.image;
    md.renderer.rules.image=(tokens,index,options,env,renderer)=>{
      const token=tokens[index],source=token.attrGet('src') || '';
      if(!/^\/api\/daily-review\/media\/[a-f0-9]{32}\/[a-f0-9]{32}\.png$/.test(source))
        return md.utils.escapeHtml(token.content || '첨부되지 않은 그림');
      token.attrSet('loading','lazy');
      return imageRule(tokens,index,options,env,renderer);
    };
    const linkRule=md.renderer.rules.link_open || ((tokens,index,options,env,renderer)=>renderer.renderToken(tokens,index,options));
    md.renderer.rules.link_open=(tokens,index,options,env,renderer)=>{
      tokens[index].attrSet('target','_blank'); tokens[index].attrSet('rel','noopener noreferrer');
      return linkRule(tokens,index,options,env,renderer);
    };
    return md;
  }
  if(typeof module==='object' && module.exports) module.exports={createRenderer};
  else root.ReviewMarkdown=createRenderer(root.markdownit,root.katex);
})(typeof window==='undefined'?globalThis:window);
