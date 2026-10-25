/** Extract original bodies, not the socket/React application. */
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import crypto from 'node:crypto';
const here=path.dirname(fileURLToPath(import.meta.url));
const repo=path.resolve(here,'../../../..');
const require=createRequire(path.join(repo,'frontend/package.json'));
const ts=require('typescript');
if(ts.version!=='5.9.3')throw new Error('UNEXPECTED_TYPESCRIPT');
const cursorSource=fs.readFileSync(path.join(here,'upstream/web/features/chat/transport/TurnRuntimeClient.ts'),'utf8');
const cursorTree=ts.createSourceFile('cursor.ts',cursorSource,ts.ScriptTarget.Latest,true);
const cursorClass=cursorTree.statements.find(n=>ts.isClassDeclaration(n)&&n.name.text==='TurnRuntimeClient');
const names=['setResumeCursor','acceptStreamEvent','emitInOrder'];
const methods=names.map(name=>{const node=cursorClass.members.find(n=>n.name?.getText(cursorTree)===name);if(!node)throw new Error(name);return node.getText(cursorTree);});
const cursor=`/** Original cursor bodies, probe-only shell: no socket or command replay. */
type StreamEvent={type:string;turn_id?:string;seq?:number;[key:string]:unknown};
export class ReplayCursorProbe {
 turnId:string|null=null;lastSeq=0;buffered=new Map<number,StreamEvent>();terminalObserved=false;
 pending:Array<{requiresAck:boolean;acknowledgedAfter:number}>=[];
 events:StreamEvent[]=[];diagnostics:string[]=[];probeDelays:number[]=[];replayRequests=0;clears=0;
 options={maxBufferedGap:32,replayProbeDelayMs:1000,onEvent:(e:StreamEvent)=>this.events.push(e),onDiagnostic:(s:string)=>this.diagnostics.push(s)};
 clearReplayProbe(){this.clears++;} scheduleReplayProbe(delay=1000){this.probeDelays.push(delay);} requestReplay(){this.replayRequests++;}
 feed(event:StreamEvent){this.acceptStreamEvent(event);}
 ${methods.join('\n')}
}
`;
fs.writeFileSync(path.join(here,'cursor_probe.ts'),cursor);
const scrollSource=fs.readFileSync(path.join(here,'upstream/web/hooks/useChatAutoScroll.ts'),'utf8');
const scrollTree=ts.createSourceFile('scroll.ts',scrollSource,ts.ScriptTarget.Latest,true);
let handle;
function visit(node){if(ts.isVariableDeclaration(node)&&node.name.getText(scrollTree)==='handleScroll')handle=node.initializer.arguments[0];ts.forEachChild(node,visit);}
visit(scrollTree);
if(!handle)throw new Error('SCROLL_HANDLER_NOT_FOUND');
const arrow=handle.getText(scrollTree);
fs.writeFileSync(path.join(here,'scroll_probe.ts'),`/** Original handleScroll body; numeric layout port, no React/DOM observers. */
export function retainedHandleScroll(previous:{top:number;height:number;viewport:number},layout:{scrollTop:number;scrollHeight:number;clientHeight:number},following:boolean) {
 const containerRef={current:layout};const positionRef={current:{...previous}};const shouldAutoScrollRef={current:following};
 const rememberPosition=(c:typeof layout)=>{positionRef.current={top:c.scrollTop,height:c.scrollHeight,viewport:c.clientHeight};};
 const handle=${arrow};handle();return {following:shouldAutoScrollRef.current,remembered:positionRef.current};
}
`);
const adapterSource=fs.readFileSync(path.join(here,'project-snapshots/frontend/src/reading/pdfAdapter.ts'),'utf8');
const adapterTree=ts.createSourceFile('adapter.ts',adapterSource,ts.ScriptTarget.Latest,true);
const adapterClass=adapterTree.statements.find(n=>ts.isClassDeclaration(n)&&n.name.text==='PdfReaderAdapter');
const jumpMethods=['jump','applyJump'].map(name=>adapterClass.members.find(n=>n.name?.getText(adapterTree)===name).getText(adapterTree));
fs.writeFileSync(path.join(here,'project_jump_probe.ts'),`// Readonly project method probe.\ntype SourceJump=any;\nexport class JumpProbe {file:any;application:any;pendingJump:any=null;disposed=false;highlight:any=null;highlightTimer=0;constructor(file:any,application:any){this.file=file;this.application=application;}\n${jumpMethods.join('\n')}\n}`);
const hash=s=>crypto.createHash('sha256').update(s).digest('hex');
fs.writeFileSync(path.join(here,'ts-extraction.json'),JSON.stringify({typescript:ts.version,original_cursor_methods:names,original_handleScroll_sha256:hash(arrow),
 target_files:['cursor_probe.ts','scroll_probe.ts','project_jump_probe.ts'].map(name=>({path:name,sha256:hash(fs.readFileSync(path.join(here,name)))})),
 changes:['Probe shells supply state/layout ports only; extracted bodies unchanged.','No socket, command resend, React hook or PDF application runtime copied.']},null,2));
console.log(JSON.stringify({original_cursor_methods:names.length,scroll_arrow_retained:true,project_jump_methods:jumpMethods.length}));
