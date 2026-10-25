"""Archive exact fixed pure TS sources. No upstream application imports."""
from pathlib import Path
import hashlib,json,subprocess
project=Path(__file__).resolve().parents[4]
here=Path(__file__).resolve().parent
source=project/'.local/cache/reuse/deepstudent'
commit='635216503c0b34bdf812e17942e0f09f51872e6f'
names=['types.ts','markerTypes.ts','streamingMarkerParser.ts','markerParser.ts','suggestionAnchors.ts','textStats.ts','essayContentState.ts']
records=[]
for name in names:
    relative='src/essay-grading/'+name
    data=(source/relative).read_bytes()
    blob=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
    expected=subprocess.check_output(['git','-C',str(source),'rev-parse',commit+':'+relative],text=True,creationflags=subprocess.CREATE_NO_WINDOW).strip()
    assert blob==expected
    dest=here/'upstream'/name;dest.parent.mkdir(parents=True,exist_ok=True)
    if dest.exists():assert dest.read_bytes()==data
    dest.write_bytes(data)
    records.append({'source_path':relative,'target_path':'upstream/'+name,'sha256':hashlib.sha256(data).hexdigest(),'git_blob_sha1':blob,'bytes':len(data),'modification':'none'})
(here/'source-manifest.json').write_text(json.dumps({'source_commit':commit,'files':records},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print('archived7 fixed pure TypeScript originals')
