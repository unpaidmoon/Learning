"use strict";
(() => {
  const diagnostic = {renderedPages: [], renderedBlockIds: [], quizTypes: [], policyViolations: [], errors: []};
  window.__offlineCandidateEvidence = diagnostic;
  document.addEventListener("securitypolicyviolation", event => diagnostic.policyViolations.push({directive:event.violatedDirective, blocked:event.blockedURI}));
  window.addEventListener("error", event => diagnostic.errors.push(String(event.message)));
  const reader = document.getElementById("reader");
  const states = new Map();
  let currentPage;
  let bookData;
  const roles = {source_content: "来源内容", ai_supplement: "补充解释", synthetic_example: "合成练习"};
  const node = (tag, className, text) => {const element = document.createElement(tag); if(className) element.className=className; if(text!==undefined) element.textContent=String(text); return element;};
  const math = (tex, displayMode=false) => {
    const element=node(displayMode?"div":"span",displayMode?"math-display":"math-inline");
    window.katex.render(String(tex),element,{displayMode,throwOnError:false,trust:false,strict:"error",maxExpand:1000,maxSize:10,output:"htmlAndMathml"});
    return element;
  };
  function localPath(path, prefix) {
    if(typeof path!=="string" || !path.startsWith(prefix) || path.includes(":") || path.includes("\\") || path.split("/").includes("..") || path.startsWith("//")) throw new Error("LOCAL_ASSET_PATH_INVALID");
    return path;
  }
  function renderText(section, payload) {
    for(const paragraph of payload.paragraphs??[]) {
      const p=node("p","paragraph");
      for(const token of paragraph) p.append(token.kind==="math"?math(token.value):document.createTextNode(String(token.value)));
      section.append(p);
    }
    for(const tex of payload.display_math??[]) section.append(math(tex,true));
  }
  function renderFigure(section,payload) {
    const figure=node("figure","figure");
    const image=node("img"); image.src=localPath(payload.asset_path,"assets/"); image.alt=String(payload.alt); image.loading="eager";
    figure.append(image,node("figcaption","caption",payload.caption)); section.append(figure);
  }
  const svgNode=(name,attributes={})=>{const result=document.createElementNS("http://www.w3.org/2000/svg",name); for(const [key,value] of Object.entries(attributes)) result.setAttribute(key,String(value)); return result;};
  function plotSpec(envelope) {
    if(envelope.renderer.target!=="native" || envelope.renderer.native_renderer!=="quadratic_tangent" || envelope.payload.format!=="learning.parameter-plot/v1") throw new Error("RENDERER_NOT_ALLOWED");
    const raw=typeof envelope.payload.data==="string"?envelope.payload.data:JSON.stringify(envelope.payload.data);
    const data=window.DTVisualization.parseChartConfigStrict(raw);
    if(data.kind!=="quadratic_tangent" || Object.keys(data).some(key=>!["kind","a","x0"].includes(key))) throw new Error("PLOT_DATA_INVALID");
    for(const key of ["a","x0"]) {
      const parameter=data[key];
      if(!parameter || ["min","max","step","initial"].some(name=>!Number.isFinite(parameter[name])) || parameter.min>=parameter.max || parameter.step<=0 || parameter.initial<parameter.min || parameter.initial>parameter.max) throw new Error("PARAMETER_RANGE_INVALID");
    }
    return data;
  }
  function renderInteractive(section,payload,block) {
    const envelope=bookData.visualizations[payload.visualization_id];
    const spec=plotSpec(envelope);
    const card=node("div","plot-card");
    const svg=svgNode("svg",{viewBox:"0 0 640 330",role:"img","aria-label":"二次函数与切线参数图"});
    const defs=svgNode("defs"), clip=svgNode("clipPath",{id:"plot-clip"}); clip.append(svgNode("rect",{x:46,y:12,width:570,height:280})); defs.append(clip); svg.append(defs);
    const px=x=>46+(x+3)*95, py=y=>292-(y+2)*20;
    for(let x=-3;x<=3;x++) svg.append(svgNode("line",{x1:px(x),x2:px(x),y1:12,y2:292,class:"svg-grid"}));
    for(let y=-2;y<=12;y+=2) svg.append(svgNode("line",{x1:46,x2:616,y1:py(y),y2:py(y),class:"svg-grid"}));
    svg.append(svgNode("line",{x1:46,x2:616,y1:py(0),y2:py(0),class:"svg-axis"}),svgNode("line",{x1:px(0),x2:px(0),y1:12,y2:292,class:"svg-axis"}));
    const clipped=svgNode("g",{"clip-path":"url(#plot-clip)"}),curve=svgNode("polyline",{class:"curve"}),tangent=svgNode("line",{class:"tangent"}),point=svgNode("circle",{r:6,class:"plot-point"});
    clipped.append(curve,tangent,point);svg.append(clipped);
    const labels=svgNode("g",{fill:"#71809a","font-family":"sans-serif","font-size":12});
    for(const [x,label] of [[-2,"−2"],[0,"0"],[2,"2"]]) {const text=svgNode("text",{x:px(x)+5,y:py(0)+18});text.textContent=label;labels.append(text);} svg.append(labels);
    const controls=node("div","plot-controls"),inputs={},outputs={};
    const previous=states.get(block.id)??{};
    for(const key of ["a","x0"]) {
      const parameter=spec[key],container=node("div","parameter"),label=node("label"),output=node("output");
      const input=node("input"); input.type="range";input.id=`parameter-${key}`;input.min=String(parameter.min);input.max=String(parameter.max);input.step=String(parameter.step);input.value=String(previous[key]??parameter.initial);input.setAttribute("aria-label",parameter.label);
      label.htmlFor=input.id;label.append(document.createTextNode(parameter.label),output);container.append(label,input);controls.append(container);inputs[key]=input;outputs[key]=output;
    }
    const result=node("div","plot-result"),value=node("strong"),explanation=node("div","plot-equation");result.append(value,explanation);
    function redraw() {
      const a=Number(inputs.a.value),x0=Number(inputs.x0.value);
      if(!Number.isFinite(a)||!Number.isFinite(x0)||a<spec.a.min||a>spec.a.max||x0<spec.x0.min||x0>spec.x0.max) throw new Error("PARAMETER_VALUE_INVALID");
      const y0=a*x0*x0,k=2*a*x0;
      curve.setAttribute("points",Array.from({length:121},(_,i)=>{const x=-3+i/20;return `${px(x).toFixed(2)},${py(a*x*x).toFixed(2)}`;}).join(" "));
      tangent.setAttribute("x1",String(px(-3)));tangent.setAttribute("y1",String(py(k*(-3-x0)+y0)));tangent.setAttribute("x2",String(px(3)));tangent.setAttribute("y2",String(py(k*(3-x0)+y0)));
      point.setAttribute("cx",String(px(x0)));point.setAttribute("cy",String(py(y0)));
      outputs.a.textContent=a.toFixed(2);outputs.x0.textContent=x0.toFixed(2);
      value.textContent=`斜率 ${k.toFixed(2)} · 切点 (${x0.toFixed(2)}, ${y0.toFixed(2)})`;
      explanation.replaceChildren(math(`k=2\\times ${a.toFixed(2)}\\times ${x0.toFixed(2)}=${k.toFixed(2)}`));
      card.dataset.slope=String(k);card.dataset.pointY=String(y0);states.set(block.id,{a,x0});
    }
    for(const input of Object.values(inputs)) input.addEventListener("input",redraw);
    card.append(svg,controls,result);section.append(card);redraw();
  }
  function renderQuiz(section,payload) {
    for(const [index,question] of payload.questions.entries()) {
      const card=node("div","question");card.dataset.questionId=question.question_id;card.append(node("h3",null,`${index+1}. ${question.question}`));
      const normalized=window.DTQuiz.normalizeQuizQuestionType(question.question_type);diagnostic.quizTypes.push(normalized);
      if(window.DTQuiz.isChoiceQuizQuestion(normalized)) {
        const choices=node("div","choices"),feedback=node("p","feedback"),submit=node("button","primary","核对选择");
        let selected=states.get(question.question_id)?.selected??null;
        for(const [key,label] of Object.entries(question.options)) {
          const option=node("label","choice"),input=node("input");input.type="radio";input.name=question.question_id;input.value=key;input.checked=selected===key;input.setAttribute("aria-label",`选项 ${key}：${label}`);
          option.classList.toggle("selected",input.checked);option.append(input,document.createTextNode(`${key}  ${label}`));
          input.addEventListener("change",()=>{selected=key;for(const peer of choices.children) peer.classList.toggle("selected",peer===option);states.set(question.question_id,{selected});feedback.textContent="";});choices.append(option);
        }
        submit.type="button";submit.addEventListener("click",()=>{
          if(!selected) {feedback.className="feedback retry";feedback.textContent="先选一个答案，再核对。";return;}
          const answerKey=window.DTQuiz.resolveChoiceAnswerKey(question.correct_answer,question.options);
          const correct=selected.toUpperCase()===answerKey;
          feedback.className=`feedback ${correct?"correct":"retry"}`;feedback.textContent=correct?`回答正确。${question.explanation}`:"还没选对，可以根据切线斜率公式再试一次。";
          states.set(question.question_id,{selected,correct});card.dataset.grade=correct?"correct":"incorrect";
        });card.append(choices,submit,feedback);
      } else {
        const explanation=node("div","reference-answer");explanation.hidden=true;
        explanation.append(node("p",null,question.correct_answer),node("p","written-note",question.explanation));
        const reveal=node("button","secondary","查看参考答案");reveal.type="button";reveal.addEventListener("click",()=>{explanation.hidden=!explanation.hidden;reveal.textContent=explanation.hidden?"查看参考答案":"收起参考答案";card.dataset.grade="ungraded";});
        card.append(node("p","written-note","先尝试口述或写在自己的纸上。查看参考不会自动判对错。"),reveal,explanation);card.dataset.grade="ungraded";
      }
      section.append(card);
    }
  }
  function renderCards(section,payload) {
    const list=node("div","flashcards");for(const item of payload.cards) {const card=node("details","flashcard");card.append(node("summary",null,item.front),node("p",null,item.back));list.append(card);}section.append(list);
  }
  function renderBlock(block) {
    if(block.status!=="ready") throw new Error("UNFINISHED_BLOCK_IN_PACKAGE");
    const section=node("section","block"),heading=node("div","block-heading"),role=block.metadata.content_role;
    section.dataset.blockId=block.id;heading.append(node("h2",null,block.title),node("span",`role ${role==="source_content"?"source":""}`,roles[role]??"合成内容"));section.append(heading);
    const renderers={text:renderText,figure:renderFigure,interactive:renderInteractive,quiz:renderQuiz,flash_cards:renderCards,
      callout:(target,payload)=>{const box=node("div","callout");box.append(node("strong",null,payload.label),document.createTextNode(payload.body));target.append(box);}};
    const render=renderers[block.type];if(!render) throw new Error("BLOCK_RENDERER_NOT_ALLOWED");render(section,block.payload,block);
    diagnostic.renderedBlockIds.push(block.id);return section;
  }
  function updatePercent(){document.getElementById("reading-percent").textContent=`${window.DTNavigation.chapterReadingPercent(reader)}%`;}
  function showPage(pageId) {
    const page=bookData.pages.find(page=>page.id===pageId);if(!page) throw new Error("PAGE_NOT_FOUND");currentPage=page;
    reader.replaceChildren(...page.blocks.map(renderBlock));reader.scrollTop=0;document.getElementById("chapter-title").textContent=page.title;
    for(const button of document.querySelectorAll("#chapters button")) {button.classList.toggle("active",button.dataset.pageId===pageId);button.setAttribute("aria-current",button.dataset.pageId===pageId?"page":"false");}
    document.body.dataset.currentPage=pageId;diagnostic.renderedPages.push(pageId);updatePercent();
  }
  function sequential(direction){const target=window.DTNavigation.sequentialReadTarget(reader,direction);if(target!==null) reader.scrollTo({top:target,behavior:"smooth"});}
  try {
    bookData=JSON.parse(document.getElementById("book-data").textContent);
    if(bookData.schema_version!=="learning.offline-candidate/v1"||bookData.pages.length!==2) throw new Error("PACKAGE_SCHEMA_INVALID");
    document.getElementById("book-title").textContent=bookData.book.title;
    for(const [index,page] of bookData.pages.entries()) {const button=node("button"),number=node("span","chapter-number",String(index+1).padStart(2,"0"));button.type="button";button.dataset.pageId=page.id;button.append(number,document.createTextNode(page.title));button.addEventListener("click",()=>showPage(page.id));document.getElementById("chapters").append(button);}
    document.getElementById("screen-previous").addEventListener("click",()=>sequential("previous"));document.getElementById("screen-next").addEventListener("click",()=>sequential("next"));reader.addEventListener("scroll",updatePercent,{passive:true});
    document.getElementById("reset-checks").addEventListener("click",()=>{states.clear();showPage(currentPage.id);});
    const sources=document.getElementById("sources"),content=document.getElementById("source-content");
    for(const source of bookData.source_catalog) {const item=node("section","source-item"),link=node("a",null,"打开本包原始合成材料");link.href=localPath(source.path,"sources/");link.target="_blank";link.rel="noopener noreferrer";item.append(node("h3",null,source.title),node("p","source-version",`来源版本 SHA-256：${source.source_version}`),node("pre",null,source.text),link);content.append(item);}
    document.getElementById("source-open").addEventListener("click",()=>sources.showModal());document.getElementById("source-close").addEventListener("click",()=>sources.close());
    showPage(bookData.pages[0].id);document.body.dataset.ready="true";
  } catch(error) {diagnostic.errors.push(String(error));reader.replaceChildren(node("div","fatal",`离线包无法显示：${String(error)}`));document.body.dataset.ready="error";}
})();
