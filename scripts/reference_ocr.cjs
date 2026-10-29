/* Offline OCR. Explicit local language data; one worker serves the selected pages. */
'use strict';
const fs = require('node:fs');
const path = require('node:path');

async function main() {
  const request = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  if (!fs.existsSync(path.join(request.modelDirectory, 'eng.traineddata')))
    throw new Error('Local English OCR model is missing');
  const { createWorker } = require('tesseract.js');
  const worker = await createWorker('eng', 1, {
    langPath: path.resolve(request.modelDirectory), gzip: false, cacheMethod: 'none'
  });
  try {
    await worker.setParameters({tessedit_pageseg_mode: '3', user_defined_dpi: '216'});
    for (const page of request.pages) {
      const started = Date.now();
      const { data } = await worker.recognize(page.image, {}, {text: true, tsv: true});
      const lines = new Map();
      let words = 0, lowConfidenceWords = 0;
      for (const row of (data.tsv || '').split('\n').slice(1)) {
        const cells = row.split('\t');
        if (cells.length < 12 || cells[0] !== '5' || !cells.slice(11).join('\t').trim()) continue;
        const confidence = Number(cells[10]);
        if (confidence < 0) continue;
        words++; if (confidence < 60) lowConfidenceWords++;
        const key = cells.slice(1, 5).join(':'), l = Number(cells[6]), t = Number(cells[7]);
        const r = l + Number(cells[8]), b = t + Number(cells[9]);
        const line = lines.get(key) || {words: [], bbox: [l, t, r, b]};
        line.words.push({text: cells.slice(11).join('\t'), confidence, bbox: [l, t, r, b]});
        line.bbox = [Math.min(line.bbox[0], l), Math.min(line.bbox[1], t),
                     Math.max(line.bbox[2], r), Math.max(line.bbox[3], b)];
        lines.set(key, line);
      }
      const recognizedLines = [...lines.values()].map(line => ({...line, text: line.words.map(w => w.text).join(' ')}));
      const labelRepairs = [];
      // A narrow ordinal gutter is sometimes segmented as its own column.
      // Re-read only visibly numeric label crops; never fill numbers from a sequence.
      for (const line of recognizedLines) {
        if (!/^\d{1,3}[%?]$/.test(line.text.trim())) continue;
        const [l, t, r, b] = line.bbox;
        const neighbor = recognizedLines.find(other => other !== line && other.bbox[0] > r &&
          other.bbox[0] - r < (b - t) * 3 && Math.abs(other.bbox[1] - t) < (b - t) * .5);
        if (!neighbor) continue;
        await worker.setParameters({tessedit_pageseg_mode: '7', tessedit_char_whitelist: ''});
        const top = Math.min(t, neighbor.bbox[1]), bottom = Math.max(b, neighbor.bbox[3]);
        const {data: retry} = await worker.recognize(page.image, {rectangle: {
          left: Math.max(0, l - 5), top: Math.max(0, top - 5),
          width: neighbor.bbox[2] - l + 10, height: bottom - top + 10
        }}, {text: true});
        const match = (retry.text || '').trim().match(/^(\d{1,3}\.)\s+\S/);
        const accepted = Boolean(match && retry.confidence >= 60);
        labelRepairs.push({bbox: line.bbox, before: line.text, crop_text: retry.text,
                          after: accepted ? match[1] : line.text, confidence: retry.confidence, accepted});
        if (accepted) {
          line.text = match[1];
        }
        await worker.setParameters({tessedit_pageseg_mode: '3', tessedit_char_whitelist: ''});
      }
      const result = {page: page.page, text: data.text || '', confidence: data.confidence,
        words, low_confidence_words: lowConfidenceWords,
        lines: recognizedLines, label_repairs: labelRepairs,
        seconds: (Date.now() - started) / 1000};
      fs.writeFileSync(page.output, JSON.stringify(result), 'utf8');
      process.stdout.write(JSON.stringify({stage: 'ocr_page', page: page.page, characters: result.text.length}) + '\n');
    }
  } finally { await worker.terminate(); }
}
main().catch(error => {process.stderr.write(String(error)); process.exitCode = 1;});
