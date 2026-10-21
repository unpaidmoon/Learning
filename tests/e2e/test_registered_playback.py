"""Playback-only check of the explicitly registered local video.

Never captures a screenshot, canvas, trace, audio, frame or source body. The video
exists only during one run-code callback and is removed before CLI auto-snapshot.
Only numeric media state and HTTP range headers leave the callback. Private path,
business DB and CLI transcript stay under ignored output/playwright.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

from test_browser import ROOT, Browser, Instance, write_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run-id", default=f"registered-playback-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    parser.add_argument(
        "--source", type=Path, default=Path(r"D:\Download\26822053135-1-192.mp4")
    )
    parser.add_argument("--browser-path", type=Path)
    parser.add_argument("--backend-port", type=int, default=18795)
    parser.add_argument("--frontend-port", type=int, default=18796)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("run-id只能包含字母、数字、横线和下划线")
    # Do not broaden playback authorization through a free-form default.
    authorized = Path(r"D:\Download\26822053135-1-192.mp4").resolve()
    if args.source.resolve() != authorized:
        parser.error("仅允许验证本任务已登记的真实视频；其他真实素材需要明确授权")
    if not authorized.is_file():
        parser.error("已登记的原件不在原路径；未搜索其他真实资料")
    executable = args.browser_path or (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe"
    )
    if not executable.is_file():
        parser.error("找不到固定Chromium，请使用--browser-path；不自动安装")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    result = {
        "run_id": args.run_id,
        "scope": "authorized registered video playback only; no image or audio inspection",
        "real_provider_attempts": 0,
        "captures": {
            "screenshots": 0,
            "canvas": 0,
            "traces": 0,
            "audio": 0,
            "frames": 0,
        },
        "status": "failed",
    }
    write_json(directory / "private-source.json", {"path": str(authorized)})
    instance = Instance(
        directory / "app", "start", args.backend_port, args.frontend_port
    )
    browser = Browser(directory / "browser", executable)
    try:
        instance.start()
        # No real video exists in the initial application page or its snapshot.
        browser.command("open", instance.url, "--config", str(browser.config))
        code = r"""async (page) => {
          const ranges = [];
          const listener = response => {
            if (response.url().includes('/content?source_version=')) {
              const h = response.headers();
              ranges.push({status:response.status(),range:response.request().headers()['range']||null,
                contentRange:h['content-range']||null,length:Number(h['content-length']||0)});
            }
          };
          page.on('response',listener);
          let observed;
          try {
            observed = await page.evaluate(async source => {
              document.body.replaceChildren();
              const session = await (await fetch('/api/v1/session')).json();
              const headers = {'Content-Type':'application/json','X-Learning-CSRF':session.csrf_token};
              async function post(path,body) {
                const response = await fetch('/api/v1'+path,{method:'POST',headers,body:JSON.stringify(body)});
                if (!response.ok) { const e=await response.json(); throw new Error(e.code||'HTTP_'+response.status); }
                return response.json();
              }
              const course = await post('/courses',{title:'已授权视频纯播放验证'});
              const asset = await post('/courses/'+course.course_id+'/videos',{path:source});
              const opened = await post('/videos/'+asset.asset_id+'/open',{source_version:asset.source_version});
              const url = '/api/v1/videos/'+asset.asset_id+'/content?source_version='+asset.source_version;
              const head = await fetch(url,{method:'HEAD'});
              const video = document.createElement('video');
              video.muted=true;
              video.preload='metadata';
              video.playsInline=true;
              // No drawing/readback or external media inspection is performed.
              document.body.append(video);
              const state = () => ({readyState:video.readyState,currentTime:video.currentTime,
                duration:video.duration,videoWidth:video.videoWidth,videoHeight:video.videoHeight,paused:video.paused});
              function event(name) {
                return new Promise((resolve,reject)=>{
                  const timer=setTimeout(()=>{cleanup();reject(new Error('MEDIA_EVENT_TIMEOUT_'+name));},20000);
                  const done=()=>{cleanup();resolve();};
                  const error=()=>{cleanup();reject(new Error('MEDIA_DECODE_ERROR_'+(video.error?.code||0)));};
                  const cleanup=()=>{clearTimeout(timer);video.removeEventListener(name,done);video.removeEventListener('error',error);};
                  video.addEventListener(name,done,{once:true});
                  video.addEventListener('error',error,{once:true});
                });
              }
              async function advances() {
                const before=video.currentTime;
                for(let i=0;i<30 && video.currentTime<before+0.2;i++) await event('timeupdate');
                if(video.currentTime<before+0.2) throw new Error('PLAYBACK_DID_NOT_ADVANCE');
              }
              try {
                const metadata=event('loadedmetadata');video.src=url;await metadata;
                const loaded=state();
                await video.play();await advances();video.pause();
                const played=state();
                const target=Math.min(video.duration-2,Math.max(30,video.duration*0.6));
                const sought=event('seeked');video.currentTime=target;await sought;
                const seeked=state();
                await video.play();await advances();video.pause();
                const resumed=state();
                const saved=await post('/videos/'+asset.asset_id+'/position',{
                  source_version:asset.source_version,position_ms:Math.round(video.currentTime*1000),
                  expected_revision:opened.position_revision});
                const restored=await post('/videos/'+asset.asset_id+'/open',{source_version:asset.source_version});
                return {source:{asset_id:asset.asset_id,sha256:asset.source_version,size_bytes:asset.size_bytes},
                  head:{status:head.status,length:Number(head.headers.get('content-length'))},
                  loaded,played,seeked,resumed,target,
                  saved:{position_ms:saved.position_ms,position_revision:saved.position_revision},
                  restored:{position_ms:restored.position_ms,position_revision:restored.position_revision}};
              } finally {
                video.pause();video.removeAttribute('src');video.load();video.remove();
                document.body.replaceChildren(document.createTextNode('纯播放验证已结束'));
              }
            }, SOURCE_PATH);
          } finally { page.off('response',listener); }
          await page.evaluate(value=>{window.__registeredPlayback=value;},{observed,ranges});
        }""".replace("SOURCE_PATH", json.dumps(str(authorized), ensure_ascii=False))
        browser.command("run-code", code, raw=True)
        # The video has already been removed before this numeric-only read.
        observed = browser.evaluate("() => window.__registeredPlayback")
        if observed is None:
            raise AssertionError("未取得纯数字媒体状态记录，私有CLI记录保留")
        result["playback"] = observed
        fact = observed["observed"]
        checks = {
            "metadata": fact["loaded"]["duration"] > 0
            and fact["loaded"]["videoWidth"] > 0,
            "played": fact["played"]["currentTime"] > 0.2 and fact["played"]["paused"],
            "seeked": abs(fact["seeked"]["currentTime"] - fact["target"]) < 1,
            "resumed": fact["resumed"]["currentTime"]
            > fact["seeked"]["currentTime"] + 0.2,
            "saved_restored": fact["saved"] == fact["restored"],
            "range_206": any(
                row["status"] == 206 and row["contentRange"]
                for row in observed["ranges"]
            ),
            "head_length": fact["head"]["status"] == 200
            and fact["head"]["length"] == fact["source"]["size_bytes"],
        }
        result["checks"] = checks
        if not all(checks.values()):
            raise AssertionError("已登记视频纯播放断言未通过")
        result["status"] = "passed"
    except Exception as exc:
        result["error"] = str(exc)
    finally:
        result["cleanup"] = {"browser": browser.close(), "application": instance.stop()}
        write_json(directory / "result.json", result)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
