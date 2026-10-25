"""Measure playback through the actual native resource UI, without capturing video pixels."""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--item", type=Path, required=True)
    parser.add_argument("--session", default="bili-final")
    args = parser.parse_args()
    path = args.item.resolve()
    if not path.is_relative_to(ROOT / ".runtime/platforms"):
        raise RuntimeError("VERIFY_SCOPE_DENIED")
    item = json.loads(path.read_text("utf-8"))
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/desktop/run.py"),
            "--backend-port",
            "8876",
            "--no-prepare",
        ],
        cwd=ROOT,
        check=True,
        stdout=subprocess.DEVNULL,
    )
    file_path = Path(item["path"])
    folder, name = file_path.parent.name, file_path.name
    code = (
        """async (page) => {
      await page.getByRole('tab', {name:'文件', exact:true}).click({force:true});
      const files=page.getByRole('region',{name:'文件资源管理器',exact:true});
      await files.getByRole('navigation',{name:'文件夹位置'}).getByRole('button').first().click({force:true});
      await files.getByRole('option',{name:FOLDER,exact:true}).dblclick({force:true});
      await files.getByRole('option',{name:NAME,exact:true}).dblclick({force:true});
      const selector='video[data-primary-video][src*="/videos/'+ASSETID+'/"]';
      const video=page.locator(selector);
      await video.waitFor();
      await page.waitForFunction(sel=>document.querySelector(sel)?.readyState>=3,selector,{timeout:25000,polling:100});
      const slider=page.getByRole('slider',{name:'视频播放进度'});
      await slider.click({force:true});
      if(await video.evaluate(v=>v.paused)) await page.getByRole('button',{name:'播放或暂停'}).click({force:true});
      await page.waitForFunction(sel=>{
        const v=document.querySelector(sel);const a=v?.closest('.video-workspace')?.querySelector('audio');
        return v&&!v.seeking&&v.readyState>=3&&(!a||(!a.seeking&&a.readyState>=3));
      },selector,{timeout:45000,polling:100});
      const before=await video.evaluate(v=>v.currentTime);
      await page.waitForTimeout(3000);
      return await video.evaluate((v,before)=>{
        const scope=v.closest('.video-workspace')||document;
        const a=scope.querySelector('audio');
        return {asset:new URL(v.currentSrc).pathname.split('/')[4],time:v.currentTime,advance:v.currentTime-before,forcedBrowserInput:true,
          duration:v.duration,paused:v.paused,ready:v.readyState,
          videoFrames:v.getVideoPlaybackQuality().totalVideoFrames,audioBytes:v.webkitAudioDecodedByteCount,
          separateAudio:!!a,syncError:a?Math.abs(a.currentTime-v.currentTime):null,
          onlySameOriginMedia:[...document.querySelectorAll('video,audio')].every(x=>new URL(x.currentSrc).origin===location.origin),
          localBadge:scope.querySelector('.integration-platform-badge')?.textContent.includes('本地副本'),
          subtitleError:scope.querySelector('.subtitle-panel [role=alert]')?.textContent||null};
      },before);
    }""".replace("FOLDER", json.dumps(folder, ensure_ascii=False))
        .replace("NAME", json.dumps(name, ensure_ascii=False))
        .replace("ASSETID", json.dumps(item["asset_id"]))
    )
    evidence = ROOT / ".runtime/platforms/evidence"
    stamp = str(time.time_ns())
    code_file = evidence / ("desktop-playback-" + stamp + ".js")
    code_file.write_text(code, encoding="utf-8")
    command = [
        str(ROOT / ".tools/node/node.exe"),
        str(ROOT / ".tools/pnpm/bin/pnpm.cjs"),
        "exec",
        "playwright-cli",
        "-s=" + args.session,
        "run-code",
        "--filename",
        str(code_file),
    ]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=60
    )
    (evidence / ("desktop-playback-" + stamp + ".log")).write_text(
        result.stdout + result.stderr, encoding="utf-8"
    )
    if result.returncode or "### Result\n" not in result.stdout:
        raise RuntimeError("DESKTOP_PLAYBACK_VERIFY_FAILED: see private evidence log")
    data = json.loads(result.stdout.split("### Result\n", 1)[1].split("\n###", 1)[0])
    (evidence / ("desktop-playback-" + stamp + ".json")).write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    if data["paused"] or data["advance"] < 2 or not data["onlySameOriginMedia"]:
        raise RuntimeError("DESKTOP_PLAYBACK_NOT_RUNNING")
    print(json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
