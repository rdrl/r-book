/* ==========================================================================
   R-Book · 前端主逻辑
   - Phase 2：资料库过滤、实时文件夹漫游、后端宿主机目录树选择器、设置中心、进度同步
   - Phase 3：TXT / UMD 按章阅读器、Markdown 专属阅读器、Aa 排版面板与四大护眼主题
   - Phase 4：EPUB 与 PDF 原生流式阅读内核（严格隔离 Vue 3 响应式）及本地满血真封面渲染
   - Phase 5：键盘快捷键、移动端三区手势、退出自动同步
   - Phase 6：2万册大书库常驻内存分页与滚动懒加载、中文 PDF CMap 缺字修复、
             100% 自动精准续读（isRestoringProgress 时序锁）、Apple Books 多书签系统
   ========================================================================== */
(function () {
  "use strict";

  const { createApp, ref, reactive, computed, watch, nextTick, markRaw, onMounted, onBeforeUnmount } = Vue;

  /* 显式配置同版本 PDF.js Worker、CMap 与标准字体 CDN 路径（防白屏 & 修复中文 CID 缺字死规） */
  const PDFJS_VERSION = "3.11.174";
  const PDFJS_CDN_BASE = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/" + PDFJS_VERSION + "/";
  const PDFJS_CMAP_URL = PDFJS_CDN_BASE + "cmaps/";
  const PDFJS_STANDARD_FONTS_URL = PDFJS_CDN_BASE + "standard_fonts/";

  if (window.pdfjsLib && window.pdfjsLib.GlobalWorkerOptions) {
    window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_CDN_BASE + "pdf.worker.min.js";
  }

  /* ==========================================================================
     【DOM 与 Vue 3 响应式隔离死规】
     EPUB.js 与 PDF.js 实例严禁放入 Vue ref() / reactive() 中！
     统一保存在模块级普通非响应式变量中，彻底杜绝 Vue 3 Proxy 代理导致白屏。
     ========================================================================== */
  let epubBookInstance = null;
  let epubRenditionInstance = null;
  let pdfDocInstance = null;
  let pdfRenderTask = null;

  /* 精准续读时序保护锁：在异步加载与恢复上次阅读位置完成前，严禁将进度覆盖为 0% */
  let isRestoringProgress = false;

  const COVER_THEMES = ["white", "navy", "amber", "classic"];
  const BOOK_EXTS = ["epub", "umd", "txt", "chm"];
  const PAGE_SIZE_DEFAULT = 60;

  /* ------------------------------ 通用请求封装 ------------------------------ */
  async function apiFetch(url, options, timeoutMs) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs || 15000);
    try {
      const resp = await fetch(url, Object.assign({}, options || {}, { signal: controller.signal }));
      const isJson = (resp.headers.get("content-type") || "").includes("application/json");
      const body = isJson ? await resp.json() : await resp.text();
      if (!resp.ok) {
        const detail = isJson && body && body.detail ? body.detail : resp.statusText;
        throw new Error(detail || ("HTTP " + resp.status));
      }
      return body;
    } catch (err) {
      if (err && err.name === "AbortError") {
        throw new Error("请求超时");
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  function postJson(url, data, timeoutMs) {
    return apiFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data || {}),
    }, timeoutMs);
  }

  function djb2(str) {
    let h = 5381;
    for (let i = 0; i < str.length; i++) {
      h = ((h << 5) + h + str.charCodeAt(i)) >>> 0;
    }
    return h;
  }

  function escapeHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  /* DOMPurify 不可用时的兜底净化：在惰性文档中移除脚本类标签、事件属性与 javascript: 链接 */
  function sanitizeHtml(html) {
    if (window.DOMPurify && typeof window.DOMPurify.sanitize === "function") {
      return window.DOMPurify.sanitize(html);
    }
    const doc = document.implementation.createHTMLDocument("");
    const box = doc.createElement("div");
    box.innerHTML = html;
    box.querySelectorAll("script, iframe, object, embed, style, link, meta, form").forEach((el) => el.remove());
    box.querySelectorAll("*").forEach((el) => {
      Array.from(el.attributes).forEach((attr) => {
        const name = attr.name.toLowerCase();
        const value = (attr.value || "").trim().toLowerCase();
        if (name.startsWith("on")) el.removeAttribute(attr.name);
        if ((name === "href" || name === "src") && value.startsWith("javascript:")) el.removeAttribute(attr.name);
      });
    });
    return box.innerHTML;
  }

  /* 将 Markdown 中的相对资源路径与文档所在文件夹拼接为 rel_key */
  function joinRelPath(folder, rel) {
    let decoded = rel;
    try { decoded = decodeURIComponent(rel); } catch (_e) { decoded = rel; }
    const stack = (folder || "").split("/").filter(Boolean);
    decoded.split("/").forEach((seg) => {
      if (!seg || seg === ".") return;
      if (seg === "..") { stack.pop(); return; }
      stack.push(seg);
    });
    return stack.join("/");
  }

  function parseLocator(loc) {
    if (loc === null || loc === undefined) return {};
    if (typeof loc === "number" && !Number.isNaN(loc)) {
      return { chapter: loc, page: loc };
    }
    if (typeof loc === "object") return loc;
    if (typeof loc === "string") {
      const trimmed = loc.trim();
      if (!trimmed) return {};
      if (trimmed.startsWith("{")) {
        try { return JSON.parse(trimmed) || {}; } catch (_e) { return {}; }
      }
      if (/^\d+$/.test(trimmed)) {
        const n = Number(trimmed);
        return { chapter: n, page: n };
      }
      if (trimmed.startsWith("epubcfi(")) {
        return { type: "epub", cfi: trimmed };
      }
    }
    return {};
  }

  function extractScrollRatio(locObj) {
    if (!locObj || typeof locObj !== "object") return 0;
    if (locObj.scrollRatio !== undefined && locObj.scrollRatio !== null) {
      return clamp(Number(locObj.scrollRatio) || 0, 0, 1);
    }
    if (locObj.scroll !== undefined && locObj.scroll !== null) {
      return clamp(Number(locObj.scroll) || 0, 0, 1);
    }
    return 0;
  }

  function clamp(val, min, max) {
    return Math.min(max, Math.max(min, val));
  }

  const READER_PREFS_KEY = "rbook.readerPrefs";
  const READER_WIDTHS = { narrow: "600px", medium: "720px", wide: "920px" };
  const READER_THEMES = [
    { key: "original", label: "Original", bg: "#ffffff", fg: "#1d1d1f", link: "#0071e3" },
    { key: "quiet", label: "Quiet", bg: "#3a3a3c", fg: "#e5e5ea", link: "#64a8ff" },
    { key: "paper", label: "Paper", bg: "#f8f1e3", fg: "#4a3b28", link: "#a35d17" },
    { key: "bold", label: "Bold Night", bg: "#000000", fg: "#f2f2f2", link: "#0a84ff" },
  ];
  const READER_FONTS = [
    { key: "serif", label: "宋体", css: '"Songti SC", "STSong", "Noto Serif CJK SC", "SimSun", serif' },
    { key: "sans", label: "无衬线", css: '-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif' },
    { key: "kai", label: "楷体", css: '"Kaiti SC", "STKaiti", "KaiTi", "Songti SC", serif' },
  ];
  const READER_WIDTH_OPTIONS = [
    { key: "narrow", label: "窄" },
    { key: "medium", label: "中" },
    { key: "wide", label: "宽" },
  ];

  function loadReaderPrefs() {
    const defaults = {
      theme: "original",
      font: "serif",
      fontSize: 19,
      lineHeight: 1.8,
      width: "medium",
      epubSpread: "auto",
      layoutMode: "paged",
    };
    try {
      const raw = localStorage.getItem(READER_PREFS_KEY);
      if (!raw) return defaults;
      const p = JSON.parse(raw) || {};
      return {
        theme: READER_THEMES.some((t) => t.key === p.theme) ? p.theme : defaults.theme,
        font: READER_FONTS.some((f) => f.key === p.font) ? p.font : defaults.font,
        fontSize: clamp(Number(p.fontSize) || defaults.fontSize, 15, 30),
        lineHeight: clamp(Number(p.lineHeight) || defaults.lineHeight, 1.5, 2.2),
        width: READER_WIDTHS[p.width] ? p.width : defaults.width,
        epubSpread: p.epubSpread === "none" ? "none" : "auto",
        layoutMode: p.layoutMode === "scroll" ? "scroll" : "paged",
      };
    } catch (_e) {
      return defaults;
    }
  }

  createApp({
    setup() {
      /* ------------------------------ 状态 ------------------------------ */
      const books = ref([]);
      const collections = ref([]);
      const config = reactive({ media_dirs: [], cloud_restricted_mode: true, peer_sync_url: "" });
      const scanStatus = reactive({ scanning: false, found_books: 0 });
      const loading = ref(false);
      const loadingMore = ref(false);

      /* 2 万册大书库后端分页与统计计数状态 */
      const pagination = reactive({
        page: 1,
        pageSize: PAGE_SIZE_DEFAULT,
        total: 0,
        hasMore: false,
      });
      const serverCounts = reactive({
        all: 0,
        want: 0,
        finished: 0,
        books: 0,
        pdfs: 0,
        markdown: 0,
      });

      const libraryContentEl = ref(null);
      const loadMoreSentinelEl = ref(null);
      let sentinelObserver = null;
      let searchDebounceTimer = null;

      const view = ref("library");          // 'library' | 'folders'
      const activeFilter = ref("all");
      const searchQuery = ref("");
      const browseRoot = ref("");
      const browseData = reactive({ current_path: "", breadcrumbs: [{ name: "全部文件夹", path: "" }], dirs: [], books: [] });

      const coverFailed = reactive({});
      const menuKey = ref(null);
      const sidebarOpen = ref(false);
      const isDark = ref(false);
      const syncing = ref(false);

      const settingsOpen = ref(false);
      const savingConfig = ref(false);
      const draft = reactive({ media_dirs: [], cloud_restricted_mode: true, peer_sync_url: "" });
      const explorerOpen = ref(false);
      const explorerLoading = ref(false);
      const explorer = reactive({ current_path: "/", parent_path: null, shortcuts: [], dirs: [], warning: "" });
      const importOpen = ref(false);
      const importText = ref("");

      const toast = reactive({ show: false, message: "", type: "info" });
      let toastTimer = null;
      let scanPollTimer = null;
      let resumeBannerTimer = null;

      /* 本地满血模式 PDF 首页封面渲染队列状态（纯非响应式控制变量） */
      const pdfCoverAttempted = new Set();
      let pdfCoverQueueRunning = false;

      /* ------------------------------ 阅读器状态 ------------------------------ */
      const readerThemes = READER_THEMES;
      const readerFonts = READER_FONTS;
      const readerWidthOptions = READER_WIDTH_OPTIONS;
      const prefs = reactive(loadReaderPrefs());
      const readerScrollEl = ref(null);
      const reader = reactive({
        open: false,
        book: null,
        kind: "text",          // 'text' | 'md' | 'epub' | 'pdf'
        title: "",
        author: "",
        chapters: [],
        current: 0,
        total: 0,
        chapterTitle: "",
        content: "",
        mdHtml: "",
        mdToc: [],
        activeHeading: "",
        epubToc: [],
        epubCurrentCfi: "",
        epubCurrentHref: "",
        pdfToc: [],
        pdfScale: 1.0,
        pdfPageInput: 1,
        pdfRendering: false,
        pdfNativeMode: false,
        pdfInvertManual: null, // null 跟随暗色主题，true/false 手动指定
        loading: false,
        error: "",
        tocOpen: false,
        drawerTab: "toc",      // 'toc' | 'bookmarks'
        bookmarks: [],
        currentScrollRatio: 0,
        currentBlock: -1,
        pageEndBlock: -1,
        page: 0,
        pages: 1,
        aaOpen: false,
        tocQuery: "",
        progress: 0,
        toolbarVisible: true,
        isFullscreen: false,
        resumeBanner: false,
        resumeBannerText: "已为您自动定位至上次阅读位置",
        pendingChapter: 0,
        pendingScroll: 0,
        /* CHM 原版网页模式（保留 CHM 自带图片导航主页与超链接结构） */
        chmWeb: false,
        chmInfo: null,
        chmPage: "",
        chmPageTitle: "",
        chmLoading: false,
        chmCanBack: false,
      });
      const chmFrameEl = ref(null);
      let chmHistoryStack = [];
      let chmNavigatingBack = false;
      let readerSaveTimer = null;
      let readerScrollRaf = 0;
      let readerLoadSeq = 0;

      const libraryNav = [
        { key: "all", label: "All", icon: "📚" },
        { key: "want", label: "Want to Read", icon: "📌" },
        { key: "finished", label: "Finished", icon: "✅" },
        { key: "books", label: "Books", icon: "📖" },
        { key: "pdfs", label: "PDFs", icon: "📄" },
        { key: "markdown", label: "Markdown", icon: "📝" },
      ];

      function showToast(message, type) {
        toast.message = message;
        toast.type = type || "info";
        toast.show = true;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { toast.show = false; }, 2800);
      }

      function triggerResumeBanner(customText) {
        reader.resumeBannerText = customText || "已为您自动定位至上次阅读位置";
        reader.resumeBanner = true;
        clearTimeout(resumeBannerTimer);
        resumeBannerTimer = setTimeout(() => {
          reader.resumeBanner = false;
        }, 2500);
      }

      /* ------------------------------ 过滤与分页计算 ------------------------------ */
      function matchSearch(item) {
        const q = searchQuery.value.trim().toLowerCase();
        if (!q) return true;
        const hay = ((item.title || item.name || "") + " " + (item.rel_key || item.path || "")).toLowerCase();
        return hay.includes(q);
      }

      const filterCounts = computed(() => ({
        all: serverCounts.all,
        want: serverCounts.want,
        finished: serverCounts.finished,
        books: serverCounts.books,
        pdfs: serverCounts.pdfs,
        markdown: serverCounts.markdown,
      }));

      const visibleBooks = computed(() => {
        if (view.value === "folders") {
          return browseData.books.filter(matchSearch);
        }
        return books.value;
      });

      const visibleDirs = computed(() => {
        if (view.value !== "folders") return [];
        return browseData.dirs.filter(matchSearch);
      });

      const pageTitle = computed(() => {
        if (view.value === "folders") {
          const crumbs = browseData.breadcrumbs;
          return crumbs.length > 1 ? crumbs[crumbs.length - 1].name : "Folders";
        }
        const nav = libraryNav.find((n) => n.key === activeFilter.value);
        return nav ? nav.label : "All";
      });

      const pageSubtitle = computed(() => {
        if (view.value === "folders") {
          return visibleDirs.value.length + " 个文件夹 · " + visibleBooks.value.length + " 本";
        }
        if (pagination.total > visibleBooks.value.length) {
          return "已显示 " + visibleBooks.value.length + " / 共 " + pagination.total + " 本";
        }
        return pagination.total + " 本";
      });

      /* ------------------------------ 封面渲染辅助 ------------------------------ */
      function coverTheme(book) {
        return COVER_THEMES[djb2(book.name || book.title || "") % COVER_THEMES.length];
      }

      function shortTitle(title, maxLen) {
        const t = (title || "").trim();
        return t.length > maxLen ? t.slice(0, maxLen - 1) + "…" : t;
      }

      function sealChar(book) {
        const t = (book.title || "书").trim();
        return t.charAt(0) || "书";
      }

      function isNew(book) {
        return (!book.status || book.status === "new") && !book.progress;
      }

      function progressLabel(book) {
        if (book.status === "finished") return "100%";
        const p = Number(book.progress || 0);
        return Math.round(p) + "%";
      }

      function patchBookCover(relKey, coverUrl) {
        const apply = (list) => {
          list.forEach((b) => {
            if (b.rel_key === relKey) {
              b.has_cover = true;
              b.cover_url = coverUrl;
              coverFailed[relKey] = false;
            }
          });
        };
        apply(books.value);
        apply(browseData.books);
      }

      /* ==================================================================
         本地满血模式专属特性：自动渲染 PDF 第 1 页生成 360px 真实封面
         （显式注入 CMap 与标准字体配置，确保中文 PDF 首页文字完整渲染）
         ================================================================== */
      function buildPdfDocumentParams(relKey) {
        return {
          url: "/api/raw?path=" + encodeURIComponent(relKey),
          rangeChunkSize: 65536,
          cMapUrl: PDFJS_CMAP_URL,
          cMapPacked: true,
          standardFontDataUrl: PDFJS_STANDARD_FONTS_URL,
          enableXfa: true,
        };
      }

      async function renderSinglePdfCoverToServer(book) {
        if (!window.pdfjsLib || !book || !book.rel_key) return;
        let doc = null;
        try {
          const loadingTask = window.pdfjsLib.getDocument(buildPdfDocumentParams(book.rel_key));
          doc = await loadingTask.promise;
          const page = await doc.getPage(1);
          const baseVp = page.getViewport({ scale: 1.0 });
          const targetWidth = 360;
          const scale = targetWidth / Math.max(baseVp.width, 1);
          const vp = page.getViewport({ scale: scale });

          const offCanvas = document.createElement("canvas");
          offCanvas.width = Math.round(vp.width);
          offCanvas.height = Math.round(vp.height);
          const ctx = offCanvas.getContext("2d", { alpha: false });
          ctx.fillStyle = "#ffffff";
          ctx.fillRect(0, 0, offCanvas.width, offCanvas.height);

          await page.render({ canvasContext: ctx, viewport: vp }).promise;

          const blob = await new Promise((resolve) => {
            offCanvas.toBlob((b) => resolve(b), "image/jpeg", 0.85);
          });
          if (!blob) return;

          const resp = await fetch("/api/cover/upload?path=" + encodeURIComponent(book.rel_key), {
            method: "POST",
            headers: { "Content-Type": "image/jpeg" },
            body: blob,
          });
          if (resp.ok) {
            const res = await resp.json();
            if (res && res.cover_url) {
              patchBookCover(book.rel_key, res.cover_url);
            }
          }
        } catch (_e) {
          /* 单个 PDF 封面生成失败静默跳过，保留仿真封面 */
        } finally {
          if (doc && typeof doc.destroy === "function") {
            try { await doc.destroy(); } catch (_e) { /* ignore */ }
          }
        }
      }

      async function runLocalPdfCoverQueue() {
        if (pdfCoverQueueRunning || config.cloud_restricted_mode || !window.pdfjsLib) return;
        pdfCoverQueueRunning = true;
        try {
          const candidates = books.value.filter(
            (b) => b.ext === "pdf" && !b.has_cover && !pdfCoverAttempted.has(b.rel_key)
          );
          for (let i = 0; i < candidates.length; i++) {
            if (config.cloud_restricted_mode) break;
            const b = candidates[i];
            pdfCoverAttempted.add(b.rel_key);
            await renderSinglePdfCoverToServer(b);
          }
        } finally {
          pdfCoverQueueRunning = false;
        }
      }

      /* ------------------------------ 2万册大书库分页与懒加载 ------------------------------ */
      function applyConfig(cfg) {
        if (!cfg) return;
        config.media_dirs = Array.isArray(cfg.media_dirs) ? cfg.media_dirs.slice() : [];
        config.cloud_restricted_mode = cfg.cloud_restricted_mode !== false;
        config.peer_sync_url = cfg.peer_sync_url || "";
      }

      function applyServerCounts(countsObj, fallbackTotal) {
        if (countsObj && typeof countsObj === "object") {
          serverCounts.all = Number(countsObj.all || 0);
          serverCounts.want = Number(countsObj.want || 0);
          serverCounts.finished = Number(countsObj.finished || 0);
          serverCounts.books = Number(countsObj.books || 0);
          serverCounts.pdfs = Number(countsObj.pdfs || 0);
          serverCounts.markdown = Number(countsObj.markdown || 0);
        } else if (typeof fallbackTotal === "number") {
          serverCounts.all = fallbackTotal;
        }
      }

      async function loadLibrary(forceRefresh, appendPage) {
        const isAppend = !!appendPage && !forceRefresh;
        const targetPage = isAppend ? pagination.page + 1 : 1;

        if (isAppend) {
          if (loadingMore.value || loading.value || !pagination.hasMore) return;
          loadingMore.value = true;
        } else {
          loading.value = true;
        }

        try {
          const params = new URLSearchParams();
          params.set("page", String(targetPage));
          params.set("page_size", String(pagination.pageSize));
          params.set("category", activeFilter.value || "all");
          if (searchQuery.value.trim()) {
            params.set("search", searchQuery.value.trim());
          }
          if (forceRefresh) {
            params.set("refresh", "1");
          }

          const url = "/api/library?" + params.toString();
          const data = await apiFetch(url, null, forceRefresh ? 90000 : 15000);
          const incomingBooks = Array.isArray(data.books) ? data.books : [];

          if (isAppend) {
            const existingKeys = new Set(books.value.map((b) => b.rel_key));
            const merged = books.value.slice();
            incomingBooks.forEach((b) => {
              if (!existingKeys.has(b.rel_key)) {
                existingKeys.add(b.rel_key);
                merged.push(b);
              }
            });
            books.value = merged;
          } else {
            books.value = incomingBooks;
            if (libraryContentEl.value) {
              libraryContentEl.value.scrollTop = 0;
            }
          }

          pagination.page = Number(data.page || targetPage);
          pagination.total = Number(data.total !== undefined ? data.total : books.value.length);
          pagination.hasMore = Boolean(
            data.has_more !== undefined
              ? data.has_more
              : books.value.length < pagination.total
          );

          applyServerCounts(data.counts, pagination.total);
          collections.value = Array.isArray(data.collections) ? data.collections : [];
          applyConfig(data.config);

          if (data.scan_status) {
            scanStatus.scanning = !!data.scan_status.scanning;
            scanStatus.found_books = data.scan_status.found_books || 0;
            if (scanStatus.scanning) startScanPolling();
          }

          await nextTick();
          setupSentinelObserver();

          if (!config.cloud_restricted_mode) {
            runLocalPdfCoverQueue();
          }
        } catch (err) {
          showToast("载入资料库失败：" + err.message, "error");
        } finally {
          loading.value = false;
          loadingMore.value = false;
        }
      }

      function loadNextPage() {
        if (view.value !== "library" || !pagination.hasMore || loading.value || loadingMore.value) {
          return;
        }
        loadLibrary(false, true);
      }

      function onLibraryScroll(e) {
        if (view.value !== "library" || !pagination.hasMore || loading.value || loadingMore.value) {
          return;
        }
        const el = (e && e.target) || libraryContentEl.value;
        if (!el) return;
        const remaining = el.scrollHeight - el.scrollTop - el.clientHeight;
        if (remaining < 360) {
          loadNextPage();
        }
      }

      function setupSentinelObserver() {
        if (sentinelObserver) {
          sentinelObserver.disconnect();
          sentinelObserver = null;
        }
        if (typeof IntersectionObserver !== "function" || !loadMoreSentinelEl.value) {
          return;
        }
        sentinelObserver = new IntersectionObserver(
          (entries) => {
            entries.forEach((entry) => {
              if (entry.isIntersecting && view.value === "library" && pagination.hasMore && !loading.value && !loadingMore.value) {
                loadNextPage();
              }
            });
          },
          {
            root: libraryContentEl.value || null,
            rootMargin: "280px 0px",
            threshold: 0.01,
          }
        );
        sentinelObserver.observe(loadMoreSentinelEl.value);
      }

      watch(searchQuery, () => {
        if (view.value !== "library") return;
        clearTimeout(searchDebounceTimer);
        searchDebounceTimer = setTimeout(() => {
          loadLibrary(false, false);
        }, 220);
      });

      function startScanPolling() {
        if (scanPollTimer) return;
        scanPollTimer = setInterval(async () => {
          try {
            const st = await apiFetch("/api/library/status", null, 6000);
            scanStatus.scanning = !!st.scanning;
            scanStatus.found_books = st.found_books || 0;
            if (!st.scanning) {
              clearInterval(scanPollTimer);
              scanPollTimer = null;
              await loadLibrary(false, false);
            }
          } catch (_e) {
            clearInterval(scanPollTimer);
            scanPollTimer = null;
          }
        }, 1500);
      }

      async function refreshLibrary() {
        await loadLibrary(true, false);
        if (view.value === "folders") await browse(browseData.current_path);
        showToast("书库已刷新：共 " + serverCounts.all + " 本", "success");
      }

      /* ------------------------------ 导航与文件夹漫游 ------------------------------ */
      function selectFilter(key) {
        const changed = view.value !== "library" || activeFilter.value !== key;
        view.value = "library";
        activeFilter.value = key;
        sidebarOpen.value = false;
        if (changed) {
          loadLibrary(false, false);
        }
      }

      async function browse(path) {
        loading.value = true;
        try {
          const data = await apiFetch("/api/browse?path=" + encodeURIComponent(path || ""), null, 10000);
          browseData.current_path = data.current_path || "";
          browseData.breadcrumbs = Array.isArray(data.breadcrumbs) && data.breadcrumbs.length ? data.breadcrumbs : [{ name: "全部文件夹", path: "" }];
          browseData.dirs = Array.isArray(data.dirs) ? data.dirs : [];
          browseData.books = Array.isArray(data.books) ? data.books : [];
          view.value = "folders";
        } catch (err) {
          showToast("打开文件夹失败：" + err.message, "error");
        } finally {
          loading.value = false;
        }
      }

      async function openFolders(rootPath) {
        browseRoot.value = rootPath || "";
        sidebarOpen.value = false;
        await browse(rootPath || "");
      }

      function openContainingFolder(book) {
        menuKey.value = null;
        const folder = book.folder || "";
        browseRoot.value = folder.split("/")[0] || "";
        browse(folder);
      }

      /* ------------------------------ 书籍状态与多书签同步 ------------------------------ */
      function toggleMenu(book) {
        menuKey.value = menuKey.value === book.rel_key ? null : book.rel_key;
      }

      function patchLocalBook(relKey, state) {
        if (!relKey || !state) return;
        const apply = (list) => {
          list.forEach((b) => {
            if (b.rel_key === relKey) {
              if (state.status !== undefined) b.status = state.status;
              if (state.progress !== undefined) b.progress = state.progress;
              if (state.locator !== undefined) b.locator = state.locator;
              if (Array.isArray(state.bookmarks)) b.bookmarks = state.bookmarks;
              if (state.updated_at !== undefined) b.updated_at = state.updated_at;
            }
          });
        };
        apply(books.value);
        apply(browseData.books);
        if (reader.book && reader.book.rel_key === relKey) {
          if (state.status !== undefined) reader.book.status = state.status;
          if (state.progress !== undefined) reader.book.progress = state.progress;
          if (state.locator !== undefined) reader.book.locator = state.locator;
          if (Array.isArray(state.bookmarks)) {
            reader.book.bookmarks = state.bookmarks;
            reader.bookmarks = state.bookmarks.slice();
          }
        }
      }

      async function setStatus(book, status) {
        menuKey.value = null;
        try {
          const res = await postJson("/api/state", { rel_key: book.rel_key, status: status });
          patchLocalBook(book.rel_key, res.state);
          loadLibrary(false, false);
          const labels = { want: "已加入 Want to Read", finished: "已标记为 Finished", new: "已重置为 NEW" };
          showToast(labels[status] || "状态已更新", "success");
        } catch (err) {
          showToast("更新状态失败：" + err.message, "error");
        }
      }

      /* ==================================================================
         Apple Books 级多书签 (Bookmarks) 系统
         ================================================================== */
      const sortedBookmarks = computed(() => {
        const list = Array.isArray(reader.bookmarks) ? reader.bookmarks.slice() : [];
        return list.sort((a, b) => Number(b.created_at || 0) - Number(a.created_at || 0));
      });

      /* 判断当前阅读位置是否已存在书签 */
      const currentPositionBookmark = computed(() => {
        const list = reader.bookmarks || [];
        if (!list.length || !reader.open) return null;

        for (let i = 0; i < list.length; i++) {
          const bm = list[i];
          const loc = parseLocator(bm.locator);
          if (reader.kind === "pdf") {
            if (Number(loc.page || 0) === Number(reader.current || 1)) {
              return bm;
            }
          } else if (reader.kind === "epub") {
            if (loc.cfi && reader.epubCurrentCfi && loc.cfi === reader.epubCurrentCfi) {
              return bm;
            }
            if (!loc.cfi && loc.href && reader.epubCurrentHref && loc.href === reader.epubCurrentHref) {
              return bm;
            }
          } else if (reader.kind === "md") {
            if (Number.isInteger(loc.block)) {
              const endBlk = isPaged.value && reader.pageEndBlock >= reader.currentBlock ? reader.pageEndBlock : reader.currentBlock;
              if (loc.block >= reader.currentBlock && loc.block <= endBlk) return bm;
              continue;
            }
            const bmRatio = extractScrollRatio(loc);
            if (Math.abs(bmRatio - reader.currentScrollRatio) < 0.03) {
              return bm;
            }
          } else {
            const bmCh = Number.isInteger(loc.chapter) ? loc.chapter : Number(loc.chapter || 0);
            if (Number.isInteger(loc.block)) {
              const endBlk = isPaged.value && reader.pageEndBlock >= reader.currentBlock ? reader.pageEndBlock : reader.currentBlock;
              if (bmCh === reader.current && loc.block >= reader.currentBlock && loc.block <= endBlk) return bm;
              continue;
            }
            const bmRatio = extractScrollRatio(loc);
            if (bmCh === reader.current && Math.abs(bmRatio - reader.currentScrollRatio) < 0.04) {
              return bm;
            }
          }
        }
        return null;
      });

      /* 自动抓取当前视口首段前 50 字作为书签摘要 snippet */
      function extractViewportSnippet() {
        try {
          if (reader.kind === "text" || reader.kind === "md") {
            if (isPagedDom()) {
              const anchorNode = readerBlocks()[captureAnchor().block];
              const anchorText = anchorNode ? (anchorNode.textContent || "").trim().replace(/\s+/g, " ") : "";
              if (anchorText) return anchorText.slice(0, 50);
            }
            const container = readerScrollEl.value;
            if (container) {
              const containerRect = container.getBoundingClientRect();
              const topThreshold = containerRect.top + 56;
              const candidates = container.querySelectorAll("p, li, blockquote, h2, h3");
              for (let i = 0; i < candidates.length; i++) {
                const node = candidates[i];
                const rect = node.getBoundingClientRect();
                if (rect.bottom > topThreshold && (node.textContent || "").trim().length > 0) {
                  return (node.textContent || "").trim().replace(/\s+/g, " ").slice(0, 50);
                }
              }
            }
            if (reader.kind === "text" && readerParagraphs.value.length > 0) {
              return readerParagraphs.value[0].slice(0, 50);
            }
          } else if (reader.kind === "epub") {
            const mount = document.getElementById("epub-viewer-mount");
            const iframe = mount ? mount.querySelector("iframe") : null;
            if (iframe && iframe.contentDocument && iframe.contentDocument.body) {
              const rawText = (iframe.contentDocument.body.innerText || "").trim().replace(/\s+/g, " ");
              if (rawText) return rawText.slice(0, 50);
            }
            return (reader.chapterTitle || reader.title || "EPUB 书签").slice(0, 50);
          } else if (reader.kind === "pdf") {
            const matchedToc = reader.pdfToc.find((t) => t.page === reader.current);
            if (matchedToc && matchedToc.title) {
              return matchedToc.title.slice(0, 50);
            }
            return (reader.title + " · 第 " + (reader.current || 1) + " 页").slice(0, 50);
          }
        } catch (_e) {
          /* ignore */
        }
        return (reader.chapterTitle || reader.title || "").slice(0, 50);
      }

      function buildCurrentBookmarkLabel() {
        if (reader.kind === "pdf") {
          return "第 " + (reader.current || 1) + " 页";
        }
        if (reader.kind === "md") {
          const h = reader.mdToc.find((x) => x.id === reader.activeHeading);
          return h && h.text ? h.text : (reader.title || "Markdown");
        }
        if (reader.kind === "epub") {
          return reader.chapterTitle || ("进度 " + Math.round(reader.progress || 0) + "%");
        }
        return reader.chapterTitle || ("第 " + ((reader.current || 0) + 1) + " 章");
      }

      function buildCurrentLocatorObject() {
        const frac = Math.round(scrollFraction() * 10000) / 10000;
        if (reader.kind === "pdf") {
          return {
            type: "pdf",
            page: reader.current || 1,
            scale: reader.pdfScale || 1.25,
          };
        }
        if (reader.kind === "epub") {
          return {
            type: "epub",
            cfi: reader.epubCurrentCfi || "",
            href: reader.epubCurrentHref || "",
          };
        }
        if (reader.kind === "md") {
          return Object.assign({
            type: "md",
            scrollRatio: frac,
            scroll: frac,
            heading: reader.activeHeading || "",
          }, captureAnchor());
        }
        return Object.assign({
          type: "text",
          chapter: reader.current || 0,
          scrollRatio: frac,
          scroll: frac,
        }, captureAnchor());
      }

      async function toggleCurrentBookmark() {
        if (!reader.book || !reader.book.rel_key) return;
        const existing = currentPositionBookmark.value;
        if (existing) {
          await removeBookmark(existing);
          return;
        }
        try {
          const label = buildCurrentBookmarkLabel();
          const snippet = extractViewportSnippet();
          const locator = buildCurrentLocatorObject();
          const res = await postJson("/api/bookmarks", {
            action: "add",
            rel_key: reader.book.rel_key,
            title: label,
            label: label,
            snippet: snippet,
            locator: locator,
            progress: Number(reader.progress || 0),
          }, 10000);
          if (res && Array.isArray(res.bookmarks)) {
            reader.bookmarks = res.bookmarks;
            if (res.state) patchLocalBook(reader.book.rel_key, res.state);
          }
          showToast("🔖 已添加书签：" + label, "success");
        } catch (err) {
          showToast("添加书签失败：" + err.message, "error");
        }
      }

      async function removeBookmark(bm) {
        if (!reader.book || !bm || !bm.id) return;
        try {
          const res = await postJson("/api/bookmarks", {
            action: "remove",
            rel_key: reader.book.rel_key,
            id: bm.id,
          }, 10000);
          if (res && Array.isArray(res.bookmarks)) {
            reader.bookmarks = res.bookmarks;
            if (res.state) patchLocalBook(reader.book.rel_key, res.state);
          }
          showToast("已移除书签", "info");
        } catch (err) {
          showToast("删除书签失败：" + err.message, "error");
        }
      }

      async function jumpToBookmark(bm) {
        if (!bm || !reader.open) return;
        if (window.innerWidth <= 820) {
          reader.tocOpen = false;
        }
        const loc = parseLocator(bm.locator);
        isRestoringProgress = true;
        try {
          if (reader.kind === "pdf") {
            const targetPage = clamp(Number(loc.page) || 1, 1, reader.total || 1);
            await renderPdfPage(targetPage);
          } else if (reader.kind === "epub") {
            if (epubRenditionInstance) {
              const target = loc.cfi || loc.href || "";
              if (target) await epubRenditionInstance.display(target);
            }
          } else if (reader.kind === "md") {
            await nextTick();
            restorePosition(loc);
            updateReaderProgress();
          } else {
            const ch = Number.isInteger(loc.chapter) ? loc.chapter : Number(loc.chapter || 0);
            if (ch !== reader.current) {
              await loadChapter(ch, loc, true);
            } else {
              await nextTick();
              restorePosition(loc);
              updateReaderProgress();
            }
          }
          triggerResumeBanner("已跳转至书签：" + (bm.label || bm.title || ""));
        } finally {
          isRestoringProgress = false;
          scheduleSave();
        }
      }

      function formatBookmarkTime(ts) {
        if (!ts) return "";
        try {
          const d = new Date(Number(ts) * 1000);
          const pad = (n) => (n < 10 ? "0" + n : String(n));
          return (
            d.getFullYear() +
            "-" +
            pad(d.getMonth() + 1) +
            "-" +
            pad(d.getDate()) +
            " " +
            pad(d.getHours()) +
            ":" +
            pad(d.getMinutes())
          );
        } catch (_e) {
          return "";
        }
      }

      /* ==================================================================
         Apple Books Zen Reader：全格式阅读内核 (TXT / UMD / MD / EPUB / PDF)
         ================================================================== */
      const readerStyle = computed(() => ({
        "--reader-font-size": prefs.fontSize + "px",
        "--reader-line-height": String(prefs.lineHeight),
        "--reader-width": READER_WIDTHS[prefs.width] || READER_WIDTHS.medium,
      }));

      const readerParagraphs = computed(() => {
        return (reader.content || "")
          .split("\n")
          .map((line) => line.replace(/^[\s\u3000]+|[\s\u3000]+$/g, ""))
          .filter((line) => line.length > 0);
      });

      const filteredChapters = computed(() => {
        const q = reader.tocQuery.trim().toLowerCase();
        if (!q) return reader.chapters;
        return reader.chapters.filter((ch) => (ch.title || "").toLowerCase().includes(q) || String(ch.index + 1) === q);
      });

      const filteredMdToc = computed(() => {
        const q = reader.tocQuery.trim().toLowerCase();
        if (!q) return reader.mdToc;
        return reader.mdToc.filter((h) => h.text.toLowerCase().includes(q));
      });

      const filteredEpubToc = computed(() => {
        const q = reader.tocQuery.trim().toLowerCase();
        if (!q) return reader.epubToc;
        return reader.epubToc.filter((item) => (item.label || "").toLowerCase().includes(q));
      });

      const filteredPdfToc = computed(() => {
        const q = reader.tocQuery.trim().toLowerCase();
        if (!q) return reader.pdfToc;
        return reader.pdfToc.filter((item) => (item.title || "").toLowerCase().includes(q));
      });

      const tocTotalCount = computed(() => {
        if (reader.chmWeb && reader.chmInfo) return (reader.chmInfo.toc || []).length;
        if (reader.kind === "md") return reader.mdToc.length;
        if (reader.kind === "epub") return reader.epubToc.length;
        if (reader.kind === "pdf") return reader.pdfToc.length;
        return reader.total;
      });

      const tocFilteredCount = computed(() => {
        if (reader.kind === "md") return filteredMdToc.value.length;
        if (reader.kind === "epub") return filteredEpubToc.value.length;
        if (reader.kind === "pdf") return filteredPdfToc.value.length;
        return filteredChapters.value.length;
      });

      const isPdfNightInverted = computed(() => {
        if (reader.pdfInvertManual !== null) return reader.pdfInvertManual;
        return prefs.theme === "quiet" || prefs.theme === "bold";
      });

      const readerSubtitle = computed(() => {
        if (reader.chmWeb) {
          return reader.chmPageTitle || "原版网页模式";
        }
        if (reader.kind === "md") {
          const h = reader.mdToc.find((x) => x.id === reader.activeHeading);
          return h ? h.text : (reader.author || "Markdown");
        }
        if (reader.kind === "epub") {
          return reader.chapterTitle || reader.author || "EPUB";
        }
        if (reader.kind === "pdf") {
          if (reader.pdfNativeMode) return "浏览器原生 PDF 预览";
          return reader.total ? ("第 " + reader.current + " / " + reader.total + " 页") : "PDF";
        }
        if (!reader.total) return reader.author || "";
        return (reader.chapterTitle || "") + " · " + (reader.current + 1) + "/" + reader.total;
      });

      /* 将四色护眼主题、字体、字号、行高实时注入 EPUB rendition.themes */
      function applyEpubThemeAndTypography() {
        if (!epubRenditionInstance) return;
        try {
          const themeObj = READER_THEMES.find((t) => t.key === prefs.theme) || READER_THEMES[0];
          const fontObj = READER_FONTS.find((f) => f.key === prefs.font) || READER_FONTS[0];
          const themes = epubRenditionInstance.themes;
          if (!themes) return;

          themes.fontSize(prefs.fontSize + "px");
          themes.override("color", themeObj.fg);
          themes.override("background", themeObj.bg);
          themes.override("font-family", fontObj.css);
          themes.override("line-height", String(prefs.lineHeight));
        } catch (_e) {
          /* ignore */
        }
      }

      watch(prefs, (val) => {
        try { localStorage.setItem(READER_PREFS_KEY, JSON.stringify(val)); } catch (_e) { /* ignore */ }
        if (reader.open && reader.kind === "epub") {
          applyEpubThemeAndTypography();
        }
        /* 字号/行高/翻页模式变化前（DOM 尚未重排）记下段落锚点，重排后精准回到同一段落 */
        if (reader.open && (reader.kind === "text" || reader.kind === "md")) {
          const anchor = (isPagedDom() && pagedAnchor && Number.isInteger(pagedAnchor.block))
            ? Object.assign({}, pagedAnchor)
            : captureAnchor();
          nextTick(() => {
            const el = readerScrollEl.value;
            if (el && !isPagedDom()) {
              el.style.removeProperty("--pg-x");
              el.style.removeProperty("--pg-dur");
            }
            if (anchor.block !== undefined) {
              scrollToAnchor(anchor.block, anchor.blockOffset);
            } else if (isPagedDom() && layoutPaged()) {
              setPage(0, false);
            }
            updateReaderProgress();
          });
        }
      }, { deep: true });

      function adjustFontSize(delta) {
        prefs.fontSize = clamp(Number(prefs.fontSize) + delta, 15, 30);
      }

      function fetchChapter(relKey, idx) {
        return apiFetch("/api/read/text?path=" + encodeURIComponent(relKey) + "&chapter=" + idx, null, 20000);
      }

      function scrollFraction() {
        if (isPagedDom()) return reader.pages > 1 ? reader.page / (reader.pages - 1) : 0;
        const el = readerScrollEl.value;
        if (!el) return 0;
        const max = el.scrollHeight - el.clientHeight;
        if (max <= 4) return 0;
        return clamp(el.scrollTop / max, 0, 1);
      }

      function restoreScroll(frac) {
        const el = readerScrollEl.value;
        if (!el) return;
        const ratio = clamp(Number(frac) || 0, 0, 1);
        if (isPagedDom()) {
          layoutPaged();
          setPage(Math.round(ratio * (reader.pages - 1)), false);
          reader.currentScrollRatio = ratio;
          pagedAnchor = captureAnchor();
          return;
        }
        const max = el.scrollHeight - el.clientHeight;
        el.scrollTop = max > 0 ? Math.round(ratio * max) : 0;
        reader.currentScrollRatio = ratio;
      }

      /* ------------------------------ 段落锚点定位 (TXT / UMD / MD) ------------------------------
         以视口顶部第一个块（TXT/UMD：章节标题 + 各段 <p>；MD：正文顶层块元素）的序号 + 块内偏移比例
         记录位置，不受字号、行高、屏幕宽度与图片懒加载影响。旧的 scrollRatio 仍保留作兼容回退。 */
      let anchorHold = null;

      function readerBlocks() {
        const el = readerScrollEl.value;
        const page = el ? el.querySelector(".reader-page") : null;
        if (!page) return [];
        return reader.kind === "md"
          ? Array.from(page.children)
          : Array.from(page.querySelectorAll(":scope > h2, :scope > p"));
      }

      function captureAnchor() {
        const el = readerScrollEl.value;
        const blocks = readerBlocks();
        if (!el || !blocks.length) return {};
        if (isPagedDom()) return capturePagedAnchor(blocks);
        reader.pageEndBlock = -1;
        const viewTop = el.getBoundingClientRect().top;
        let lo = 0;
        let hi = blocks.length - 1;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (blocks[mid].getBoundingClientRect().bottom > viewTop + 1) hi = mid;
          else lo = mid + 1;
        }
        const rect = blocks[lo].getBoundingClientRect();
        const offset = rect.height > 0 ? clamp((viewTop - rect.top) / rect.height, 0, 1) : 0;
        return { block: lo, blockOffset: Math.round(offset * 10000) / 10000 };
      }

      function scrollToAnchor(block, offset) {
        const el = readerScrollEl.value;
        const blocks = readerBlocks();
        if (!el || !blocks.length) return false;
        const node = blocks[clamp(Number(block) || 0, 0, blocks.length - 1)];
        if (isPagedDom()) return scrollToPagedAnchor(node, block, offset);
        const rect = node.getBoundingClientRect();
        if (anchorHold) anchorHold.selfScroll = true;
        el.scrollTop += rect.top - el.getBoundingClientRect().top + clamp(Number(offset) || 0, 0, 1) * rect.height;
        return true;
      }

      /* MD 中上方图片懒加载完成会把正文往下推：在用户手动滚动前，图片每加载一张就重新对齐锚点 */
      function holdAnchorWhileImagesLoad(block, offset) {
        anchorHold = null;
        const el = readerScrollEl.value;
        if (!el || reader.kind !== "md" || isPagedDom()) return;
        const pending = Array.from(el.querySelectorAll(".reader-page img")).filter((img) => !img.complete);
        if (!pending.length) return;
        const hold = { selfScroll: false, until: Date.now() + 8000 };
        anchorHold = hold;
        const realign = () => {
          if (anchorHold === hold && Date.now() < hold.until) scrollToAnchor(block, offset);
        };
        pending.forEach((img) => {
          img.addEventListener("load", realign, { once: true });
          img.addEventListener("error", realign, { once: true });
        });
      }

      /* 统一恢复入口：有段落锚点按锚点定位，旧数据回退到滚动比例 */
      function restorePosition(loc) {
        const pos = loc && typeof loc === "object" ? loc : { scrollRatio: loc };
        if (pos.lastPage) {
          anchorHold = null;
          restoreScroll(1);
          return;
        }
        if (Number.isInteger(pos.block) && scrollToAnchor(pos.block, pos.blockOffset)) {
          reader.currentScrollRatio = scrollFraction();
          holdAnchorWhileImagesLoad(pos.block, pos.blockOffset);
          return;
        }
        anchorHold = null;
        restoreScroll(extractScrollRatio(pos));
      }

      /* ------------------------------ 自适应屏幕分页与手势划屏引擎 (TXT / UMD / MD + EPUB / PDF) ------------------------------
         CSS 多栏排版：每栏宽高严格贴合当前手机屏幕可用视口，栏距 = 左右内边距之和，
         翻一页正好平移一个容器宽度 (pageStride)。同时归一化缩放比 visualStride，兼容入场缩放动画与双指缩放。 */
      const isPaged = computed(() =>
        prefs.layoutMode === "paged" && (reader.kind === "text" || reader.kind === "md"));
      let pageStride = 0;
      let pagedAnchor = null;   // 当前页锚点：图片加载、转屏、地址栏伸缩重排后据此回到原段落
      let relayoutTimer = null;
      let swipe = null;
      let stageSwipe = null;
      let suppressTapUntil = 0;

      /* 以 DOM 实际类名判断当前是否已完成分页挂载（防止模式切换瞬间 DOM 尚未更新） */
      function isPagedDom() {
        const el = readerScrollEl.value;
        return !!(el && el.classList.contains("paged"));
      }

      function startColOfRect(r, origin, vStride) {
        return Math.max(0, Math.floor((r.left - origin) / vStride + 0.02));
      }

      function endColOfRect(r, origin, vStride) {
        if ((r.right - r.left) / vStride > 0.96) {
          return Math.max(0, Math.floor((r.right - origin) / vStride - 0.002));
        }
        return startColOfRect(r, origin, vStride);
      }

      function measurePagedGeometry() {
        const el = readerScrollEl.value;
        const page = el ? el.querySelector(".reader-page") : null;
        if (!el || !page) return null;
        const vStride = el.getBoundingClientRect().width || pageStride || 1;
        const origin = page.getBoundingClientRect().left;
        return { el, page, origin, vStride };
      }

      function layoutPaged() {
        const el = readerScrollEl.value;
        const page = el ? el.querySelector(".reader-page") : null;
        if (!el || !page) return false;
        const cs = getComputedStyle(el);
        const padX = Math.max(12, Math.round(parseFloat(cs.paddingLeft) || 20));
        const padY = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
        const clientW = el.clientWidth || Math.round(el.getBoundingClientRect().width) || 360;
        const colW = Math.max(120, clientW - padX * 2);
        const colGap = clientW - colW;
        pageStride = colW + colGap;
        el.style.setProperty("--pg-w", colW + "px");
        el.style.setProperty("--pg-gap", colGap + "px");
        el.style.setProperty("--pg-h", Math.max(120, Math.floor(el.clientHeight - padY)) + "px");
        el.scrollTop = 0;
        el.scrollLeft = 0;

        /* 横向多栏下原生 lazy 图片在视口右侧不会触发加载：改为 eager 并在加载后自动重排回锚点 */
        page.querySelectorAll("img").forEach((img) => {
          if (img.dataset.pgBound) return;
          img.dataset.pgBound = "1";
          if (img.loading === "lazy") img.loading = "eager";
          if (!img.complete) {
            img.addEventListener("load", scheduleRelayout, { once: true });
            img.addEventListener("error", scheduleRelayout, { once: true });
          }
        });

        const geo = measurePagedGeometry();
        let maxCol = 0;
        if (geo) {
          const children = page.children;
          for (let i = children.length - 1; i >= 0; i--) {
            const rs = children[i].getClientRects();
            if (rs.length > 0) {
              maxCol = endColOfRect(rs[rs.length - 1], geo.origin, geo.vStride);
              break;
            }
          }
        }
        reader.pages = Math.max(1, maxCol + 1);
        if (reader.page > reader.pages - 1) reader.page = reader.pages - 1;
        return true;
      }

      function setPage(n, animate) {
        const el = readerScrollEl.value;
        if (!el) return;
        reader.page = clamp(Number(n) || 0, 0, Math.max(0, reader.pages - 1));
        el.style.setProperty("--pg-dur", animate ? "260ms" : "0ms");
        el.style.setProperty("--pg-x", (-reader.page * pageStride) + "px");
      }

      function pageOfNode(node) {
        const geo = measurePagedGeometry();
        const rects = node ? node.getClientRects() : [];
        if (!geo || !rects.length) return 0;
        return clamp(startColOfRect(rects[0], geo.origin, geo.vStride), 0, reader.pages - 1);
      }

      /* 记录当前页首个可见块 + 块内已排在前面页的高度比例，并同步计算本页末尾块序号供书签高亮使用 */
      function capturePagedAnchor(blocks) {
        if (!pageStride) layoutPaged();
        const geo = measurePagedGeometry();
        if (!geo) return {};
        const curPage = reader.page;
        const endColOf = (node) => {
          const rs = node.getClientRects();
          return rs.length ? endColOfRect(rs[rs.length - 1], geo.origin, geo.vStride) : -1;
        };
        const startColOf = (node) => {
          const rs = node.getClientRects();
          return rs.length ? startColOfRect(rs[0], geo.origin, geo.vStride) : 999999;
        };

        let lo = 0;
        let hi = blocks.length - 1;
        while (lo < hi) {
          const mid = (lo + hi) >> 1;
          if (endColOf(blocks[mid]) >= curPage) hi = mid;
          else lo = mid + 1;
        }

        let eLo = lo;
        let eHi = blocks.length - 1;
        while (eLo < eHi) {
          const mid = (eLo + eHi + 1) >> 1;
          if (startColOf(blocks[mid]) <= curPage) eLo = mid;
          else eHi = mid - 1;
        }
        reader.pageEndBlock = eLo;

        let total = 0;
        let before = 0;
        Array.from(blocks[lo].getClientRects()).forEach((r) => {
          total += r.height;
          if (endColOfRect(r, geo.origin, geo.vStride) < curPage) before += r.height;
        });
        const offset = total > 0 ? clamp(before / total, 0, 1) : 0;
        return { block: lo, blockOffset: Math.round(offset * 10000) / 10000 };
      }

      function scrollToPagedAnchor(node, block, offset) {
        layoutPaged();
        const geo = measurePagedGeometry();
        const rects = Array.from(node.getClientRects());
        if (!geo || !rects.length) return false;
        const total = rects.reduce((sum, r) => sum + r.height, 0);
        const target = clamp(Number(offset) || 0, 0, 1) * total;
        let acc = 0;
        let hit = rects[rects.length - 1];
        for (let i = 0; i < rects.length; i++) {
          if (acc + rects[i].height > target) { hit = rects[i]; break; }
          acc += rects[i].height;
        }
        setPage(startColOfRect(hit, geo.origin, geo.vStride), false);
        pagedAnchor = { block: Number(block) || 0, blockOffset: Number(offset) || 0 };
        capturePagedAnchor(readerBlocks());
        return true;
      }

      function scheduleRelayout() {
        clearTimeout(relayoutTimer);
        relayoutTimer = setTimeout(() => {
          if (!reader.open || !isPagedDom() || swipe) return;
          const a = pagedAnchor;
          if (!(a && Number.isInteger(a.block) && scrollToAnchor(a.block, a.blockOffset))) {
            if (layoutPaged()) setPage(reader.page, false);
          }
          updateReaderProgress();
        }, 100);
      }

      const pagedResizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(() => scheduleRelayout()) : null;
      watch(readerScrollEl, (el) => {
        if (!pagedResizeObserver) return;
        pagedResizeObserver.disconnect();
        if (el) pagedResizeObserver.observe(el);
      });

      /* 仅在分页模式下挂载非 passive 的 touchmove，拦截横向划屏时的浏览器橡皮筋回弹，同时保留代码块/表格滚动 */
      watch([readerScrollEl, isPaged], ([el, paged], _prev, onCleanup) => {
        if (!el || !paged) return;
        const onMove = (ev) => onReaderTouchMove(ev);
        el.addEventListener("touchmove", onMove, { passive: false });
        onCleanup(() => {
          el.removeEventListener("touchmove", onMove);
        });
      }, { immediate: true });

      function afterPageTurn() {
        pagedAnchor = captureAnchor();
        updateReaderProgress();
        scheduleSave();
      }

      /* 翻页：TXT/UMD 越过本章首尾自动平滑进入上一章末页 / 下一章首页；MD 到头阻尼回弹 */
      async function goPage(n) {
        if (!isPagedDom()) return;
        anchorHold = null;
        if (reader.aaOpen) reader.aaOpen = false;
        if (n < 0 || n >= reader.pages) {
          const forward = n >= reader.pages;
          const canCross = reader.kind === "text" && !reader.loading &&
            (forward ? reader.current < reader.total - 1 : reader.current > 0);
          if (!canCross) {
            setPage(reader.page, true);
            showToast(forward ? "已是最后一页" : "已是第一页", "info");
            return;
          }
          await loadChapter(reader.current + (forward ? 1 : -1), forward ? 0 : { lastPage: true }, false);
          pagedAnchor = captureAnchor();
          scheduleSave();
          return;
        }
        setPage(n, true);
        afterPageTurn();
      }

      /* ------------------------------ 手机划屏翻页手势（TXT/UMD/MD 跟手平移 + EPUB/PDF 划屏翻页） ------------------------------ */
      function onReaderTouchStart(e) {
        swipe = null;
        if (!isPagedDom() || !e.touches || e.touches.length !== 1 || reader.loading) return;
        const t = e.target;
        if (t && t.closest && t.closest("button, a, input, textarea, select, label, .aa-panel, .toc-drawer")) return;
        const scroller = t && t.closest ? t.closest("pre, .table-wrap") : null;
        if (scroller && scroller.scrollWidth > scroller.clientWidth + 2) return;
        const p = e.touches[0];
        swipe = { x: p.clientX, y: p.clientY, t: Date.now(), dx: 0, dir: "" };
      }

      function onReaderTouchMove(e) {
        if (!swipe) return;
        if (!e.touches || e.touches.length !== 1) { onReaderTouchCancel(); return; }
        const p = e.touches[0];
        const dx = p.clientX - swipe.x;
        const dy = p.clientY - swipe.y;
        if (!swipe.dir) {
          if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
          swipe.dir = Math.abs(dx) > Math.abs(dy) * 1.15 ? "h" : "v";
        }
        if (swipe.dir !== "h") return;
        const sel = window.getSelection ? String(window.getSelection()).trim() : "";
        if (sel) { onReaderTouchCancel(); return; }
        if (e.cancelable) e.preventDefault();
        swipe.dx = dx;
        const atStart = reader.page === 0 && !(reader.kind === "text" && reader.current > 0);
        const atEnd = reader.page >= reader.pages - 1 && !(reader.kind === "text" && reader.current < reader.total - 1);
        const shift = (dx > 0 && atStart) || (dx < 0 && atEnd) ? dx * 0.28 : dx;
        const el = readerScrollEl.value;
        if (el) {
          el.style.setProperty("--pg-dur", "0ms");
          el.style.setProperty("--pg-x", (-reader.page * pageStride + shift) + "px");
        }
      }

      function onReaderTouchEnd() {
        const s = swipe;
        swipe = null;
        if (!s || s.dir !== "h") return;
        suppressTapUntil = Date.now() + 420;   // 屏蔽划屏松手后浏览器补发的合成 click
        const speed = Math.abs(s.dx) / Math.max(1, Date.now() - s.t);
        if (Math.abs(s.dx) > 46 || (Math.abs(s.dx) > 18 && speed > 0.35)) {
          if (reader.toolbarVisible) reader.toolbarVisible = false;
          goPage(reader.page + (s.dx < 0 ? 1 : -1));
        } else {
          setPage(reader.page, true);
        }
      }

      function onReaderTouchCancel() {
        if (swipe && swipe.dir === "h") setPage(reader.page, true);
        swipe = null;
      }

      /* EPUB 外框与 PDF 画布区的跟手轻扫翻页手势（放大横向溢出时自动让位给画布平移） */
      function onStageSwipeStart(e) {
        stageSwipe = null;
        if (!reader.open || !e.touches || e.touches.length !== 1 || reader.loading) return;
        if (reader.kind === "pdf") {
          if (reader.pdfNativeMode || reader.pdfRendering) return;
          const scroller = e.currentTarget;
          if (scroller && scroller.scrollWidth > scroller.clientWidth + 6) return;
        }
        const p = e.touches[0];
        stageSwipe = { x: p.screenX, y: p.screenY, dx: 0, dir: "", t: Date.now() };
      }

      function onStageSwipeMove(e) {
        if (!stageSwipe || !e.touches || e.touches.length !== 1) return;
        const p = e.touches[0];
        const dx = p.screenX - stageSwipe.x;
        const dy = p.screenY - stageSwipe.y;
        if (!stageSwipe.dir) {
          if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
          stageSwipe.dir = Math.abs(dx) > Math.abs(dy) * 1.15 ? "h" : "v";
        }
        if (stageSwipe.dir !== "h") return;
        stageSwipe.dx = dx;
        if (reader.kind === "epub") {
          setEpubMountTransform(dx * 0.85, 1, 0);
        }
      }

      function onStageSwipeEnd(e) {
        const s = stageSwipe;
        stageSwipe = null;
        if (!s || !e.changedTouches || !e.changedTouches.length) return;
        const p = e.changedTouches[0];
        const dx = p.screenX - s.x;
        const dy = p.screenY - s.y;
        const dt = Math.max(1, Date.now() - s.t);
        if (s.dir === "v" || Math.abs(dx) <= Math.abs(dy) * 1.15) {
          if (reader.kind === "epub") setEpubMountTransform(0, 1, 180);
          return;
        }
        if (Math.abs(dx) > 42 || (Math.abs(dx) > 16 && Math.abs(dx) / dt > 0.3)) {
          suppressTapUntil = Date.now() + 420;
          if (reader.aaOpen) reader.aaOpen = false;
          if (reader.toolbarVisible) reader.toolbarVisible = false;
          if (reader.kind === "epub") {
            animateEpubTurn(dx < 0 ? "next" : "prev", dx);
          } else {
            if (dx < 0) readerNextAction(false);
            else readerPrevAction(false);
          }
        } else if (reader.kind === "epub") {
          setEpubMountTransform(0, 1, 180);
        }
      }

      function updateActiveHeading() {
        const el = readerScrollEl.value;
        if (!el || reader.mdToc.length === 0) return;
        if (isPagedDom()) {
          let activeId = reader.mdToc[0].id;
          for (let i = 0; i < reader.mdToc.length; i++) {
            const node = el.querySelector("#" + reader.mdToc[i].id);
            if (node && pageOfNode(node) <= reader.page) activeId = reader.mdToc[i].id;
            else if (node) break;
          }
          reader.activeHeading = activeId;
          return;
        }
        const threshold = el.scrollTop + 96;
        let active = reader.mdToc[0].id;
        for (let i = 0; i < reader.mdToc.length; i++) {
          const node = el.querySelector("#" + reader.mdToc[i].id);
          if (node && node.offsetTop <= threshold) {
            active = reader.mdToc[i].id;
          } else if (node) {
            break;
          }
        }
        reader.activeHeading = active;
      }

      function updateReaderProgress() {
        const frac = scrollFraction();
        reader.currentScrollRatio = frac;
        const anchor = captureAnchor();
        reader.currentBlock = anchor.block !== undefined ? anchor.block : -1;
        let pct;
        if (reader.kind === "md") {
          const el = readerScrollEl.value;
          const max = el ? (el.scrollHeight - el.clientHeight) : 0;
          const singleScreen = isPagedDom() ? reader.pages <= 1 : max <= 4;
          pct = singleScreen ? 100 : frac * 100;
          updateActiveHeading();
        } else {
          const total = Math.max(reader.total, 1);
          pct = ((reader.current + frac) / total) * 100;
        }
        reader.progress = Math.round(clamp(pct, 0, 100) * 10) / 10;
      }

      function buildReaderPayload() {
        if (reader.book && reader.chmWeb && !isRestoringProgress) {
          return buildChmWebPayload();
        }
        if (!reader.book || reader.loading || reader.error || isRestoringProgress) return null;
        const effectiveProgress = Math.max(Number(reader.progress) || 0, 1);
        if (reader.kind === "text") {
          if (!reader.total) return null;
          const frac = Math.round(scrollFraction() * 10000) / 10000;
          return {
            rel_key: reader.book.rel_key,
            progress: effectiveProgress,
            locator: Object.assign({
              type: "text",
              chapter: reader.current,
              scrollRatio: frac,
              scroll: frac,
            }, captureAnchor(), reader.chmInfo ? { chmWeb: false, chmPage: reader.chmPage || "" } : {}),
          };
        }
        if (reader.kind === "md") {
          if (!reader.mdHtml) return null;
          const frac = Math.round(scrollFraction() * 10000) / 10000;
          return {
            rel_key: reader.book.rel_key,
            progress: effectiveProgress,
            locator: Object.assign({
              type: "md",
              scrollRatio: frac,
              scroll: frac,
              heading: reader.activeHeading,
            }, captureAnchor()),
          };
        }
        if (reader.kind === "epub") {
          return {
            rel_key: reader.book.rel_key,
            progress: effectiveProgress,
            locator: {
              type: "epub",
              cfi: reader.epubCurrentCfi || "",
              href: reader.epubCurrentHref || "",
            },
          };
        }
        if (reader.kind === "pdf") {
          if (!reader.total && !reader.pdfNativeMode) return null;
          return {
            rel_key: reader.book.rel_key,
            progress: effectiveProgress,
            locator: {
              type: "pdf",
              page: reader.current || 1,
              scale: reader.pdfScale,
            },
          };
        }
        return null;
      }

      async function saveReaderState(payload) {
        const body = payload || buildReaderPayload();
        if (!body) return;
        try {
          const res = await postJson("/api/state", body, 10000);
          if (res && res.state) patchLocalBook(body.rel_key, res.state);
        } catch (_e) {
          /* 进度保存失败时静默忽略 */
        }
      }

      function scheduleSave() {
        if (isRestoringProgress) return;
        clearTimeout(readerSaveTimer);
        readerSaveTimer = setTimeout(() => {
          if (!isRestoringProgress) {
            saveReaderState();
          }
        }, 800);
      }

      function onReaderScroll() {
        if (anchorHold) {
          if (anchorHold.selfScroll) anchorHold.selfScroll = false;
          else anchorHold = null;
        }
        if (isRestoringProgress) return;
        if (reader.kind !== "text" && reader.kind !== "md") return;
        if (readerScrollRaf) return;
        readerScrollRaf = requestAnimationFrame(() => {
          readerScrollRaf = 0;
          if (isRestoringProgress) return;
          updateReaderProgress();
          scheduleSave();
        });
      }

      /* ------------------------------ 安全销毁 EPUB / PDF 实例 ------------------------------ */
      async function destroyNativeInstances() {
        if (pdfRenderTask && typeof pdfRenderTask.cancel === "function") {
          try { pdfRenderTask.cancel(); } catch (_e) { /* ignore */ }
        }
        pdfRenderTask = null;

        if (pdfDocInstance && typeof pdfDocInstance.destroy === "function") {
          try { await pdfDocInstance.destroy(); } catch (_e) { /* ignore */ }
        }
        pdfDocInstance = null;

        if (epubRenditionInstance && typeof epubRenditionInstance.destroy === "function") {
          try { epubRenditionInstance.destroy(); } catch (_e) { /* ignore */ }
        }
        epubRenditionInstance = null;

        if (epubBookInstance && typeof epubBookInstance.destroy === "function") {
          try { epubBookInstance.destroy(); } catch (_e) { /* ignore */ }
        }
        epubBookInstance = null;

        const epubMount = document.getElementById("epub-viewer-mount");
        if (epubMount) epubMount.innerHTML = "";

        const pdfCanvas = document.getElementById("pdf-canvas-mount");
        if (pdfCanvas) {
          const ctx = pdfCanvas.getContext("2d");
          if (ctx) ctx.clearRect(0, 0, pdfCanvas.width, pdfCanvas.height);
        }
      }

      /* ------------------------------ TXT / UMD 加载 ------------------------------ */
      async function loadChapter(idx, position, keepRestoreLock) {
        if (!reader.book) return false;
        const seq = ++readerLoadSeq;
        reader.pendingChapter = idx;
        reader.pendingScroll = position;
        pagedAnchor = null;
        reader.loading = true;
        reader.error = "";
        isRestoringProgress = true;

        try {
          const data = await fetchChapter(reader.book.rel_key, idx);
          if (seq !== readerLoadSeq || !reader.open) return false;
          reader.title = data.title || reader.title;
          reader.author = data.author || "";
          if (!reader.chapters.length || reader.chapters.length !== (data.chapters || []).length) {
            reader.chapters = Array.isArray(data.chapters) ? data.chapters : [];
          }
          reader.total = data.total_chapters || 1;
          reader.current = data.current_chapter || 0;
          reader.chapterTitle = data.chapter_title || "";
          reader.content = data.content || "";
          reader.loading = false;

          await nextTick();
          await new Promise((resolve) => requestAnimationFrame(resolve));
          if (seq !== readerLoadSeq || !reader.open) return false;

          restorePosition(position);
          updateReaderProgress();
          if (isPagedDom()) setTimeout(scheduleRelayout, 280);
          return true;
        } catch (err) {
          if (seq === readerLoadSeq) {
            reader.error = "读取失败：" + err.message;
            reader.loading = false;
          }
          return false;
        } finally {
          if (seq === readerLoadSeq && !keepRestoreLock) {
            isRestoringProgress = false;
          }
        }
      }

      /* ------------------------------ Markdown 加载 ------------------------------ */
      function renderMarkdown(src, folder) {
        let html;
        if (window.marked && typeof window.marked.parse === "function") {
          html = window.marked.parse(src, { gfm: true, breaks: true });
        } else {
          html = "<pre><code>" + escapeHtml(src) + "</code></pre>";
        }
        html = sanitizeHtml(html);

        const doc = document.implementation.createHTMLDocument("");
        const box = doc.createElement("div");
        box.innerHTML = html;

        const toc = [];
        box.querySelectorAll("h1, h2, h3").forEach((h, i) => {
          const id = "md-h-" + i;
          h.id = id;
          toc.push({ id: id, level: Number(h.tagName.slice(1)), text: (h.textContent || "").trim() });
        });
        box.querySelectorAll("a[href]").forEach((a) => {
          const href = a.getAttribute("href") || "";
          if (/^https?:/i.test(href)) {
            a.setAttribute("target", "_blank");
            a.setAttribute("rel", "noopener noreferrer");
          }
        });
        box.querySelectorAll("img[src]").forEach((img) => {
          const s = img.getAttribute("src") || "";
          if (!/^(https?:|data:|blob:|\/)/i.test(s)) {
            img.setAttribute("src", "/api/raw?path=" + encodeURIComponent(joinRelPath(folder, s)));
          }
          img.setAttribute("loading", "lazy");
        });
        if (window.hljs && typeof window.hljs.highlightElement === "function") {
          box.querySelectorAll("pre code").forEach((code) => {
            try { window.hljs.highlightElement(code); } catch (_e) { /* ignore */ }
          });
        }
        box.querySelectorAll("table").forEach((table) => {
          const wrap = doc.createElement("div");
          wrap.className = "table-wrap";
          table.parentNode.insertBefore(wrap, table);
          wrap.appendChild(table);
        });
        return { html: box.innerHTML, toc: toc };
      }

      async function loadMarkdown(position, keepRestoreLock) {
        if (!reader.book) return false;
        const seq = ++readerLoadSeq;
        reader.pendingScroll = position;
        pagedAnchor = null;
        reader.loading = true;
        reader.error = "";
        isRestoringProgress = true;

        try {
          const data = await fetchChapter(reader.book.rel_key, 0);
          if (seq !== readerLoadSeq || !reader.open) return false;
          reader.title = data.title || reader.title;
          reader.author = data.author || "";
          const rendered = renderMarkdown(data.content || "", reader.book.folder || "");
          reader.mdToc = rendered.toc;
          reader.mdHtml = rendered.html;
          reader.loading = false;

          await nextTick();
          await new Promise((resolve) => requestAnimationFrame(resolve));
          if (seq !== readerLoadSeq || !reader.open) return false;

          restorePosition(position);
          updateReaderProgress();
          if (isPagedDom()) setTimeout(scheduleRelayout, 280);
          return true;
        } catch (err) {
          if (seq === readerLoadSeq) {
            reader.error = "读取失败：" + err.message;
            reader.loading = false;
          }
          return false;
        } finally {
          if (seq === readerLoadSeq && !keepRestoreLock) {
            isRestoringProgress = false;
          }
        }
      }

      /* ------------------------------ EPUB 阅读内核 (jszip + epub.js) ------------------------------ */
      function flattenEpubNav(items, level, out) {
        if (!Array.isArray(items)) return out;
        items.forEach((item) => {
          if (!item) return;
          out.push({
            label: (item.label || "").trim() || "未命名章节",
            href: item.href || "",
            level: Math.min(3, level),
          });
          if (Array.isArray(item.subitems) && item.subitems.length > 0) {
            flattenEpubNav(item.subitems, level + 1, out);
          }
        });
        return out;
      }

      async function loadEpubBook(initialCfiOrHref, keepRestoreLock) {
        if (!reader.book) return false;
        const seq = ++readerLoadSeq;
        const relKey = reader.book.rel_key;
        reader.loading = true;
        reader.error = "";
        isRestoringProgress = true;

        await destroyNativeInstances();

        /* 异步触发后端提取 EPUB 封面并缓存（4 秒超时保护） */
        postJson("/api/cover/extract", { path: relKey }, 6000)
          .then((res) => {
            if (res && res.has_cover && res.cover_url) {
              patchBookCover(relKey, res.cover_url);
            }
          })
          .catch(() => {});

        try {
          if (typeof window.ePub !== "function") {
            throw new Error("EPUB.js 组件尚未就绪");
          }

          const resp = await fetch("/api/raw?path=" + encodeURIComponent(relKey));
          if (!resp.ok) {
            throw new Error("HTTP " + resp.status);
          }
          const arrayBuffer = await resp.arrayBuffer();
          if (seq !== readerLoadSeq || !reader.open) return false;

          const mountEl = document.getElementById("epub-viewer-mount");
          if (!mountEl) {
            throw new Error("未找到 EPUB 原生挂载容器 #epub-viewer-mount");
          }
          mountEl.innerHTML = "";

          /* 创建非响应式 EPUB 实例 */
          const bookInst = markRaw(window.ePub(arrayBuffer));
          epubBookInstance = bookInst;

          const rendition = markRaw(
            bookInst.renderTo(mountEl, {
              width: "100%",
              height: "100%",
              spread: prefs.epubSpread || "auto",
              flow: "paginated",
            })
          );
          epubRenditionInstance = rendition;

          applyEpubThemeAndTypography();

          /* 提取元信息与目录 */
          try {
            const meta = await bookInst.loaded.metadata;
            if (seq === readerLoadSeq && meta) {
              if (meta.title) reader.title = meta.title;
              if (meta.creator) reader.author = meta.creator;
            }
          } catch (_e) { /* ignore */ }

          try {
            const nav = await bookInst.loaded.navigation;
            if (seq === readerLoadSeq && nav && Array.isArray(nav.toc)) {
              reader.epubToc = flattenEpubNav(nav.toc, 1, []);
              reader.total = reader.epubToc.length || 1;
            }
          } catch (_e) { /* ignore */ }

          rendition.on("relocated", (location) => {
            if (seq !== readerLoadSeq || !location || !location.start) return;
            const cfi = location.start.cfi || "";
            const href = location.start.href || "";
            reader.epubCurrentCfi = cfi;
            reader.epubCurrentHref = href;

            /* 匹配当前章节名 */
            const matchedToc = reader.epubToc.find((t) => href && t.href && (t.href === href || t.href.split("#")[0] === href.split("#")[0]));
            if (matchedToc) {
              reader.chapterTitle = matchedToc.label;
            }

            if (isRestoringProgress) {
              return;
            }

            /* 计算百分比进度 */
            let pct = reader.progress;
            if (bookInst.locations && bookInst.locations.length() > 0 && cfi) {
              const p = bookInst.locations.percentageFromCfi(cfi);
              if (typeof p === "number" && !Number.isNaN(p)) {
                pct = p * 100;
              }
            } else if (location.start.index !== undefined && bookInst.spine && bookInst.spine.length) {
              pct = ((location.start.index + 0.5) / Math.max(bookInst.spine.length, 1)) * 100;
            }
            reader.progress = Math.round(clamp(pct, 0, 100) * 10) / 10;
            scheduleSave();
          });

          /* EPUB 键盘快捷键支持 */
          rendition.on("keyup", (ev) => {
            if (!ev) return;
            if (ev.key === "ArrowLeft") epubPrevPage();
            else if (ev.key === "ArrowRight") epubNextPage();
            else if (ev.key === "Escape") closeReader();
          });

          if (initialCfiOrHref) {
            await rendition.display(initialCfiOrHref);
          } else {
            await rendition.display();
          }

          if (seq !== readerLoadSeq || !reader.open) return false;
          applyEpubThemeAndTypography();
          reader.loading = false;

          /* 后台生成精确 locations 索引供 CFI 百分比换算 */
          bookInst.ready
            .then(() => bookInst.locations.generate(1200))
            .then(() => {
              if (seq === readerLoadSeq && reader.epubCurrentCfi && bookInst.locations.length() > 0 && !isRestoringProgress) {
                const p = bookInst.locations.percentageFromCfi(reader.epubCurrentCfi);
                if (typeof p === "number" && !Number.isNaN(p)) {
                  reader.progress = Math.round(clamp(p * 100, 0, 100) * 10) / 10;
                }
              }
            })
            .catch(() => {});

          return true;
        } catch (err) {
          if (seq === readerLoadSeq) {
            reader.error = "EPUB 解析失败：" + err.message;
            reader.loading = false;
          }
          return false;
        } finally {
          if (seq === readerLoadSeq && !keepRestoreLock) {
            isRestoringProgress = false;
          }
        }
      }

      let epubTurning = false;

      function setEpubMountTransform(tx, op, durMs) {
        const el = document.getElementById("epub-viewer-mount");
        if (!el) return;
        el.style.setProperty("--epub-dur", (durMs || 0) + "ms");
        el.style.setProperty("--epub-tx", Math.round(tx || 0) + "px");
        el.style.setProperty("--epub-op", op === undefined ? "1" : String(op));
      }

      async function animateEpubTurn(dir) {
        if (!epubRenditionInstance || epubTurning) return;
        epubTurning = true;
        const isNext = dir === "next";
        const inShift = isNext ? 26 : -26;
        try {
          if (isNext) await epubRenditionInstance.next();
          else await epubRenditionInstance.prev();
          setEpubMountTransform(inShift, 0.42, 0);
          await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)));
          setEpubMountTransform(0, 1, 180);
        } catch (_e) {
          setEpubMountTransform(0, 1, 0);
        } finally {
          setTimeout(() => { epubTurning = false; }, 110);
        }
      }

      function epubPrevPage() {
        animateEpubTurn("prev");
      }

      function epubNextPage() {
        animateEpubTurn("next");
      }

      function onEpubTap(e) {
        if (!reader.open || reader.kind !== "epub" || reader.loading || reader.error || !e) return;
        if (reader.aaOpen) {
          reader.aaOpen = false;
          return;
        }
        const viewW = window.innerWidth || document.documentElement.clientWidth || 1;
        const ratio = e.clientX / viewW;
        if (ratio < 0.3333) {
          if (reader.toolbarVisible) reader.toolbarVisible = false;
          epubPrevPage();
        } else if (ratio > 0.6667) {
          if (reader.toolbarVisible) reader.toolbarVisible = false;
          epubNextPage();
        } else {
          reader.toolbarVisible = !reader.toolbarVisible;
        }
      }

      function goEpubHref(href) {
        if (!href || !epubRenditionInstance) return;
        if (window.innerWidth <= 820) reader.tocOpen = false;
        try { epubRenditionInstance.display(href); } catch (_e) { /* ignore */ }
      }

      function setEpubSpread(mode) {
        prefs.epubSpread = mode === "none" ? "none" : "auto";
        if (epubRenditionInstance && typeof epubRenditionInstance.spread === "function") {
          try { epubRenditionInstance.spread(prefs.epubSpread); } catch (_e) { /* ignore */ }
        }
      }

      function toggleEpubSpread() {
        setEpubSpread(prefs.epubSpread === "auto" ? "none" : "auto");
      }

      /* ------------------------------ PDF 阅读内核 (PDF.js + CMap 中文修复 + 64KB Range 流式) ------------------------------ */
      async function renderPdfPage(pageNum) {
        if (!pdfDocInstance || !reader.open || reader.kind !== "pdf" || reader.pdfNativeMode) return;
        const targetPage = clamp(Math.round(Number(pageNum) || 1), 1, reader.total || 1);
        reader.current = targetPage;
        reader.pdfPageInput = targetPage;
        reader.pdfRendering = true;

        try {
          if (pdfRenderTask && typeof pdfRenderTask.cancel === "function") {
            try { pdfRenderTask.cancel(); } catch (_e) { /* ignore */ }
          }
          const page = await pdfDocInstance.getPage(targetPage);
          const canvas = document.getElementById("pdf-canvas-mount");
          if (!canvas) return;

          const scroller = canvas.parentElement;
          const baseViewport = page.getViewport({ scale: 1 });
          const padX = window.innerWidth <= 820 ? 16 : 32;
          const availW = scroller && scroller.clientWidth > 0
            ? Math.max(240, scroller.clientWidth - padX)
            : Math.max(240, window.innerWidth - padX);
          /* 100% (pdfScale=1.0) 自适应贴合视口可用宽度（桌面大屏舒适上限 1080px），严格保持原始长宽比 */
          const fitWidth = Math.min(availW, 1080);
          const fitScale = baseViewport.width > 0 ? (fitWidth / baseViewport.width) : 1;
          const userZoom = clamp(Number(reader.pdfScale) || 1.0, 0.5, 3.0);
          const actualScale = fitScale * userZoom;

          const dpr = Math.min(window.devicePixelRatio || 1, 2.5);
          const viewport = page.getViewport({ scale: actualScale });
          const cssW = Math.max(1, Math.round(viewport.width));
          const cssH = Math.max(1, Math.round(viewport.height));
          canvas.width = Math.round(cssW * dpr);
          canvas.height = Math.round(cssH * dpr);
          canvas.style.width = cssW + "px";
          canvas.style.height = cssH + "px";
          if (scroller && userZoom <= 1.0) {
            scroller.scrollLeft = 0;
          }

          const ctx = canvas.getContext("2d", { alpha: false });
          ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
          ctx.fillStyle = "#ffffff";
          ctx.fillRect(0, 0, viewport.width, viewport.height);

          const task = page.render({ canvasContext: ctx, viewport: viewport });
          pdfRenderTask = task;
          await task.promise;
          pdfRenderTask = null;

          reader.progress = Math.round((targetPage / Math.max(reader.total, 1)) * 1000) / 10;
          if (!isRestoringProgress) {
            scheduleSave();
          }
        } catch (err) {
          if (!err || err.name !== "RenderingCancelledException") {
            /* 单页渲染异常静默处理 */
          }
        } finally {
          reader.pdfRendering = false;
        }
      }

      async function extractPdfOutline(doc) {
        const list = [];
        try {
          const outline = await doc.getOutline();
          if (!Array.isArray(outline) || outline.length === 0) {
            for (let p = 1; p <= doc.numPages; p++) {
              list.push({ title: "第 " + p + " 页", page: p });
            }
            return list;
          }
          for (let i = 0; i < outline.length; i++) {
            const item = outline[i];
            let pageNum = 1;
            try {
              let dest = item.dest;
              if (typeof dest === "string") {
                dest = await doc.getDestination(dest);
              }
              if (Array.isArray(dest) && dest[0]) {
                const idx = await doc.getPageIndex(dest[0]);
                pageNum = idx + 1;
              }
            } catch (_e) {
              pageNum = i + 1;
            }
            list.push({
              title: (item.title || "").trim() || ("书签 " + (i + 1)),
              page: clamp(pageNum, 1, doc.numPages),
            });
          }
        } catch (_e) {
          for (let p = 1; p <= doc.numPages; p++) {
            list.push({ title: "第 " + p + " 页", page: p });
          }
        }
        return list;
      }

      async function loadPdfBook(initialPage, initialScale, keepRestoreLock) {
        if (!reader.book) return false;
        const seq = ++readerLoadSeq;
        const relKey = reader.book.rel_key;
        reader.loading = true;
        reader.error = "";
        reader.pdfNativeMode = false;
        isRestoringProgress = true;

        await destroyNativeInstances();

        try {
          if (!window.pdfjsLib || typeof window.pdfjsLib.getDocument !== "function") {
            throw new Error("PDF.js 组件尚未就绪");
          }
          /* 显式配置同版本 cMapUrl、cMapPacked、standardFontDataUrl 与 enableXfa，解决中文 PDF 缺字空白 */
          const loadingTask = window.pdfjsLib.getDocument(buildPdfDocumentParams(relKey));
          const doc = await loadingTask.promise;
          if (seq !== readerLoadSeq || !reader.open) {
            await doc.destroy();
            return false;
          }

          pdfDocInstance = markRaw(doc);
          reader.total = doc.numPages || 1;
          reader.pdfScale = clamp(Number(initialScale) || 1.0, 0.5, 3.0);
          const startPage = clamp(Number(initialPage) || 1, 1, reader.total);
          reader.current = startPage;
          reader.pdfPageInput = startPage;
          reader.loading = false;

          await nextTick();
          if (seq !== readerLoadSeq || !reader.open) return false;
          await renderPdfPage(startPage);

          /* 异步提取 PDF 书签目录 */
          extractPdfOutline(doc).then((toc) => {
            if (seq === readerLoadSeq) reader.pdfToc = toc;
          });
          return true;
        } catch (err) {
          if (seq === readerLoadSeq) {
            reader.error = "PDF 流式加载失败：" + err.message;
            reader.loading = false;
          }
          return false;
        } finally {
          if (seq === readerLoadSeq && !keepRestoreLock) {
            isRestoringProgress = false;
          }
        }
      }

      function goPdfPage(pageNum) {
        if (!pdfDocInstance) return;
        if (window.innerWidth <= 820) reader.tocOpen = false;
        renderPdfPage(pageNum);
      }

      function jumpPdfPageInput() {
        goPdfPage(reader.pdfPageInput);
      }

      function pdfPrevPage() {
        if (reader.current > 1) goPdfPage(reader.current - 1);
      }

      function pdfNextPage() {
        if (reader.current < reader.total) goPdfPage(reader.current + 1);
      }

      function adjustPdfZoom(delta) {
        reader.pdfScale = Math.round(clamp(reader.pdfScale + delta, 0.6, 3.0) * 100) / 100;
        renderPdfPage(reader.current);
      }

      function togglePdfInvert() {
        reader.pdfInvertManual = !isPdfNightInverted.value;
      }

      function togglePdfNativeMode() {
        reader.pdfNativeMode = !reader.pdfNativeMode;
        if (!reader.pdfNativeMode && pdfDocInstance) {
          nextTick(() => renderPdfPage(reader.current || 1));
        }
      }

      /* ------------------------------ 打开阅读器与 100% 精准续读时序控制 ------------------------------ */
      async function openBook(book) {
        menuKey.value = null;
        clearTimeout(readerSaveTimer);
        clearTimeout(resumeBannerTimer);

        /* 1. 严格在初始化前读取该书已保存的 locator、progress 与 bookmarks，并立即加锁 isRestoringProgress = true */
        isRestoringProgress = true;
        const savedProgress = Number(book.progress || 0);
        const savedLocator = parseLocator(book.locator);
        const savedBookmarks = Array.isArray(book.bookmarks) ? book.bookmarks.slice() : [];

        await destroyNativeInstances();

        const ext = (book.ext || "").toLowerCase();
        reader.book = book;
        if (ext === "epub") reader.kind = "epub";
        else if (ext === "pdf") reader.kind = "pdf";
        else if (ext === "md") reader.kind = "md";
        else reader.kind = "text";

        reader.title = book.title;
        reader.author = "";
        reader.chapters = [];
        reader.current = 0;
        reader.total = 0;
        reader.chapterTitle = "";
        reader.content = "";
        reader.mdHtml = "";
        reader.mdToc = [];
        reader.activeHeading = "";
        reader.epubToc = [];
        reader.epubCurrentCfi = "";
        reader.epubCurrentHref = "";
        reader.pdfToc = [];
        reader.pdfScale = 1.0;
        reader.pdfPageInput = 1;
        reader.pdfRendering = false;
        reader.pdfNativeMode = false;
        reader.pdfInvertManual = null;
        reader.error = "";
        reader.tocOpen = false;
        reader.drawerTab = "toc";
        reader.bookmarks = savedBookmarks;
        reader.currentScrollRatio = 0;
        reader.currentBlock = -1;
        reader.pageEndBlock = -1;
        reader.page = 0;
        reader.pages = 1;
        reader.aaOpen = false;
        reader.tocQuery = "";
        reader.progress = savedProgress;
        reader.toolbarVisible = true;
        reader.resumeBanner = false;
        reader.chmWeb = false;
        reader.chmInfo = null;
        reader.chmPage = "";
        reader.chmPageTitle = "";
        reader.chmLoading = false;
        reader.chmCanBack = false;
        chmHistoryStack = [];
        reader.open = true;

        await nextTick();

        /* CHM：并行拉取原版网页信息（主页 / 层级目录），按记忆或推荐自动进入原版网页模式 */
        if (ext === "chm") {
          initChmWeb(book, savedLocator);
        }

        /* 后台静默同步本书最新书签列表 */
        apiFetch("/api/bookmarks?path=" + encodeURIComponent(book.rel_key), null, 6000)
          .then((res) => {
            if (res && Array.isArray(res.bookmarks) && reader.book && reader.book.rel_key === book.rel_key) {
              reader.bookmarks = res.bookmarks;
              book.bookmarks = res.bookmarks;
            }
          })
          .catch(() => {});

        let restoredOk = false;
        let hadSavedPosition = false;

        try {
          if (reader.kind === "epub") {
            const targetCfi = savedLocator.cfi || savedLocator.href || "";
            hadSavedPosition = Boolean(targetCfi || savedProgress > 1);
            restoredOk = await loadEpubBook(targetCfi, true);
          } else if (reader.kind === "pdf") {
            const targetPage = Number(savedLocator.page) || 1;
            const rawSavedScale = Number(savedLocator.scale);
            const targetScale = (!rawSavedScale || Math.abs(rawSavedScale - 1.25) < 0.01) ? 1.0 : rawSavedScale;
            hadSavedPosition = Boolean(targetPage > 1 || savedProgress > 1);
            restoredOk = await loadPdfBook(targetPage, targetScale, true);
          } else if (reader.kind === "md") {
            const targetRatio = extractScrollRatio(savedLocator);
            hadSavedPosition = Boolean(targetRatio > 0.01 || savedProgress > 1);
            restoredOk = await loadMarkdown(savedLocator, true);
          } else {
            if (ext === "chm" && savedLocator.chmWeb === true) {
              restoredOk = true;
            } else {
              const targetChapter = Number.isInteger(savedLocator.chapter)
                ? savedLocator.chapter
                : Math.max(0, Number(savedLocator.chapter || 0));
              const targetRatio = extractScrollRatio(savedLocator);
              hadSavedPosition = Boolean(targetChapter > 0 || targetRatio > 0.01 || savedProgress > 1);
              restoredOk = await loadChapter(targetChapter, savedLocator, true);
            }
          }

          if (restoredOk && hadSavedPosition) {
            triggerResumeBanner("已为您自动定位至上次阅读位置");
          }
        } finally {
          /* 释放恢复锁，允许后续翻页与滚动正常记录进度 */
          isRestoringProgress = false;
        }
      }

      /* ------------------------------ CHM 原版网页模式 ------------------------------ */
      function chmKeyToUrl(key, anchor) {
        const info = reader.chmInfo;
        if (!info || !key) return "about:blank";
        const encoded = String(key).split("/").map((seg) => encodeURIComponent(seg)).join("/");
        return info.base + (encoded.startsWith("/") ? "" : "/") + encoded + (anchor || "");
      }

      function chmUrlToKey(pathname) {
        const info = reader.chmInfo;
        if (!info || !pathname || pathname.indexOf(info.base) !== 0) return "";
        const rest = pathname.slice(info.base.length);
        try {
          return rest.split("/").map((seg) => decodeURIComponent(seg)).join("/");
        } catch (_e) {
          return rest;
        }
      }

      function chmChapterOfKey(key) {
        const info = reader.chmInfo;
        if (!info || !key || !info.chapter_map) return -1;
        if (Object.prototype.hasOwnProperty.call(info.chapter_map, key)) return info.chapter_map[key];
        const low = key.toLowerCase();
        for (const k in info.chapter_map) {
          if (k.toLowerCase() === low) return info.chapter_map[k];
        }
        return -1;
      }

      function chmKeyOfChapter(idx) {
        const info = reader.chmInfo;
        if (!info || !info.chapter_map) return "";
        for (const k in info.chapter_map) {
          if (info.chapter_map[k] === idx) return k;
        }
        return "";
      }

      async function initChmWeb(book, savedLocator) {
        reader.chmLoading = true;
        try {
          const info = await apiFetch("/api/chm/info?path=" + encodeURIComponent(book.rel_key), null, 70000);
          if (!reader.open || !reader.book || reader.book.rel_key !== book.rel_key) return;
          if (!info || !info.home) return;
          reader.chmInfo = info;
          const loc = savedLocator || {};
          const wantWeb = loc.chmWeb === true || (info.recommend_web && (!loc.chmPage || loc.chmWeb !== false)) || (loc.chmWeb === undefined && info.recommend_web);
          if (wantWeb) {
            enterChmWeb(loc.chmPage || info.home);
          }
        } catch (_e) {
          /* 网页模式不可用时静默保持纯文本阅读 */
        } finally {
          if (reader.book && reader.book.rel_key === book.rel_key) reader.chmLoading = false;
        }
      }

      async function enterChmWeb(targetKey) {
        if (!reader.chmInfo) return;
        reader.error = "";
        reader.loading = false;
        reader.aaOpen = false;
        reader.toolbarVisible = true;
        reader.chmWeb = true;
        chmHistoryStack = [];
        reader.chmCanBack = false;
        reader.chmPage = targetKey || reader.chmInfo.home;
        await nextTick();
        const frame = chmFrameEl.value;
        if (frame) frame.src = chmKeyToUrl(reader.chmPage);
      }

      async function toggleChmWeb() {
        if (!reader.chmInfo) {
          showToast(reader.chmLoading ? "正在打开原版网页，请稍候…" : "该 CHM 没有可浏览的网页主页", "info");
          return;
        }
        if (reader.chmWeb) {
          /* 退出网页模式：若当前网页对应某个文本章节，则直接定位到该章 */
          const idx = chmChapterOfKey(reader.chmPage);
          reader.chmWeb = false;
          await nextTick();
          if (idx >= 0 && idx !== reader.current) {
            await loadChapter(idx, 0, false);
          }
          scheduleSave();
          return;
        }
        const key = reader.current > 0 ? chmKeyOfChapter(reader.current) : "";
        await enterChmWeb(key || reader.chmInfo.home);
        scheduleSave();
      }

      function chmNavigate(key, anchor) {
        if (!key) return;
        const frame = chmFrameEl.value;
        if (!frame) return;
        if (window.innerWidth <= 820) reader.tocOpen = false;
        const url = chmKeyToUrl(key, anchor);
        try {
          frame.contentWindow.location.assign(url);
        } catch (_e) {
          frame.src = url;
        }
      }

      function chmGoHome() {
        if (reader.chmInfo) chmNavigate(reader.chmInfo.home, "");
      }

      function chmGoBack() {
        if (chmHistoryStack.length < 2) return;
        chmHistoryStack.pop();
        const prev = chmHistoryStack[chmHistoryStack.length - 1];
        chmNavigatingBack = true;
        const frame = chmFrameEl.value;
        if (!frame) return;
        try {
          frame.contentWindow.location.replace(chmKeyToUrl(prev));
        } catch (_e) {
          frame.src = chmKeyToUrl(prev);
        }
      }

      function onChmFrameLoad() {
        const frame = chmFrameEl.value;
        if (!frame || !reader.chmWeb) return;
        let key = "";
        let title = "";
        try {
          const win = frame.contentWindow;
          key = chmUrlToKey(win.location.pathname);
          title = (win.document && win.document.title) || "";
          /* 网页内按 Esc 也能关闭目录 / 返回书架；← 键后退 */
          win.document.addEventListener("keydown", (ev) => {
            if (ev.key === "Escape") {
              if (reader.tocOpen) reader.tocOpen = false;
              else closeReader();
            }
          });
        } catch (_e) { /* ignore */ }
        if (!key) return;
        if (chmNavigatingBack) {
          chmNavigatingBack = false;
        } else if (chmHistoryStack[chmHistoryStack.length - 1] !== key) {
          chmHistoryStack.push(key);
          if (chmHistoryStack.length > 200) chmHistoryStack.shift();
        }
        reader.chmCanBack = chmHistoryStack.length > 1;
        reader.chmPage = key;
        const tocHit = (reader.chmInfo.toc || []).find((t) => t.key === key);
        reader.chmPageTitle = (tocHit && tocHit.title) || title.trim() || key.split("/").pop();
        const idx = chmChapterOfKey(key);
        if (idx >= 0) {
          reader.current = idx;
          if (reader.total) {
            reader.progress = Math.round(clamp(((idx + 1) / reader.total) * 100, 0, 100) * 10) / 10;
          }
        }
        scheduleSave();
      }

      function buildChmWebPayload() {
        if (!reader.chmInfo || !reader.chmPage) return null;
        return {
          rel_key: reader.book.rel_key,
          progress: Math.max(Number(reader.progress) || 0, 1),
          locator: {
            type: "text",
            chapter: reader.current || 0,
            scrollRatio: 0,
            scroll: 0,
            chmWeb: true,
            chmPage: reader.chmPage,
          },
        };
      }

      const filteredChmToc = computed(() => {
        const info = reader.chmInfo;
        if (!info || !Array.isArray(info.toc)) return [];
        const q = (reader.tocQuery || "").trim().toLowerCase();
        if (!q) return info.toc;
        return info.toc.filter((t) => (t.title || "").toLowerCase().includes(q));
      });

      function retryReader() {
        const loc = reader.book ? parseLocator(reader.book.locator) : {};
        if (reader.kind === "epub") loadEpubBook(reader.epubCurrentCfi || loc.cfi || "", false);
        else if (reader.kind === "pdf") loadPdfBook(reader.current || loc.page || 1, reader.pdfScale, false);
        else if (reader.kind === "md") loadMarkdown(reader.pendingScroll, false);
        else loadChapter(reader.pendingChapter, reader.pendingScroll, false);
      }

      async function goChapter(idx) {
        if (idx < 0 || (reader.total && idx >= reader.total)) return;
        if (window.innerWidth <= 820) reader.tocOpen = false;
        await loadChapter(idx, 0, false);
        scheduleSave();
      }

      function prevChapter() {
        if (reader.current > 0) goChapter(reader.current - 1);
      }

      function nextChapter() {
        if (reader.current < reader.total - 1) goChapter(reader.current + 1);
      }

      function scrollToHeading(id) {
        const el = readerScrollEl.value;
        if (!el) return;
        const node = el.querySelector("#" + id);
        if (!node) return;
        if (isPagedDom()) {
          if (window.innerWidth <= 820) reader.tocOpen = false;
          setPage(pageOfNode(node), true);
          afterPageTurn();
          return;
        }
        el.scrollTo({ top: Math.max(0, node.offsetTop - 72), behavior: "smooth" });
        reader.activeHeading = id;
        if (window.innerWidth <= 820) reader.tocOpen = false;
      }

      async function openDrawerTab(tabName) {
        reader.aaOpen = false;
        const targetTab = tabName === "bookmarks" ? "bookmarks" : "toc";
        if (reader.tocOpen && reader.drawerTab === targetTab) {
          reader.tocOpen = false;
          return;
        }
        reader.drawerTab = targetTab;
        reader.tocOpen = true;
        if (targetTab === "toc") {
          await nextTick();
          const active = document.querySelector(".toc-drawer .toc-item.active");
          if (active) active.scrollIntoView({ block: "center" });
        }
      }

      async function toggleToc() {
        await openDrawerTab(reader.drawerTab || "toc");
      }

      function isFullscreenActive() {
        return !!(document.fullscreenElement || document.webkitFullscreenElement);
      }

      function exitFullscreenSafe() {
        if (!isFullscreenActive()) return;
        const exit = document.exitFullscreen || document.webkitExitFullscreen;
        if (exit) {
          try {
            const r = exit.call(document);
            if (r && typeof r.catch === "function") r.catch(() => {});
          } catch (_e) { /* ignore */ }
        }
      }

      function toggleFullscreen() {
        if (isFullscreenActive()) {
          exitFullscreenSafe();
          return;
        }
        const target = document.documentElement;
        const req = target.requestFullscreen || target.webkitRequestFullscreen;
        if (!req) {
          showToast("当前浏览器不支持全屏，可将网页添加到主屏幕获得沉浸体验", "info");
          return;
        }
        try {
          const r = req.call(target);
          if (r && typeof r.catch === "function") r.catch(() => showToast("无法进入全屏", "error"));
        } catch (_e) {
          showToast("无法进入全屏", "error");
        }
      }

      function onFullscreenChange() {
        reader.isFullscreen = isFullscreenActive();
      }

      async function closeReader() {
        clearTimeout(readerSaveTimer);
        clearTimeout(resumeBannerTimer);
        isRestoringProgress = false;
        const payload = buildReaderPayload();
        readerLoadSeq++;
        exitFullscreenSafe();
        await destroyNativeInstances();
        reader.open = false;
        reader.tocOpen = false;
        reader.aaOpen = false;
        reader.resumeBanner = false;
        reader.chmWeb = false;
        reader.chmInfo = null;
        chmHistoryStack = [];
        const closedBookmarks = reader.bookmarks.slice();
        reader.book = null;
        if (payload) {
          /* 立即乐观刷新书架封面底部百分比与 NEW 徽章 */
          patchLocalBook(payload.rel_key, {
            progress: payload.progress,
            status: payload.progress >= 99.5 ? "finished" : "reading",
            locator: payload.locator,
            bookmarks: closedBookmarks,
            updated_at: Date.now() / 1000,
          });
          await saveReaderState(payload);
        }
        /* 若配置了 peer_sync_url，退出阅读器时后台静默触发一次双向同步 */
        if (config.peer_sync_url) {
          postJson("/api/sync/trigger", {}, 15000)
            .then((res) => {
              if (res && res.status === "ok" && res.updated_local > 0) {
                loadLibrary(false, false);
              }
            })
            .catch(() => {});
        }
      }

      /* ------------------------------ 阅读器键盘翻页与移动端三区手势 ------------------------------ */
      function readerPrevAction(preferPageScroll) {
        if (isPagedDom()) { goPage(reader.page - 1); return; }
        if (reader.kind === "epub") {
          epubPrevPage();
        } else if (reader.kind === "pdf") {
          pdfPrevPage();
        } else if (reader.kind === "text") {
          const el = readerScrollEl.value;
          if (preferPageScroll && el && el.scrollTop > 20) {
            el.scrollBy({ top: -Math.max(200, el.clientHeight * 0.85), behavior: "smooth" });
          } else if (reader.current > 0) {
            prevChapter();
          } else if (el) {
            el.scrollBy({ top: -Math.max(200, el.clientHeight * 0.85), behavior: "smooth" });
          }
        } else if (reader.kind === "md") {
          const el = readerScrollEl.value;
          if (el) el.scrollBy({ top: -Math.max(200, el.clientHeight * 0.85), behavior: "smooth" });
        }
      }

      function readerNextAction(preferPageScroll) {
        if (isPagedDom()) { goPage(reader.page + 1); return; }
        if (reader.kind === "epub") {
          epubNextPage();
        } else if (reader.kind === "pdf") {
          pdfNextPage();
        } else if (reader.kind === "text") {
          const el = readerScrollEl.value;
          const atBottom = el ? (el.scrollTop + el.clientHeight >= el.scrollHeight - 24) : true;
          if (preferPageScroll && el && !atBottom) {
            el.scrollBy({ top: Math.max(200, el.clientHeight * 0.85), behavior: "smooth" });
          } else if (reader.current < reader.total - 1) {
            nextChapter();
          } else if (el) {
            el.scrollBy({ top: Math.max(200, el.clientHeight * 0.85), behavior: "smooth" });
          }
        } else if (reader.kind === "md") {
          const el = readerScrollEl.value;
          if (el) el.scrollBy({ top: Math.max(200, el.clientHeight * 0.85), behavior: "smooth" });
        }
      }

      function onReaderStageTap(e) {
        if (!reader.open || !e) return;
        if (Date.now() < suppressTapUntil) return;
        const target = e.target;
        if (target && target.closest && target.closest("button, a, input, textarea, select, label, .chapter-nav, .pdf-bottom-bar, .md-outline, .aa-panel, .toc-drawer")) {
          return;
        }
        const sel = window.getSelection ? window.getSelection() : null;
        if (sel && String(sel.toString()).trim().length > 0) {
          return;
        }
        if (reader.aaOpen) {
          reader.aaOpen = false;
          return;
        }
        if (reader.tocOpen) {
          reader.tocOpen = false;
          return;
        }
        const vw = window.innerWidth || document.documentElement.clientWidth || 1;
        const ratio = e.clientX / vw;
        if (ratio < 0.3333) {
          readerPrevAction(true);
        } else if (ratio > 0.6667) {
          readerNextAction(true);
        } else {
          reader.toolbarVisible = !reader.toolbarVisible;
        }
      }

      /* ------------------------------ 深浅色主题 ------------------------------ */
      function applyDark(val) {
        document.documentElement.classList.toggle("dark", !!val);
        const meta = document.querySelector('meta[name="theme-color"]');
        if (meta) meta.setAttribute("content", val ? "#1c1c1e" : "#f5f5f7");
      }

      function toggleDark() {
        isDark.value = !isDark.value;
      }

      watch(isDark, (val) => {
        applyDark(val);
        try { localStorage.setItem("rbook.dark", val ? "1" : "0"); } catch (_e) { /* ignore */ }
      });

      /* ------------------------------ 进度同步 ------------------------------ */
      async function runSync(peerUrl) {
        syncing.value = true;
        try {
          const payload = peerUrl ? { peer_sync_url: peerUrl } : {};
          const res = await postJson("/api/sync/trigger", payload, 20000);
          if (res.status === "ok") {
            showToast("同步完成：本地更新 " + (res.updated_local || 0) + " 项，对端更新 " + (res.updated_remote || 0) + " 项", "success");
            await loadLibrary(false, false);
          } else if (res.status === "skipped") {
            showToast("请先在设置中填写对端服务器 URL", "info");
          } else {
            showToast(res.message || "同步失败", "error");
          }
        } catch (err) {
          showToast("同步失败：" + err.message, "error");
        } finally {
          syncing.value = false;
        }
      }

      function triggerSync() {
        runSync(null);
      }

      function syncNowFromSettings() {
        runSync(draft.peer_sync_url || null);
      }

      async function exportProgress() {
        try {
          const data = await apiFetch("/api/sync/state", null, 10000);
          const blob = new Blob([JSON.stringify({ state: data.state || {} }, null, 2)], { type: "application/json" });
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
          a.href = url;
          a.download = "rbook-progress-" + stamp + ".json";
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
          showToast("进度与书签已导出", "success");
        } catch (err) {
          showToast("导出失败：" + err.message, "error");
        }
      }

      async function importProgress() {
        let parsed;
        try {
          parsed = JSON.parse(importText.value);
        } catch (_e) {
          showToast("JSON 格式不正确", "error");
          return;
        }
        if (!parsed || typeof parsed !== "object") {
          showToast("JSON 内容为空", "error");
          return;
        }
        try {
          const payload = parsed.state ? parsed : { state: parsed };
          const res = await postJson("/api/sync/merge", payload, 10000);
          showToast("导入完成：更新 " + (res.updated_count || 0) + " 项", "success");
          importText.value = "";
          importOpen.value = false;
          await loadLibrary(false, false);
        } catch (err) {
          showToast("导入失败：" + err.message, "error");
        }
      }

      /* ------------------------------ 设置中心 ------------------------------ */
      function openSettings() {
        draft.media_dirs = config.media_dirs.slice();
        draft.cloud_restricted_mode = config.cloud_restricted_mode;
        draft.peer_sync_url = config.peer_sync_url;
        explorerOpen.value = false;
        importOpen.value = false;
        settingsOpen.value = true;
        sidebarOpen.value = false;
      }

      function closeSettings() {
        settingsOpen.value = false;
      }

      function removeMediaDir(idx) {
        draft.media_dirs.splice(idx, 1);
      }

      function pickDir(path) {
        if (!path) return;
        if (!draft.media_dirs.includes(path)) {
          draft.media_dirs.push(path);
          showToast("已选中：" + path, "success");
        }
      }

      async function loadHostDir(path) {
        if (!path) return;
        explorerLoading.value = true;
        explorer.warning = "";
        try {
          const data = await apiFetch("/api/fs/dirs?path=" + encodeURIComponent(path), null, 8000);
          explorer.current_path = data.current_path || path;
          explorer.parent_path = data.parent_path || null;
          explorer.shortcuts = Array.isArray(data.shortcuts) ? data.shortcuts : [];
          explorer.dirs = Array.isArray(data.dirs) ? data.dirs : [];
          explorer.warning = data.warning || "";
        } catch (err) {
          explorer.dirs = [];
          explorer.warning = "读取宿主机目录失败：" + err.message;
        } finally {
          explorerLoading.value = false;
        }
      }

      function openExplorer() {
        explorerOpen.value = true;
        const start = draft.media_dirs.length ? draft.media_dirs[draft.media_dirs.length - 1] : "/";
        loadHostDir(start);
      }

      const explorerShortcuts = computed(() => {
        const base = ["/", "/srv/oc/gdrive", "/mnt", "/home"];
        (explorer.shortcuts || []).forEach((s) => { if (!base.includes(s)) base.push(s); });
        return base;
      });

      function shortcutLabel(path) {
        if (path === "/") return "/";
        const base = ["/srv/oc/gdrive", "/mnt", "/home"];
        if (base.includes(path)) return path;
        const parts = path.split("/").filter(Boolean);
        return "📦 " + (parts[parts.length - 1] || path);
      }

      const hostCrumbs = computed(() => {
        const cur = explorer.current_path || "/";
        const parts = cur.split("/").filter(Boolean);
        const crumbs = [{ name: "/", path: "/" }];
        let acc = "";
        parts.forEach((p) => {
          acc += "/" + p;
          crumbs.push({ name: p, path: acc });
        });
        return crumbs;
      });

      async function saveSettings() {
        savingConfig.value = true;
        try {
          const res = await postJson("/api/config", {
            media_dirs: draft.media_dirs.slice(),
            cloud_restricted_mode: !!draft.cloud_restricted_mode,
            peer_sync_url: draft.peer_sync_url || "",
          }, 90000);
          applyConfig(res.config || res);
          settingsOpen.value = false;
          await loadLibrary(false, false);
          if (view.value === "folders") await browse("");
          showToast("设置已保存，书库已刷新（共 " + serverCounts.all + " 本）", "success");
        } catch (err) {
          showToast("保存设置失败：" + err.message, "error");
        } finally {
          savingConfig.value = false;
        }
      }

      /* ------------------------------ 全局事件 ------------------------------ */
      function onDocClick() {
        if (menuKey.value) menuKey.value = null;
        if (reader.aaOpen) reader.aaOpen = false;
      }

      function onKeydown(e) {
        const tag = (e.target && e.target.tagName) || "";
        const typing = tag === "INPUT" || tag === "TEXTAREA";
        if (e.key === "Escape") {
          if (menuKey.value) { menuKey.value = null; return; }
          if (reader.aaOpen) { reader.aaOpen = false; return; }
          if (reader.tocOpen) { reader.tocOpen = false; return; }
          if (reader.open) { closeReader(); return; }
          if (settingsOpen.value) { closeSettings(); return; }
          if (sidebarOpen.value) { sidebarOpen.value = false; }
          return;
        }
        if (reader.open && !typing && !reader.chmWeb) {
          if (e.key === "ArrowLeft") {
            e.preventDefault();
            readerPrevAction(false);
            return;
          }
          if (e.key === "ArrowRight") {
            e.preventDefault();
            readerNextAction(false);
            return;
          }
        }
        if (e.key === "/" && !typing && !settingsOpen.value && !reader.open) {
          e.preventDefault();
          const input = document.getElementById("search-input");
          if (input) input.focus();
        }
      }

      function onPageHide() {
        if (!reader.open || isRestoringProgress) return;
        const payload = buildReaderPayload();
        if (payload && navigator.sendBeacon) {
          try {
            navigator.sendBeacon("/api/state", new Blob([JSON.stringify(payload)], { type: "application/json" }));
          } catch (_e) { /* ignore */ }
        }
      }

      let pdfResizeTimer = null;
      function onPdfWindowResize() {
        if (!reader.open || reader.kind !== "pdf" || reader.pdfNativeMode || !pdfDocInstance) return;
        clearTimeout(pdfResizeTimer);
        pdfResizeTimer = setTimeout(() => {
          renderPdfPage(reader.current || 1);
        }, 180);
      }

      onMounted(() => {
        let stored = null;
        try { stored = localStorage.getItem("rbook.dark"); } catch (_e) { stored = null; }
        if (stored === "1" || stored === "0") {
          isDark.value = stored === "1";
        } else {
          isDark.value = !!(window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches);
        }
        applyDark(isDark.value);
        document.addEventListener("click", onDocClick);
        document.addEventListener("keydown", onKeydown);
        document.addEventListener("fullscreenchange", onFullscreenChange);
        document.addEventListener("webkitfullscreenchange", onFullscreenChange);
        window.addEventListener("pagehide", onPageHide);
        window.addEventListener("resize", onPdfWindowResize);
        loadLibrary(false, false);
      });

      onBeforeUnmount(() => {
        document.removeEventListener("click", onDocClick);
        document.removeEventListener("keydown", onKeydown);
        document.removeEventListener("fullscreenchange", onFullscreenChange);
        document.removeEventListener("webkitfullscreenchange", onFullscreenChange);
        window.removeEventListener("pagehide", onPageHide);
        window.removeEventListener("resize", onPdfWindowResize);
        clearTimeout(pdfResizeTimer);
        if (sentinelObserver) {
          sentinelObserver.disconnect();
          sentinelObserver = null;
        }
        clearInterval(scanPollTimer);
        clearTimeout(readerSaveTimer);
        clearTimeout(resumeBannerTimer);
        clearTimeout(searchDebounceTimer);
        destroyNativeInstances();
      });

      return {
        books, collections, config, scanStatus, loading, loadingMore,
        pagination, libraryContentEl, loadMoreSentinelEl,
        view, activeFilter, searchQuery, browseRoot, browseData,
        coverFailed, menuKey, sidebarOpen, isDark, syncing,
        settingsOpen, savingConfig, draft, explorerOpen, explorerLoading, explorer,
        importOpen, importText, toast, libraryNav,
        filterCounts, visibleBooks, visibleDirs, pageTitle, pageSubtitle,
        explorerShortcuts, hostCrumbs,
        coverTheme, shortTitle, sealChar, isNew, progressLabel,
        refreshLibrary, loadNextPage, onLibraryScroll, selectFilter, browse, openFolders, openContainingFolder,
        toggleMenu, setStatus, openBook, toggleDark, triggerSync, syncNowFromSettings,
        exportProgress, importProgress, openSettings, closeSettings, removeMediaDir,
        pickDir, loadHostDir, openExplorer, shortcutLabel, saveSettings,
        readerThemes, readerFonts, readerWidthOptions, prefs, reader, readerScrollEl,
        isPaged,
        onReaderTouchStart, onReaderTouchEnd, onReaderTouchCancel,
        onStageSwipeStart, onStageSwipeMove, onStageSwipeEnd, onEpubTap,
        readerStyle, readerParagraphs, filteredChapters, filteredMdToc,
        filteredEpubToc, filteredPdfToc, tocTotalCount, tocFilteredCount,
        isPdfNightInverted, readerSubtitle,
        sortedBookmarks, currentPositionBookmark, toggleCurrentBookmark, removeBookmark,
        jumpToBookmark, formatBookmarkTime, openDrawerTab,
        adjustFontSize, onReaderScroll, retryReader, goChapter, prevChapter, nextChapter,
        scrollToHeading, toggleToc, toggleFullscreen, closeReader, onReaderStageTap,
        epubPrevPage, epubNextPage, goEpubHref, setEpubSpread, toggleEpubSpread,
        goPdfPage, jumpPdfPageInput, pdfPrevPage, pdfNextPage, adjustPdfZoom,
        togglePdfInvert, togglePdfNativeMode,
        chmFrameEl, toggleChmWeb, chmNavigate, chmGoHome, chmGoBack, onChmFrameLoad, filteredChmToc,
      };
    },
  }).mount("#app");
})();
