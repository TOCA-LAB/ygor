/* Dependency-free unit checks for rendering helpers, not a browser E2E test. */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../src/ygor/web_assets/app.js'),'utf8');
const boot = source.indexOf('$("#workspace-icon").innerHTML');
assert.ok(boot > 0);
const context = vm.createContext({Intl,URLSearchParams,setTimeout,clearTimeout,console,
  document:{querySelector(){return null;}},navigator:{},location:{hash:''}});
vm.runInContext(source.slice(0,boot)+'\nglobalThis.helpers={esc,icon,duration,size,date,yamlHighlight,progress,localPath,showPreview,runApi,download,readThemePreference};',context);
const h = context.helpers;

test('theme preference survives the rename and honors the new setting',()=>{
  context.localStorage={getItem(key){return key==='xlab-theme' ? 'dark' : null;}};
  assert.equal(h.readThemePreference(),'dark');
  context.localStorage={getItem(key){return key==='ygor-theme' ? 'light' : 'dark';}};
  assert.equal(h.readThemePreference(),'light');
  context.localStorage={getItem(){throw new Error('storage blocked');}};
  assert.equal(h.readThemePreference(),'light');
  delete context.localStorage;
});

test('untrusted text and attributes are escaped',()=>{
  assert.equal(h.esc('<script x="a">&\'</script>'),'&lt;script x=&quot;a&quot;&gt;&amp;&#39;&lt;/script&gt;');
  assert.ok(!h.yamlHighlight('command: <script>alert(1)</script>').includes('<script>'));
  assert.ok(h.yamlHighlight('  name: example').includes('yaml-key'));
  assert.ok(h.yamlHighlight('# comment').includes('yaml-comment'));
});
test('number, date, time and file size formatting handles empty data',()=>{
  assert.equal(h.duration(null),'—'); assert.equal(h.date(null),'—');
  assert.equal(h.date('not a date'),'—');
  assert.match(h.duration(90),/min/); assert.match(h.duration(7200),/h/);
  assert.match(h.duration(200000),/d/); assert.match(h.size(1048576),/MB/);
});
test('run and file URLs cannot inject query arguments',()=>{
  assert.equal(h.runApi('a/b','recipe'),'/api/runs/a%2Fb/recipe');
  assert.ok(h.download('abc','outputs/a&path=/etc/passwd').endsWith('outputs%2Fa%26path%3D%2Fetc%2Fpasswd'));
});
test('progress and legacy absolute artifact paths',()=>{
  assert.ok(!h.progress({},0).includes('NaN'));
  assert.ok(h.progress({completed:3,failed:1},4).includes('width="75"'));
  assert.equal(h.localPath('/old/store/runs/hash/outputs/exec_0001','fallback'),'outputs/exec_0001');
  assert.equal(h.localPath('/etc/passwd','fallback'),'fallback');
});
function preview(file){const el={innerHTML:'',querySelector(){return null;}};h.showPreview('abc',file,el);return el.innerHTML;}
test('output rendering distinguishes empty and incomplete algorithm output',()=>{
  assert.match(preview({name:'result.json',path:'outputs/result.json',size:0,empty:true,text:''}),/vazio/);
  assert.match(preview({name:'result.json',path:'outputs/result.json',size:20,json:{completed:false,value:3},text:'{}'}),/completed: false/);
});
test('HTML and SVG outputs remain inert text',()=>{
  const html=preview({name:'<img onerror=alert(1)>',path:'outputs/result.html',size:40,text:'<script>alert(1)</script>'});
  assert.ok(!html.includes('<script>'));assert.ok(!html.includes('<img onerror='));
  assert.ok(html.includes('&lt;script&gt;'));
});
test('binary and truncated output previews are explicit',()=>{
  assert.match(preview({name:'data.bin',path:'outputs/data.bin',size:20,binary:true,text:''}),/Arquivo binário/);
  assert.match(preview({name:'large.log',path:'logs/large.log',size:900000,truncated:true,tail:true,text:'end'}),/512 KB finais/);
});
