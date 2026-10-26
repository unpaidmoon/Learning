"""Pinned native player, real H264 decoding and guarded synthetic online Range data."""

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from test_bilibili_catalogue import cli
from test_bilibili_media_reopen import encode, serve

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "backend/.venv/Scripts/python.exe"


async def serve_preview(directory, port):
    sys.path.insert(0, str(ROOT / "backend/tests"))
    from test_bilibili_media_faults import FaultProvider

    media = FaultProvider.media

    def available(self, grant, part):
        if not self.resolutions:
            self.resolutions = 1  # v1 is the old fixture's intentionally expired URL.
        return media(self, grant, part)

    FaultProvider.media = available
    await serve(directory, port)


def verify(port):
    from learning_app.processes import OwnedProcessGroup, require_free_port

    require_free_port("127.0.0.1", port)
    directory = ROOT / ".runtime/platforms/media-reopen" / uuid4().hex
    directory.mkdir(parents=True)
    encode(directory / "synthetic.mp4")
    course = directory / "course"
    course.mkdir()
    shutil.copyfile(directory / "synthetic.mp4", course / "local.mp4")
    group = OwnedProcessGroup("video-preview-pipeline")
    session = "previews-" + directory.name[:12]
    result = {"passed": False, "scope": "synthetic H264 + native Harness UI; no real source/model"}
    log = (directory / "server.log").open("w", encoding="utf-8")
    process = None
    try:
        process = group.spawn([str(PYTHON), str(Path(__file__).resolve()), "--serve", str(directory),
                               "--backend-port", str(port)], cwd=ROOT,
                              env={**os.environ, "PYTHONIOENCODING": "utf-8"}, stdout=log,
                              stderr=subprocess.STDOUT)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            deadline = time.monotonic() + 45
            while True:
                if process.poll() is not None:
                    raise RuntimeError("PREVIEW_FIXTURE_START_FAILED")
                try:
                    if client.get("/api/v1/health").json().get("ready"):
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("PREVIEW_FIXTURE_READINESS_TIMEOUT")
                time.sleep(.2)
            client.headers["X-Learning-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
            cid = client.post("/api/v1/courses/folder", json={"path": str(course)}).json()["course_id"]
            client.post("/api/v1/integrations/modules/bilibili", json={"enabled": True}).raise_for_status()
            connection = client.post("/api/v1/integrations/bilibili/connect", json={"guest": True}).json()["connection_id"]
            plan = client.post(f"/api/v1/courses/{cid}/integrations/bilibili/resolve", json={
                "connection_id": connection, "url": "BV1234567890"}).json()
            remote = client.post(f"/api/v1/courses/{cid}/integrations/import", json={
                "operation_id": "preview-import", "plan_id": plan["plan_id"],
                "object_ids": [plan["parts"][0]["object_id"]]}).json()["items"][0]
        cli(directory, session, "open", f"http://127.0.0.1:{port}/desktop/", "--browser", "msedge",
            "--persistent", "--profile", directory / "profile")
        cli(directory, session, "snapshot")
        code = r"""async page=>{
          const listing=page.getByRole('listbox',{name:'文件列表'});
          await page.getByRole('list',{name:'工作区列表'}).getByRole('button',{name:'course',exact:true}).click();
          await listing.getByRole('option',{name:'local.mp4',exact:true}).dblclick();
          const active=()=>page.locator('video[data-primary-video]:visible');
          await page.waitForFunction(()=>[...document.querySelectorAll('video[data-primary-video]')].some(v=>!v.closest('[hidden]')&&v.readyState>=2));
          await active().evaluate(v=>v.pause());
          const localTime=await active().evaluate(v=>v.currentTime);
          await page.waitForFunction(async()=>{const v=document.querySelector('video[data-primary-video]');const u=new URL(v.src);u.pathname=u.pathname.replace('/content','/previews');return (await(await fetch(u)).json()).status==='ready';});
          const hover=async fraction=>{const box=await page.locator('.learning-material-page:not([hidden]) .player-progress').boundingBox();await page.mouse.move(box.x+7+(box.width-14)*fraction,box.y+box.height/2);};
          await hover(.75);
          await page.waitForFunction(()=>document.querySelector('[data-preview-frame-time="4"]'));
          if(Math.abs(await active().evaluate(v=>v.currentTime)-localTime)>.05) throw Error('LOCAL_PREVIEW_SEEKS_PRIMARY');
          await page.mouse.move(30,30);
          await page.getByRole('tab',{name:'course',exact:true}).click();
          const parts=REMOTE_PATH.split('/');
          for(const part of parts) await listing.getByRole('option',{name:part,exact:true}).dblclick();
          await page.waitForFunction(()=>[...document.querySelectorAll('video[data-primary-video]')].some(v=>!v.closest('[hidden]')&&v.src.includes('playback_session=')&&v.readyState>=2));
          await active().evaluate(v=>v.pause());
          const remoteTime=await active().evaluate(v=>v.currentTime);
          let requests=0;page.on('request',r=>{if(r.url().includes('/content?')) requests++;});
          await hover(.2);
          await page.waitForFunction(()=>document.querySelector('[data-preview-frame-time="0"]'));
          const first=await page.locator('.seek-preview-image:visible').getAttribute('src');
          await hover(.75);
          await page.waitForFunction(()=>document.querySelector('[data-preview-frame-time="4"]'));
          const second=await page.locator('.seek-preview-image:visible').getAttribute('src');
          if(first===second||!second.startsWith('data:image/jpeg')) throw Error('ONLINE_TARGET_PREVIEW_STALE');
          if(Math.abs(await active().evaluate(v=>v.currentTime)-remoteTime)>.05) throw Error('ONLINE_PREVIEW_SEEKS_PRIMARY');
          await page.mouse.move(30,30);
          await page.waitForTimeout(150);
          const before=requests;
          await hover(.75);await page.waitForTimeout(200);
          if(requests!==before) throw Error('CACHED_ONLINE_FRAME_REOPENS_MEDIA');
          return {localFrameTime:4,onlineFrameTimes:[0,4],onlineImagesDiffer:true,primaryPositionsPreserved:true,cachedHoverNewMediaRequests:0};
        }""".replace("REMOTE_PATH", json.dumps(remote["path"], ensure_ascii=False))
        script = directory / "flow.js"
        script.write_text(code, "utf-8")
        output = cli(directory, session, "run-code", "--filename", script)
        result["browser"] = json.loads(output.split("### Result\n", 1)[1].split("\n###", 1)[0])
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            client.headers["X-Learning-CSRF"] = client.get("/api/v1/session").json()["csrf_token"]
            client.post("/api/v1/integrations/modules/bilibili", json={"enabled": False}).raise_for_status()
            denied = client.get(f"/api/v1/videos/{remote['asset_id']}/previews",
                                params={"source_version": remote["source_version"]})
            assert denied.status_code == 409 and denied.json()["code"] == "PLATFORM_DISABLED"
        result.update(passed=True, online_revocation_checked=True)
    finally:
        try:
            cli(directory, session, "close")
        except (RuntimeError, subprocess.TimeoutExpired):
            pass
        if process and process.poll() is None:
            process.stdin.write("shutdown\n")
            process.stdin.flush()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                pass
        result["cleanup"] = group.close()
        log.close()
        with socket.socket() as sock:
            result["port_closed"] = sock.connect_ex(("127.0.0.1", port)) != 0
        if result["cleanup"].get("remaining_pids") or not result["port_closed"]:
            result["passed"] = False
        (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        print(json.dumps({"result": str(directory / "result.json"), "passed": result["passed"]}))
    if not result["passed"]:
        raise RuntimeError("VIDEO_PREVIEW_PIPELINE_FAILED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--backend-port", type=int, default=8918)
    args = parser.parse_args()
    if args.serve:
        asyncio.run(serve_preview(args.serve, args.backend_port))
    else:
        verify(args.backend_port)
