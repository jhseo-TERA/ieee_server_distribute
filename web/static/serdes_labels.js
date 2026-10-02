/* Presentation only: keep normalized database enums unchanged. */
const SerdesLabels = Object.freeze({
  component(value) {
    const key=String(value||"").trim().toLowerCase();
    const labels={tx:"TX",rx:"RX",trx:"TRX",tx_rx:"TX / RX",rx_tx:"RX / TX",full_link:"Full link",driver_only:"Driver only",unknown:"Unknown"};
    return labels[key] || key.split(/[_\s]+/).map(token=>/^(tx|rx|trx|cdr|pll|dsp|ffe|dfe|adc|dac)$/.test(token)?token.toUpperCase():token).join(" ");
  },
  modulation(value) {
    const key=String(value||"").trim();
    if(!key||key.toLowerCase()==="unknown")return "Unknown";
    return key.toUpperCase().replace(/\bPAM[-_\s]?([0-9]+)\b/g,"PAM-$1");
  },
  berScope(value) {
    return String(value||"").trim().replaceAll("_","-").replace(/fec/gi,"FEC");
  },
  citationSource(value) {
    return ({ieee:"IEEE Xplore",crossref:"Crossref",openalex:"OpenAlex"})[String(value||"").toLowerCase()] || String(value||"출처 미확인");
  }
});
if(typeof module!=="undefined"&&module.exports)module.exports=SerdesLabels;
