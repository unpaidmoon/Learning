"""Production CSS/Button/MarkdownAnswer, real Chromium and synthetic content."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from test_browser import (
    Browser,
    Instance,
    NODE,
    ROOT,
    port_closed,
    wait_until,
    write_json,
)
from test_native_video_picker import api
from learning_app.processes import OwnedProcessGroup, require_free_port

LATEX = r"\det M = \det A \det B = 3\cdot(-1)=-3."
SAMPLE = "合成公式 $" + LATEX + "$ 后文不得加入。"


def run(directory: Path, port: int, executable: Path) -> dict:
    require_free_port("127.0.0.1", port)
    paths = [
        ROOT / "frontend/src" / name
        for name in [
            "styles.css",
            "components/ui/button.tsx",
            "chat/MarkdownAnswer.tsx",
            "chat/selection.ts",
        ]
    ]
    result = {
        "run_id": directory.name,
        "status": "running",
        "real_model_attempts": 0,
        "scope": "real Chromium; production CSS/Button/MarkdownAnswer; synthetic local page",
        "checks": {},
        "source_before": {
            p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths
        },
    }
    html = """<!doctype html><meta charset='utf-8'><div id='root'></div>
    <script type='module'>
    import RefreshRuntime from '/@react-refresh';RefreshRuntime.injectIntoGlobalHook(window);
    window.$RefreshReg$=()=>{};window.$RefreshSig$=()=>type=>type;
    window.__vite_plugin_react_preamble_installed__=true;
    await import('/src/styles.css');await import('/src/library/library.css');
    const rm=await import('/node_modules/.vite/deps/react.js');const React=rm.default??rm;
    const dm=await import('/node_modules/.vite/deps/react-dom_client.js');
    const {createRoot}=dm.default??dm;
    const {Button}=await import('/src/components/ui/button.tsx');
    const {MarkdownAnswer}=await import('/src/chat/MarkdownAnswer.tsx');
    const km=await import('/node_modules/katex/dist/katex.mjs');const katex=km.default??km;
    const e=React.createElement;window.quotes=[];window.sample=SAMPLE_VALUE;
    const variants=['default','outline','secondary','ghost','destructive','link'];
    const select=(id,items)=>e('select',{id},items.map(t=>e('option',{key:t},t)));
    const theme=t=>e('section',{key:t,id:t,className:'app-shell '+(t==='dark'?'dark':''),
      style:{height:'auto',minHeight:0,padding:24,gap:12}},
      e('h2',null,t==='dark'?'深色实际组件':'浅色实际组件'),
      e('div',{style:{display:'flex',gap:12}},variants.map(v=>e(Button,{key:v,variant:v,
        'data-variant':v},v==='default'?'刷新模型与额度':v))),
      e('div',{className:'file-dialog'},e('h2',null,'新建文件夹 / 重命名'),
        e('label',null,'名称',e('input',{defaultValue:'合成目录'})),
        e('div',{className:'file-dialog-buttons'},e(Button,{variant:'ghost'},'取消'),
          e(Button,{disabled:true,'data-disabled-check':true},'创建'),e(Button,null,'确认'))),
      e('div',{style:{display:'flex',gap:16}},
        e('div',{className:'file-manager-view'},select('sort-'+t,['名称','类型','大小','修改时间'])),
        e('div',{className:'chat-title'},select('history-'+t,['对话1','对话2','对话3']))));
    createRoot(document.querySelector('#root')).render(e(React.Fragment,null,
      theme('light'),theme('dark'),e('section',{id:'formula',className:'app-shell dark',
      style:{height:'auto',minHeight:220,padding:40,fontSize:26}},
      e(MarkdownAnswer,{text:window.sample,onQuote:q=>window.quotes.push(q)}))));
    const baseline=document.createElement('div');baseline.id='baseline';
    baseline.innerHTML=katex.renderToString(LATEX_VALUE,{trust:false});
    const wait=setInterval(()=>{const host=document.querySelector('#formula');if(host){
      host.append(baseline);clearInterval(wait)}},10);
    </script>""".replace("SAMPLE_VALUE", json.dumps(SAMPLE, ensure_ascii=False))
    html = html.replace("LATEX_VALUE", json.dumps(LATEX))
    target = directory / "synthetic-theme-formula.html"
    target.write_text(html, encoding="utf-8")
    group = OwnedProcessGroup("theme-formula-vite")
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
            "async(page)=>{await page.locator('.markdown-answer .katex-html').waitFor();"
            "await page.evaluate(()=>document.fonts.ready);}",
        )
        browser.snapshot("loaded-production-components")
        observed = browser.evaluate("""()=>{
          const canvas=document.createElement('canvas');canvas.width=canvas.height=1;
          const ctx=canvas.getContext('2d',{willReadFrequently:true});
          const rgba=c=>{ctx.clearRect(0,0,1,1);ctx.fillStyle=c;ctx.fillRect(0,0,1,1);
            const p=Array.from(ctx.getImageData(0,0,1,1).data);return [...p.slice(0,3),p[3]/255]};
          const rgb=c=>rgba(c).slice(0,3);
          const mix=(fg,bg,alpha)=>fg.map((v,i)=>v*alpha+bg[i]*(1-alpha));
          const lum=c=>c.map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4})
            .reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
          const ratio=(a,b)=>{const x=lum(a),y=lum(b);return (Math.max(x,y)+.05)/(Math.min(x,y)+.05)};
          const background=el=>{const s=getComputedStyle(el),c=s.backgroundColor;
            const parts=rgba(c);
            const parent=el.parentElement?background(el.parentElement):[255,255,255];
            return mix(parts.slice(0,3),parent,parts[3]??1)};
          window.themeReadButton=b=>{
            const s=getComputedStyle(b),bg=background(b),parent=background(b.parentElement);
            const alpha=Number(s.opacity);return {text:b.textContent,disabled:b.disabled,
              variant:b.dataset.variant,color:s.color,background:s.backgroundColor,
              contrast:ratio(mix(rgb(s.color),parent,alpha),mix(bg,parent,alpha))};};
          return ['light','dark'].map(theme=>{
            const root=document.querySelector('#'+theme);
            return {theme,scheme:getComputedStyle(root).colorScheme,
              buttons:Array.from(root.querySelectorAll('[data-slot=button]')).map(window.themeReadButton),
              options:Array.from(root.querySelectorAll('option')).map(o=>{
                const s=getComputedStyle(o);return {text:o.textContent,color:s.color,
                  background:s.backgroundColor,explicitSurface:s.backgroundColor!=='rgba(0, 0, 0, 0)'
                    &&s.backgroundColor!=='transparent',contrast:ratio(rgb(s.color),background(o))};})};})} """)
        result["theme_observations"] = observed
        for theme in observed:
            prefix = theme["theme"]
            result["checks"][prefix + "_buttons_readable"] = all(
                b["contrast"] >= (2.5 if b["disabled"] else 4.5)
                for b in theme["buttons"]
            )
            result["checks"][prefix + "_option_foreground_and_surface"] = all(
                o["explicitSurface"] and o["contrast"] >= 4.5 for o in theme["options"]
            )
            result["checks"][prefix + "_explicit_native_color_scheme"] = (
                theme["scheme"] == prefix
            )
        browser.command(
            "run-code",
            "async(page)=>{await page.locator('#baseline .katex-html').waitFor();}",
        )
        spacing = browser.evaluate("""()=>{
          const original=document.querySelector('#baseline .katex-html').getBoundingClientRect();
          const mapped=document.querySelector('#formula .markdown-answer .katex-html').getBoundingClientRect();
          return {original:original.width,mapped:mapped.width,delta:Math.abs(original.width-mapped.width)};}""")
        result["operator_layout"] = spacing
        result["checks"]["operator_instrumentation_preserves_formula_width"] = (
            spacing["delta"] < 0.5
        )
        for theme in ("light", "dark"):
            browser.command(
                "run-code",
                "async(page)=>{const b=page.locator('#"
                + theme
                + " [data-variant=default]');await b.hover();"
                "await b.evaluate(async el=>{await Promise.all(el.getAnimations()"
                ".map(a=>a.finished.catch(()=>{})))});}",
            )
            hover = browser.evaluate(
                "()=>window.themeReadButton(document.querySelector('#"
                + theme
                + " [data-variant=default]'))"
            )
            result.setdefault("hover_observations", {})[theme] = hover
            result["checks"][theme + "_primary_hover_readable"] = (
                hover["contrast"] >= 4.5
            )
            for control in ("sort", "history"):
                browser.command(
                    "run-code",
                    "async(page)=>{await page.locator('#"
                    + control
                    + "-"
                    + theme
                    + "').click();await page.screenshot({path:"
                    + json.dumps(
                        (directory / f"{theme}-{control}-expanded.png").as_posix()
                    )
                    + "});await page.keyboard.press('Escape');}",
                )
        browser.command(
            "run-code",
            "async(page)=>{await page.screenshot({path:"
            + json.dumps((directory / "theme-controls.png").as_posix())
            + ",fullPage:true});await page.locator('#formula').scrollIntoViewIfNeeded();}",
        )
        browser.command(
            "run-code",
            """async(page)=>{
          const p=await page.evaluate(()=>{
            const walker=document.createTreeWalker(document.querySelector('.katex-html'),NodeFilter.SHOW_TEXT);
            const nodes=[];let n;while(n=walker.nextNode()){if(n.data==='det')nodes.push(n)}
            const a=document.createRange(),b=document.createRange();a.selectNodeContents(nodes[0]);b.selectNodeContents(nodes[2]);
            const x=a.getBoundingClientRect(),y=b.getBoundingClientRect();return {x1:x.left+.1,y1:x.top+x.height/2,
              x2:y.right-.1,y2:y.top+y.height/2};});
          await page.mouse.move(p.x1,p.y1);await page.mouse.down();
          await page.mouse.move(p.x2,p.y2,{steps:18});await page.mouse.up();} """,
        )
        selection = browser.evaluate("""()=>({selected:window.getSelection().toString(),
          entry:!!document.querySelector('.selection-quote-button'),
          error:document.querySelector('.selection-quote-error')?.textContent,quotes:window.quotes})""")
        result["mouse_selection"] = selection
        result["checks"]["actual_mouse_partial_det_has_entry"] = (
            selection["entry"]
            and not selection.get("error")
            and not selection["quotes"]
        )
        browser.command(
            "run-code",
            "async(page)=>{await page.screenshot({path:"
            + json.dumps((directory / "partial-det-selected.png").as_posix())
            + "});}",
        )
        if selection["entry"]:
            browser.command(
                "run-code",
                "async(page)=>{await page.getByRole('button',{name:'添加到对话',exact:true}).click();}",
            )
            quote = browser.evaluate("()=>window.quotes[0]")
            result["quote"] = quote
            result["checks"]["partial_det_exact_markdown_no_B_or_remainder"] = (
                quote["markdown"] == r"$\det M = \det A \det$"
                and SAMPLE[quote["source_start"] : quote["source_end"]]
                == r"\det M = \det A \det"
            )
        else:
            result["checks"]["partial_det_exact_markdown_no_B_or_remainder"] = False
        result["status"] = "passed" if all(result["checks"].values()) else "failed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["browser_cleanup"] = browser.close()
        result["server_cleanup"] = group.close()
        log.close()
        result["port_closed"] = port_closed(port)
        result["source_digests_match_after"] = all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == sha
            for name, sha in result["source_before"].items()
        )
        write_json(directory / "result.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--port", type=int, default=19177)
    parser.add_argument("--mode", choices=["components", "app"], default="components")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.run_id):
        parser.error("invalid run-id")
    executable = (
        Path(os.environ.get("LOCALAPPDATA", ""))
        / "ms-playwright/chromium-1234/chrome-win64/chrome.exe"
    )
    if not executable.is_file():
        parser.error("existing pinned Chromium required")
    directory = ROOT / "output/playwright" / args.run_id
    directory.mkdir(parents=True, exist_ok=False)
    result = (run_app if args.mode == "app" else run)(directory, args.port, executable)
    print(
        json.dumps(
            {
                "status": result["status"],
                "checks": result["checks"],
                "error": result.get("error"),
                "result": str(directory / "result.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "passed" else 1


def run_app(directory: Path, port: int, executable: Path) -> dict:
    """Actual built App/FileManager/AccountPanel; isolated business data only."""
    require_free_port("127.0.0.1", port)
    synthetic = directory / "synthetic-course"
    synthetic.mkdir()
    (synthetic / "synthetic-color.txt").write_text("SYNTHETIC", encoding="utf-8")
    instance = Instance(directory / "app", "start", port, port)
    browser = Browser(directory / "browser", executable)
    result = {
        "run_id": directory.name,
        "status": "running",
        "real_model_attempts": 0,
        "scope": "production built App/FileManager; isolated real business API; account GET DTO mocked for visual state only",
        "checks": {},
    }
    try:
        instance.start()
        browser.command("open", instance.url, f"--config={browser.config}")
        browser.wait_text("后台已连接")
        browser.command(
            "run-code",
            """async(page)=>{await page.route('**/api/v1/account',route=>
          route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
            route:'account',mode:'official',status:'credential-stored',can_sign_in:true,
            api_key_configured:false,attempt:null,profile:null,balance_status:'unknown',
            wallets:[],links:null,models:[],error_code:null})}));} """,
        )
        created = api(
            browser,
            "/courses/folder",
            {"path": str(synthetic), "title": "合成颜色验证"},
        )
        assert created["status"] == 200
        course = created["body"]["course_id"]
        for _ in range(2):
            assert api(browser, f"/courses/{course}/chats", {})["status"] == 200
        # Seed through the real API, then reload so App fetches the new list.
        browser.command("run-code", "async(page)=>{await page.reload();}")
        browser.command(
            "run-code",
            "async(page)=>{await page.getByRole('combobox',"
            "{name:'选择课程',exact:true}).selectOption("
            + json.dumps(course)
            + ");await page.locator('.file-manager-entry').first().waitFor();}",
        )

        def audit(selector: str):
            return browser.evaluate(
                """()=>{
              const b=document.querySelector(SELECTOR);if(!b)return {readable:false};
              const s=getComputedStyle(b),canvas=document.createElement('canvas');
              const ctx=canvas.getContext('2d');const normalize=c=>{ctx.fillStyle=c;return ctx.fillStyle};
              return {text:b.textContent,color:s.color,background:s.backgroundColor,disabled:b.disabled,
                readable:!!b.textContent.trim()&&normalize(s.color)===normalize(s.getPropertyValue('--primary-foreground'))
                  &&normalize(s.color)!==normalize(s.backgroundColor)};}""".replace(
                    "SELECTOR", json.dumps(selector)
                )
            )

        for theme in ("light", "dark"):
            if theme == "dark":
                browser.command(
                    "run-code",
                    "async(page)=>{await page.getByRole('button',"
                    "{name:'切换深色',exact:true}).click();}",
                )
            browser.command(
                "run-code",
                "async(page)=>{await page.getByRole('button',"
                "{name:'新建文件夹',exact:true}).click();"
                "await page.getByRole('dialog',{name:'新建文件夹',exact:true}).waitFor();}",
            )
            disabled = audit('.file-dialog button[type="submit"]')
            result.setdefault("observations", {})[theme + "_create_empty"] = disabled
            result["checks"][theme + "_real_create_disabled_label"] = (
                disabled["readable"] and disabled["disabled"]
            )
            browser.command(
                "run-code",
                "async(page)=>{await page.getByRole('dialog')"
                ".getByRole('textbox',{name:'名称',exact:true}).fill('合成文件夹');}",
            )
            enabled = audit('.file-dialog button[type="submit"]')
            result["observations"][theme + "_create_ready"] = enabled
            result["checks"][theme + "_real_create_enabled_label"] = (
                enabled["readable"] and not enabled["disabled"]
            )
            browser.command(
                "run-code",
                "async(page)=>{await page.screenshot({path:"
                + json.dumps((directory / f"{theme}-actual-create.png").as_posix())
                + "});await page.getByRole('dialog').getByRole('button',{name:'取消',exact:true}).click();"
                "await page.locator('.file-manager-entry').filter({hasText:'synthetic-color.txt'}).click();"
                "await page.getByRole('button',{name:'重命名',exact:true}).click();"
                "await page.getByRole('dialog',{name:'重命名',exact:true}).waitFor();}",
            )
            renamed = audit('.file-dialog button[type="submit"]')
            result["observations"][theme + "_rename"] = renamed
            result["checks"][theme + "_real_rename_label"] = (
                renamed["readable"] and not renamed["disabled"]
            )
            browser.command(
                "run-code",
                "async(page)=>{await page.screenshot({path:"
                + json.dumps((directory / f"{theme}-actual-rename.png").as_posix())
                + "});await page.getByRole('dialog').getByRole('button',{name:'取消',exact:true}).click();"
                "await page.getByRole('button',{name:'打开账号设置',exact:true}).click();"
                "await page.getByRole('button',{name:'刷新模型与额度',exact:true}).waitFor();}",
            )
            account = browser.evaluate("""()=>{
              const b=Array.from(document.querySelectorAll('.account-panel button')).find(b=>b.textContent==='刷新模型与额度');
              b.id='account-color-fixture';return !!b;}""")
            assert account
            refreshed = audit("#account-color-fixture")
            result["observations"][theme + "_account_refresh"] = refreshed
            result["checks"][theme + "_real_account_refresh_label"] = refreshed[
                "readable"
            ]
            browser.command(
                "run-code",
                "async(page)=>{await page.screenshot({path:"
                + json.dumps((directory / f"{theme}-actual-account.png").as_posix())
                + "});await page.getByRole('button',{name:'关闭账号设置',exact:true}).click();}",
            )
        result["status"] = "passed" if all(result["checks"].values()) else "failed"
    except Exception as exc:
        result["status"] = "failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        result["browser_cleanup"] = browser.close()
        result["application_cleanup"] = instance.stop()
        result["port_closed"] = port_closed(port)
        write_json(directory / "result.json", result)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
