"""Real mouse regression for the user-reported full block-matrix quote.

Only synthetic content is shown. Vite transforms the production MarkdownAnswer;
the project Playwright CLI owns all browser/server processes. No model calls.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from test_browser import Browser, NODE, ROOT, check, port_closed, wait_until, write_json

from learning_app.processes import OwnedProcessGroup, require_free_port

LATEX = r"M=\begin{pmatrix}O&A\\B&E\end{pmatrix}."
SAMPLE = "合成分块矩阵 $" + LATEX + "$。后面的文字不应被引用。"


def run(directory: Path, port: int, executable: Path) -> dict:
    require_free_port("127.0.0.1", port)
    paths = [
        ROOT / "frontend/src/chat/MarkdownAnswer.tsx",
        ROOT / "frontend/src/chat/selection.ts",
    ]
    result = {
        "run_id": directory.name,
        "status": "running",
        "evidence_level": "real Chromium mouse drag and production MarkdownAnswer; synthetic only",
        "real_model_attempts": 0,
        "source_digests": {
            path.relative_to(ROOT).as_posix(): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in paths
        },
    }
    html = """<!doctype html><html><head><meta charset='utf-8'><style>
      body{padding:48px;background:#181818;color:white;font-family:'Microsoft YaHei UI',sans-serif}
      #root{width:760px;font-size:28px;line-height:1.8}.markdown-answer{position:relative}
      .selection-quote-button{background:white;color:#181818;border:0;border-radius:8px;padding:8px 12px;font-size:14px}
      .selection-quote-error{font-size:14px;background:#542020;padding:8px}#quotes{font-size:14px}
      </style></head><body><h1>合成内容 · 分块矩阵真实鼠标验证</h1>
      <main id='root'></main><pre id='quotes'></pre><script type='module'>
      import RefreshRuntime from '/@react-refresh';RefreshRuntime.injectIntoGlobalHook(window);
      window.$RefreshReg$=()=>{};window.$RefreshSig$=()=>type=>type;
      window.__vite_plugin_react_preamble_installed__=true;
      const reactModule=await import('/node_modules/.vite/deps/react.js');
      const React=reactModule.default??reactModule;
      const domModule=await import('/node_modules/.vite/deps/react-dom_client.js');
      const {createRoot}=domModule.default??domModule;
      const {MarkdownAnswer}=await import('/src/chat/MarkdownAnswer.tsx');
      window.sample=SAMPLE_VALUE;window.quotes=[];
      createRoot(document.querySelector('#root')).render(React.createElement(MarkdownAnswer,{
        text:window.sample,onQuote:quote=>{window.quotes.push(quote);
        document.querySelector('#quotes').textContent=JSON.stringify(window.quotes,null,2)}}));
      </script></body></html>""".replace(
        "SAMPLE_VALUE", json.dumps(SAMPLE, ensure_ascii=False)
    )
    target = directory / "synthetic-matrix.html"
    target.write_text(html, encoding="utf-8")
    group = OwnedProcessGroup("matrix-selection-vite")
    browser = Browser(directory / "browser", executable)
    log = (directory / "vite.stdout.txt").open("w", encoding="utf-8")
    try:
        group.spawn(
            [
                str(NODE),
                str(ROOT / "frontend/node_modules/vite/bin/vite.js"),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--strictPort",
            ],
            cwd=ROOT / "frontend",
            env=os.environ,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        wait_until(lambda: not port_closed(port), timeout=20)
        browser.command(
            "open",
            f"http://127.0.0.1:{port}/@fs/{target.as_posix()}",
            f"--config={browser.config}",
        )
        browser.command(
            "run-code",
            """async(page)=>{
          await page.locator('.katex-html').waitFor({state:'visible',timeout:20000});
          await page.evaluate(()=>document.fonts.ready);
        }""",
        )
        snapshot = browser.snapshot("matrix-loaded")
        check("合成分块矩阵" in snapshot, "production_component_loaded_matrix", result)
        browser.command(
            "run-code",
            """async(page)=>{
          const points=await page.evaluate(()=>{
            const nodes=[];const walker=document.createTreeWalker(document.querySelector('.katex-html'),NodeFilter.SHOW_TEXT);
            let n;while(n=walker.nextNode()){if(n.data==='M'||n.data==='.')nodes.push(n);}
            const first=nodes.find(n=>n.data==='M');const last=nodes.find(n=>n.data==='.');
            const a=document.createRange();a.selectNodeContents(first);
            const b=document.createRange();b.selectNodeContents(last);
            const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();
            return {a:{x:x.left+.2,y:x.top+x.height/2},b:{x:y.right-.2,y:y.top+y.height/2}};
          });await page.mouse.move(points.a.x,points.a.y);await page.mouse.down();
          await page.mouse.move(points.b.x,points.b.y,{steps:18});await page.mouse.up();
        }""",
        )
        browser.snapshot("matrix-selected")
        observed = browser.evaluate("""()=>({selected:window.getSelection().toString(),
          quoteCount:window.quotes.length,error:document.querySelector('.selection-quote-error')?.textContent,
          button:!!document.querySelector('.selection-quote-button')})""")
        check(
            observed["quoteCount"] == 0, "mouse_selection_does_not_auto_quote", result
        )
        check(
            observed["button"] and not observed.get("error"),
            "full_visible_matrix_has_quote_entry_without_unmapped_error",
            result,
            observed=observed,
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.screenshot({path:"
            + json.dumps((directory / "matrix-selected.png").as_posix())
            + "});await page.getByRole('button',{name:'添加到对话',exact:true}).click();}",
        )
        quote = browser.evaluate("()=>window.quotes[0]")
        start = SAMPLE.index(LATEX)
        check(
            quote["markdown"] == "$" + LATEX + "$",
            "matrix_markdown_structure_preserved",
            result,
            observed=quote,
        )
        check(
            quote["source_start"] == start
            and quote["source_end"] == start + len(LATEX),
            "matrix_exact_original_source_range",
            result,
        )
        check(
            "后面的文字" not in quote["markdown"],
            "unselected_outside_text_not_added",
            result,
        )
        browser.command(
            "run-code",
            """async(page)=>{
          const point=await page.evaluate(()=>{
            const start=window.sample.indexOf('A');
            const el=document.querySelector('.katex-html [data-selection-start="'+start+'"]');
            const r=el.getBoundingClientRect();return {left:r.left+.2,right:r.right-.2,y:r.top+r.height/2};
          });await page.mouse.move(point.left,point.y);await page.mouse.down();
          await page.mouse.move(point.right,point.y,{steps:8});await page.mouse.up();
          await page.getByRole('button',{name:'添加到对话',exact:true}).click();
        }""",
        )
        partial = browser.evaluate("()=>window.quotes[1]")
        check(
            partial["markdown"] == "$A$",
            "partial_cell_does_not_expand_to_matrix",
            result,
            observed=partial,
        )
        result["status"] = "passed"
    except Exception as error:
        result["status"] = "failed"
        result["error"] = str(error)
    finally:
        result["browser_cleanup"] = browser.close()
        result["server_cleanup"] = group.close()
        log.close()
        result["port_closed"] = port_closed(port)
        result["source_digests_match_after"] = all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == expected
            for name, expected in result["source_digests"].items()
        )
        write_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--port", type=int, default=19174)
    parser.add_argument("--browser-path", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("invalid run-id")
    executable = args.browser_path or (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe"
    )
    if not executable.is_file():
        parser.error("existing project Chromium is required; no browser is downloaded")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    result = run(directory, args.port, executable)
    print(
        json.dumps(
            {"status": result["status"], "evidence": str(directory / "result.json")},
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
