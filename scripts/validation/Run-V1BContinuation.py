"""Autonomous V1 stop/follow-up validation under an explicitly saved user grant.

The old six-attempt ledger remains immutable. New attempts have a separate,
unlimited but exclusively owned and durable counter; all input guards remain.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys

REPO=Path(__file__).resolve().parents[2]
PROBE=REPO/'research/probes/v1-harness'
sys.path.insert(0,str(PROBE))
from runner import Host,Peer,save,source_digests,load_b_key
from fixture import BoundaryServer
from b_cases import execute_case


async def main(args):
    grant=json.loads(Path(args.authorization).read_text(encoding='utf-8'))
    if (grant.get('enabled') is not True or grant.get('scope')!='V1'
            or grant.get('real_attempt_limit','missing') is not None
            or not grant.get('message_reference') or grant.get('require_per_batch_approval') is not False
            or grant.get('model')!='deepseek-flash' or grant.get('reasoning_effort')!='high'
            or grant.get('max_tokens')!=8192 or grant.get('observation_seconds')!=180):
        raise ValueError('AUTONOMOUS_V1_GRANT_INVALID')
    source=REPO/'.runtime/validation/v1-b-stop-retest-real-20261021'
    prior=json.loads((source/'private/raw-case-results/V1-B05-retest.json').read_text(encoding='utf-8'))
    assert prior['active_cancel_proven'] and prior['terminal_reason']['kind']=='aborted'
    gate=json.loads((REPO/'.runtime/validation/v1-a-final-20261021-r3/a-gate.json').read_text(encoding='utf-8'))
    current=source_digests()
    changed={k for k,v in current.items() if gate['source_digests'].get(k)!=v}
    assert changed=={'b_cases.py','test_b_cases.py','fixture.py','test_fixture.py'}
    assert len(gate['cases'])==12 and all(c['status']=='passed' for c in gate['cases'])
    if not args.fixture_check:
        local=json.loads((REPO/'.runtime/validation/v1-b-autonomous-local/results.json').read_text(encoding='utf-8'))
        assert local['source_digests']==current and local['real_attempts']==0
        assert local['cases'][0]['active_cancel_proven'] and local['cases'][1]['transport_status']=='passed'
    directory=REPO/'.runtime/validation'/args.run_id
    if directory.exists():raise ValueError('REFUSE_EXISTING_RUN')
    directory.mkdir(parents=True)
    boundary=None;host=None;peer=None;rows=[]
    try:
        if args.fixture_check:
            boundary=BoundaryServer(directory/'boundary',mode='A',limit=0)
        else:
            boundary=BoundaryServer(REPO/'.runtime/validation/v1-b-autonomous-2026-10-21',mode='B',limit=None,
                                    key=load_b_key(),authorization_ref=grant['message_reference'])
        boundary.start()
        host=Host(directory/'host',boundary.url,local_token=boundary.local_token)
        shutil.copytree(source/'host/home',host.home,dirs_exist_ok=True)
        host.env['V1_STAGE']='B'
        await asyncio.to_thread(host.start)
        peer=await Peer(host).connect()
        stream=await peer.follow('v1-b-c1')
        baseline=peer.streams[stream][0]['wire']['value']
        assert baseline['header']['id']=='v1-b-c1'
        assert not baseline['assistantStream'].get('activeAttempt')
        inbox=baseline['projections']['values']['inbox']
        assert not inbox['next-turn'] and not inbox['next-step']
        save(directory/'private/reconciled-baseline.json',baseline)
        save(directory/'preflight.json',{'authorization':grant,'source_digests':current,
            'coordinator_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'A_gate_ref':'.runtime/validation/v1-a-final-20261021-r3/a-gate.json','B_specific_delta':sorted(changed),
            'binding_epoch':3,'planned_cases':['V1-B05-proxy-fixed','V1-B06'],'fixture_check':args.fixture_check,
            'attempt_limit':0 if args.fixture_check else None,'source_run':source.name})
        for cid,text,cancel in [
            ('V1-B05-proxy-fixed','请按更正后的题目详细推导，展开说明泰勒余项、洛必达和两种方法的适用条件，再比较步骤。',True),
            ('V1-B06','按更正后的分母解释泰勒展开为什么可用，并给出最终极限。',False),
        ]:
            if args.fixture_check:
                boundary.set_scenario(thinking_chunks=['Restored genuine reasoning activity.'] if cancel else [],
                                      text_chunks=[] if cancel else ['Corrected denominator retained; fixture follow-up.'],
                                      delay=.12,hold_seconds=10 if cancel else 0)
            row=await execute_case(peer,boundary,'v1-b-c1',stream,text,None,cancel,cid)
            row.update(binding_epoch=3,fixture_check=args.fixture_check)
            rows.append(row);save(directory/'cases'/(cid+'.json'),row)
            save(directory/'private/peer-frames.json',peer.frames)
            print(json.dumps({'case':cid,'transport':row['transport_status'],'stop_group':row['stop_group'],
                'active_cancel_proven':row['active_cancel_proven'],'attempts_in_autonomous_scope':boundary.actual_outbound_attempts}),flush=True)
            if row['stop_group'] or (cancel and not row['active_cancel_proven']):break
    finally:
        if peer:save(directory/'private/peer-frames.json',peer.frames);await peer.close()
        if host:host.stop()
        if boundary:boundary.stop();save(directory/'boundary-records.json',boundary.records)
        save(directory/'results.json',{'cases':rows,'source_digests':current,'real_attempts':boundary.actual_outbound_attempts if boundary else 0,
             'attempt_limit':0 if args.fixture_check else None,'authorization_ref':grant['message_reference'],
             'cleanup':host.cleanup if host else None})


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--authorization',required=True)
    parser.add_argument('--fixture-check',action='store_true')
    asyncio.run(main(parser.parse_args()))
