"""Export DSH Study SVG masters with the project's pinned browser and Pillow."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import xml.etree.ElementTree as ET
import zipfile

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "resources/branding/dsh-study"
OUTPUT = ROOT / "output/playwright/dsh-study-icons-v1"
APP_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 192, 256, 512, 1024)
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
TRAY_SIZES = (16, 20, 24, 32, 48)
NS = {"s": "http://www.w3.org/2000/svg"}


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def paths_from(path: Path, group_id: str) -> str:
    root = ET.fromstring(path.read_text("utf-8"))
    group = root.find(f"s:g[@id='{group_id}']", NS)
    if group is None:
        raise ValueError(f"BRAND_MASTER_GROUP_MISSING: {group_id}")
    return "\n".join(f'    <path d="{item.attrib["d"]}"/>' for item in group)


def mark_svg(paths: str, color: str, *, tray: bool = False) -> str:
    transform = ' transform="translate(-76 -118) scale(1.15)"' if tray else ""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" '
        'viewBox="0 0 1024 1024" role="img" aria-label="DSH Study">\n'
        f'  <g fill="{color}"{transform}>\n{paths}\n  </g>\n</svg>\n'
    )


def write_ico(path: Path, frames: list[Path]) -> None:
    """Keep the independently rasterized PNG for every ICO size, without resampling."""
    images = [frame.read_bytes() for frame in frames]
    offset = 6 + 16 * len(frames)
    directory = []
    for frame, payload in zip(frames, images, strict=True):
        with Image.open(frame) as image:
            size = image.width
        directory.append(struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(payload), offset,
        ))
        offset += len(payload)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(frames))
                     + b"".join(directory) + b"".join(images))


def preview_html() -> str:
    def app(size: int) -> str:
        return f'<img width="{size}" height="{size}" src="png/icon-{size}.png" alt="">'

    def row(theme: str) -> str:
        apps = "".join(f'<span>{app(size)}<small>{size} px</small></span>'
                       for size in (16, 20, 24, 32, 48, 64, 128))
        tone = "dark" if theme == "light" else "light"
        trays = "".join(
            f'<span><img width="{size}" height="{size}" '
            f'src="tray/{tone}-{size}.png" alt=""><small>{size} px</small></span>'
            for size in TRAY_SIZES
        )
        label = "浅色背景" if theme == "light" else "深色背景"
        return (f'<section class="{theme}"><h2>{label}</h2><div class="sizes">{apps}</div>'
                f'<div class="tray"><b>托盘简化版</b>{trays}</div></section>')

    return f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>DSH Study — 正式图标</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#eeedf4;color:#202124;
font-family:"Segoe UI","Microsoft YaHei",sans-serif}}main{{width:1160px;margin:auto;padding:40px}}
header{{display:flex;align-items:center;gap:32px;margin-bottom:28px}}h1{{font-size:42px;
letter-spacing:-1px;margin:0 0 8px}}p{{margin:6px 0;color:#62616b}}.tag{{font-size:14px}}
section{{padding:24px 28px;border-radius:20px;margin-top:18px}}section.light{{background:#fafafa}}
section.dark{{background:#202124;color:#f9fafb}}h2{{font-size:16px;font-weight:600;margin:0 0 22px}}
.sizes,.tray{{display:flex;align-items:center;gap:30px}}span{{display:flex;flex-direction:column;
align-items:center;gap:10px;min-width:44px}}small{{font-size:12px;opacity:.65}}
img{{display:block}}.tray{{margin-top:24px;padding-top:20px;border-top:1px solid #8883;gap:30px}}
.tray b{{font-size:13px;font-weight:500;width:96px}}footer{{font-size:13px;color:#62616b;margin-top:22px}}
</style><main><header>{app(256)}<div><h1>DSH Study</h1>
<p>展开的书页 · S 形留白 · 靛蓝方案 A</p><p class="tag">INDIGO #4F46E5 / WHITE #FFFFFF</p>
<p class="tag">纯矢量母版 · 平涂颜色 · 各尺寸独立渲染</p></div></header>
{row("light")}{row("dark")}
<footer>图标素材预览 · 小图按标注尺寸 1:1 显示 · 尚未接入桌面应用</footer></main></html>'''


def validate() -> dict:
    for size in APP_SIZES:
        with Image.open(ASSETS / f"png/icon-{size}.png") as image:
            assert image.size == (size, size) and image.mode == "RGBA"
            # A 16px rounded corner may contain a partially covered antialiased pixel.
            assert image.getpixel((0, 0))[3] < 64
            assert image.getpixel((size // 2, size // 8)) == (79, 70, 229, 255)
    for tone in ("dark", "light"):
        for size in TRAY_SIZES:
            with Image.open(ASSETS / f"tray/{tone}-{size}.png") as image:
                assert image.size == (size, size) and image.mode == "RGBA"
                assert image.getpixel((0, 0))[3] == 0
                assert image.getchannel("A").getextrema() == (0, 255)
    with Image.open(ASSETS / "icon.ico") as icon:
        assert icon.ico.sizes() == {(size, size) for size in ICO_SIZES}
        for size in ICO_SIZES:
            frame = icon.ico.getimage((size, size)).convert("RGBA")
            with Image.open(ASSETS / f"png/icon-{size}.png") as png:
                assert frame.tobytes() == png.tobytes()
    for tone in ("dark", "light"):
        with Image.open(ASSETS / f"tray-{tone}.ico") as icon:
            assert icon.ico.sizes() == {(size, size) for size in TRAY_SIZES}
    return {"png_dimensions_alpha_color": "passed", "ico_frames": "passed",
            "app_sizes": APP_SIZES, "tray_sizes": TRAY_SIZES}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-path", type=Path, default=(
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe"
    ))
    args = parser.parse_args()
    node = ROOT / ".tools/node/node.exe"
    cli = ROOT / "node_modules/@playwright/cli/playwright-cli.js"
    for required in (node, cli, args.browser_path):
        if not required.is_file():
            raise FileNotFoundError(required)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for folder in ("png", "mark", "tray"):
        (ASSETS / folder).mkdir(parents=True, exist_ok=True)
    pages = paths_from(ASSETS / "icon.svg", "pages")
    tray_pages = paths_from(ASSETS / "tray-dark.svg", "tray-pages")
    for tone, color in (("indigo", "#4F46E5"), ("dark", "#202124"), ("light", "#FFFFFF")):
        write(ASSETS / f"mark-{tone}.svg", mark_svg(pages, color))
    write(ASSETS / "tray-light.svg", mark_svg(tray_pages, "#FFFFFF", tray=True))
    jobs = [("icon.svg", f"png/icon-{size}.png", size) for size in APP_SIZES]
    jobs += [(f"mark-{tone}.svg", f"mark/{tone}-1024.png", 1024)
             for tone in ("indigo", "dark", "light")]
    jobs += [(f"tray-{tone}.svg", f"tray/{tone}-{size}.png", size)
             for tone in ("dark", "light") for size in TRAY_SIZES]
    # Run a single tiny export first so path, alpha, dimensions and color are checked
    # before the remaining assets. The check uses the actual rendered SVG pixels.
    jobs.sort(key=lambda job: (job[1] != "png/icon-32.png", job[1]))
    payload = [{"svg": (ASSETS / source).read_text("utf-8"),
                "path": str(ASSETS / target), "size": size}
               for source, target, size in jobs]
    write(OUTPUT / "cli.config.json", json.dumps({"browser": {
        "browserName": "chromium", "launchOptions": {
            "executablePath": str(args.browser_path), "headless": True,
        }, "contextOptions": {"deviceScaleFactor": 1},
    }}, indent=2))
    write(OUTPUT / "render.js", '''async page => {
const jobs = ''' + json.dumps(payload) + ''';
await page.route('**/*', route => {
  if (/^https?:/.test(route.request().url())) return route.abort();
  return route.continue();
});
for (const [index, job] of jobs.entries()) {
  await page.setViewportSize({width:job.size,height:job.size});
  const svg = job.svg.replace(/width="\\d+" height="\\d+"/,
    `width="${job.size}" height="${job.size}"`);
  await page.setContent(`<style>html,body{margin:0;background:transparent}
    svg{display:block}</style>${svg}`);
  if (index === 0) {
    const probe = await page.evaluate(async source => {
      const picture = new Image();
      picture.src = 'data:image/svg+xml,' + encodeURIComponent(source);
      await picture.decode();
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 32;
      const ctx = canvas.getContext('2d'); ctx.drawImage(picture,0,0,32,32);
      return {corner:[...ctx.getImageData(0,0,1,1).data],
        color:[...ctx.getImageData(16,4,1,1).data]};
    }, svg);
    if (probe.corner[3] !== 0 || JSON.stringify(probe.color) !== '[79,70,229,255]')
      throw new Error('BRAND_SMALL_EXPORT_INVALID: ' + JSON.stringify(probe));
  }
  await page.screenshot({path:job.path,omitBackground:true,animations:'disabled'});
}
console.log('BRAND_RASTER_EXPORTS=' + jobs.length);
}''')
    command = [str(node), str(cli), "-s=dsh-study-brand-assets"]
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"NODE_OPTIONS", "ELECTRON_RUN_AS_NODE"}}

    def run(*arguments: str) -> str:
        result = subprocess.run(
            [*command, *arguments], cwd=ROOT, env=environment,
            capture_output=True, text=True, encoding="utf-8", timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=True,
        )
        if "### Error" in result.stdout:
            raise RuntimeError(result.stdout)
        return result.stdout

    try:
        run("open", "about:blank", "--config", str(OUTPUT / "cli.config.json"))
        run("run-code", "--filename", str(OUTPUT / "render.js"))
        write_ico(ASSETS / "icon.ico", [ASSETS / f"png/icon-{size}.png"
                                       for size in ICO_SIZES])
        for tone in ("dark", "light"):
            write_ico(ASSETS / f"tray-{tone}.ico", [
                ASSETS / f"tray/{tone}-{size}.png" for size in TRAY_SIZES
            ])
        checks = validate()
        write(ASSETS / "preview.html", preview_html())
        write(OUTPUT / "preview.js", '''async page => {
await page.setViewportSize({width:1160,height:1100});
await page.goto(''' + json.dumps((ASSETS / "preview.html").as_uri()) + ''');
await page.evaluate(() => Promise.all([...document.images].map(img => img.decode())));
await page.screenshot({path:''' + json.dumps(str(ASSETS / "preview.png")) + ''',fullPage:true});
console.log('BRAND_PREVIEW_READY');
}''')
        run("run-code", "--filename", str(OUTPUT / "preview.js"))
    finally:
        run("close")
    files = {}
    for path in sorted(ASSETS.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            files[path.relative_to(ASSETS).as_posix()] = {
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "bytes": path.stat().st_size,
            }
    write(ASSETS / "manifest.json", json.dumps({
        "product": "DSH Study", "edition": "indigo-v1", "primary_color": "#4F46E5",
        "foreground": "#FFFFFF", "masters": ["icon.svg", "tray-dark.svg"],
        "rendering": "project Playwright CLI; Chromium; deviceScaleFactor=1",
        "checks": checks, "files": files,
    }, ensure_ascii=False, indent=2) + "\n")
    archive = OUTPUT / "dsh-study-icons-v1.zip"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(ASSETS.rglob("*")):
            if path.is_file():
                bundle.write(path, Path("dsh-study") / path.relative_to(ASSETS))
    print(json.dumps({"assets": str(ASSETS), "archive": str(archive), "checks": checks},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
