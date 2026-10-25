// Fixed PDF.js 5.4.624 compatibility seam. Evidence and original seam locations:
// research/reports/workbench/pdf-ocr-layer-audit.md. No vendor bytes are changed.
const OCR_FONT_FAMILY = "Arial, sans-serif";
export function makeTextContent(payload, ascentRatio = 0.8) {
    const matrix = payload.image_to_pdf;
    if (!Array.isArray(matrix) || matrix.length !== 6 || !matrix.every(Number.isFinite))
        throw Error("OCR_TRANSFORM_UNSUPPORTED");
    if (
        typeof payload.text !== "string" ||
        payload.text.length > 500_000 ||
        !Array.isArray(payload.words) ||
        !payload.words.length ||
        payload.words.length > 100_000 ||
        !Number.isFinite(payload.bitmap_width) ||
        !Number.isFinite(payload.bitmap_height) ||
        payload.bitmap_width <= 0 ||
        payload.bitmap_height <= 0
    )
        throw Error("OCR_PAYLOAD_INVALID");
    // Accept an explicit orthogonal bitmap->PDF affine, including raster quarter
    // turns and nonzero crop origins. Never infer it from current display zoom.
    const [a, b, c, d, e, f] = matrix;
    const sx = Math.hypot(a, b),
        sy = Math.hypot(c, d);
    if (sx <= 0 || sy <= 0 || a * d - b * c >= 0 || Math.abs(a * c + b * d) > 1e-8 * sx * sy)
        throw Error("OCR_TRANSFORM_UNSUPPORTED");
    const point = (x, y) => [a * x + c * y + e, b * x + d * y + f];
    const items = [],
        mapping = [];
    let cursor = 0,
        previous = null;
    function add(str, bbox, start, end, whitespace = false) {
        const bl = point(bbox.x0, bbox.y1),
            br = point(bbox.x1, bbox.y1),
            tr = point(bbox.x1, bbox.y0),
            tl = point(bbox.x0, bbox.y0);
        const width = sx * (bbox.x1 - bbox.x0),
            height = sy * (bbox.y1 - bbox.y0);
        const baseline = [
            bl[0] - (c / sy) * (1 - ascentRatio) * height,
            bl[1] - (d / sy) * (1 - ascentRatio) * height,
        ];
        // The pinned TextLayer skips width fitting for a single character unless
        // the transform is sufficiently anisotropic. Keep str exact and trigger
        // its existing geom.width fitting rather than append a hidden character.
        const fit = str.length === 1 ? 2 : 1;
        items.push({
            str,
            dir: "ltr",
            width: Math.max(width, 0.01),
            height,
            transform: [
                (a / sx) * height * fit,
                (b / sx) * height * fit,
                (-c / sy) * height,
                (-d / sy) * height,
                ...baseline,
            ],
            fontName: "ocr-font",
            hasEOL: false,
        });
        mapping.push({
            start,
            end,
            whitespace,
            pdf_quad: [bl, br, tr, tl],
            pdf_box: [
                Math.min(...[bl, br, tr, tl].map((p) => p[0])),
                Math.min(...[bl, br, tr, tl].map((p) => p[1])),
                Math.max(...[bl, br, tr, tl].map((p) => p[0])),
                Math.max(...[bl, br, tr, tl].map((p) => p[1])),
            ],
            bitmap_box: { ...bbox },
        });
    }
    for (const word of payload.words) {
        const b = word.bbox;
        if (
            typeof word.text !== "string" ||
            !word.text ||
            ![b.x0, b.y0, b.x1, b.y1].every(Number.isFinite) ||
            b.x0 < 0 ||
            b.y0 < 0 ||
            b.x0 >= b.x1 ||
            b.y0 >= b.y1 ||
            b.x1 > payload.bitmap_width ||
            b.y1 > payload.bitmap_height
        )
            throw Error("OCR_WORD_INVALID");
        const at = payload.text.indexOf(word.text, cursor);
        if (at < 0 || /\S/u.test(payload.text.slice(cursor, at)))
            throw Error("OCR_TEXT_ALIGNMENT_FAILED");
        if (at > cursor) {
            const anchor = previous || b,
                gap = payload.text.slice(cursor, at);
            add(
                gap,
                {
                    x0: anchor.x1,
                    y0: anchor.y0,
                    x1: !/\n/u.test(gap) ? Math.max(anchor.x1 + 0.1, b.x0) : anchor.x1 + 0.1,
                    y1: anchor.y1,
                },
                cursor,
                at,
                true,
            );
        }
        add(word.text, b, at, at + word.text.length);
        cursor = at + word.text.length;
        previous = b;
    }
    if (cursor < payload.text.length) {
        if (/\S/u.test(payload.text.slice(cursor)) || !previous)
            throw Error("OCR_TEXT_ALIGNMENT_FAILED");
        add(
            payload.text.slice(cursor),
            { x0: previous.x1, y0: previous.y0, x1: previous.x1 + 0.1, y1: previous.y1 },
            cursor,
            payload.text.length,
            true,
        );
    }
    if (items.map((i) => i.str).join("") !== payload.text) throw Error("OCR_CHARACTER_CHANGE");
    return {
        content: {
            items,
            styles: {
                "ocr-font": {
                    fontFamily: OCR_FONT_FAMILY,
                    ascent: ascentRatio,
                    descent: ascentRatio - 1,
                    vertical: false,
                },
            },
            lang: "zh-CN",
        },
        mapping,
    };
}

export async function attachOcrAdapter(app, expectedSourceVersion, onError) {
    const frameWindow = app.appConfig.mainContainer.ownerDocument.defaultView;
    const document = frameWindow.document;
    const measure = document.createElement("canvas");
    measure.lang = "zh-CN";
    measure.width = measure.height = 30;
    const context = measure.getContext("2d");
    context.font = `30px ${OCR_FONT_FAMILY}`;
    const metrics = context.measureText("");
    const ascentRatio =
        metrics.fontBoundingBoxAscent /
        (metrics.fontBoundingBoxAscent + Math.abs(metrics.fontBoundingBoxDescent));
    if (!Number.isFinite(ascentRatio) || ascentRatio <= 0 || ascentRatio > 1)
        throw Error("OCR_FONT_METRICS_UNAVAILABLE");
    if (frameWindow.pdfjsLib.version !== "5.4.624") throw Error("PDFJS_VERSION_UNSUPPORTED");
    const pdf = app.pdfDocument,
        controller = app.findController;
    const permissions = await pdf.getPermissions();
    if (permissions && !permissions.includes(frameWindow.pdfjsLib.PermissionFlag.COPY))
        throw Error("OCR_COPY_PERMISSION_DENIED");
    if (!Array.isArray(controller._extractTextPromises)) throw Error("PDFJS_FIND_SEAM_UNSUPPORTED");
    const entries = new Map(),
        patches = new Map(),
        tagged = new WeakMap();
    let disposed = false,
        generation = 0,
        findTimer = 0,
        finding = false;
    const originalGetPage = pdf.getPage;
    function assertCurrent() {
        if (disposed || app.pdfDocument !== pdf) throw Error("OCR_STALE_DOCUMENT");
    }
    function patchPage(page) {
        if (patches.has(page.pageNumber)) return page;
        const nativeStream = page.streamTextContent.bind(page),
            originalStream = page.streamTextContent,
            originalText = page.getTextContent;
        const nativeText = async (params = {}) => {
            const reader = nativeStream(params).getReader(),
                content = { items: [], styles: {}, lang: null };
            while (true) {
                const next = await reader.read();
                if (next.done) break;
                content.items.push(...next.value.items);
                Object.assign(content.styles, next.value.styles);
                content.lang ??= next.value.lang;
            }
            return content;
        };
        const stream = (params) => {
            const entry = entries.get(page.pageNumber);
            return entry
                ? new frameWindow.ReadableStream({
                      start(c) {
                          c.enqueue(entry.content);
                          c.close();
                      },
                  })
                : nativeStream(params);
        };
        const text = (params) => {
            const entry = entries.get(page.pageNumber);
            return entry ? Promise.resolve(entry.content) : nativeText(params);
        };
        page.streamTextContent = stream;
        page.getTextContent = text;
        patches.set(page.pageNumber, {
            page,
            nativeText,
            originalStream,
            originalText,
            stream,
            text,
        });
        return page;
    }
    const getPage = async function (number) {
        return patchPage(await originalGetPage.call(pdf, number));
    };
    pdf.getPage = getPage;
    for (let i = 0; i < app.pdfViewer.pagesCount; i++) {
        const page = app.pdfViewer.getPageView(i)?.pdfPage;
        if (page) patchPage(page);
    }
    function facade() {
        const snapshot = new Map(entries);
        return {
            numPages: pdf.numPages,
            getPage: async (number) => {
                await getPage(number);
                const captured = snapshot.get(number);
                return {
                    getTextContent: (params) =>
                        captured
                            ? Promise.resolve(captured.content)
                            : patches.get(number).nativeText(params),
                };
            },
        };
    }
    async function refreshFind() {
        // No public single-page invalidation exists in this pinned build. Awaiting
        // old extraction is essential: its in-flight then-handler otherwise writes
        // into arrays that setDocument has just reset.
        await Promise.allSettled([...controller._extractTextPromises]);
        assertCurrent();
        const state = controller.state ? { ...controller.state } : null;
        controller.setDocument(facade());
        app.eventBus.dispatch("updatetextlayermatches", { source: controller, pageIndex: -1 });
        if (state?.query?.length)
            app.eventBus.dispatch("find", { ...state, source: controller, type: "again" });
    }
    function scheduleFind() {
        if (disposed || finding || findTimer) return;
        findTimer = frameWindow.setTimeout(async () => {
            findTimer = 0;
            finding = true;
            try {
                await refreshFind();
            } catch (error) {
                if (!disposed) onError?.(error);
            } finally {
                finding = false;
            }
        }, 150);
    }
    await refreshFind();
    function tagLayer(pageNumber) {
        const view = app.pdfViewer.getPageView(pageNumber - 1),
            entry = entries.get(pageNumber);
        if (!view?.textLayer || !entry) return;
        const divs = view.textLayer.highlighter?.textDivs || [];
        if (divs.length !== entry.content.items.length) return;
        for (let i = 0; i < divs.length; i++) {
            divs[i].dataset.ocrItem = String(i);
            divs[i].dataset.ocrStart = String(entry.mapping[i].start);
            divs[i].dataset.ocrEnd = String(entry.mapping[i].end);
        }
        tagged.set(view.textLayer, entry.revision);
    }
    const onText = ({ pageNumber }) => {
        if (!disposed && app.pdfDocument === pdf) tagLayer(pageNumber);
    };
    app.eventBus.on("textlayerrendered", onText);
    function captureSelection() {
        if (disposed || app.pdfDocument !== pdf) return null;
        const selection = document.getSelection();
        if (!selection?.rangeCount || selection.isCollapsed) return null;
        const range = selection.getRangeAt(0);
        const pieces = [],
            fragments = [];
        for (const layer of document.querySelectorAll(".page .textLayer")) {
            if (!range.intersectsNode(layer)) continue;
            const part = range.cloneRange();
            if (!layer.contains(range.startContainer)) part.setStart(layer, 0);
            if (!layer.contains(range.endContainer)) part.setEnd(layer, layer.childNodes.length);
            const fragment = part.cloneContents();
            // Native hasEOL is represented by actual BR nodes. OCR whitespace is
            // already literal text. Do not trim, normalize, correct or join pages
            // with invented characters; browser Selection.toString drops final LF.
            for (const br of fragment.querySelectorAll("br"))
                br.replaceWith(document.createTextNode("\n"));
            const text = fragment.textContent || "";
            if (!text) continue;
            pieces.push(text);
            const pageNumber = Number(layer.closest("[data-page-number]").dataset.pageNumber),
                entry = entries.get(pageNumber),
                regions = [];
            if (entry)
                for (const span of layer.querySelectorAll("[data-ocr-item]")) {
                    if (!range.intersectsNode(span)) continue;
                    const selected = range.cloneRange();
                    if (!span.contains(range.startContainer)) selected.setStart(span, 0);
                    if (!span.contains(range.endContainer))
                        selected.setEnd(span, span.childNodes.length);
                    if (!selected.toString()) continue;
                    const prefix = document.createRange();
                    prefix.selectNodeContents(span);
                    prefix.setEnd(selected.startContainer, selected.startOffset);
                    const item = entry.mapping[Number(span.dataset.ocrItem)];
                    regions.push({
                        start: item.start + prefix.toString().length,
                        end: item.start + prefix.toString().length + selected.toString().length,
                        offset_unit: "utf16",
                        pdf_quad: item.pdf_quad.map((point) => point.slice()),
                        geometry_precision: item.whitespace
                            ? "inferred_whitespace"
                            : "ocr_word_box",
                    });
                }
            fragments.push({
                page_number: pageNumber,
                text,
                origin: entry ? "ocr" : "native",
                ocr_revision: entry?.payload.revision ?? null,
                regions,
            });
        }
        return pieces.length
            ? { source_version: expectedSourceVersion, text: pieces.join(""), fragments }
            : null;
    }
    const onCopy = (event) => {
        const selection = document.getSelection();
        if (!selection?.rangeCount || selection.isCollapsed) return;
        const range = selection.getRangeAt(0);
        if (
            ![...document.querySelectorAll("[data-ocr-item]")].some((span) =>
                range.intersectsNode(span),
            )
        )
            return;
        const captured = captureSelection();
        if (!captured) return;
        event.clipboardData?.setData("text/plain", captured.text);
        event.preventDefault();
        event.stopImmediatePropagation();
    };
    document.addEventListener("copy", onCopy, true);
    return {
        async apply(payload, canReplace = () => true) {
            assertCurrent();
            if (
                payload.source_version !== expectedSourceVersion ||
                !Number.isInteger(payload.page_number) ||
                payload.page_number < 1 ||
                payload.page_number > pdf.numPages
            )
                throw Error("OCR_SOURCE_MISMATCH");
            const page = await getPage(payload.page_number),
                native = await patches
                    .get(payload.page_number)
                    .nativeText({ disableNormalization: true });
            assertCurrent();
            if (native.items.some((item) => item.str?.trim()))
                throw Error("OCR_NATIVE_TEXT_EXISTS");
            const converted = makeTextContent(payload, ascentRatio);
            const [x0, y0, x1, y1] = page.view;
            if (
                converted.mapping.some(
                    (m) =>
                        !m.whitespace &&
                        m.pdf_quad.some(
                            ([x, y]) =>
                                x < x0 - 0.01 || x > x1 + 0.01 || y < y0 - 0.01 || y > y1 + 0.01,
                        ),
                )
            )
                throw Error("OCR_WORD_OUTSIDE_PAGE_VIEW");
            if (!canReplace()) return false;
            const revision = ++generation;
            const entry = {
                ...converted,
                revision,
                payload: { revision: payload.revision, source_version: payload.source_version },
            };
            entries.set(payload.page_number, entry);
            const view = app.pdfViewer.getPageView(payload.page_number - 1),
                builder = view?.textLayer;
            const canvasBefore = view?.canvas;
            if (builder) {
                builder.cancel();
                const oldDiv = builder.div,
                    nextDiv = oldDiv.cloneNode(false);
                nextDiv.hidden = false;
                nextDiv.classList.remove("selecting");
                oldDiv.replaceWith(nextDiv);
                builder.div = nextDiv;
                await builder.render({ viewport: view.viewport });
                tagLayer(payload.page_number);
            }
            if (canvasBefore !== view?.canvas) throw Error("OCR_CANVAS_IDENTITY_CHANGED");
            scheduleFind();
            return true;
        },
        captureSelection,
        dispose() {
            disposed = true;
            generation++;
            frameWindow.clearTimeout(findTimer);
            app.eventBus.off("textlayerrendered", onText);
            document.removeEventListener("copy", onCopy, true);
            if (pdf.getPage === getPage) pdf.getPage = originalGetPage;
            for (const patch of patches.values()) {
                if (patch.page.streamTextContent === patch.stream)
                    patch.page.streamTextContent = patch.originalStream;
                if (patch.page.getTextContent === patch.text)
                    patch.page.getTextContent = patch.originalText;
            }
        },
    };
}
