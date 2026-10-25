'use strict';
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto'),{createRequire}=require('node:module');
const project=path.resolve(__dirname,'../../../..');
const target=path.resolve(process.argv[2]||path.join(project,'.local/cache/workbench/ds-essay-candidate/dist'));
if(process.version!=='v24.14.0')throw Error('ESSAY_NODE_VERSION');
const ts=createRequire(path.join(project,'frontend/package.json'))('typescript');if(ts.version!=='5.9.3')throw Error('ESSAY_COMPILER_VERSION');
const source=JSON.parse(fs.readFileSync(path.join(__dirname,'source-manifest.json'),'utf8'));
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
fs.mkdirSync(target,{recursive:true});
const outputs=[];
for(const file of source.files){
 const raw=fs.readFileSync(path.join(__dirname,file.target_path));if(sha(raw)!==file.sha256)throw Error('ESSAY_SOURCE_HASH');
 const code=ts.transpileModule(raw.toString('utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;
 const name=path.basename(file.target_path,'.ts')+'.js';fs.writeFileSync(path.join(target,name),code);outputs.push({path:name,sha256:sha(Buffer.from(code)),bytes:Buffer.byteLength(code)});
}
fs.writeFileSync(path.join(target,'package.json'),'{"type":"commonjs","private":true}\n');
const program=ts.createProgram(source.files.map(f=>path.join(__dirname,f.target_path)),{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,strict:true,noEmit:true,skipLibCheck:true,types:[],lib:['lib.es2022.d.ts','lib.dom.d.ts']});
const diagnostics=ts.getPreEmitDiagnostics(program);if(diagnostics.length)throw Error(ts.formatDiagnosticsWithColorAndContext(diagnostics,{getCanonicalFileName:f=>f,getCurrentDirectory:()=>project,getNewLine:()=>"\n"}));
fs.mkdirSync(path.join(target,'notices'),{recursive:true});
for(const name of ['LICENSE.AGPL-3.0','THIRD_PARTY_NOTICES.txt','THIRD_PARTY_LICENSES.upstream.txt'])fs.copyFileSync(path.join(project,'third_party/deepstudent/notices',name),path.join(target,'notices',name));
fs.copyFileSync(path.join(__dirname,'ADAPTATION-NOTICE.txt'),path.join(target,'notices/ADAPTATION-NOTICE.txt'));
fs.writeFileSync(path.join(target,'build-manifest.json'),JSON.stringify({source_commit:source.source_commit,node:process.version,typescript:ts.version,strict_typecheck:true,sources:source.files,outputs},null,2)+'\n');
console.log(JSON.stringify({status:'built',target,files:outputs.length,strict_typecheck:true,runtime_dependencies:'local JS plus Node builtins only'}));
