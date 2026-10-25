"""Pinned native UI + synthetic H264 + controlled expired URL; never user pixels."""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx
from test_bilibili_catalogue import cli

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "backend/.venv/Scripts/python.exe"


def encode(target):
    from learning_app.integrations.mux import av_runtime

    code = """import sys
sys.path.insert(0,sys.argv[1])
import av
with av.open(sys.argv[2],'w',options={'movflags':'faststart'}) as out:
 s=out.add_stream('libx264',rate=10);s.width=160;s.height=96;s.pix_fmt='yuv420p'
 for i in range(60):
  f=av.VideoFrame(160,96,'yuv420p')
  for n,p in enumerate(f.planes):p.update(bytes([64+i%64 if n==0 else 128])*p.buffer_size)
  for p in s.encode(f):out.mux(p)
 for p in s.encode(None):out.mux(p)
"""
    subprocess.run(
        [str(PYTHON), "-I", "-S", "-c", code, str(av_runtime(ROOT)), str(target)],
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )


async def serve(directory, port):
    if not directory.resolve().is_relative_to(ROOT / ".runtime/platforms/media-reopen"):
        raise RuntimeError("FIXTURE_DIRECTORY_SCOPE_DENIED")
    import uvicorn
    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.execution.adapter import ExecutionAdapter

    sys.path.insert(0, str(ROOT / "backend/tests"))
    from test_bilibili_media_faults import FaultProvider
    from test_bilibili_covers import synthetic_cover

    class ReopenProvider(FaultProvider):
        def resolve(self, grant, value):
            result = super().resolve(grant, value)
            for part in result["parts"]:
                part["duration_ms"] = 6000
            return result

        def cover(self, grant, part):
            self.access.check(grant)
            return synthetic_cover()

        def media(self, grant, part):
            result = super().media(grant, part)
            result["duration_ms"] = 6000
            return result

    data = (directory / "synthetic.mp4").read_bytes()
    calls = directory / "transport.jsonl"

    def http(req):
        version = int(req.url.path.removeprefix("/v"))
        with calls.open("a", encoding="utf-8") as out:
            out.write(
                json.dumps({"version": version, "range": req.headers.get("range")})
                + "\n"
            )
        if version == 1:
            return httpx.Response(403)
        value = req.headers.get("range", "bytes=0-").removeprefix("bytes=")
        first, last = value.split("-")
        first = int(first or 0)
        last = min(int(last) if last else len(data) - 1, len(data) - 1)
        return httpx.Response(
            206,
            content=data[first : last + 1],
            headers={
                "Content-Length": str(last - first + 1),
                "Content-Range": f"bytes {first}-{last}/{len(data)}",
                "Content-Type": "video/mp4",
            },
        )

    client_type = httpx.Client
    httpx.Client = lambda *args, **kwargs: client_type(
        *args, transport=httpx.MockTransport(http), **kwargs
    )
    adapter = ExecutionAdapter(
        ROOT, directory / "execution", mode="fixture", desktop_enabled=True
    )
    await adapter.open()
    app = create_app(
        AppConfig(
            mode="start",
            backend_port=port,
            frontend_port=port,
            data_dir=directory / "data",
            runtime_dir=directory / "runtime",
            desktop_enabled=True,
            harness_enabled=True,
            instance_id="media-fixture-" + directory.name,
        ),
        execution=adapter,
        integrations_provider_factory=ReopenProvider,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
    )

    async def shutdown():
        await asyncio.to_thread(sys.stdin.readline)
        server.should_exit = True

    monitor = asyncio.create_task(shutdown())
    try:
        await server.serve()
    finally:
        monitor.cancel()
        await adapter.close()


def verify(port):
    from learning_app.processes import OwnedProcessGroup, require_free_port

    require_free_port("127.0.0.1", port)
    directory = ROOT / ".runtime/platforms/media-reopen" / uuid4().hex
    directory.mkdir(parents=True)
    encode(directory / "synthetic.mp4")
    group = OwnedProcessGroup("synthetic-media-reopen")
    result = {
        "passed": False,
        "scope": "synthetic H264 and platform transport, actual native UI, no cloud",
    }
    session = "reopen-" + directory.name[:10]
    process = None
    log = (directory / "server.log").open("w", encoding="utf-8")
    try:
        process = group.spawn(
            [
                str(PYTHON),
                str(Path(__file__).resolve()),
                "--serve",
                str(directory),
                "--backend-port",
                str(port),
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            deadline = time.monotonic() + 50
            while True:
                if process.poll() is not None:
                    raise RuntimeError("FIXTURE_SERVER_FAILED")
                try:
                    if client.get("/api/v1/health").json().get("ready"):
                        break
                except (httpx.HTTPError, ValueError):
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError("FIXTURE_READINESS_TIMEOUT")
                time.sleep(0.2)
            client.headers["X-Learning-CSRF"] = client.get("/api/v1/session").json()[
                "csrf_token"
            ]
            course = directory / "course"
            course.mkdir()
            cid = client.post(
                "/api/v1/courses/folder",
                json={"path": str(course), "title": "合成失效会话验证"},
            ).json()["course_id"]
            client.post(
                "/api/v1/integrations/modules/bilibili", json={"enabled": True}
            ).raise_for_status()
            connection = client.post(
                "/api/v1/integrations/bilibili/connect", json={"guest": True}
            ).json()["connection_id"]
            plan = client.post(
                f"/api/v1/courses/{cid}/integrations/bilibili/resolve",
                json={"connection_id": connection, "url": "BV1234567890"},
            ).json()
            item = client.post(
                f"/api/v1/courses/{cid}/integrations/import",
                json={
                    "operation_id": "synthetic-import",
                    "plan_id": plan["plan_id"],
                    "object_ids": [plan["parts"][0]["object_id"]],
                },
            ).json()["items"][0]
            client.post(
                f"/api/v1/videos/{item['asset_id']}/position",
                json={
                    "source_version": item["source_version"],
                    "position_ms": 1000,
                    "expected_revision": 0,
                },
            ).raise_for_status()
        cli(
            directory,
            session,
            "open",
            f"http://127.0.0.1:{port}/desktop/",
            "--browser",
            "msedge",
            "--persistent",
            "--profile",
            directory / "profile",
        )
        code = r"""async(page)=>{
          const listing=page.getByRole('listbox',{name:'文件列表'});
          await listing.getByRole('option',{name:/^合成两P-/}).dblclick();
          await listing.getByRole('option',{name:'001 合成课1.bili',exact:true}).dblclick();
          const video=page.locator('video[data-primary-video]');
          await video.waitFor();
          await page.waitForFunction(()=>!!document.querySelector('video[data-primary-video]')?.error);
          const before=await video.getAttribute('src');
          await page.getByRole('button',{name:'重新打开',exact:true}).click();
          await page.waitForFunction(()=>document.querySelector('video[data-primary-video]')?.readyState>=2,{},{polling:100});
          const after=await video.getAttribute('src');
          const position=await video.evaluate(v=>v.currentTime);
          if(before===after||position<.9) throw new Error('REOPEN_DID_NOT_REFRESH_SESSION_AND_POSITION '+JSON.stringify({before,after,position}));
          if(await video.evaluate(v=>v.paused)) await page.getByRole('button',{name:'播放或暂停',exact:true}).click();
          const first=await video.evaluate(v=>v.currentTime);
          await page.waitForTimeout(500);
          const progress=await video.evaluate(v=>({advance:v.currentTime,frames:v.getVideoPlaybackQuality().totalVideoFrames,paused:v.paused}));
          if(progress.paused||progress.advance-first<.2||progress.frames<1) throw new Error('SYNTHETIC_REOPEN_NOT_PLAYING');
          await video.evaluate(v=>v.pause());
          return {sessionChanged:true,positionRestored:true,decodedFrames:progress.frames,screenshotTaken:false};
        }"""
        script = directory / "flow.js"
        script.write_text(code, "utf-8")
        output = cli(directory, session, "run-code", "--filename", script)
        result["browser"] = json.loads(
            output.split("### Result\n", 1)[1].split("\n###", 1)[0]
        )
        attempts = [
            json.loads(line)
            for line in (directory / "transport.jsonl").read_text("utf-8").splitlines()
        ]
        assert sum(a["version"] == 1 for a in attempts) == 1
        assert any(a["version"] == 2 for a in attempts)
        result.update(passed=True, expired_http_attempts=1, fresh_session_used=True)
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
        (directory / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), "utf-8"
        )
        print(
            json.dumps(
                {"result": str(directory / "result.json"), "passed": result["passed"]}
            )
        )
    if not result["passed"]:
        raise RuntimeError("FIXTURE_MEDIA_OR_CLEANUP_FAILED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--backend-port", type=int, default=8918)
    args = parser.parse_args()
    if args.serve:
        asyncio.run(serve(args.serve, args.backend_port))
    else:
        verify(args.backend_port)
