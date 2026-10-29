const {test}=require('node:test');
const assert=require('node:assert/strict');
const {createRenderer}=require('../web/static/review_markdown.js');
const markdownit=require('../web/static/vendor/markdown-it/markdown-it.min.js');
const katex=require('../web/static/vendor/katex/katex.min.js');
const render=text=>createRenderer(markdownit,katex).render(text);

test('renders inline and block LaTeX including quoted Obsidian review blocks',()=>{
  const html=render('gain $A_v$\n\n$$\nH(s)=\\frac{V_o}{V_i}\n$$\n\n> $$\\alpha+\\beta$$');
  assert.match(html,/class="katex"/);
  assert.equal((html.match(/class="katex-display"/g)||[]).length,2);
  assert.match(html,/MathML/);
});
test('renders saved review image URLs and blocks arbitrary HTML or external images',()=>{
  const source='/api/daily-review/media/'+'a'.repeat(32)+'/'+'b'.repeat(32)+'.png';
  const html=render(`![그림](${source})\n\n<script>alert(1)</script>\n\n![remote](https://example.test/track.png)`);
  assert.match(html,new RegExp('src="'+source+'"'));
  assert.doesNotMatch(html,/<script>/);
  assert.doesNotMatch(html,/src="https:/);
  assert.doesNotMatch(render('$\\href{javascript:alert(1)}{x}$'),/href="javascript:/);
});
test('hides only the recognized imported-annotation marker and keeps normal comments literal',()=>{
  const marker='<!-- pdf-review:'+'a'.repeat(24)+' -->';
  assert.doesNotMatch(render(marker+'\n\n> Highlighted text'),/pdf-review/);
  assert.match(render('<!-- any untrusted comment -->'),/&lt;!--/);
  assert.match(render('`$not_math$`'),/<code>\$not_math\$<\/code>/);
});
