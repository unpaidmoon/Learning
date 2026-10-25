/* Exact quote adaptation for the original Harness assistant renderer.
 * Native markup is untouched. A local, ignored mirror supplies the existing
 * project's source-location checks only after its visible text is verified.
 */
(() => {
  'use strict';
  const hidden = '.katex-mathml,button,script,style,.selection-quote-button,.selection-quote-error';
  const textError = '此选区暂不能精确对应原文，请重新选择（TEXT_SELECTION_UNMAPPED）';
  const formulaError = '此公式选区暂不能精确对应源码，请重新选择（FORMULA_SELECTION_UNMAPPED）';
  const significant = text => !!text && !/^[\s\u200b-\u200f\u2060\ufeff]*$/.test(text);
  function texts(root) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const result = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (significant(node.data) && !node.parentElement?.closest(hidden)) result.push(node);
    }
    return result;
  }
  function selected(range, node) {
    if (!range.intersectsNode(node) || range.comparePoint(node, node.length) < 0 || range.comparePoint(node, 0) > 0) return null;
    const start = range.startContainer === node ? range.startOffset : 0;
    const end = range.endContainer === node ? range.endOffset : node.length;
    return end > start ? { start, end } : null;
  }
  function covers(range, node) {
    const own = document.createRange(); own.selectNode(node);
    return range.compareBoundaryPoints(Range.START_TO_START, own) <= 0 && range.compareBoundaryPoints(Range.END_TO_END, own) >= 0;
  }
  function coversFormula(range, formula) {
    const visual = formula.querySelector('.katex-html');
    if (!visual) return false;
    const nodes = texts(visual);
    return nodes.length > 0 && nodes.every(node => {
      const part = selected(range, node);
      return part?.start === 0 && part.end === node.length;
    }) && [...visual.querySelectorAll('svg,.frac-line,.sqrt-line,.overline-line,.underline-line')].every(node => covers(range, node));
  }
  function sameRange(left, right) {
    return left.startContainer === right.startContainer && left.endContainer === right.endContainer
      && left.startOffset === right.startOffset && left.endOffset === right.endOffset;
  }
  function rowOf(range) {
    const start = range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer : range.startContainer.parentElement;
    const row = start?.closest('[data-chat-node-key][data-chat-flow-kind="assistant-step"]');
    if (!row || row.dataset.chatGroupPart === 'reasoning' || !row.contains(range.endContainer)) return null;
    if (!row.closest('[data-learning-region="ai"]')) return null;
    return row;
  }
  function markdownOf(range, row) {
    let current = range.startContainer.nodeType === Node.ELEMENT_NODE ? range.startContainer : range.startContainer.parentElement;
    while (current && current !== row) {
      if ([...current.classList].some(name => /(?:^|[_-])markdown(?:[_-]|$)/i.test(name))
          && current.contains(range.endContainer)) return current;
      current = current.parentElement;
    }
    return null;
  }
  function sourceOf(ctx, sid, key) {
    const snapshot = ctx.uiConversation.binding(sid).target('chat').getSnapshot();
    const node = snapshot?.nodes?.get(key);
    if (!node || node.kind !== 'assistant-step') return null;
    const blocks = node.data?.blocks?.filter(block => block.kind === 'text') || [];
    // A native row can contain independent Markdown documents. Never infer
    // separators or quote a neighboring document to bridge that difference.
    if (blocks.length !== 1 || typeof blocks[0].text !== 'string') return null;
    return blocks[0].text;
  }
  function projectRange(nativeRoot, mirrorRoot, range) {
    const nativeNodes = texts(nativeRoot), mirrorNodes = texts(mirrorRoot);
    if (nativeNodes.map(node => node.data).join('') !== mirrorNodes.map(node => node.data).join('')) return null;
    let offset = 0, start, end;
    for (const node of nativeNodes) {
      const part = selected(range, node);
      if (part) { start ??= offset + part.start; end = offset + part.end; }
      offset += node.length;
    }
    if (start === undefined || end === undefined || end <= start) return null;
    const mapped = document.createRange();
    let at = 0, begun = false, ended = false;
    for (const node of mirrorNodes) {
      if (!begun && start < at + node.length) { mapped.setStart(node, start - at); begun = true; }
      if (begun && end <= at + node.length) { mapped.setEnd(node, end - at); ended = true; break; }
      at += node.length;
    }
    return begun && ended ? mapped : null;
  }
  function validatePaintedRange(nativeRoot, mirrorRoot, nativeRange, quote) {
    const nativeFormulas = [...nativeRoot.querySelectorAll('.katex')];
    const mirrored = [...mirrorRoot.querySelectorAll('.selection-formula')];
    if (nativeFormulas.length !== mirrored.length) return false;
    for (let index = 0; index < mirrored.length; index++) {
      const formula = mirrored[index];
      const start = Number(formula.dataset.formulaStart), end = Number(formula.dataset.formulaEnd);
      // Only whole formulas (or a mixed prose/formula selection) require all
      // painted structures. A genuine mapped sub-expression stays partial.
      if (quote.source_start <= start && quote.source_end >= end && !coversFormula(nativeRange, nativeFormulas[index])) return false;
    }
    return true;
  }
  window.LearningAnswerQuotes = {
    create({ React, ReactDOMClient, flushSync, Button, ctx, api, components, getActiveSession, bindingFor, onQuote, onError }) {
      const h = React.createElement;
      const mirror = document.createElement('div');
      mirror.dataset.workspaceCaptureIgnore = '';
      mirror.dataset.learningQuoteMirror = '';
      mirror.setAttribute('aria-hidden', 'true');
      Object.assign(mirror.style, { position: 'fixed', left: '-100000px', top: '0', width: '1000px', opacity: '0', pointerEvents: 'none' });
      document.body.append(mirror);
      const mirrorRenderer = ReactDOMClient.createRoot(mirror);
      const toolbar = document.createElement('div');
      toolbar.dataset.workspaceCaptureIgnore = '';
      toolbar.dataset.learningAnswerQuote = '';
      Object.assign(toolbar.style, { position: 'fixed', zIndex: '90', display: 'none', maxWidth: '350px', fontSize: '13px' });
      document.body.append(toolbar);
      const toolbarRenderer = ReactDOMClient.createRoot(toolbar);
      let mirrorSource;
      let sequence = 0;
      let disposed = false;
      let candidate;
      let picking = false;
      let scheduled;
      const hide = () => { sequence++; candidate = null; toolbar.style.display = 'none'; };
      const currentRange = () => {
        const selection = window.getSelection();
        return selection?.rangeCount && !selection.isCollapsed ? selection.getRangeAt(0) : null;
      };
      function renderToolbar(range, contents) {
        const bounds = range.getBoundingClientRect?.() || { left: 8, top: 42 };
        toolbar.style.left = Math.max(8, Math.min(bounds.left, innerWidth - 360)) + 'px';
        toolbar.style.top = Math.max(8, bounds.top - 38) + 'px';
        toolbar.style.display = 'block';
        flushSync(() => toolbarRenderer.render(contents));
      }
      async function inspect() {
        const range = currentRange()?.cloneRange();
        if (!range) { hide(); return; }
        const row = rowOf(range);
        if (!row) { hide(); return; }
        const nativeRoot = markdownOf(range, row);
        if (!nativeRoot) { hide(); return; }
        const attempt = ++sequence;
        candidate = null; toolbar.style.display = 'none';
        const sid = getActiveSession();
        if (!sid) return;
        try {
          const source = sourceOf(ctx, sid, row.dataset.chatNodeKey);
          if (source === null || source.length > 200000) return;
          const binding = await bindingFor(sid);
          const chat = await api('/chats/' + binding.chat_id);
          const messages = (chat.messages || []).filter(message => message.role === 'assistant' && message.text === source);
          const liveRange = currentRange();
          if (disposed || sequence !== attempt || sid !== getActiveSession() || !liveRange || !sameRange(range, liveRange) || !nativeRoot.isConnected) return;
          let result;
          if (messages.length !== 1) result = { error: textError };
          else {
            if (mirrorSource !== source) {
              flushSync(() => mirrorRenderer.render(h(components.MarkdownAnswer, { text: source })));
              mirrorSource = source;
            }
            const mappedRoot = mirror.querySelector('.markdown-answer');
            const mappedRange = mappedRoot && projectRange(nativeRoot, mappedRoot, range);
            result = mappedRange ? components.quoteFromRange(mappedRoot, mappedRange, source) : { error: nativeRoot.querySelector('.katex') ? formulaError : textError };
            if (result.quote && !validatePaintedRange(nativeRoot, mappedRoot, range, result.quote)) result = { error: formulaError };
          }
          if (result.error) {
            renderToolbar(range, h('span', { role: 'status', className: 'selection-quote-error', style: { padding: '6px 10px', borderRadius: '8px', background: 'var(--dsw-alias-bg-base)', color: 'var(--dsw-alias-label-primary)' } }, result.error));
            return;
          }
          if (!result.quote) return;
          candidate = { sid, row, key: row.dataset.chatNodeKey, source, range, reference: {
            ...result.quote, reference_id: crypto.randomUUID(), message_id: messages[0].message_id,
          } };
          const pick = async () => {
            if (picking) return;
            const chosen = candidate;
            if (!chosen || chosen.sid !== getActiveSession() || !chosen.row.isConnected
                || sourceOf(ctx, chosen.sid, chosen.key) !== chosen.source) { hide(); return; }
            picking = true;
            try { await onQuote(chosen.reference); window.getSelection()?.removeAllRanges(); hide(); }
            catch (error) { onError?.(error); }
            finally { picking = false; }
          };
          renderToolbar(range, h(Button, { type: 'button', size: 'sm', variant: 'outline', 'aria-label': '添加到对话',
            onPointerDown: event => event.preventDefault(), onMouseDown: event => event.preventDefault(), onClick: pick,
          }, '添加到对话'));
        } catch (error) {
          if (disposed || sequence !== attempt) return;
          onError?.(error);
        }
      }
      const key = event => { if (event.key === 'Escape') hide(); else if (event.key.startsWith('Arrow') && event.shiftKey) void inspect(); };
      const up = event => {
        if (toolbar.contains(event.target)) return;
        clearTimeout(scheduled);
        scheduled = setTimeout(() => { scheduled = undefined; void inspect(); }, 0);
      };
      document.addEventListener('mouseup', up);
      document.addEventListener('pointerup', up);
      document.addEventListener('keyup', key);
      document.addEventListener('scroll', hide, true);
      window.addEventListener('resize', hide);
      return { inspect, dispose() {
        disposed = true; clearTimeout(scheduled); hide();
        document.removeEventListener('mouseup', up); document.removeEventListener('pointerup', up);
        document.removeEventListener('keyup', key); document.removeEventListener('scroll', hide, true);
        window.removeEventListener('resize', hide);
        mirrorRenderer.unmount(); toolbarRenderer.unmount(); mirror.remove(); toolbar.remove();
      } };
    },
  };
})();
