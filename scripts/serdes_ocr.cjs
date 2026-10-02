/* Offline OCR only. Model and images must already exist on the local filesystem. */
'use strict';
const fs=require('node:fs'), path=require('node:path');
async function main(){
  const [input,modelDirectory]=process.argv.slice(2);
  if(!input||!modelDirectory||!fs.existsSync(input)||!fs.existsSync(path.join(modelDirectory,'eng.traineddata')))
    throw new Error('Local input image and English model are required');
  const {createWorker}=require('tesseract.js');
  const worker=await createWorker('eng',1,{langPath:path.resolve(modelDirectory),gzip:false,cacheMethod:'none'});
  try{
    await worker.setParameters({tessedit_pageseg_mode:'3',user_defined_dpi:'180'});
    const {data}=await worker.recognize(input,{}, {text:true,tsv:true});
    const groups=new Map();
    for(const row of (data.tsv||'').split('\n').slice(1)){
      const cells=row.split('\t');
      if(cells.length<12||cells[0]!=='5'||Number(cells[10])<25||!cells[11].trim())continue;
      const key=cells.slice(1,5).join(':'),l=Number(cells[6]),t=Number(cells[7]),r=l+Number(cells[8]),b=t+Number(cells[9]);
      const g=groups.get(key)||{text:[],box:[l,t,r,b]};
      g.text.push(cells.slice(11).join('\t').trim());g.box=[Math.min(g.box[0],l),Math.min(g.box[1],t),Math.max(g.box[2],r),Math.max(g.box[3],b)];groups.set(key,g);
    }
    process.stdout.write(JSON.stringify({text:data.text,lines:[...groups.values()].map(g=>({...g,text:g.text.join(' ')}))}));
  }finally{await worker.terminate();}
}
main().catch(error=>{process.stderr.write(String(error));process.exitCode=1;});
