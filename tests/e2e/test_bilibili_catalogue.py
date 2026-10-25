"""Synthetic catalogue flow in the pinned native Harness UI; no cloud/platform calls."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[2]
PYTHON = ROOT / "backend/.venv/Scripts/python.exe"
NODE = ROOT / ".tools/node/node.exe"
PNPM = ROOT / ".tools/pnpm/bin/pnpm.cjs"


async def serve(directory, port, favorites=False):
    import uvicorn
    from learning_app.app import create_app
    from learning_app.config import AppConfig
    from learning_app.execution.adapter import ExecutionAdapter

    sys.path.insert(0, str(ROOT / "backend/tests"))
    from test_bilibili_catalogue import FixtureCatalogueProvider
    from test_bilibili_covers import synthetic_cover
    from test_bilibili_favorites import FixtureFavoriteProvider

    class BrowserFixtureCatalogue(
        FixtureFavoriteProvider if favorites else FixtureCatalogueProvider
    ):
        def record(self, kind):
            with (directory / "fixture-calls.jsonl").open(
                "a", encoding="utf-8"
            ) as stream:
                stream.write(json.dumps({"kind": kind}) + "\n")

        def json(self, *args, **kwargs):
            self.record(
                "login"
                if args[1].endswith("/qrcode/generate")
                else "favorite"
                if favorites
                else "catalogue"
            )
            return super().json(*args, **kwargs)

        def resolve(self, *args, **kwargs):
            self.record("resolve")
            return super().resolve(*args, **kwargs)

        def media(self, *args, **kwargs):
            self.record("media")
            raise RuntimeError("FIXTURE_MEDIA_UNEXPECTED")

        def subtitle(self, *args, **kwargs):
            self.record("subtitle")
            raise RuntimeError("FIXTURE_SUBTITLE_UNEXPECTED")

        def cover(self, grant, part):
            self.access.check(grant)
            self.record("cover")
            return synthetic_cover()

    adapter = ExecutionAdapter(
        ROOT, directory / "execution", mode="fixture", timeout=60, desktop_enabled=True
    )
    await adapter.open()
    app = create_app(
        AppConfig(
            mode="start",
            backend_port=port,
            frontend_port=port,
            data_dir=directory / "data",
            runtime_dir=directory / "runtime",
            harness_enabled=True,
            desktop_enabled=True,
            instance_id="catalogue-fixture-" + directory.name,
        ),
        execution=adapter,
        integrations_provider_factory=BrowserFixtureCatalogue,
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
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line or line.strip() == "shutdown":
                server.should_exit = True
                return

    monitor = asyncio.create_task(shutdown())
    try:
        await server.serve()
    finally:
        monitor.cancel()
        await adapter.close()


def cli(directory, session, *args):
    command = [
        str(NODE),
        str(PNPM),
        "exec",
        "playwright-cli",
        "-s=" + session,
        *map(str, args),
    ]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=90
    )
    (directory / f"cli-{time.time_ns()}.log").write_text(
        result.stdout + result.stderr, encoding="utf-8"
    )
    if result.returncode or "### Error" in result.stdout:
        raise RuntimeError("CATALOGUE_BROWSER_FAILED: see isolated CLI log")
    return result.stdout


def verify(port, favorites=False, followed=False, source_recovery=False):
    from learning_app.processes import OwnedProcessGroup, require_free_port

    require_free_port("127.0.0.1", port)
    directory = ROOT / ".runtime/platforms/catalogue-browser" / uuid4().hex
    directory.mkdir(parents=True)
    (directory / "draft-note.txt").write_text(
        "Synthetic unsent attachment. Do not submit.", "utf-8"
    )
    session = "catalogue-" + directory.name[:10]
    group = OwnedProcessGroup("catalogue-browser-fixture")
    result = {
        "scope": "synthetic catalogue adapter + fixed Harness UI, no real platform or model",
        "port": port,
        "favorites_flow": favorites,
        "followed_flow": followed,
        "source_recovery_flow": source_recovery,
    }
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
                *(["--favorites"] if favorites else []),
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
                    raise RuntimeError("FIXTURE_START_FAILED")
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
            course_root = directory / "course"
            course_root.mkdir()
            r = client.post(
                "/api/v1/courses/folder",
                json={
                    "path": str(course_root),
                    "title": "收藏合成浏览验证" if favorites else "合集合成浏览验证",
                },
            )
            r.raise_for_status()
        cli(
            directory,
            session,
            "open",
            f"http://127.0.0.1:{port}/desktop/",
            "--browser",
            "msedge",
            "--persistent",
            "--profile",
            directory / "browser-profile",
        )
        code = r"""async(page)=>{
          const favoriteFlow=FAVORITES_FLOW;
          const followedFlow=FOLLOWED_FLOW;
          await page.getByRole('button',{name:'导入B站链接',exact:true}).waitFor();
          const plus=page.getByRole('button',{name:'添加文件或调用指令',exact:true});
          const nativeEntry=page.getByRole('option',{name:/^导入B站资料/});
          await plus.click();
          await page.getByRole('option',{name:/^文件\s/}).waitFor();
          if(await nativeEntry.count()) throw new Error('DISABLED_NATIVE_ENTRY_VISIBLE');
          await page.keyboard.press('Escape');
          await page.locator('[data-slot="conversation.composer.bar"] input[type=file]').setInputFiles(DRAFT_FILE_PATH);
          const attachment=page.getByText('draft-note.txt',{exact:true});
          await attachment.waitFor();
          const changeModule=async(enabled,guest=false)=>page.evaluate(async({enabled,guest,favoriteFlow})=>{
            const session=await fetch('/api/v1/session').then(r=>r.json());
            const write=async(path,body)=>{
              const r=await fetch('/api/v1'+path,{method:'POST',headers:{'Content-Type':'application/json','X-Learning-CSRF':session.csrf_token},body:JSON.stringify(body)});
              if(!r.ok) throw new Error('FIXTURE_MODULE_CHANGE_FAILED');
              return r.json();
            };
            await write('/integrations/modules/bilibili',{enabled});
            if(guest){
              if(favoriteFlow){
                const login=await write('/integrations/bilibili/login',{});
                await write('/integrations/bilibili/login/'+login.login_id,{});
              }else await write('/integrations/bilibili/connect',{guest:true});
            }
            window.dispatchEvent(new Event('learning:integrations-changed'));
          },{enabled,guest,favoriteFlow});
          await changeModule(true,true);
          const editor=page.locator('[data-composer-input][contenteditable=true]');
          await editor.fill('保留的合成草稿');
          await plus.click();
          await nativeEntry.waitFor();
          await nativeEntry.click();
          await page.getByRole('region',{name:'导入B站资料',exact:true}).waitFor();
          if(await editor.textContent()!=='保留的合成草稿') throw new Error('NATIVE_ACTION_CHANGED_DRAFT');
          if(!await attachment.isVisible()) throw new Error('NATIVE_ACTION_CHANGED_ATTACHMENT');
          let sourceRecovered=false;
          let sourceRecoveryMode=null;
          await page.getByRole('button',{name:'导入B站链接',exact:true}).click();
          await editor.fill('/bilibili');
          await editor.press('Enter');
          await page.getByRole('region',{name:'导入B站资料',exact:true}).waitFor();
          if((await editor.textContent()).trim()!=='') throw new Error('SLASH_TOKEN_NOT_CONSUMED');
          if(!await attachment.isVisible()) throw new Error('SLASH_ACTION_CHANGED_ATTACHMENT');
          if(favoriteFlow){
            await page.getByRole('button',{name:'浏览账号收藏夹',exact:true}).click();
            await page.getByRole('button',{name:'合成收藏夹1（21）',exact:true}).waitFor();
            if(followedFlow){
              await page.getByRole('button',{name:'我收藏的合集和收藏夹',exact:true}).click();
              await page.getByRole('button',{name:'合集 · 合成关注合集（21）',exact:true}).click();
            }else await page.getByRole('button',{name:'合成收藏夹1（21）',exact:true}).click();
            await page.getByRole('button',{name:'合成收藏视频2（已失效）',exact:true}).waitFor();
            const importBody=page.getByRole('region',{name:'导入B站资料',exact:true});
            const scrolling=await importBody.evaluate(e=>({overflow:getComputedStyle(e).overflowY,scroll:e.scrollHeight,client:e.clientHeight}));
            if(!['auto','scroll'].includes(scrolling.overflow)||scrolling.scroll<=scrolling.client) throw new Error('FAVORITE_IMPORT_MUST_SCROLL '+JSON.stringify(scrolling));
            const bounds=await importBody.boundingBox();
            await page.mouse.move(bounds.x+bounds.width/2,bounds.y+8);
            await page.mouse.wheel(0,600);
            await page.waitForFunction(()=>document.querySelector('[aria-label="导入B站资料"]')?.scrollTop>0,{},{polling:100});
            await importBody.evaluate(e=>e.scrollTo(0,0));
            if(!followedFlow){
              await page.getByRole('button',{name:'下一页目录',exact:true}).click();
              await page.getByRole('button',{name:'合成收藏夹21（21）',exact:true}).click();
            }
            const invalid=page.getByRole('button',{name:'合成收藏视频2（已失效）',exact:true});
            await invalid.waitFor();
            if(!await invalid.isDisabled()) throw new Error('INVALID_FAVORITE_ENABLED');
            await page.getByRole('button',{name:'下一页视频',exact:true}).click();
            await page.getByRole('button',{name:'合成收藏视频21',exact:true}).click();
            await page.getByRole('button',{name:'读取链接',exact:true}).click();
          }else{
            await page.getByRole('textbox',{name:'B站视频链接'}).fill('https://space.bilibili.com/10/lists/20?type=season');
            await page.getByRole('button',{name:'读取链接',exact:true}).click();
            const catalogue=page.getByRole('region',{name:'合集与系列目录'});
            await catalogue.getByText('合成合集',{exact:true}).waitFor();
            await catalogue.getByRole('button',{name:'下一页',exact:true}).click();
            await catalogue.getByRole('button',{name:'31. 目录视频31 · 读取分P',exact:true}).click();
          }
          const second=page.getByRole('checkbox',{name:/P2/});
          if(await second.isChecked()) throw new Error('PARTS_MUST_START_UNSELECTED');
          const layout=[];
          const checkBoundary=async()=>{
            const bounds=await page.evaluate(()=>{
              const courses=document.querySelector('[data-learning-region=courses]').getBoundingClientRect();
              const material=document.querySelector('[data-learning-region=materials]').getBoundingClientRect();
              const handle=document.querySelector('[data-learning-frame] > [data-side=sidebar]')?.getBoundingClientRect();
              return {courseRight:courses.right,materialLeft:material.left,handleCenter:handle?handle.left+handle.width/2:null};
            });
            if(bounds.handleCenter!==null&&Math.abs(bounds.handleCenter-bounds.courseRight)>1) throw new Error('SIDEBAR_HANDLE_MISALIGNED '+JSON.stringify(bounds));
            if(Math.abs(bounds.courseRight-bounds.materialLeft)>1) throw new Error('COURSE_MATERIAL_BOUNDARY_MISMATCH');
            layout.push(bounds);
          };
          await checkBoundary();
          await page.setViewportSize({width:800,height:900});
          await page.waitForFunction(()=>document.querySelector('[data-learning-region=courses]')?.getBoundingClientRect().width<100,{},{polling:100});
          await page.getByRole('button',{name:'打开侧边栏',exact:true}).click();
          await page.waitForFunction(()=>document.querySelector('[data-learning-region=courses]')?.getBoundingClientRect().width>=264,{},{polling:100});
          await page.waitForFunction(()=>document.getAnimations().every(animation=>animation.playState!=='running'),{},{polling:100});
          await checkBoundary();
          await second.check();
          await page.setViewportSize({width:1280,height:900});
          await page.getByRole('button',{name:'导入所选分P',exact:true}).click();
          await page.getByText('已导入 1 个条目，可在当前课程中打开。',{exact:true}).waitFor();
          const listing=page.getByRole('listbox',{name:'文件列表'});
          if(followedFlow){
            await listing.getByRole('option',{name:/^合成关注合集-/}).dblclick();
            await listing.getByRole('option',{name:/^000021 /}).dblclick();
          }else if(favoriteFlow){await listing.getByRole('option',{name:/^合成两P-/}).dblclick();}
          else{
            await listing.getByRole('option',{name:/^合成合集-/}).dblclick();
            await listing.getByRole('option',{name:/^000031 /}).dblclick();
          }
          await listing.getByRole('option',{name:'002 合成课2.bili',exact:true}).waitFor();
          const cover=listing.getByRole('img',{name:'B站视频封面',exact:true});
          await cover.waitFor();
          await page.waitForFunction(()=>document.querySelector('img[alt="B站视频封面"]')?.naturalWidth===240,{},{polling:100});
          if(new URL(await cover.getAttribute('src'),page.url()).origin!==new URL(page.url()).origin) throw new Error('COVER_MUST_STAY_SAME_ORIGIN');
          if(SOURCE_RECOVERY){
            const originalConnection=await page.evaluate(async()=>{
              const session=await fetch('/api/v1/session').then(r=>r.json());
              const modules=await fetch('/api/v1/integrations/modules').then(r=>r.json());
              const module=modules.find(m=>m.platform==='bilibili');
              const identity=module.connection_id;
              const response=await fetch('/api/v1/integrations/connections/'+identity+'/disconnect',{
                method:'POST',headers:{'Content-Type':'application/json','X-Learning-CSRF':session.csrf_token}});
              if(!response.ok)throw new Error('FIXTURE_DISCONNECT_FAILED');
              return {identity,kind:module.connection_state==='guest'?'anonymous':'account'};
            });
            await listing.getByRole('option',{name:'002 合成课2.bili',exact:true}).click();
            await page.getByRole('button',{name:'恢复来源连接',exact:true}).click();
            await page.getByText('原来源连接已恢复，可重新打开；学习记录保持。原暂停任务仍需手动恢复。',{exact:true}).waitFor();
            const current=await page.evaluate(async()=>{
              const modules=await fetch('/api/v1/integrations/modules').then(r=>r.json());
              return modules.find(m=>m.platform==='bilibili').connection_id;
            });
            if(current!==originalConnection.identity)throw new Error('RESTORE_REBOUND_ORIGINAL_SOURCE');
            sourceRecovered=true;
            sourceRecoveryMode=originalConnection.kind;
          }
          await page.getByRole('button',{name:'导入B站链接',exact:true}).click();
          await changeModule(false);
          let removed=false;
          for(let attempt=0;attempt<20;attempt++){
            await plus.click();
            await page.getByRole('listbox').last().waitFor();
            if(await nativeEntry.count()===0){removed=true;await page.keyboard.press('Escape');break;}
            await page.keyboard.press('Escape');
            await page.waitForTimeout(100);
          }
          if(!removed) throw new Error('DISABLED_NATIVE_ENTRY_NOT_REMOVED');
          await page.screenshot({path:SCREENSHOT,fullPage:true});
          return {nativePlus:true,nativeSlash:true,nativeDisabledAbsent:true,draftPreserved:true,attachmentPreserved:true,manualSelection:true,selectedPart:2,selectedVideoOrdinal:favoriteFlow?21:31,favoritePaging:favoriteFlow,resourceVisible:true,coverLoaded:true,sourceRecovered,sourceRecoveryMode,layout,browserVersion:page.context().browser().version()};
        }""".replace(
            "SCREENSHOT", json.dumps(str(directory / "catalogue-selected.png"))
        )
        code = code.replace(
            "DRAFT_FILE_PATH", json.dumps(str(directory / "draft-note.txt"))
        )
        code = code.replace("FAVORITES_FLOW", "true" if favorites else "false")
        code = code.replace("FOLLOWED_FLOW", "true" if followed else "false")
        code = code.replace("SOURCE_RECOVERY", "true" if source_recovery else "false")
        script = directory / "flow.js"
        script.write_text(code, encoding="utf-8")
        output = cli(directory, session, "run-code", "--filename", script)
        result["browser"] = json.loads(
            output.split("### Result\n", 1)[1].split("\n###", 1)[0]
        )
        with sqlite3.connect(
            "file:" + (directory / "data/business.sqlite3").as_posix() + "?mode=ro",
            uri=True,
        ) as db:
            objects = [
                row[0]
                for row in db.execute("select object_id from integration_sources")
            ]
            result["objects"] = objects
            result["actual_provider_attempts"] = db.execute(
                "select count(*) from outbound_attempts"
            ).fetchone()[0]
            result["generations"] = db.execute(
                "select count(*) from generations"
            ).fetchone()[0]
            if followed:
                payload = json.loads(
                    db.execute("select payload from integration_sources").fetchone()[0]
                )
                result["collection_preserved"] = (
                    payload["collection"]["group_id"] == "collection:99:500"
                    and payload["collection"]["ordinal"] == 21
                )
                assert result["collection_preserved"]
        calls = [
            json.loads(line)["kind"]
            for line in (directory / "fixture-calls.jsonl")
            .read_text("utf-8")
            .splitlines()
        ]
        result["calls"] = {
            key: calls.count(key)
            for key in [
                "favorite" if favorites else "catalogue",
                "resolve",
                "media",
                "subtitle",
                "cover",
                "login",
            ]
        }
        if (
            result["actual_provider_attempts"] != 0
            or result["generations"] != 0
            or objects != ["BV0000000021:2" if favorites else "BV0000000031:2"]
            or result["calls"]
            != {
                ("favorite" if favorites else "catalogue"): 8
                if followed
                else 10
                if favorites
                else 2,
                "resolve": 1,
                "media": 0,
                "subtitle": 0,
                "cover": 1,
                "login": (2 if source_recovery else 1) if favorites else 0,
            }
        ):
            raise RuntimeError("CATALOGUE_SCOPE_VIOLATION")
        result["passed"] = True
    except Exception as error:
        result["passed"] = False
        result["error"] = type(error).__name__ + ": " + str(error)
        for observation in ("snapshot", "console"):
            try:
                cli(directory, session, observation)
            except (RuntimeError, subprocess.TimeoutExpired):
                pass
        raise
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
        if not result["port_closed"] or result["cleanup"].get("remaining_pids"):
            result["passed"] = False
        (directory / "result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    "result": str(directory / "result.json"),
                    "passed": result.get("passed"),
                    "port_closed": result["port_closed"],
                },
                ensure_ascii=False,
            )
        )

    if not result.get("passed"):
        raise RuntimeError("CATALOGUE_BROWSER_OR_CLEANUP_FAILED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", type=Path)
    parser.add_argument("--backend-port", type=int, default=8916)
    parser.add_argument("--favorites", action="store_true")
    parser.add_argument("--followed", action="store_true")
    parser.add_argument("--source-recovery", action="store_true")
    args = parser.parse_args()
    if args.serve:
        asyncio.run(serve(args.serve, args.backend_port, args.favorites))
    else:
        verify(
            args.backend_port,
            args.favorites or args.followed,
            args.followed,
            args.source_recovery,
        )
