"""Synthetic real-Chromium checks for the second workspace interaction pass.

The formal application and pinned Host fixture are used for complete flows.
Focused component probes use production modules with synthetic local sources.
No model key, real course, user screen or actual system clipboard is read.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import http.server as http_server
from http.cookiejar import CookieJar
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import threading
import time
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
import uuid

from test_browser import (
    NODE,
    PYTHON,
    ROOT,
    Browser,
    check,
    http,
    port_closed,
    wait_until,
    write_json,
)
from test_mvp_a import MvpInstance, active_stop, api, duplicate_probe
from test_redesign import click, fill, secret_field_paths


def source_digests(*, media_only: bool = False) -> dict[str, str]:
    paths = [Path(__file__).resolve(), ROOT / "tests/fixtures/workspace_v2/generate.py",
             ROOT / "pnpm-lock.yaml", ROOT / "backend/uv.lock", ROOT / "package.json",
             ROOT / "frontend/package.json", ROOT / "backend/pyproject.toml",
             ROOT / "harness/runtime.lock.json"]
    folders = ("frontend/src/media",) if media_only else (
        "frontend/src", "frontend/dist", "backend/src/learning_app",
        "harness/profiles", "harness/plugins",
    )
    for name in folders:
        paths.extend(path for path in (ROOT / name).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts)
    return {path.relative_to(ROOT).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(set(paths))}


def synthetic_assets(directory: Path, ffmpeg: Path | None) -> dict:
    spec = importlib.util.spec_from_file_location(
        "workspace_v2_assets", ROOT / "tests/fixtures/workspace_v2/generate.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("SYNTHETIC_GENERATOR_MISSING")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.generate(directory, ffmpeg=ffmpeg)


def build_pair() -> dict:
    path = ROOT / ".runtime/validation/workspace-v2-build-manifest.json"
    if not path.exists():
        return {"present": False, "reason": "No explicit source-to-dist manifest was supplied for this run."}
    data = json.loads(path.read_text("utf-8"))
    mismatches = []
    for collection in ("source", "dist"):
        for name, expected in data[collection].items():
            item = ROOT / name
            if not item.is_file() or hashlib.sha256(item.read_bytes()).hexdigest() != expected:
                mismatches.append(name)
    return {"present": True, "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_count": len(data["source"]), "dist_count": len(data["dist"]),
            "build_newer_than_all_sources": data.get("build_newer_than_all_sources"),
            "mismatches": mismatches, "matched": not mismatches}


class WorkspaceInstance(MvpInstance):
    def start(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        self.log = (self.directory / "server.stdout.txt").open("w", encoding="utf-8")
        self.process = self.group.spawn(
            [str(PYTHON), str(Path(__file__).resolve()), "--serve-fixture", str(self.directory),
             "--backend-port", str(self.backend)],
            cwd=ROOT, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            stdout=self.log, stderr=subprocess.STDOUT,
        )

        def ready():
            if self.process.poll() is not None:
                raise RuntimeError("WORKSPACE_FIXTURE_START_FAILED: see server.stdout.txt")
            try:
                status, _, health = http(self.url + "/api/v1/health")
                return status == 200 and health["capabilities"]["question_answering"] == "ready"
            except OSError:
                return False

        wait_until(ready, timeout=45)
        return self

    def native_selection(self, *, folder: str | None = None, clipboard: list[str] | None = None):
        write_json(self.directory / "synthetic-native-input.json", {
            "folder": folder, "clipboard": clipboard or [],
        })


async def serve_fixture(directory: Path, port: int) -> int:
    """Native UI substitution is private to this synthetic fixture process."""
    import learning_app.course_files as files
    from test_mvp_a import serve_fixture as serve_formal_fixture

    def chosen(*args, **kwargs):
        value = json.loads((directory / "synthetic-native-input.json").read_text("utf-8"))
        return [value["folder"]] if value.get("folder") else []

    def clipboard():
        from learning_app.errors import AppError

        paths = json.loads((directory / "synthetic-native-input.json").read_text("utf-8")).get("clipboard", [])
        if not paths:
            raise AppError("CLIPBOARD_FILES_EMPTY", "合成剪贴板没有文件", 422)
        return paths

    files.choose_paths = chosen
    files.read_clipboard_paths = clipboard
    return await serve_formal_fixture(directory, port)


class SyntheticVideoServer:
    """A single synthetic Range asset, used only by the component probe."""

    def __init__(self, path: Path):
        target = path

        class Handler(http_server.BaseHTTPRequestHandler):
            def do_GET(self):
                if urlsplit(self.path).path != "/api/v1/videos/synthetic/content":
                    self.send_error(404)
                    return
                total = target.stat().st_size
                start, end = 0, total - 1
                partial = self.headers.get("Range")
                if partial:
                    match = re.fullmatch(r"bytes=(\d+)-(\d*)", partial)
                    if not match:
                        self.send_error(416)
                        return
                    start = int(match[1])
                    end = min(total - 1, int(match[2]) if match[2] else total - 1)
                if start >= total or start > end:
                    self.send_error(416)
                    return
                self.send_response(206 if partial else 200)
                self.send_header("Content-Type", "video/webm")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.end_headers()
                try:
                    with target.open("rb") as stream:
                        stream.seek(start)
                        self.wfile.write(stream.read(end - start + 1))
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, *_args):
                pass

        self.server = http_server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        return {"port_closed": port_closed(self.port), "thread_finished": not self.thread.is_alive()}


def video_probe(directory: Path, executable: Path, port: int, ffmpeg: Path | None) -> dict:
    from learning_app.processes import OwnedProcessGroup

    manifest = synthetic_assets(directory / "synthetic", ffmpeg)
    source = SyntheticVideoServer(Path(manifest["seek_video"]))
    browser = Browser(directory / "browser", executable)
    group = OwnedProcessGroup("workspace-v2-video-probe")
    log = (directory / "vite.stdout.txt").open("w", encoding="utf-8")
    result = {"run_id": directory.name, "status": "running", "assertions": [],
              "evidence_level": "real Chromium + production VideoControls/SeekPreview + synthetic Range asset",
              "real_model_attempts": 0, "source_digests": source_digests(media_only=True)}
    html = """<!doctype html><html><head><meta charset='utf-8'>
      <style>body{background:#181818;color:white;margin:36px;font-family:'Microsoft YaHei UI',sans-serif}
      #root{width:820px}.synthetic-stage{width:820px;position:relative;padding-bottom:150px}
      video[data-primary-video]{display:block;width:820px;height:461px;background:black}
      .player-toolbar{position:relative!important;bottom:auto!important;left:auto!important;right:auto!important;
      opacity:1!important;visibility:visible!important;width:820px;background:#181818!important}
      .player-seek-track{margin-top:20px}button{color:white}h1{font-size:24px}</style></head>
      <body><h1>合成视频 · 预览与拖动真实鼠标验证</h1><main id='root'></main>
      <script type='module'>import RefreshRuntime from '/@react-refresh';
      RefreshRuntime.injectIntoGlobalHook(window);window.$RefreshReg$=()=>{};
      window.$RefreshSig$=()=>type=>type;window.__vite_plugin_react_preamble_installed__=true;
      const reactModule=await import('/node_modules/.vite/deps/react.js');const React=reactModule.default??reactModule;
      const domModule=await import('/node_modules/.vite/deps/react-dom_client.js');const {createRoot}=domModule.default??domModule;
      const {VideoControls}=await import('/src/media/VideoControls.tsx');await import('/src/media/VideoWorkspace.css');
      const src='/api/v1/videos/synthetic/content?source_version=workspace-v2';window.seekCalls=[];window.primarySeeking=[];
      function Fixture(){const video=React.useRef(null);const [elapsed,setElapsed]=React.useState(0);
      const [duration,setDuration]=React.useState(0);const [playing,setPlaying]=React.useState(false);
      return React.createElement('section',{className:'synthetic-stage'},
        React.createElement('video',{ref:video,src,muted:true,preload:'auto','data-primary-video':'true',
          onLoadedMetadata:()=>setDuration(video.current.duration),onTimeUpdate:()=>setElapsed(video.current.currentTime),
          onPlay:()=>setPlaying(true),onPause:()=>setPlaying(false),onSeeking:()=>window.primarySeeking.push(video.current.currentTime)}),
        React.createElement(VideoControls,{playing,elapsed,duration,volume:1,muted:true,rate:1,previewSrc:src,sourceKey:'synthetic-v2',
          onFullscreen:async()=>{},onSeek:seconds=>{window.seekCalls.push(seconds);video.current.currentTime=seconds;setElapsed(seconds)},
          onTogglePlay:()=>video.current.paused?video.current.play():video.current.pause(),onMute:()=>{},onVolume:()=>{},onRate:()=>{}}));}
      createRoot(document.querySelector('#root')).render(React.createElement(Fixture));
      </script></body></html>"""
    target = directory / "synthetic-video.html"
    target.write_text(html, encoding="utf-8")
    try:
        process = group.spawn(
            [str(NODE), str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
             "--host", "127.0.0.1", "--port", str(port), "--strictPort"],
            cwd=ROOT / "frontend",
            env={**os.environ, "LEARNING_APP_BACKEND_PORT": str(source.port)},
            stdout=log, stderr=subprocess.STDOUT,
        )
        url = f"http://127.0.0.1:{port}/@fs/{target.as_posix()}"

        def ready():
            if process.poll() is not None:
                raise RuntimeError("VIDEO_PROBE_VITE_FAILED")
            try:
                return http(url)[0] == 200
            except OSError:
                return False

        wait_until(ready, timeout=30)
        browser.command("open", url, f"--config={browser.config}")
        browser.command("run-code", """async(page)=>{
          await page.waitForFunction(()=>{const v=document.querySelector('[data-primary-video]');return v&&v.readyState>=2&&v.duration>119});
        }""")
        browser.snapshot("video-ready")
        browser.command("run-code", """async(page)=>{
          const r=page.getByRole('slider',{name:'视频播放进度',exact:true});const b=await r.boundingBox();
          await page.mouse.move(b.x+7+(b.width-14)*.5,b.y+b.height/2);
          await page.waitForFunction(()=>{const p=document.querySelector('[aria-label="视频进度预览"]');return p&&p.getAttribute('aria-busy')==='false'&&Math.abs(Number(p.dataset.previewFrameTime)-60)<2});
        }""")
        preview = browser.evaluate("""()=>{const p=document.querySelector('[aria-label="视频进度预览"]');const v=p.querySelector('video');
          const c=document.createElement('canvas');c.width=1;c.height=1;c.getContext('2d').drawImage(v,320,180,1,1,0,0,1,1);
          return {target:Number(p.dataset.previewTime),frame:Number(p.dataset.previewFrameTime),pixel:Array.from(c.getContext('2d').getImageData(0,0,1,1).data),paused:v.paused,seeks:window.seekCalls.length}}
        """)
        check(preview["pixel"][1] > preview["pixel"][0] * 1.4
              and preview["pixel"][1] > preview["pixel"][2] * 1.4,
              "hover_preview_decodes_the_green_middle_time_segment", result, observed=preview)
        check(preview["paused"] and preview["seeks"] == 0,
              "hover_preview_neither_plays_decoder_nor_seeks_main_video", result)
        browser.command("run-code", """async(page)=>{
          await page.evaluate(async()=>{const v=document.querySelector('[data-primary-video]');v.currentTime=5;await new Promise(r=>v.addEventListener('seeked',r,{once:true}));await v.play();window.seekCalls=[];window.primarySeeking=[]});
          const r=page.getByRole('slider',{name:'视频播放进度',exact:true});const b=await r.boundingBox();
          const y=b.y+b.height/2;await page.mouse.move(b.x+7+(b.width-14)*.25,y);await page.mouse.down();
          await page.mouse.move(b.x+7+(b.width-14)*.75,y,{steps:16});
          await page.waitForFunction(()=>{const p=document.querySelector('[aria-label="视频进度预览"]');return p&&p.getAttribute('aria-busy')==='false'&&Number(p.dataset.previewFrameTime)>85});
          await page.waitForFunction(()=>document.querySelector('[data-primary-video]').currentTime>5.4);
          await page.evaluate(()=>{window.beforeRelease={time:document.querySelector('[data-primary-video]').currentTime,paused:document.querySelector('[data-primary-video]').paused,
            value:Number(document.querySelector('[aria-label="视频播放进度"]').value),seeks:window.seekCalls.length,events:window.primarySeeking.length}});
        }""")
        moving = browser.evaluate("()=>window.beforeRelease")
        check(not moving["paused"] and 5.4 < moving["time"] < 20
              and moving["seeks"] == moving["events"] == 0,
              "drag_keeps_primary_playing_at_original_timeline_without_seek", result, observed=moving)
        check(abs(moving["value"] - 90) < 2,
              "drag_thumb_immediately_follows_pointer_independently_of_primary_time", result)
        browser.command("screenshot", f"--filename={directory / 'drag-preview-synthetic.png'}", "--full-page")
        browser.command("run-code", """async(page)=>{await page.mouse.up();await page.waitForFunction(()=>window.seekCalls.length>0&&!document.querySelector('[data-primary-video]').seeking)}""")
        released = browser.evaluate("""()=>{const v=document.querySelector('[data-primary-video]');return {time:v.currentTime,paused:v.paused,seeks:window.seekCalls,events:window.primarySeeking}}""")
        check(len(released["seeks"]) == len(released["events"]) == 1
              and 88 < released["time"] < 96 and not released["paused"],
              "pointer_release_seeks_once_and_preserves_playing_state", result, observed=released)
        browser.command("run-code", """async(page)=>{
          await page.evaluate(()=>{document.querySelector('[data-primary-video]').pause();window.seekCalls=[];window.primarySeeking=[]});
          const r=page.getByRole('slider',{name:'视频播放进度',exact:true});const b=await r.boundingBox();
          await page.mouse.move(b.x+7+(b.width-14)*.2,b.y+b.height/2);await page.mouse.down();
          await page.mouse.move(b.x+7+(b.width-14)*.4,b.y+b.height/2,{steps:12});
          await page.evaluate(()=>{window.pausedDrag={time:document.querySelector('[data-primary-video]').currentTime,paused:document.querySelector('[data-primary-video]').paused,seeks:window.seekCalls.length}});
          await page.mouse.up();await page.waitForFunction(()=>window.seekCalls.length>0&&!document.querySelector('[data-primary-video]').seeking);
        }""")
        paused = browser.evaluate("""()=>({during:window.pausedDrag,after:{time:document.querySelector('[data-primary-video]').currentTime,paused:document.querySelector('[data-primary-video]').paused,seeks:window.seekCalls.length}})""")
        check(paused["during"]["paused"] and paused["during"]["seeks"] == 0
              and paused["after"]["paused"] and paused["after"]["seeks"] == 1,
              "paused_video_remains_paused_during_and_after_drag", result, observed=paused)
        check(source_digests(media_only=True) == result["source_digests"],
              "production_media_sources_unchanged_during_probe", result)
        result["status"] = "passed"
    except Exception as error:
        result["status"] = "failed"
        result["failure"] = {"type": type(error).__name__, "message": str(error)}
        try:
            browser.snapshot("failed")
            browser.command("screenshot", f"--filename={directory / 'failure-synthetic.png'}", "--full-page")
        except Exception as capture_error:
            result["failure_capture"] = type(capture_error).__name__
    finally:
        result["browser_cleanup"] = browser.close()
        result["server_cleanup"] = group.close()
        result["source_cleanup"] = source.close()
        result["vite_port_closed"] = port_closed(port)
        log.close()
        write_json(directory / "result.json", result)
    return result


def click_first(browser: Browser, name: str) -> None:
    snapshot = browser.snapshot("before-" + re.sub(r"[^A-Za-z0-9_-]", "_", name))
    browser.ref(snapshot, f'button "{name}"')
    browser.command("run-code", "async(page)=>{await page.getByRole('button',{name:"
                    + json.dumps(name, ensure_ascii=False) + ",exact:true}).first().click();}")


def manager_click(browser: Browser, name: str) -> None:
    browser.snapshot("before-manager-action")
    browser.command("run-code", "async(page)=>{const manager=page.getByRole('region',{name:'文件资源管理器',exact:true});"
                    "const action=manager.getByRole('button',{name:" + json.dumps(name, ensure_ascii=False)
                    + ",exact:true});if(await action.isVisible()){await action.click();}else{"
                    "await manager.locator('summary[aria-label=\"更多文件操作\"]').click();"
                    "await manager.getByRole('menuitem',{name:" + json.dumps(name, ensure_ascii=False) + ",exact:true}).click();}}")


def manager_entry(browser: Browser, path: str, *, double: bool = False) -> None:
    browser.snapshot("before-manager-entry")
    selector = '[data-file-path="' + path.replace('"', '\\"') + '"]'
    browser.command("run-code", "async(page)=>{await page.getByRole('region',{name:'文件资源管理器',exact:true})"
                    ".locator(" + json.dumps(selector, ensure_ascii=False) + ")."
                    + ("dblclick" if double else "click") + "();}")


def wait_file(browser: Browser, name: str) -> None:
    browser.command("run-code", "async(page)=>{await page.getByRole('region',{name:'文件资源管理器',exact:true})"
                    ".getByRole('option',{name:" + json.dumps(name, ensure_ascii=False)
                    + ",exact:true}).waitFor({state:'visible',timeout:20000});}")


def mutate(browser: Browser, path: str, body: dict) -> dict:
    return api(browser, path, {**body, "operation_id": "workspace-v2-" + uuid.uuid4().hex}, method="POST")


def create_course_scene(browser: Browser, instance: WorkspaceInstance, manifest: dict, result: dict) -> tuple[str, str]:
    browser.command("run-code", "async(page)=>{await page.getByRole('combobox',{name:'选择课程',exact:true}).waitFor({state:'visible'});}")
    empty = browser.snapshot("initial-empty-workspace")
    check('button "开始问答"' not in empty and 'textbox "输入问题"' in empty,
          "no_course_shows_composer_instead_of_start_question_page", result)
    account = api(browser, "/account")
    check(account["status"] == 200 and not secret_field_paths(account["body"]),
          "account_DTO_stays_available_without_credentials", result)
    instance.native_selection(folder=None)
    click_first(browser, "创建课程")
    dialog = browser.snapshot("create-course-with-folder-picker")
    check('textbox "课程名称"' in dialog and "课程文件夹路径" not in dialog
          and 'button "选择课程文件夹"' in dialog,
          "create_course_uses_name_and_folder_picker_without_path_textbox", result)
    click(browser, "选择课程文件夹")
    check(api(browser, "/courses")["body"] == [],
          "cancelling_native_folder_selection_does_not_create_course", result)
    instance.native_selection(folder=manifest["course_root"])
    click(browser, "选择课程文件夹")
    check(api(browser, "/courses")["body"] == [],
          "choosing_folder_only_prepares_creation_until_explicit_create", result)
    alias = "合成显示别名 A"
    fill(browser, "课程名称", alias)
    click(browser, "创建")
    wait_file(browser, "synthetic-course.webm")
    courses = api(browser, "/courses")["body"]
    course = next(item for item in courses if item["title"] == alias)
    course_id = course["course_id"]
    check(Path(manifest["course_root"]).is_dir()
          and Path(manifest["course_root"]).name != alias,
          "course_display_name_is_independent_of_existing_disk_folder", result)
    browser.command("run-code", "async(page)=>{await page.getByRole('textbox',{name:'问题或追问',exact:true}).waitFor({state:'visible'});}")
    chat_id = browser.evaluate("()=>document.querySelector('[aria-label=\"选择对话\"]').value")
    chats = api(browser, f"/courses/{course_id}/chats")["body"]
    check(chat_id and len(chats) == 1 and chats[0]["chat_id"] == chat_id,
          "course_opens_one_current_empty_chat_with_direct_composer", result)
    surface = browser.evaluate("""()=>({footer:!!document.querySelector('.composer-footer'),
      start:document.body.innerText.includes('一起弄懂这一处'),historyOptions:Array.from(document.querySelector('[aria-label="选择对话"]').options).map(o=>o.text),
      fileTab:!!document.querySelector('[role="tab"][aria-selected="true"]')})""")
    check(not surface["footer"] and not surface["start"] and "历史对话" not in surface["historyOptions"],
          "chat_has_no_start_page_footer_or_history_placeholder_option", result, observed=surface)
    file_tab = browser.evaluate("""()=>{const t=Array.from(document.querySelectorAll('[role="tab"]')).find(t=>t.textContent.trim()==='文件');
      return t?{selected:t.getAttribute('aria-selected'),close:!!t.querySelector('[aria-label^="关闭"]')}:null}""")
    check(file_tab and file_tab["selected"] == "true" and not file_tab["close"],
          "permanent_file_tab_is_initially_selected_and_has_no_close_action", result)
    before = browser.evaluate("()=>document.querySelector('.study-columns').getBoundingClientRect().width")
    click(browser, "收起课程目录")
    hidden = browser.evaluate("()=>({hidden:document.querySelector('.sidebar').hidden,width:document.querySelector('.study-columns').getBoundingClientRect().width})")
    check(hidden["hidden"] and hidden["width"] > before,
          "sidebar_button_hides_whole_course_area_and_releases_space", result)
    click(browser, "展开课程目录")
    check(browser.evaluate("()=>document.querySelector('[aria-label=\"选择对话\"]').value") == chat_id,
          "sidebar_toggle_preserves_current_chat", result)
    return course_id, chat_id


def file_manager_scene(browser: Browser, instance: WorkspaceInstance, manifest: dict, course_id: str, result: dict) -> None:
    root = Path(manifest["course_root"])
    prefix = f"/courses/{course_id}/files"
    names = browser.evaluate("()=>Array.from(document.querySelectorAll('.file-manager-entry')).map(n=>n.getAttribute('aria-label'))")
    check({"子目录", "说明.txt", "不支持的格式.fixture", "seek-preview.webm"} <= set(names),
          "icon_file_manager_lists_all_formats_and_folders", result)
    check(browser.evaluate("()=>document.querySelector('.file-manager-content').classList.contains('file-grid')"),
          "file_manager_defaults_to_icon_view", result)
    sidebar = browser.evaluate("()=>document.querySelector('.sidebar').innerText")
    check("导入资料" not in sidebar and "新建文件夹" not in sidebar,
          "sidebar_has_no_import_or_new_folder_entry", result)
    external = Path(manifest["external_imports"])
    original = (root / "copy-collision.txt").read_bytes()
    copied = mutate(browser, prefix + "/import", {"paths": [str(external / "copy-collision.txt")]})
    check(copied["status"] == 200 and copied["body"]["state"] == "completed"
          and (external / "copy-collision.txt").exists()
          and (root / "copy-collision.txt").read_bytes() == original
          and (root / "copy-collision (2).txt").read_bytes() == (external / "copy-collision.txt").read_bytes(),
          "external_copy_keeps_source_and_auto_numbers_collision_without_replacement", result)
    forbidden = mutate(browser, prefix + "/import", {"paths": [str(external / "copy-me.txt")], "mode": "move"})
    check(forbidden["status"] == 422 and (external / "copy-me.txt").is_file(),
          "external_import_endpoint_rejects_move_mode", result)
    instance.native_selection(folder=manifest["course_root"], clipboard=[str(external / "clipboard-copy.txt")])
    manager_click(browser, "粘贴")
    wait_file(browser, "clipboard-copy.txt")
    check((external / "clipboard-copy.txt").is_file()
          and (root / "clipboard-copy.txt").read_bytes() == (external / "clipboard-copy.txt").read_bytes(),
          "explicit_native_clipboard_paste_copies_and_preserves_source", result)
    check(not browser.evaluate("()=>Boolean(document.querySelector('[role=dialog]'))"),
          "normal_external_copy_displays_no_copy_move_or_collision_dialog", result)
    browser.snapshot("before-external-file-drop")
    browser.command("run-code", """async(page)=>{const data=await page.evaluateHandle(encoded=>{
      const bytes=Uint8Array.from(atob(encoded),c=>c.charCodeAt(0));
      const d=new DataTransfer();d.items.add(new File([bytes],'drag-copy.txt',{type:'text/plain'}));return d},DRAG_BASE64);
      await page.getByRole('region',{name:'文件资源管理器',exact:true}).dispatchEvent('dragenter',{dataTransfer:data});
      await page.getByRole('region',{name:'文件资源管理器',exact:true}).dispatchEvent('drop',{dataTransfer:data});}
    """.replace("DRAG_BASE64", json.dumps(base64.b64encode((external / "drag-copy.txt").read_bytes()).decode())))
    wait_file(browser, "drag-copy.txt")
    check((root / "drag-copy.txt").read_bytes() == (external / "drag-copy.txt").read_bytes()
          and (external / "drag-copy.txt").is_file(),
          "browser_DataTransfer_drop_copies_file_bytes_without_touching_external_original", result)
    instance.native_selection(folder=manifest["course_root"], clipboard=[])
    manager_entry(browser, "internal-copy.txt")
    manager_click(browser, "复制")
    manager_entry(browser, "子目录", double=True)
    wait_file(browser, "nested-search.txt")
    manager_click(browser, "粘贴")
    wait_file(browser, "internal-copy.txt")
    check((root / "internal-copy.txt").is_file() and (root / "子目录/internal-copy.txt").is_file(),
          "internal_copy_and_toolbar_paste_copy_into_current_directory", result)
    manager_entry(browser, "子目录/internal-copy.txt")
    manager_click(browser, "剪切")
    manager_click(browser, "上一级")
    wait_file(browser, "internal-copy.txt")
    manager_click(browser, "粘贴")
    wait_file(browser, "internal-copy (2).txt")
    check(not (root / "子目录/internal-copy.txt").exists() and (root / "internal-copy (2).txt").is_file(),
          "internal_cut_paste_moves_and_auto_preserves_existing_target", result)
    manager_click(browser, "新建文件夹")
    fill(browser, "名称", "新建合成目录")
    click(browser, "确定")
    wait_file(browser, "新建合成目录")
    check((root / "新建合成目录").is_dir(), "file_manager_creates_real_directory", result)
    manager_entry(browser, "internal-copy (2).txt")
    manager_click(browser, "重命名")
    fill(browser, "名称", "renamed-internal.txt")
    click(browser, "确定")
    wait_file(browser, "renamed-internal.txt")
    check((root / "renamed-internal.txt").is_file() and not (root / "internal-copy (2).txt").exists(),
          "file_manager_renames_real_file", result)
    manager_entry(browser, "renamed-internal.txt")
    manager_click(browser, "属性")
    properties = browser.snapshot("file-properties")
    check("renamed-internal.txt" in properties and "修改时间" in properties,
          "selected_file_properties_show_actual_name_size_and_time", result)
    click(browser, "关闭")
    manager_click(browser, "列表视图")
    check(browser.evaluate("()=>document.querySelector('.file-manager-content').classList.contains('file-list')"),
          "file_manager_can_switch_to_list_view", result)
    fill(browser, "搜索当前文件夹", "renamed-internal")
    filtered = browser.evaluate("()=>Array.from(document.querySelectorAll('.file-manager-entry')).map(n=>n.getAttribute('aria-label'))")
    check(filtered == ["renamed-internal.txt"], "filename_search_filters_current_directory", result)
    fill(browser, "搜索当前文件夹", "")
    manager_click(browser, "图标视图")
    manager_entry(browser, "子目录", double=True)
    wait_file(browser, "nested-search.txt")
    manager_click(browser, "后退")
    wait_file(browser, "renamed-internal.txt")
    manager_click(browser, "前进")
    wait_file(browser, "nested-search.txt")
    manager_click(browser, "上一级")
    wait_file(browser, "renamed-internal.txt")
    check(True, "folder_navigation_back_forward_and_parent_are_usable", result)
    manager_entry(browser, "recycle-only.txt")
    manager_click(browser, "移入回收站")
    wait_until(lambda: not (root / "recycle-only.txt").exists(), timeout=15)
    check(not (root / "recycle-only.txt").exists(),
          "explicit_delete_invokes_real_Windows_recycle_operation_on_synthetic_file", result,
          boundary="actual recycle helper is used; this runner does not enumerate, restore or empty the recycle bin")
    rejected = api(browser, prefix + "?path=..")
    check(rejected["status"] == 422, "file_browser_rejects_parent_path_escape", result)
    browser.snapshot("before-hover-course-rename")
    browser.command("run-code", """async(page)=>{await page.mouse.move(1100,520);
      await page.evaluate(()=>{window.renameOpacityBefore=getComputedStyle(document.querySelector('[aria-label="重命名课程"]')).opacity});
      await page.getByRole('button',{name:'重命名课程',exact:true}).hover();
      await page.waitForFunction(()=>Number(getComputedStyle(document.querySelector('[aria-label="重命名课程"]')).opacity)>.9);}""")
    check(float(browser.evaluate("()=>window.renameOpacityBefore")) < 0.1,
          "course_rename_button_is_hidden_until_pointer_nears_course_name", result)
    click(browser, "重命名课程")
    fill(browser, "课程名称", "仅显示名称已改变")
    click(browser, "保存")
    browser.command("run-code", """async(page)=>{await page.waitForFunction(()=>document.querySelector('#course-select option:checked')?.textContent==='仅显示名称已改变');}""")
    check(root.is_dir() and api(browser, prefix)["body"]["root_path"] == str(root.resolve()),
          "UI_course_rename_changes_only_display_name_not_real_root", result)


def http_api(base_url: str, path: str, body: dict) -> dict:
    """Private in-process cookie/CSRF; large fixture bytes never enter CLI argv."""
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))
    with opener.open(base_url + "/api/v1/session", timeout=10) as response:
        csrf = json.load(response)["csrf_token"]
    request = Request(base_url + "/api/v1" + path, method="POST",
                      data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", "X-Learning-CSRF": csrf})
    try:
        with opener.open(request, timeout=90) as response:
            return {"status": response.status, "body": json.load(response)}
    except HTTPError as error:
        return {"status": error.code, "body": json.load(error)}


def document_upload(base_url: str, chat_id: str, path: Path, media_type: str, *, upload_id: str | None = None) -> dict:
    return http_api(base_url, "/document-attachments", {
        "chat_id": chat_id, "filename": path.name, "media_type": media_type,
        "data_base64": base64.b64encode(path.read_bytes()).decode(),
        "confirmed": True, "upload_id": upload_id or "workspace-v2-" + uuid.uuid4().hex,
    })


def documents_scene(browser: Browser, instance: WorkspaceInstance, manifest: dict, course_id: str, chat_id: str, result: dict) -> None:
    documents = Path(manifest["attachment_directory"])
    root = Path(manifest["course_root"])
    before = {path.relative_to(root).as_posix() for path in root.rglob("*")}
    uploaded = []
    for name, kind in (("two-page-text.pdf", "application/pdf"),
                       ("whole-document.txt", "text/plain"),
                       ("whole-document.md", "text/markdown")):
        response = document_upload(instance.url, chat_id, documents / name, kind)
        check(response["status"] == 200, "accepted_whole_chat_document_" + name, result)
        uploaded.append(response["body"])
    check(uploaded[0]["kind"] == "pdf" and uploaded[0]["page_count"] == 2,
          "PDF_metadata_retains_both_selected_pages", result)
    check({path.relative_to(root).as_posix() for path in root.rglob("*")} == before,
          "chat_document_upload_does_not_import_into_course_folder", result)
    over = document_upload(instance.url, chat_id, documents / "thirty-three-pages.pdf", "application/pdf")
    check(over["status"] == 422 and "LIMIT" in over["body"].get("code", ""),
          "PDF_over_page_limit_is_rejected_without_silent_truncation", result, code=over["body"].get("code"))
    malformed = api(browser, "/document-attachments", {
        "chat_id": chat_id, "filename": "bad.pdf", "media_type": "application/pdf",
        "confirmed": True, "data_base64": base64.b64encode(b"not-a-pdf").decode(),
    }, method="POST")
    check(malformed["status"] == 422, "invalid_PDF_is_rejected_before_submission", result)
    instance.control("scenario", scenario={"text_chunks": ["合成附件实际出站完成。"]})
    payload = {"chat_id": chat_id, "submission_id": "workspace-doc-" + uuid.uuid4().hex,
               "text": "请根据明确选择的整份合成文档回答。",
               "attachment_ids": [item["attachment_id"] for item in uploaded],
               "model": "deepseek-flash", "thinking": "high"}
    receipt = api(browser, "/submissions", payload, method="POST")
    check(receipt["status"] == 200, "whole_documents_publish_through_formal_submission", result)
    gid = receipt["body"]["generation_id"]
    wait_until(lambda: instance.generation_for(payload["text"])["state"] == "completed", timeout=30)
    native = instance.control("inspect", generation_id=gid)
    images = native["boundary"][0]["images"]
    check(len(images) == 2 and len({item["sha256"] for item in images}) == 2,
          "two_distinct_full_PDF_pages_reach_actual_Host_as_two_confirmed_images", result, images=images)
    raw = next((instance.directory / "execution/boundary/private/boundary").glob("*.request.json"))
    request = json.loads(raw.read_text("utf-8"))
    serialized = json.dumps(request, ensure_ascii=False)
    check("TEXT_START_1729" in serialized and "TEXT_END_2718" in serialized
          and "MARKDOWN_START_3141" in serialized and "MARKDOWN_END_5926" in serialized,
          "text_and_Markdown_reach_actual_request_without_head_or_tail_truncation", result)
    check("application/pdf" not in serialized and "data_base64" not in serialized,
          "original_PDF_binary_is_not_misrepresented_as_native_provider_document", result)
    result["document_generation_id"] = gid
    result["committed_document_attachment_ids"] = payload["attachment_ids"]


def prepared_page_hashes(instance: WorkspaceInstance, chat_id: str, attachment_ids: list[str]) -> set[str]:
    placeholders = ",".join("?" for _ in attachment_ids)
    with sqlite3.connect(instance.directory / "data/business.sqlite3") as connection:
        values = connection.execute(
            "SELECT payload FROM document_attachments WHERE chat_id=? AND attachment_id IN (" + placeholders + ")",
            [chat_id, *attachment_ids],
        ).fetchall()
    return {page["sha256"] for row in values for page in json.loads(row[0]).get("pages", [])}


def reopen_chat(browser: Browser, course_id: str, chat_id: str) -> None:
    browser.command("reload")
    browser.command("run-code", "async(page)=>{const c=page.getByRole('combobox',{name:'选择课程',exact:true});"
                    "await c.waitFor({state:'visible'});await c.selectOption(" + json.dumps(course_id) + ");"
                    "const h=page.getByRole('combobox',{name:'选择对话',exact:true});await h.locator('option[value=\"' + "
                    + json.dumps(chat_id) + " + '\"]').waitFor({state:'attached'});await h.selectOption("
                    + json.dumps(chat_id) + ");await page.waitForFunction(id=>document.querySelector('[aria-label=\"选择对话\"]').value===id,"
                    + json.dumps(chat_id) + ");await page.getByText('合成附件实际出站完成。',{exact:true}).waitFor({state:'visible'});"
                    "await page.getByRole('textbox',{name:'问题或追问',exact:true}).waitFor({state:'visible'});}")


def fill_and_wait_saved(browser: Browser, chat_id: str, text: str) -> None:
    """Observe the actual completed autosave response, not an async DOM poll."""
    browser.snapshot("before-question-auto-save")
    browser.command("run-code", "async(page)=>{const saved=page.waitForResponse(response=>"
                    "response.url().endsWith('/api/v1/chats/" + chat_id + "/draft')&&"
                    "response.request().method()==='POST'&&response.request().postDataJSON().text==="
                    + json.dumps(text, ensure_ascii=False) + ",{timeout:20000});"
                    "await page.getByRole('textbox',{name:'问题或追问',exact:true}).fill("
                    + json.dumps(text, ensure_ascii=False) + ");const response=await saved;"
                    "if(!response.ok())throw Error('AUTOSAVE_HTTP_'+response.status());}")


def last_video_scene(browser: Browser, instance: WorkspaceInstance, course_id: str, chat_id: str, result: dict) -> None:
    manager_entry(browser, "synthetic-course.webm", double=True)
    browser.command("run-code", """async(page)=>{await page.waitForFunction(()=>{const v=document.querySelector('[data-primary-video]');return v&&v.readyState>=2&&v.videoWidth===640});
      await page.evaluate(()=>document.querySelector('[data-primary-video]').play());}""")
    check(browser.evaluate("()=>Array.from(document.querySelectorAll('[role=tab]')).map(e=>e.textContent.trim())")
          == ["文件", "synthetic-course.webm"],
          "one_open_video_has_a_separate_closable_tab_beside_permanent_files", result)
    click(browser, "关闭 synthetic-course.webm")
    browser.command("run-code", """async(page)=>{await page.getByRole('region',{name:'文件资源管理器',exact:true}).waitFor({state:'visible'});
      await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));}""")
    state = browser.evaluate("""()=>({tabs:Array.from(document.querySelectorAll('[role=tab]')).map(e=>e.textContent.trim()),
      selected:Array.from(document.querySelectorAll('[role=tab]')).find(e=>e.getAttribute('aria-selected')==='true')?.textContent.trim(),
      paused:document.querySelector('[data-primary-video]')?.paused,hidden:document.querySelector('.video-content')?.hidden,
      chat:document.querySelector('[aria-label="选择对话"]').value})""")
    check(state["tabs"] == ["文件"] and state["selected"] == "文件"
          and state["paused"] and state["hidden"] and state["chat"] == chat_id,
          "closing_last_video_returns_icon_file_manager_and_preserves_chat", result, observed=state)
    check(len(api(browser, f"/courses/{course_id}/chats")["body"]) == 1,
          "closing_last_file_does_not_create_conversation", result)
    manager_entry(browser, "synthetic-course.webm", double=True)
    browser.command("run-code", """async(page)=>{await page.waitForFunction(()=>document.querySelector('[data-primary-video]')?.readyState>=2);
      const r=page.getByRole('slider',{name:'视频播放进度',exact:true});const b=await r.boundingBox();
      await page.mouse.move(b.x+7+(b.width-14)*.5,b.y+b.height/2);
      await page.waitForFunction(()=>document.querySelector('[aria-label="视频进度预览"]')?.getAttribute('aria-busy')==='false');}""")
    before = instance.counts()
    preview = browser.evaluate("""()=>{const p=document.querySelector('[aria-label="视频进度预览"]');return {target:Number(p.dataset.previewTime),frame:Number(p.dataset.previewFrameTime),local:p.dataset.previewLocalOnly}}""")
    check(abs(preview["target"] - preview["frame"]) < 0.1 and preview["local"] == "true"
          and instance.counts() == before,
          "formal_registered_video_can_preview_locally_without_model_dispatch", result, observed=preview)
    click(browser, "关闭 synthetic-course.webm")
    wait_file(browser, "synthetic-course.webm")


MATRIX_LATEX = r"M=\begin{pmatrix}O&A\\B&E\end{pmatrix}."
MATRIX_ANSWER = "合成分块矩阵：$" + MATRIX_LATEX + "$。选区外的这句话不应被引用。"


def ui_documents_and_matrix(browser: Browser, instance: WorkspaceInstance, manifest: dict, course_id: str, chat_id: str, result: dict) -> None:
    reopen_chat(browser, course_id, chat_id)
    documents = Path(manifest["attachment_directory"])
    baseline = instance.counts()
    browser.snapshot("before-native-file-input-upload")
    browser.command("run-code", "async(page)=>{await page.getByLabel('添加附件',{exact:true}).setInputFiles("
                    + json.dumps(str(documents / "two-page-scanned.pdf"), ensure_ascii=False)
                    + ");await page.getByRole('button',{name:'移除附件 two-page-scanned.pdf',exact:true}).waitFor({state:'visible',timeout:30000});}")
    metadata = browser.snapshot("whole-scanned-PDF-in-draft")
    check("整份 PDF" in metadata and "2 页" in metadata and instance.counts() == baseline,
          "pure_plus_file_input_accepts_entire_PDF_into_draft_without_dispatch", result)
    check(not browser.evaluate("()=>document.body.innerText.includes('选择图片')||Boolean(document.querySelector('.composer-footer'))"),
          "plus_has_no_choose_image_text_and_composer_has_no_normal_footer", result)
    question = "请阅读界面上传的整份扫描PDF，并用合成分块矩阵作答。"
    fill_and_wait_saved(browser, chat_id, question)
    draft = api(browser, f"/chats/{chat_id}/draft")["body"]
    check(draft["text"] == question and len(draft["attachment_ids"]) == 1,
          "documents_and_question_auto_save_without_save_button", result)
    instance.control("scenario", scenario={"text_chunks": [MATRIX_ANSWER]})
    click(browser, "发送")
    browser.command("run-code", """async(page)=>{await page.locator('.message.assistant .katex-html').last().waitFor({state:'visible'});
      await page.evaluate(()=>document.fonts.ready);}""")
    gid = instance.generation_for(question)["generation_id"]
    wait_until(lambda: instance.generation_for(question)["state"] == "completed", timeout=30)
    native = instance.control("inspect", generation_id=gid)
    current_images = native["boundary"][0]["images"]
    allowed_hashes = prepared_page_hashes(instance, chat_id, [
        *result["committed_document_attachment_ids"], *draft["attachment_ids"],
    ])
    actual_hashes = {image["sha256"] for image in current_images}
    result["PDF_page_observation"] = {"actual_images": len(current_images),
                                       "unique_pages": len(actual_hashes),
                                       "repeated_pages": len(current_images) - len(actual_hashes),
                                       "allowed_page_hashes": sorted(allowed_hashes)}
    check(actual_hashes == allowed_hashes and len(allowed_hashes) == 4,
          "UI_uploaded_scanned_PDF_sends_both_new_pages_and_only_previously_confirmed_history_pages", result,
          images=current_images, note="native history and the protected current snapshot can repeat an already confirmed page")
    browser.snapshot("before-whole-block-matrix-mouse-selection")
    browser.command("run-code", """async(page)=>{
      const points=await page.evaluate(()=>{const root=Array.from(document.querySelectorAll('.message.assistant')).at(-1).querySelector('.katex-html');
        const nodes=[];const walker=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);let n;while(n=walker.nextNode()){if(n.data==='M'||n.data==='.')nodes.push(n)}
        const first=nodes.find(n=>n.data==='M'),last=nodes.find(n=>n.data==='.');const a=document.createRange(),b=document.createRange();
        a.selectNodeContents(first);b.selectNodeContents(last);const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();return {a:{x:x.left+.2,y:x.top+x.height/2},b:{x:y.right-.2,y:y.top+y.height/2}}});
      await page.mouse.move(points.a.x,points.a.y);await page.mouse.down();await page.mouse.move(points.b.x,points.b.y,{steps:18});await page.mouse.up();
    }""")
    selected = browser.evaluate("""()=>({text:window.getSelection().toString(),error:document.querySelector('.selection-quote-error')?.textContent,
      button:!!document.querySelector('.selection-quote-button')})""")
    check(selected["button"] and not selected.get("error"),
          "whole_visible_block_matrix_selection_has_add_entry_without_unmapped_error", result, observed=selected)
    before_references = api(browser, f"/chats/{chat_id}/draft")["body"]["references"]
    check(before_references == [], "ordinary_matrix_selection_does_not_add_to_draft", result)
    click(browser, "添加到对话")
    fill_and_wait_saved(browser, chat_id, "请解释所选矩阵，不扩大到后面的文字。")
    saved = api(browser, f"/chats/{chat_id}/draft")["body"]
    check(saved["references"][0]["markdown"] == "$" + MATRIX_LATEX + "$"
          and "选区外" not in saved["references"][0]["markdown"],
          "matrix_card_preserves_exact_full_matrix_LaTeX_and_excludes_other_text", result)
    check(browser.evaluate("()=>document.querySelectorAll('.reference-card .katex').length") == 1,
          "whole_matrix_reference_card_remains_rendered_mathematics", result)
    browser.command("screenshot", f"--filename={directory_from_browser(browser) / 'learning-files-light.png'}", "--full-page")
    click(browser, "切换深色")
    browser.command("screenshot", f"--filename={directory_from_browser(browser) / 'learning-files-dark.png'}", "--full-page")
    click(browser, "切换浅色")
    reopen_chat(browser, course_id, chat_id)
    restored = api(browser, f"/chats/{chat_id}/draft")["body"]
    check(restored["references"] == saved["references"] and restored["text"] == saved["text"],
          "auto_saved_full_matrix_reference_and_question_restore_after_reload", result)


def directory_from_browser(browser: Browser) -> Path:
    return browser.directory


def lost_receipt_scene(browser: Browser, instance: WorkspaceInstance, course_id: str,
                       chat_id: str, result: dict) -> None:
    """Lose an accepted HTTP receipt, then reconcile without a second dispatch."""
    baseline = instance.counts()
    question = "本轮工作区合成丢失提交回执验证。"
    answer = "这次工作区提交已被接受，核对后继续原回答。"
    instance.control("scenario", scenario={"text_chunks": [answer]})
    fill(browser, "问题或追问", question)
    browser.snapshot("before-workspace-receipt-drop")
    browser.command("run-code", "async(page)=>{await page.route('**/api/v1/submissions',"
                    "async route=>{await route.fetch();await route.abort('connectionfailed');},{times:1});}")
    click(browser, "发送")
    notice = "这次提交的回执尚未确认。先核对，不会自动重发。"
    browser.wait_text(notice)
    wait_until(lambda: instance.counts()["local_messages"] == baseline["local_messages"] + 1,
               timeout=30)
    accepted = instance.counts()
    check(accepted["submissions"] == baseline["submissions"] + 1
          and accepted["local_messages"] == baseline["local_messages"] + 1,
          "lost_receipt_is_an_accepted_real_local_dispatch_with_original_identity", result)
    reopen_chat(browser, course_id, chat_id)
    browser.wait_text(notice)
    check(instance.counts() == accepted,
          "workspace_reload_and_history_switch_do_not_resend_unknown_submission", result)
    click(browser, "核对提交")
    browser.wait_text(answer)
    generation = instance.generation_for(question)
    wait_until(lambda: instance.generation_for(question)["state"] == "completed", timeout=30)
    details = api(browser, f"/generations/{generation['generation_id']}")["body"]
    check(instance.counts() == accepted and details["chat_id"] == chat_id,
          "explicit_workspace_reconciliation_uses_original_generation_without_resubmit", result)
    result["unknown_receipt_generation_id"] = generation["generation_id"]


def integration(directory: Path, executable: Path, port: int, ffmpeg: Path | None, mode: str) -> dict:
    manifest = synthetic_assets(directory / "synthetic", ffmpeg)
    instance = WorkspaceInstance(directory / "server", port)
    instance.native_selection(folder=manifest["course_root"])
    browser = Browser(directory / "browser", executable)
    result = {"run_id": directory.name, "status": "running", "mode": mode, "assertions": [],
              "real_model_attempts": 0,
              "evidence_level": "real Chromium + formal ASGI + pinned Host + local provider + generated files",
              "native_picker": "deterministic seam; no actual system dialog clicks",
              "native_clipboard": "deterministic CF_HDROP seam; system clipboard untouched",
              "source_digests_before": source_digests()}
    try:
        result["build_pair_before"] = build_pair()
        if result["build_pair_before"]["present"]:
            check(result["build_pair_before"]["matched"],
                  "served_build_matches_explicit_production_source_manifest", result)
        instance.start()
        browser.command("open", instance.url, f"--config={browser.config}")
        course_id, chat_id = create_course_scene(browser, instance, manifest, result)
        result.update(course_id=course_id, chat_id=chat_id)
        if mode in {"full", "files"}:
            file_manager_scene(browser, instance, manifest, course_id, result)
        if mode == "full":
            last_video_scene(browser, instance, course_id, chat_id, result)
        if mode in {"full", "attachments", "late"}:
            documents_scene(browser, instance, manifest, course_id, chat_id, result)
        if mode in {"full", "late"}:
            ui_documents_and_matrix(browser, instance, manifest, course_id, chat_id, result)
            active_stop(browser, instance, result)
            duplicate_probe(browser, instance, chat_id, result)
            wait_until(lambda: instance.generation_for("重复身份验证。")["state"] == "completed", timeout=30)
            lost_receipt_scene(browser, instance, course_id, chat_id, result)
        result["source_digests_after"] = source_digests()
        result["changed_source_files"] = [path for path in result["source_digests_after"]
                                          if result["source_digests_before"].get(path) != result["source_digests_after"][path]]
        result["source_digests_match_after"] = not result["changed_source_files"]
        check(result["source_digests_match_after"], "implementation_and_build_unchanged_during_integration", result)
        result["build_pair_after"] = build_pair()
        if result["build_pair_before"]["present"]:
            check(result["build_pair_after"] == result["build_pair_before"],
                  "source_to_build_pair_manifest_stays_matched_after_whole_suite", result)
        counts = instance.counts()
        check(counts["real_provider_attempts"] == 0, "synthetic_suite_never_dispatched_real_model", result)
        result["transport_counts"] = counts
        result["status"] = "passed"
    except Exception as error:
        result["status"] = "failed"
        result["failure"] = {"type": type(error).__name__, "message": str(error)}
        try:
            browser.snapshot("failure")
            browser.command("screenshot", f"--filename={directory / 'failure-synthetic.png'}", "--full-page")
        except Exception as capture_error:
            result["failure_capture"] = type(capture_error).__name__
    finally:
        result["browser_cleanup"] = browser.close()
        result["server_cleanup"] = instance.stop()
        write_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", default="workspace-v2-" + time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--backend-port", type=int, default=19170)
    parser.add_argument("--browser-path", type=Path)
    parser.add_argument("--ffmpeg-path", type=Path)
    parser.add_argument("--mode", choices=["full", "video", "files", "attachments", "late"], default="full")
    parser.add_argument("--serve-fixture", type=Path)
    args = parser.parse_args()
    if args.serve_fixture:
        return asyncio.run(serve_fixture(args.serve_fixture, args.backend_port))
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run-id must contain only letters, digits, underscores and hyphens")
    executable = args.browser_path or (Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe")
    if not executable.is_file():
        raise RuntimeError("BROWSER_MISSING: specify existing fixed Chromium")
    if not port_closed(args.backend_port):
        raise RuntimeError("PORT_IN_USE: refusing another process's listener")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    record = (video_probe(directory, executable, args.backend_port, args.ffmpeg_path)
              if args.mode == "video"
              else integration(directory, executable, args.backend_port, args.ffmpeg_path, args.mode))
    print(json.dumps({"status": record["status"], "evidence": str(directory / "result.json")}, ensure_ascii=False))
    return 0 if record["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
