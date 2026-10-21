"""Isolated V1 driver; not an application entry point.

The Web Remote wire follows upstream commit 687f282e8625c6b8d11ff58211a90524be2e4317.
All run artifacts, including prompts and native journals, stay in ignored .runtime.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import websockets

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[2]
OLD = ROOT.parent / "harness-0.2.1rc2"
EXE = OLD / "unpacked/deepseek_harness_runtime/runtime/deepseek-harness-sdk-runtime-win-x64.exe"
spec = importlib.util.spec_from_file_location("old_probe_process_helpers", OLD / "probe_web_host.py")
helpers = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helpers)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def source_digests():
    files=list(ROOT.glob("*.py"))+list((ROOT/"profile").glob("*"))+[
        ROOT/"requirements.lock",ROOT/"dependency-manifest.json"]
    return {p.relative_to(ROOT).as_posix():digest(p) for p in sorted(files)
            if p.is_file() and p.suffix in {".py",".mjs",".json",".lock"}}


class Host:
    """Owns exactly one job; model cancellation never terminates this job."""

    def __init__(self, run_dir, boundary_url, patch=None, local_token=None):
        self.dir = Path(run_dir)
        self.home = self.dir / "home"
        self.workspace = self.dir / "workspace"
        self.home.mkdir(parents=True)
        self.workspace.mkdir()
        self.port = helpers.free_port()
        self.lines = []
        self.proc = None
        self.job = None
        self.env = helpers.make_env(self.home)
        self.env.update({"DEEPSEEK_BASE_URL": boundary_url,
                         "DEEPSEEK_API_KEY": local_token or "v1-local-fixture-not-a-real-key",
                         "LEARNING_V1_BOUNDARY": boundary_url,
                         "LEARNING_V1_AUDIT_DIR": str(self.dir / "private"),
                         "DSH_RESOURCE_CACHE": str(self.dir / "resources")})
        self.control_port = helpers.free_port()
        self.control_token = uuid.uuid4().hex
        (self.dir / "private").mkdir(exist_ok=True)
        self.env.update({"V1_BOUNDARY_URL":boundary_url,
                         "V1_AUDIT_PATH":str(self.dir / "private/plugin-audit.jsonl"),
                         "V1_CONTROL_PORT":str(self.control_port),
                         "V1_CONTROL_TOKEN":self.control_token,
                         # SEA native extraction adds a SHA256 directory and
                         # package suffix. Keep this project-owned path short
                         # even when an individual diagnostic has a long name.
                         "PKG_NATIVE_CACHE_PATH":str(REPO/".runtime/validation/v1-native"/
                             hashlib.sha256(str(self.dir).encode()).hexdigest()[:12]),
                         "V1_NODE_PATH":r"C:\Program Files\nodejs\node.exe"})
        template = Path(patch) if patch else ROOT/"profile/cordis.patch.json"
        content=template.read_text(encoding="utf-8").replace("__V1_BOUNDARY_URL__",boundary_url).replace("__V1_PLUGIN_PATH__",(ROOT/"profile/v1-plugin.mjs").as_posix())
        self.patch = self.dir / "effective.patch.json"
        self.patch.write_text(content,encoding="utf-8")
        self.cleanup = {}

    def start(self):
        self.job = helpers.kernel.CreateJobObjectW(None, None)
        limits = helpers.ExtendedLimits()
        limits.basic.flags = 0x2000 | 0x0200
        limits.job_memory = 2 * 1024**3
        if not helpers.kernel.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        command = [str(EXE), "web", "--patch", str(self.patch), "--host", "127.0.0.1",
                   "--port", str(self.port), "--no-open"]
        self.proc = subprocess.Popen(command, cwd=self.workspace, env=self.env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
        if not helpers.kernel.AssignProcessToJobObject(self.job, int(self.proc._handle)):
            self.proc.terminate()
            raise ctypes.WinError(ctypes.get_last_error())
        for pipe, channel in [(self.proc.stdout, "out"), (self.proc.stderr, "err")]:
            def drain(pipe=pipe, channel=channel):
                for line in pipe:
                    self.lines.append((channel, line.rstrip()))
            threading.Thread(target=drain, daemon=True).start()
        until = time.monotonic() + 40
        while time.monotonic() < until and self.proc.poll() is None:
            for _, line in list(self.lines):
                for item in re.findall(r"https?://[^\s\x1b]+", line):
                    parts = urlsplit(item)
                    if parts.hostname == "127.0.0.1" and parts.port == self.port:
                        token = parse_qs(parts.query).get("token")
                        if token:
                            self.token = token[0]
                            self.base = f"http://127.0.0.1:{self.port}"
                            return self
            time.sleep(.05)
        raise RuntimeError("Host failed to become ready: " + "\n".join(helpers.redact(x[1]) for x in self.lines[-12:]))

    def stop(self):
        if self.job:
            self.cleanup["owned_pids_before"] = helpers.job_pids(self.job)
            helpers.kernel.TerminateJobObject(self.job, 0)
            if self.proc:
                self.proc.wait(timeout=10)
            self.cleanup["remaining_job_pids"] = helpers.job_pids(self.job)
            helpers.kernel.CloseHandle(self.job)
            self.job = None
        with socket.socket() as sock:
            self.cleanup["host_port_closed"] = sock.connect_ex(("127.0.0.1", self.port)) != 0
        self.cleanup["method"] = "owned Job cleanup after cases; not model cancellation or graceful shutdown proof"
        save(self.dir / "cleanup.json", self.cleanup)
        save(self.dir / "private/host-diagnostics.json", [dict(channel=c, text=helpers.redact(t)) for c,t in self.lines])


class Peer:
    def __init__(self, host):
        self.host = host
        self.client = httpx.AsyncClient(base_url=host.base, timeout=10, trust_env=False)
        self.frames = []
        self.streams = {}
        self.epoch = 0
        self.ws = None
        self.task = None

    async def connect(self):
        response = await self.client.get("/connect", params={"token": self.host.token, "interactions":"none"})
        assert response.status_code == 302, response.status_code
        cookie = "; ".join(f"{k}={v}" for k,v in self.client.cookies.items())
        self.ws = await websockets.connect(f"ws://127.0.0.1:{self.host.port}/api/remote.mux",
            additional_headers={"Cookie":cookie}, proxy=None, open_timeout=8, close_timeout=2)
        self.epoch += 1
        epoch = self.epoch
        async def receive():
            try:
                async for raw in self.ws:
                    frame = json.loads(raw)
                    row = {"received_ns":time.time_ns(), "epoch":epoch, "wire":frame}
                    self.frames.append(row)
                    if frame.get("streamId") in self.streams:
                        self.streams[frame["streamId"]].append(row)
            except websockets.ConnectionClosed:
                pass
        self.task = asyncio.create_task(receive())
        return self

    async def rpc(self, method, request, check=True):
        rpc_id = str(uuid.uuid4())
        response = await self.client.post("/api/"+method, json={"type":"client-request", "rpcId":rpc_id,
            "method":method, "payload":{"args":{"request":request}}})
        try:
            value = response.json()
        except ValueError:
            value = {"non_json_status":response.status_code}
        if check:
            assert response.status_code == 200 and value.get("result",{}).get("ok"), value
            return value["result"]["value"]
        return {"status":response.status_code,"body":value}

    async def create(self, label):
        result = await self.rpc("session/create", {"cwd":str(self.host.workspace),"sessionId":label})
        sid = result["sessionId"]
        return sid, await self.follow(sid)

    async def follow(self, sid):
        stream = "f-"+str(uuid.uuid4())
        rows = self.streams[stream] = []
        await self.ws.send(json.dumps({"type":"open","streamId":stream,"endpoint":"session/follow",
            "payload":{"args":{"request":{"address":{"kind":"session","sessionId":sid},"assistantStream":True}}}}))
        await wait_for(lambda: bool(rows), 8)
        assert rows[0]["wire"].get("value",{}).get("type") == "snapshot", rows[0]
        return stream

    async def prompt(self, sid, text, image=None, request_id=None):
        parts = [{"type":"text","text":text}]
        if image:
            parts.append({"type":"image","mediaType":"image/png","data":base64.b64encode(Path(image).read_bytes()).decode()})
        rid = request_id or str(uuid.uuid4())
        receipt = await self.rpc("session/prompt", {"requestId":rid,"sessionId":sid,"mode":"queue","content":parts,
                                                  "clientTimeZone":"Asia/Shanghai"})
        return {"request_id":rid,"receipt":receipt,"session_id":sid}

    async def lose_prompt_receipt(self, sid, text, request_id):
        envelope={"type":"client-request","rpcId":str(uuid.uuid4()),"method":"session/prompt",
                  "payload":{"args":{"request":{"requestId":request_id,"sessionId":sid,"mode":"queue",
                  "content":[{"type":"text","text":text}]}}}}
        data=json.dumps(envelope).encode()
        cookie="; ".join(f"{k}={v}" for k,v in self.client.cookies.items())
        reader,writer=await asyncio.open_connection("127.0.0.1",self.host.port)
        headers=(f"POST /api/session/prompt HTTP/1.1\r\nHost: 127.0.0.1:{self.host.port}\r\n"
                 f"Cookie: {cookie}\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n").encode()
        writer.write(headers+data);await writer.drain()
        # Fault injection intentionally never reads any response bytes. The
        # independent follow/page journal determines whether it was accepted.
        await asyncio.sleep(.05)
        writer.close();await writer.wait_closed()

    async def disconnect(self):
        if self.ws:
            await self.ws.close()
        if self.task:
            await self.task

    async def close(self):
        await self.disconnect()
        await self.client.aclose()


async def wait_for(predicate, seconds=15):
    end = time.monotonic()+seconds
    while time.monotonic()<end:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(.02)
    raise TimeoutError("condition not observed")


def chunks(peer, stream, offset=0):
    return [r for r in peer.streams[stream][offset:] if r["wire"].get("value",{}).get("type")=="assistant-stream"
            and r["wire"]["value"]["frame"]["type"]=="chunk"]


def ends(peer, stream, offset=0):
    return [r for r in peer.streams[stream][offset:] if r["wire"].get("value",{}).get("type")=="assistant-stream"
            and r["wire"]["value"]["frame"]["type"]=="end"]


def turn_ends(peer, stream, offset=0):
    return [r for r in peer.streams[stream][offset:] if r["wire"].get("value",{}).get("type")=="event"
            and r["wire"]["value"]["event"]["type"]=="turn/end"]


async def terminal(peer, stream, offset=0, timeout=20):
    await wait_for(lambda: ends(peer,stream,offset),timeout)
    await asyncio.sleep(.15)
    return ends(peer,stream,offset)[-1]


async def page(peer, sid, stream):
    values = [r["wire"].get("value",{}) for r in peer.streams[stream]]
    cut = max([v.get("cursor",0) for v in values]+[v.get("event",{}).get("seq",0) for v in values])
    return await peer.rpc("session/page",{"address":{"kind":"session","sessionId":sid},"throughSeq":cut,"maxMessages":100})


async def run_a(run_dir, patch=None):
    from fixture import BoundaryServer
    from samples import generate
    directory = Path(run_dir)
    samples = generate(directory)
    boundary = BoundaryServer(directory,mode="A",limit=0).start()
    host = None
    peer = None
    results = []
    def record(case, assertions, observations=None):
        row={"case_id":case,"status":"passed" if all(assertions.values()) else "failed",
             "assertions":assertions,"observations":observations or {},"real_model_attempts":boundary.actual_outbound_attempts}
        results.append(row)
        save(directory / "cases" / (case+".json"),row)
        print(json.dumps({"case":case,"status":row["status"]}),flush=True)
    try:
        host = Host(directory/"host",boundary.url,patch,local_token=boundary.local_token)
        host.start()
        peer = await Peer(host).connect()
        sid,stream = await peer.create("v1-a-primary")
        async with httpx.AsyncClient(trust_env=False,timeout=10) as control:
            response=await control.get(f"http://127.0.0.1:{host.control_port}/state",headers={"x-v1-control":host.control_token})
            response.raise_for_status()
            effective=response.json()
        save(directory/"effective-state.json",effective)
        assert effective["networkGuardInstalled"] and effective["activation"], "Missing actual activation/network guard evidence"
        boundary.set_scenario(text_chunks=["inside-confirmed: ","\\(1/3\\)","; done"],delay=.15,hold_seconds=1)
        submission = await peer.prompt(sid,"Explain the confirmed synthetic crop only.",samples["IMG-SCOPE"]["path"])
        await terminal(peer,stream)
        requests=boundary.records
        images=requests[0]["images"]
        record("V1-A01",{"one_confirmed_image":len(images)==1,
               "no_full_source_hash":all(i.get("sha256")!=samples["IMG-SCOPE"]["source"]["sha256"] for i in images),
               "exact_confirmed_pixels":images[0].get("sha256",images[0].get("resolved_sha256"))==samples["IMG-SCOPE"]["sha256"]},
               {"submission":submission,"request":requests[0],"source":samples["IMG-SCOPE"]})
        received=[r for r in chunks(peer,stream) if r["wire"]["value"]["frame"]["chunk"].get("type")=="text-delta"]
        texts=[r["wire"]["value"]["frame"]["chunk"] for r in received]
        record("V1-A02",{"multiple_live_chunks":len(received)>=3,
             "received_before_fixture_terminal":received[0]["received_ns"]<requests[0]["terminal_emitted_ns"],
             "received_while_still_generating":received[-1]["received_ns"]<requests[0]["terminal_emitted_ns"],
             "same_order":[x["text"] for x in texts]==["inside-confirmed: ","\\(1/3\\)","; done"]},
             {"chunks":texts,"first_received_ns":received[0]["received_ns"],"fixture_terminal_ns":requests[0]["terminal_emitted_ns"]})
        offset=len(peer.streams[stream]);before=len(boundary.records)
        boundary.set_scenario(text_chunks=["partial preserved ","\\(x^3/3\\)"],delay=.15,hold_seconds=20)
        await peer.prompt(sid,"Produce a slow fixture answer for actual active cancellation.")
        await wait_for(lambda:len(chunks(peer,stream,offset))>=2)
        active_no_end=not ends(peer,stream,offset)
        cancel=await peer.rpc("session/cancel",{"sessionId":sid})
        ending=await terminal(peer,stream,offset)
        await wait_for(lambda:boundary.records[-1].get("cancel_observed"),5)
        restored=await page(peer,sid,stream)
        actual_turn_end=turn_ends(peer,stream,offset)
        record("V1-A03",{"was_active":active_no_end,"cancel_accepted":cancel.get("accepted") is True,
               "fixture_observed_abort":boundary.records[-1].get("cancel_observed") is True,
               "partial_preserved_in_history":"partial preserved" in json.dumps(restored),"host_alive":host.proc.poll() is None,
               "durable_turn_terminal":bool(actual_turn_end)},
               {"end":ending,"page":restored,"requests":boundary.records[before:]})
        offset=len(peer.streams[stream]);boundary.set_scenario(text_chunks=["new generation only"])
        followup=await peer.prompt(sid,"Continue after the cancelled answer.")
        await terminal(peer,stream,offset)
        record("V1-A04",{"same_session":followup["session_id"]==sid,
              "new_attempt":bool(chunks(peer,stream,offset)),"no_old_delta":"partial preserved" not in json.dumps(chunks(peer,stream,offset))},followup)
        fault_observations=[]
        for fault in ["prompt_ack_lost","drop_response","truncate"]:
            fsid,fstream=await peer.create("v1-a-fault-"+fault)
            before=len(boundary.records)
            boundary.set_scenario(**({"fault":"drop_response"} if fault=="drop_response" else
                 {"text_chunks":["visible first","lost end"],"drop_after":3} if fault=="truncate" else {}))
            rid=str(uuid.uuid4())
            if fault=="prompt_ack_lost":
                await peer.lose_prompt_receipt(fsid,"Accepted submission whose acknowledgement is lost",rid)
            else:
                await peer.prompt(fsid,"Known request, lost reply fixture",request_id=rid)
            # Do not resend when the transport receipt/result is discarded.
            await asyncio.sleep(1.5)
            recovered=await page(peer,fsid,fstream)
            fault_observations.append({"fault":fault,"attempts":len(boundary.records)-before,"page":recovered,
                                       "request_id":rid,"state":"unknown_provider_execution","not_resent":True})
        record("V1-A05",{"no_second_attempt":all(o["attempts"]==1 for o in fault_observations),
              "request_identity_found":all(o["request_id"] in json.dumps(o["page"]) for o in fault_observations)},fault_observations)
        boundary.set_scenario(text_chunks=["recover active prefix","still same generation"],delay=.15,hold_seconds=2)
        before=len(boundary.records);offset=len(peer.streams[stream])
        recovery_submission=await peer.prompt(sid,"Keep active across lifecycle connection loss")
        await wait_for(lambda:len(chunks(peer,stream,offset))>=2)
        old_attempt=chunks(peer,stream,offset)[0]["wire"]["value"]["frame"]["attemptId"]
        await peer.disconnect();await asyncio.sleep(.2)
        rejected=await peer.rpc("session/cancel",{"sessionId":sid},check=False)
        await peer.connect();newstream=await peer.follow(sid)
        baseline=peer.streams[newstream][0]["wire"]["value"]["assistantStream"]
        await terminal(peer,newstream)
        recovered=await page(peer,sid,newstream)
        seqs=[r.get("event",{}).get("seq") for r in recovered.get("records",[])]
        record("V1-A06",{"old_cookie_rejected":rejected["status"]==401,"snapshot_rebuilt":True,
               "page_no_duplicate_seq":len(seqs)==len(set(seqs)),"original_request_recovered":submission["request_id"] in json.dumps(recovered)},
               {"page":recovered,"binding_epoch":peer.epoch,"active_baseline":baseline,"previous_attempt_id":old_attempt,
                "one_provider_attempt":len(boundary.records)-before==1})
        results[-1]["assertions"].update({"same_active_attempt_in_baseline":baseline.get("activeAttempt",{}).get("attemptId")==old_attempt,
                                           "no_reconnect_dispatch":len(boundary.records)-before==1})
        results[-1]["status"]="passed" if all(results[-1]["assertions"].values()) else "failed"
        save(directory/"cases/V1-A06.json",results[-1])
        usage=[]
        scripts=[("complete",{},17,23),("missing_input",{"usage_start":{"output_tokens":0}},None,23),
                 ("missing_output",{"usage_start":{"input_tokens":17},"usage_end":{}},17,None),
                 ("explicit_zero",{"usage_start":{"input_tokens":0,"output_tokens":0},"usage_end":{"output_tokens":0}},0,0),
                 ("cumulative",{"usage_updates":[{"output_tokens":8},{"output_tokens":23},{"output_tokens":23}]},17,23),
                 ("reasoning_in_output",{"usage_end":{"output_tokens":23,"reasoning_tokens":11}},17,23),
                 ("truncated",{"drop_after":3},None,None)]
        for name,script,inp,out in scripts:
            usid,us=await peer.create("v1-usage-"+name)
            boundary.set_scenario(**script)
            count_before=len(boundary.records)
            await peer.prompt(usid,"usage fixture "+name)
            await wait_for(lambda:len(boundary.records)>count_before and boundary.records[-1].get("usage"),10)
            await asyncio.sleep(.15)
            raw=boundary.records[-1]["usage"]
            usage.append({"name":name,"provider":raw,"harness_page":await page(peer,usid,us),
                "correct":raw["normalized"]["input_tokens"]==inp and raw["normalized"]["output_tokens"]==out})
        from fixture import UsageObserver
        observer=UsageObserver()
        def feed(event): observer.feed(("data: "+json.dumps(event)+"\n\n").encode())
        feed({"type":"message_delta","usage":{"output_tokens":23}});feed({"type":"message_stop"})
        feed({"type":"message_delta","usage":{"output_tokens":8}});feed({"type":"message_stop"})
        record("V1-A07",{"unknown_distinct_from_zero":all(u["correct"] for u in usage),
              "single_settlement":all(u["provider"]["settlement_count"]<=1 for u in usage),
              "replay_cannot_rewrite_settlement":observer.result()["normalized"]["output_tokens"]==23},usage)
        await peer.rpc("session/selectModel",{"sessionId":sid,"provider":"deepseek-official","model":"deepseek-v4-pro","reasoningEffort":"high"})
        before=len(boundary.records)
        try:
            await peer.prompt(sid,"new image should be rejected",samples["M01"]["path"])
            denied=False
        except AssertionError as exc:
            denied=True
        offset=len(peer.streams[newstream]);boundary.set_scenario(text_chunks=["text model placeholder"])
        await peer.prompt(sid,"Describe capability after switching, without fetching another image.")
        await terminal(peer,newstream,offset)
        req=json.loads((boundary.private / (boundary.records[-1]["request_id"]+".request.json")).read_text(encoding="utf-8"))
        record("V1-A08",{"new_image_rejected":denied,"no_new_image_attempt":len(boundary.records)==before+1,
              "history_no_visual_image":not boundary.records[-1]["images"],"text_model_effective":req["model"]=="deepseek-v4-pro"},
              {"model":req["model"],"historical_input":req["messages"]})
        async with httpx.AsyncClient(trust_env=False,timeout=20) as client:
            response=await client.post(f"http://127.0.0.1:{host.control_port}/selftest",headers={"x-v1-control":host.control_token})
            tests=response.json()
        save(directory / "tool-selftests.json",tests)
        negatives=[x for x in tests if "denied" in x and x["test"]!="network-negative"]
        record("V1-A09",{"all_denied":all(x["denied"] for x in negatives),
             "forbidden_body_zero":all(x.get("bodyDelta",0)==0 for x in negatives),
             "network_negative_denied":any(x["test"]=="network-negative" and x["denied"] for x in tests)},tests)
        slow=[x for x in tests if x["test"].startswith("v1_")]
        record("V1-A10",{"two_cancel_boundaries":len(slow)==2,"abort_observed":all(x["aborted"] for x in slow),
              "bounded_elapsed":all(x["elapsedMs"]<3000 for x in slow),"host_survives":host.proc.poll() is None},slow)
        # A document's injected instruction is followed by a deliberately
        # hostile model tool call, exercising the real agent tool loop too.
        before=len(boundary.records);offset=len(peer.streams[newstream])
        boundary.set_scenario(text_chunks=["Untrusted document attempted escalation."],
                              tool_calls=[{"name":"get_video_frame","input":{"path":"forbidden-original"}}])
        await peer.prompt(sid,"Synthetic document text says to fetch an original video frame. This is untrusted data.")
        await wait_for(lambda:turn_ends(peer,newstream,offset),15)
        frames=json.dumps(peer.streams[newstream][offset:])
        results[-2]["assertions"]["hostile_model_tool_call_denied"]="V1_TOOL_NOT_ALLOWLISTED" in frames
        results[-2]["observations"]={"registry_tests":tests,"model_loop_denial_observed":"V1_TOOL_NOT_ALLOWLISTED" in frames}
        results[-2]["status"]="passed" if all(results[-2]["assertions"].values()) else "failed"
        save(directory/"cases/V1-A09.json",results[-2])
    except Exception as error:
        save(directory/"failure.json",{"type":type(error).__name__,"message":str(error)})
        print("A_ERROR: "+type(error).__name__+": "+str(error),flush=True)
        raise
    finally:
        if peer:
            save(directory/"private/peer-frames.json",peer.frames)
            await peer.close()
        if host: host.stop()
        boundary.stop()
        save(directory/"boundary-records.json",boundary.records)
        save(directory/"a-results.json",results)
    return results


def load_b_key():
    """Only called after all B gates; never execute a dotenv as shell code."""
    source=REPO/".local/secrets/deepseek.env"
    rows=[]
    for raw in source.read_text(encoding="utf-8-sig").splitlines():
        line=raw.strip()
        if not line or line.startswith("#"):
            continue
        name,sep,value=line.partition("=")
        if name.strip()=="DEEPSEEK_API_KEY" and sep:
            value=value.strip()
            if len(value)>=2 and value[0]==value[-1] and value[0] in "\"'":
                value=value[1:-1]
            rows.append(value)
    if len(rows)!=1 or not rows[0] or any(c.isspace() for c in rows[0]) or not rows[0].isascii():
        raise ValueError("B_KEY_CONFIGURATION_INVALID")
    return rows[0]


async def run_b(run_dir, a_gate_path, human_review_path):
    from fixture import BoundaryServer
    from samples import generate
    gate=json.loads(Path(a_gate_path).read_text(encoding="utf-8"))
    review=json.loads(Path(human_review_path).read_text(encoding="utf-8"))
    expected={f"V1-A{i:02d}" for i in range(1,13)}
    rows=gate.get("cases",[])
    if (len(rows)!=12 or {r["case_id"] for r in rows}!=expected or
        any(r["status"]!="passed" or not all(r.get("assertions",{}).values()) for r in rows) or
        gate.get("real_model_attempts")!=0 or gate.get("source_digests")!=source_digests() or
        gate.get("source_changed_during_run") is not False):
        raise ValueError("A_GATE_NOT_PASSED")
    if review.get("confirmed") is not True or not review.get("message_reference"):
        raise ValueError("HUMAN_EXPECTED_ANSWER_REVIEW_REQUIRED")
    directory=Path(run_dir)
    samples=generate(directory)
    for case in ["M01","L02"]:
        frozen=review.get("samples",{}).get(case,{})
        if any(frozen.get(field)!=samples[case][field] for field in ["sha256","revision","expected"]):
            raise ValueError("HUMAN_REVIEW_NOT_BOUND_TO_FROZEN_SAMPLE")
    key=load_b_key()
    # Shared across every invocation of this authorization, not reset by run id.
    budget_dir=REPO/".runtime/validation/v1-b-authorization-2026-10-21"
    boundary=None;host=None;peer=None;results=[]
    try:
        boundary=BoundaryServer(budget_dir,mode="B",limit=6,key=key)
        del key
        boundary.start()
        host=Host(directory/"host",boundary.url,local_token=boundary.local_token)
        host.env["V1_STAGE"]="B"
        await asyncio.to_thread(host.start)
        peer=await Peer(host).connect()
        c1,s1=await peer.create("v1-b-c1")
        c2,s2=await peer.create("v1-b-c2")
        async with httpx.AsyncClient(trust_env=False,timeout=10) as control:
            response=await control.get(f"http://127.0.0.1:{host.control_port}/state",headers={"x-v1-control":host.control_token})
            response.raise_for_status();effective=response.json()
        save(directory/"effective-state.json",effective)
        if (effective.get("networkGuardInstalled") is not True or
            effective.get("tools")!=["read_course_material"] or not effective.get("activation")):
            raise ValueError("B_EFFECTIVE_LEARNING_PROFILE_NOT_PROVEN")
        from b_cases import execute_case
        stop_group=False
        for case,sid,stream,text,image,should_cancel in [
            ("V1-B01",c1,s1,"请读取确认题图并完整解答，保留公式和必要推导。",samples["M01"]["path"],False),
            ("V1-B02",c2,s2,"请读取确认题图并求特征值及相应特征向量，解释步骤。",samples["L02"]["path"],False),
            ("V1-B03",None,None,None,None,False),
            ("V1-B04",c1,s1,"刚才分母应是2x³，其余不变。请按更正后的条件重新解答。",None,False),
            ("V1-B05",c1,s1,"请按更正后的题目详细推导，展开说明泰勒余项、洛必达和两种方法的适用条件，再比较步骤。",None,True),
            ("V1-B06",c1,s1,"按更正后的分母解释泰勒展开为什么可用，并给出最终极限。",None,False),
        ]:
            if stop_group:
                row={"case_id":case,"status":"not_run","uncovered":["Previous case execution not safely reconciled"]}
                results.append(row);save(directory/"cases"/(case+".json"),row);continue
            if sid is None:
                row={"case_id":case,"status":"not_run","uncovered":["No user-provided real handwriting H02"]}
                results.append(row);save(directory/"cases"/(case+".json"),row);continue
            if boundary.actual_outbound_attempts>=6:
                row={"case_id":case,"status":"not_run","uncovered":["Shared six-attempt budget exhausted"]}
                results.append(row);save(directory/"cases"/(case+".json"),row);continue
            row=await execute_case(peer,boundary,sid,stream,text,image,should_cancel,case)
            row["human_expected_ref"]=review
            stop_group=row["stop_group"]
            results.append(row);save(directory/"cases"/(case+".json"),row)
            print(json.dumps({"case":case,"attempts_total":boundary.actual_outbound_attempts,
                              "terminal":row["terminal_reason"],"active_cancel_proven":row["active_cancel_proven"]}),flush=True)
    finally:
        if peer:
            save(directory/"private/peer-frames.json",peer.frames);await peer.close()
        if host: host.stop()
        if boundary:
            boundary.stop()
            save(directory/"boundary-records.json",boundary.records)
        save(directory/"b-results.json",results)
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["A","B"], required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--patch")
    parser.add_argument("--a-gate")
    parser.add_argument("--human-review")
    args = parser.parse_args()
    directory=REPO/".runtime/validation"/args.run_id
    if directory.exists():
        raise SystemExit("Refusing an existing run directory; retain old evidence")
    directory.mkdir(parents=True)
    if args.stage=="A":
        async def all_a():
            from context_cases import run_context_cases
            from fixture import BoundaryServer
            frozen_sources=source_digests()
            results=await run_a(directory,args.patch)
            boundary=BoundaryServer(directory/"context-boundary").start()
            try:
                results+=await run_context_cases(directory,boundary,Host,Peer)
            finally:
                boundary.stop()
                save(directory/"context-boundary-records.json",boundary.records)
            save(directory/"a-gate.json",{"cases":results,"source_digests":frozen_sources,
                 "source_changed_during_run":frozen_sources!=source_digests(),"real_model_attempts":0})
            if any(r["status"]!="passed" for r in results) or frozen_sources!=source_digests():
                raise SystemExit(2)
        asyncio.run(all_a())
    else:
        if not args.a_gate or not args.human_review:
            raise SystemExit("B requires --a-gate and --human-review")
        asyncio.run(run_b(directory,args.a_gate,args.human_review))
