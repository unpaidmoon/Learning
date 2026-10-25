'use strict';
// Fixed-source candidate build only. No npm install, product build, network or model calls.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),{createRequire}=require('node:module');
const project=path.resolve(__dirname,'../../../..');
const target=path.resolve(process.argv[2]||path.join(project,'.local/cache/workbench/ds-selection-translation-candidate/dist'));
if(process.version!=='v24.14.0')throw Error('CANDIDATE_NODE_VERSION');
const ts=createRequire(path.join(project,'frontend/package.json'))('typescript');
if(ts.version!=='5.9.3')throw Error('CANDIDATE_TYPESCRIPT_VERSION');
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
const sourceBase=path.join(project,'third_party/deepstudent/upstream/src/features/chat/components');
const sources=[
 ['translationNdjsonParser',path.join(sourceBase,'translationNdjsonParser.ts'),'86118e364aa9c87b8e285d0a8923e320e6e9f931437c85fa8e624ca774931b64'],
 ['translationCache',path.join(sourceBase,'translationCache.ts'),'0bbc34b989ac18f6f9fe3bb41c75fc87dde522208da5aa450b043c413e68c210'],
 ['translationTypes',path.join(sourceBase,'translationTypes.ts'),'4c1c77e6b389c7562df35d187f57be04114e8cb7c98fc7d1c974a7b43da009d8'],
 ['selectionRef',path.join(__dirname,'upstream/selectionRef.ts'),'56ddea18cf3ac459b348f5ed8edc8fc9c8e55f018114c7fd2779c58c88e35347'],
];
fs.mkdirSync(target,{recursive:true});
const manifest={source_commit:'635216503c0b34bdf812e17942e0f09f51872e6f',node:process.version,typescript:ts.version,sources:[],outputs:[],extracted:[]};
function compile(name,source){
 const result=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS},reportDiagnostics:true});
 if(result.diagnostics?.some(d=>d.category===ts.DiagnosticCategory.Error))throw Error('CANDIDATE_COMPILE');
 const file=path.join(target,name+'.cjs');fs.writeFileSync(file,result.outputText);
 manifest.outputs.push({path:path.basename(file),sha256:sha(Buffer.from(result.outputText)),bytes:Buffer.byteLength(result.outputText)});
}
for(const[name,file,expected]of sources){
 const raw=fs.readFileSync(file);if(sha(raw)!==expected)throw Error('CANDIDATE_SOURCE_HASH:'+name);
 manifest.sources.push({path:path.relative(project,file).replaceAll('\\','/'),sha256:expected,bytes:raw.length});
 if(name!=='selectionRef'){compile(name,raw.toString('utf8'));continue;}
 const source=raw.toString('utf8'),ast=ts.createSourceFile(file,source,ts.ScriptTarget.ESNext,true);
 const names=new Set(['SelectionSourceKind','SelectionRefData','MAX_SELECTION_TEXT_CHARS','describeSelectionLocator','buildSelectionDisplayName','serializeSelectionRefData']);
 const fragments=[];
 for(const node of ast.statements){
   const name=node.name?.text||(ts.isVariableStatement(node)?node.declarationList.declarations[0]?.name.getText(ast):null);
   if(!names.has(name))continue;
   const text=source.slice(node.getFullStart(),node.end);fragments.push(text);names.delete(name);
   const start=ast.getLineAndCharacterOfPosition(node.getStart(ast)).line+1,end=ast.getLineAndCharacterOfPosition(node.end).line+1;
   manifest.extracted.push({symbol:name,start_line:start,end_line:end,sha256:sha(Buffer.from(text)),modification:'none; exact source slice'});
 }
 if(names.size)throw Error('CANDIDATE_SELECTION_SYMBOLS');
 // Original function bodies call t. This narrow local dependency supplies only
 // the upstream default Chinese labels; no upstream application stores or UI.
 const labels=String.raw`const t=(_key:string,options:Record<string,unknown>,_namespace?:string):string=>String(options.defaultValue||_key).replace(/\{\{(\w+)\}\}/g,(_m,k)=>String(options[k]??''));`+'\n';
 fs.writeFileSync(path.join(target,'selectionPure.extracted.ts'),labels+fragments.join('\n'));
 compile('selectionPure',labels+fragments.join('\n'));
}
const noticeDir=path.join(target,'notices');fs.mkdirSync(noticeDir,{recursive:true});
for(const name of ['LICENSE.AGPL-3.0','THIRD_PARTY_NOTICES.txt','THIRD_PARTY_LICENSES.upstream.txt']){
 const raw=fs.readFileSync(path.join(project,'third_party/deepstudent/notices',name));fs.writeFileSync(path.join(noticeDir,name),raw);
 manifest.outputs.push({path:'notices/'+name,sha256:sha(raw),bytes:raw.length});
}
fs.writeFileSync(path.join(target,'build-manifest.json'),JSON.stringify(manifest,null,2)+'\n');
console.log(JSON.stringify({target,node:process.version,typescript:ts.version,files:manifest.outputs.length,source_commit:manifest.source_commit}));
