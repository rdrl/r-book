# R-Book (Apple Books Library 视觉复刻 + 云地通用阅读器) 全局开发蓝图

## 1. 项目定位与铁律 (AI 开发者必读)
你正在开发一个可同时部署在 Ubuntu 云服务器与个人 Linux 电脑上的前后端一体文档与电子书阅读器 R-Book。用户会通过手机、iPad、Mac 或 Windows 浏览器远程访问本服务。
- **服务监听端口**：`0.0.0.0:8001`
- **【第一铁律：严禁硬编码绝对路径，确保跨账户/跨机器零配置迁移】**：
  1. 在 `main.py` 中，**严禁硬编码 `/home/ubuntu/...` 等绝对路径字符串！** 必须使用 `BASE_DIR = Path(__file__).resolve().parent` 动态定位程序根目录，并由此派生 `DATA_DIR = BASE_DIR / "data"`、`COVERS_DIR = DATA_DIR / "covers"`、`STATIC_DIR = BASE_DIR / "static"`、`SAMPLE_DIR = BASE_DIR / "sample_docs"`。
  2. **启动自愈与防崩溃 (`on_startup` / `lifespan`)**：
     - 启动初始化时必须自动创建所需目录，并主动清理 `DATA_DIR` 下可能残留的 `*.tmp` 临时锁文件。
     - 读取 `data/config.json` 时，检查 `media_dirs` 中的路径：若某个路径包含旧迁移目录（如无法访问的 `/home/ubuntu/...`）或目录不存在，自动将其平滑替换为当前 `str(SAMPLE_DIR)`，防止跨用户迁移到 `/srv/oc/app-rbook` 时因权限不足崩溃。
     - **所有的启动初始化逻辑必须包裹在 `try...except Exception` 中，严禁在启动阶段抛出未捕获异常导致 Uvicorn 进程退出！**
- **核心架构与双模防崩铁律（必须 100% 遵守）**：
  1. **【绝对铁律】目录选择器必须浏览「后端 Linux 宿主机」而非「浏览器客户端设备」**：
     - **前端严禁使用 `<input type="file">`、`webkitdirectory` 或 `window.showDirectoryPicker()` 等浏览器原生本地文件选择器！**（防止用户用 iPad/iPhone/Mac 访问时错误打开客户端本机目录）。
     - **必须采用「后端 API 驱动的远程目录树弹窗」**：前端通过请求后端接口 `GET /api/fs/dirs?path=/xxx` 获取**运行 FastAPI 的那台 Linux 机器**上的真实目录列表（如 `/srv/oc/gdrive`、`/mnt/data`、`/mnt/4t`、`/home`），在网页弹窗内点击展开并勾选目录后，通过 `POST /api/config` 保存。
  2. **严禁阻塞主线程**：所有涉及后端文件系统的操作（os.scandir、os.walk、文件读取）必须在独立后台线程或 `asyncio.to_thread` 线程池中执行，并套上超时保护（timeout=6.0）与 try-except 兜底。
  3. **「云盘受限模式 (`cloud_restricted_mode == true`，默认开启)」下的铁律**：
     - 书架浏览零文件读取：遍历目录与展示书架时，仅读取文件名、相对路径、文件大小与修改时间，严禁打开任何 .epub/.pdf/.txt 文件！
     - 封面被动懒加载：`GET /api/cover` 仅检查本地缓存目录 `data/covers/` 或同级目录下的本地图片；若无缓存封面立即返回 404，由前端渲染 Apple Books 仿真文字封面。仅当用户点开阅读某本 EPUB 时，才在后台提取封面并缓存。
     - 扫描避让：后台扫描目录时若检测到系统存在 `flush_gdrive.sh` 或 `rclone move` 进程，自动每目录微休眠 0.05s 避让云盘 API。
  4. **「本地满血模式 (`cloud_restricted_mode == false`)」下的增强特性**：
     - 当用户在设置中关闭“云盘受限模式”（代表后端使用的是本地物理硬盘）后，后端在扫描完目录树后自动启动低优先级后台线程 `local_cover_warmer`，静默遍历未缓存封面的 `.epub` 提取封面至 `data/covers/<md5>.jpg`。
     - 前端在 `cloud_restricted_mode == false` 时，允许对进入屏幕视野且无封面的 `.pdf` 书籍通过 `PDF.js` 静默渲染第 1 页缩略图，并调用 `POST /api/cover/upload` 缓存为真实封面。
  5. **TXT 编码确定性回退链与按章切片**：识别 .txt 编码时仅读取前 64KB 字节，优先按顺序尝试严格解码：`utf-8-sig` -> `utf-8` -> `gb18030` -> `big5`；均失败时才调用 `chardet`，最后用 `gb18030` (`errors='replace'`) 终极兜底。解析后的章节存入内存 LRU 缓存（上限 5 本），按章节索引分片返回。
  6. **Range 请求精准 seek 切片**：实现 `GET /api/raw` 支持 HTTP 206 Range 时，必须解析 `Range: bytes=start-end`，使用 `f.seek(start)` 定位，并按 `65536` (64KB) 块大小循环 `yield f.read(min(65536, remaining))`，严禁一次性 `f.read()` 整个大文件！
  7. **前端 PDF.js / EPUB.js 防白屏死规**：必须显式配置匹配的 `pdfjsLib.GlobalWorkerOptions.workerSrc` CDN 路径；严禁将 `PDF.js` 或 `EPUB.js` 的实例直接放入 Vue 3 的 `ref()` 或 `reactive()` 中（必须使用 `Vue.markRaw()` 或普通非响应式变量），并在 HTML 中使用独立的挂载容器。
  8. **代码质量铁律**：严禁使用省略号跳过代码！每次修改文件必须输出完整、可直接运行的代码。

## 2. 目录结构规范 (全部基于 BASE_DIR 相对定位)
- **SPEC_AND_PLAN.md** : 本蓝图与进度追踪文件
- **main.py** : FastAPI 后端主程序 (监听 0.0.0.0:8001)
- **umd_parser.py** : 纯 Python 实现的 UMD 二进制电子书高容错解析模块
- **start.sh** : 启动与守护管理脚本 (默认监听 8001 端口)
- **data/**
  - **config.json** : 系统设置（后端已选媒体文件夹列表、云盘受限模式开关、对端同步 URL）
  - **reading_state.json** : 阅读进度、NEW/想读/已读状态原子存储（以相对路径 `rel_key` 为主键）
  - **library_cache.json** : 书架文件索引快照缓存
  - **covers/** : EPUB/PDF 封面缓存目录 (<rel_key_md5>.jpg)
- **static/**
  - **index.html** : 1:1 复刻 macOS Apple Books Library 的响应式骨架与可视化设置中心
  - **css/style.css** : Apple Books 仿真立体书封、左侧书脊折痕、NEW 徽章、四色护眼阅读主题样式
  - **js/app.js** : 资料库过滤、后端宿主机目录树选择器、双模阅读内核调度、双向同步与进度管理逻辑

## 3. 分阶段开发计划 (Phases Checklist)
每次执行时，请只完成第一个未勾选 `- [ ]` 的阶段，完成后务必修改本文件将其更新为 `- [x]`。

- [x] **Phase 1: FastAPI 核心后端 (8001端口)、动态路径自愈、宿主机远程目录树 API 与 UMD/TXT 引擎 (main.py & umd_parser.py)**
  1. 编写 `umd_parser.py`（极度防崩防护）：
     - 校验魔数 `\x89\x9b\x9a\xde`，解析 `0x02` 标题、`0x03` 作者（`utf-16-le`, `errors='replace'`）、`0x83` 章节偏移、`0x84` 正文数据块。
     - 每个数据块的 `zlib.decompress` 单独包裹在 `try...except zlib.error` 中；最外层包裹 `try...except Exception`，遇损坏文件返回友好提示单章，绝不抛 500 错误。
  2. 实现动态路径配置中心与**后端 Linux 宿主机目录树浏览接口** (`main.py`)：
     - 使用 `BASE_DIR = Path(__file__).resolve().parent` 定义所有本地目录。原子读写 JSON 时若遇权限或残留 `.tmp` 异常自动捕获清理。
     - `GET /api/config` 与 `POST /api/config`：读取和保存 `data/config.json`（含 `media_dirs` 数组、`cloud_restricted_mode` 布尔值默认 `true`、`peer_sync_url` 字符串默认 `""`）。首次启动若无有效目录，默认检查 `/srv/oc/gdrive/documents` 是否存在且可读，否则自动回退至 `str(SAMPLE_DIR)`。
     - `GET /api/fs/dirs?path=/`（供前端远程点选后端服务器/Linux主机上的文件夹）：
       - 使用 `os.scandir` 读取**运行当前 FastAPI 后端的 Linux 主机**上指定路径下的所有子文件夹（返回 `current_path`、`parent_path`、`shortcuts` 常用根路径快捷入口 `["/", "/srv/oc/gdrive", "/mnt", "/home", str(BASE_DIR)]`、以及子目录数组 `dirs: [{name, path, has_subdirs}]`）。
       - 自动过滤隐藏文件夹（`.` 开头）及 `/proc`、`/sys`、`/dev`、`/run` 等系统虚拟目录，遇到 `PermissionError` 的目录自动跳过不报错，且绝不读取普通文件。
  3. 实现非阻塞资料库扫描与实时目录漫游接口（支持 `.pdf`, `.epub`, `.md`, `.txt`, `.umd`）：
     - 每本书的唯一标识 `rel_key` 统一为相对于其所属 `media_dir` 根目录的相对路径（如 `人文历史/唐诗三百首.txt`）。
     - `GET /api/library`：立即返回 `data/library_cache.json` 快照并合并 `reading_state.json` 进度及当前 `config`。支持 `?refresh=1` 或超过 600 秒唤醒后台线程非阻塞扫描所有 `media_dirs`；提供 `GET /api/library/status` 返回扫描进度。当 `cloud_restricted_mode == false` 时，扫描结束后自动在后台低优先级线程启动 `local_cover_warmer` 为本地 EPUB 批量提取封面。
     - `GET /api/browse?path=xxx`：使用 `asyncio.to_thread` 实时浏览子目录与文档列表。
     - `POST /api/state`：使用线程锁与原子替换更新文档的 `progress` (0-100)、`status` ('new' | 'reading' | 'want' | 'finished')、`locator` 与 `updated_at`。
  4. 实现**跨端双向进度同步接口**：
     - `GET /api/sync/state`、`POST /api/sync/merge`（按 `rel_key` 比对 `updated_at` 时间戳保留较新项）、`POST /api/sync/trigger`（主动请求 `peer_sync_url` 完成双向合并同步）。
  5. 实现封面接口（`GET /api/cover`、`POST /api/cover/extract` 带 4 秒超时保护、`POST /api/cover/upload`）、确定性编码按章切片接口（`GET /api/read/text?path=xxx&chapter=0`）以及带 `f.seek(start)` 的 64KB 分块 `Range (206)` 流式接口（`GET /api/raw`）。
  6. 启动时若 `SAMPLE_DIR` 为空，自动生成测试子文件夹（`人文历史`、`投资经典`、`硬件手册`）及测试文档（《唐诗三百首.txt》、《背包十年：我的职业是旅行.md》、《中国为什么有前途.txt》、《GA-B75M-S.md》），并用 `./venv/bin/python -m py_compile main.py umd_parser.py` 验证通过。

- [x] **Phase 2: 像素级复刻 Apple Books Library 界面与远程宿主机目录选择器 (static/index.html & static/css/style.css)**
  1. 严格参照 macOS Apple Books 截图视觉风格，砍掉所有 Store 商店项，仅保留纯净的 Library 与实时目录系统：
     - **左侧浅灰磨砂导航栏 (`#f5f5f7` / 深色 `#1c1c1e`，宽 240px)**：
       - 顶部：`Search` 搜索框。
       - **Library 分组**：`All`、`Want to Read`、`Finished`、`Books` (EPUB/UMD/TXT)、`PDFs`、`Markdown`、`Folders` (实时文件夹漫游)。选中项采用圆角胶囊高亮 (`#e4e4e6`)。
       - **My Collections 分组**：自动列出所有一级子文件夹，点击直接进入实时目录视图（配面包屑导航）。
       - **底部控制区**：展示用户头像与名称 `Rric Brook`、深浅色切换按钮、**「⚙️ 设置 (Settings)」按钮**与**「🔄 同步进度」快捷按钮**。
  2. **核心交互：可视化设置中心弹窗 (Settings Modal)**：
     - **模块 A：后端宿主机文件夹可视化选择器（严禁使用 `<input type="file">` 或浏览器原生选择框！）**：
       - 展示当前已绑定的宿主机 `media_dirs` 路径列表（每项右侧带移除按钮）。
       - 点击「+ 从服务器/主机添加媒体文件夹」时，展开网页内置的**「宿主机目录树浏览器 (Host Directory Explorer)」**：
         - 顶部提供快捷跳转标签（`/`、`/srv/oc/gdrive`、`/mnt`、`/home`）与当前宿主机路径面包屑 +「⬆️ 返回上一级」按钮。
         - 中央列表实时渲染 `GET /api/fs/dirs?path=...` 返回的后端 Linux 机器子文件夹列表：点击文件夹名称进入下一层子目录；每个文件夹右侧配有醒目的「✅ 选中此目录」按钮，点击后将该宿主机绝对路径加入已选列表。
         - 底部点击「保存设置并刷新书库」调用 `POST /api/config` 立即生效。
     - **模块 B：「云盘受限模式 (Cloud Restricted Mode)」显式开关**：
       - 提供 Apple 风格拨动开关（Switch）与清晰的状态说明：
         - 开启时：`☁️ 云盘受限保护中（零预读 / 防阻塞 / 4秒超时熔断 / 保护 Google Drive API）`。
         - 关闭时：`⚡ 本地磁盘满血模式（后台自动提取全部 EPUB 封面 / 书架自动渲染 PDF 首页真封面）`。
     - **模块 C：跨设备进度双向同步配置**：
       - 提供对端服务器 URL 输入框（如 `http://100.98.229.9:8001`）与「立即双向同步」按钮，以及「导出/导入进度 JSON」备份功能。
  3. **核心视觉：高兼容性 Apple Books 仿真立式书封 (Book Cover Card)**：
     - 严禁使用复杂的 3D `transform: rotateY()`。采用标准比例 (`aspect-ratio: 1 / 1.42`)、多层 `box-shadow` 立体阴影、以及 `::before` 伪元素配合 `linear-gradient` 在左侧 `12px` 处绘制精装书脊折痕。
     - 无真实封面时，按文件名 Hash 自动渲染 4 套 Apple Books 高定封面：古典黛紫竖排风（复刻《唐诗三百首》，严格使用 `writing-mode: vertical-rl; text-orientation: upright;`）、焦糖琥珀横排风（复刻《背包十年》）、深海藏青现代风（复刻《股市趋势》）、纯净工程白底风（复刻 `GA-B75M-S`）。
     - **封面底部状态栏（1:1 还原截图）**：左下方显示深蓝圆角胶囊 **`NEW`**（`#00376b`）或已读百分比（如 `42%`、`16%`）；右下方显示空心小云朵/本地磁盘图标与 `•••` 菜单（加入 Want to Read、标记 Finished、重置 NEW、打开所在文件夹）。

- [x] **Phase 3: TXT / UMD / Markdown 沉浸式按章阅读器与四大护眼主题 (static/js/app.js & static/index.html)**
  1. 点击书籍平滑展开全屏沉浸式阅读器 (`Apple Books Zen Reader`)，顶部悬浮毛玻璃工具栏（返回书架、目录抽屉开关、书名与当前章节、右侧 `Aa` 排版面板与全屏按钮）。
  2. **Apple Books 级 `Aa` 排版控制面板**：
     - 四种护眼纸张主题：Original 纯白 (`#ffffff`)、Quiet 静谧暗灰 (`#3a3a3c`)、Paper 羊皮纸暖黄 (`#f8f1e3`)、Bold Night 纯黑 (`#000000`)。
     - 支持字体族切换（衬线宋体 / 系统无衬线 / 楷体）、字号缩放 (`15px ~ 30px`)、行高调节 (`1.5 ~ 2.2`)、版心宽度切换。
  3. **TXT & UMD 极速按章阅读器**：
     - 左侧滑出章节目录抽屉（支持搜索章节、高亮当前章），点击章节通过 `/api/read/text?path=xxx&chapter=N` 毫秒级切换，实时计算阅读百分比并更新状态。
  4. **Markdown (MD) 专属阅读器**：
     - 集成 `marked.js` 与代码高亮，定制 Typora / Apple Notes 级精美排版样式，自动提取 `H1~H3` 生成侧边浮动大纲 (TOC)。

- [x] **Phase 4: EPUB 与 PDF 原生流式阅读内核及本地满血真封面渲染 (static/js/app.js & static/index.html)**
  1. **DOM 与 Vue 3 响应式隔离死规**：
     - 阅读器内预留独立原生挂载容器（`<div id="epub-viewer-mount"></div>` 和 `<canvas id="pdf-canvas-mount"></canvas>`），**绝不将 `book`、`rendition`、`pdfDoc` 实例存入 Vue `ref`/`reactive` 中**（必须存放在普通非响应式变量或使用 `Vue.markRaw`），彻底杜绝 Vue 3 Proxy 代理导致白屏！
  2. **EPUB 阅读内核 (`jszip` + `epub.js`)**：
     - 使用 `fetch("/api/raw?path=" + encodeURIComponent(path))` 获取 `ArrayBuffer` 后调用 `ePub(arrayBuffer)` 渲染（同时异步触发 `POST /api/cover/extract` 缓存封面）。
     - 支持宽屏双页并排翻书模式 (Two-page spread) 与单页切换，提取原生 TOC 目录，将四色护眼主题与字号实时注入 `rendition.themes`，基于 `CFI` 精确保存与恢复进度。
  3. **PDF 阅读内核 (`PDF.js` 显式 Worker + 原生流式双保障 + 本地满血封面生成)**：
     - 显式设置同版本 CDN 的 `pdfjsLib.GlobalWorkerOptions.workerSrc`。
     - 通过 `pdfjsLib.getDocument({ url: "/api/raw?path=...", rangeChunkSize: 65536 })` 按块流式加载，支持翻页、页码跳转、缩放控制、一键切换浏览器原生 `<iframe>` 预览兜底、以及暗黑模式下的 **PDF 夜间护眼反相滤镜 (`invert(0.92) hue-rotate(180deg)`)**。
     - **本地满血模式专属特性**：当 `config.cloud_restricted_mode === false` 时，书架界面自动利用轻量队列对未缓存封面的 `.pdf` 文件加载第 1 页渲染至离屏 Canvas（宽 360px），转为 JPEG Blob 上传至 `POST /api/cover/upload?path=xxx`，让本地 PDF 立即拥有真实书籍封面！

- [x] **Phase 5: 键盘快捷键、移动端手势、自动同步与 8001 端口端到端联调测试 (start.sh)**
  1. 完善交互细节：
     - 阅读器内按 `←` / `→` 翻页或切章，按 `Esc` 安全销毁实例返回书架（立即刷新封面底部百分比与 `NEW` 徽章；若配置了 `peer_sync_url`，退出阅读器时后台静默触发一次 `/api/sync/trigger` 自动同步进度），书架按 `/` 聚焦搜索框。
     - 移动端阅读器支持点击左 1/3 上一页、右 1/3 下一页、中间 1/3 呼出工具栏。
  2. 编写并赋予执行权限 `start.sh`（使用 `cd "$(dirname "$0")"` 相对路径定位，支持前台与 `./start.sh --daemon` 后台运行在 `0.0.0.0:8001`）。
  3. 启动服务并对所有静态资源与核心接口（`http://127.0.0.1:8001/api/config`、`/api/fs/dirs`、`/api/library`、`/api/browse`、`/api/sync/state`、`/api/sync/merge`、`/api/read/text`、`/api/raw` 含 `Range` 206 请求）进行全链路测试，确认全部返回 200/206，将 Phase 5 标记为 `[x]`。
- [x] **Phase 6: 2万册大书库毫秒级分页秒开、中文 PDF 缺字修复与精准续读+多书签系统 (main.py & static/index.html & static/js/app.js & static/css/style.css)**
  1. **2 万册大书库服务器内存常驻与分页秒开 (`main.py` & `static/js/app.js`)**：
     - **彻底取消 600 秒自动重扫云盘机制**：`data/library_cache.json` 一旦生成即永久有效，并在后端启动时直接加载常驻于内存变量 `MEMORY_LIBRARY_CACHE` 中。只要用户未显式传入 `?refresh=1`（手动点击刷新书库按钮），每次打开网页绝对不触发 `os.walk` 扫描 Google Drive，直接从服务器内存毫秒级读取！
     - **后端内存过滤与分页 (`GET /api/library`)**：新增分页与过滤参数 `?page=1&page_size=60&category=all&search=&folder=&sort=`。后端直接在内存列表上完成分类过滤（All / Want to Read / Finished / Books / PDFs / Markdown / 一级 Collection）、关键词搜索与排序，每次仅返回当前页的 60 本书籍数据及总数量 `total`、总统计计数 `counts`。
     - **前端滚动懒加载 (Infinite Scroll)**：改造前端书架网格，首次打开页面仅加载第 1 页（60 本），当用户向下滚动到底部（通过 `IntersectionObserver` 或滚动监听）或点击“加载更多”时，自动追加加载下一页 `page + 1`。彻底消灭一次性渲染 2 万个 DOM 节点导致的页面卡死！
  2. **修复中文 PDF 不显示文字/空白问题 (`static/js/app.js`)**：
     - 在调用 `pdfjsLib.getDocument` 时，**必须显式配置与 PDF.js 同版本的 `cMapUrl`、`cMapPacked: true` 以及 `standardFontDataUrl`**（例如指向 `https://cdnjs.cloudflare.com/ajax/libs/pdf.js/<version>/cmaps/` 和 `standard_fonts/`），并开启 `enableXfa: true`，确保未内嵌中文字体的 CID 中文 PDF 电子书 100% 正常渲染文字。
  3. **修复 100% 自动精准续读（拒绝从头翻起）(`static/js/app.js`)**：
     - 严格时序控制：在 `openBook(book)` 时，必须先读取该书已保存的 `locator` 与 `progress`，并在异步加载期间加锁 `isRestoringProgress = true`（防止初始化第 0 章/第 1 页时把已有进度覆盖为 0%！）：
       - **TXT / UMD**：解析 `locator`（支持记录 `{chapter: N, scrollRatio: 0.xx}` 或章节号），直接请求对应章节 `chapter=N`，并在 DOM 渲染后 `nextTick` 自动滚动到 `scrollRatio` 对应位置。
       - **Markdown**：渲染完成后自动根据 `locator` 的 `scrollRatio` 恢复滚动条位置。
       - **PDF**：直接调用 `renderPdfPage(savedPage)` 跳转到上次阅读页码。
       - **EPUB**：调用 `rendition.display(savedCfi)` 精确恢复到上次阅读的那一页。
     - 恢复成功后，在阅读器顶部显示 2.5 秒优雅的浮动胶囊提示：`已为您自动定位至上次阅读位置`。
  4. **新增 Apple Books 级「多书签 (Bookmarks)」系统 (`main.py` & 前端)**：
     - **后端持久化**：在 `data/reading_state.json` 的每本书记录中扩展 `bookmarks` 数组字段（每项结构：`{id, title, label, snippet, locator, progress, created_at}`）。新增或扩展 `POST /api/bookmarks`（添加/删除指定书籍的书签）并在 `GET /api/library` 及单书状态中返回 `bookmarks` 列表（同时天然支持跨设备双向同步）。
     - **阅读器顶部「🔖 书签」按钮**：在阅读器顶部工具栏右侧（`Aa` 按钮旁）增加书签图标按钮。若当前页/当前章节已添加书签则高亮为苹果红（实心），点击可一键添加或移除当前位置书签（自动抓取当前章节标题/页码作为 `label`，抓取当前视口首段前 50 字作为 `snippet`）。
     - **左侧抽屉「目录 | 书签」双标签页**：在阅读器左侧滑出抽屉顶部增加 `[目录 (TOC)]` 与 `[书签 (Bookmarks)]` 切换选项卡。在「书签」页下按时间倒序展示本书所有书签（显示章节/页码徽章、文本摘要、添加时间与删除按钮），点击任意书签立即精准跳转到该书签对应的章节与滚动位置 / 页码 / CFI！
  5. 启动测试服务验证分页接口 `GET /api/library?page=1&page_size=60`、书签增删接口 `POST /api/bookmarks` 均返回 200 OK，并将 Phase 6 标记为 `[x]`。

- [x] **Phase 7: CHM 原版网页模式智能判定、大图文索引页（色库全书等）与占位废词目录修复 (main.py & chm_parser.py & static/js/app.js) (2026-11-09)**
  1. **智能废词目录识别与模式推荐 (`main.py` & `chm_parser.py`)**：
     - **病因**：制作工具（如 Visual CHM）将 9,978 个页面全部硬塞进 `.hhc` 目录树，且 99.96% 标题皆为软件占位废词 `"ggz"`。原规则仅统计目录项数量（`covered < html_count * 0.7`），误判该书“目录覆盖完备”从而返回 `recommend_web = False` 强行进入纯文本阅读模式。
     - **修复**：解析 `.hhc` 时检测原始标题分布。若超 35% 章节共享同一个重复标题，或超 40% 属于占位废词（`ggz`、`new page`、`untitled`、`未命名` 等），自动判定为 `is_garbage_toc = True`，**强制返回 `recommend_web = True`**。
  2. **主页精确定位至分类总索引大厅 (`chm_parser.py`)**：
     - 若默认主题为单张封面/免责引导过渡页（如 `aaa.htm`），且归档中存在包含分类索引的大厅页（如 `bbb.htm`），**自动将真正的图文索引大厅设为主页 `home`**，打开即直接呈现 12 大分类封面总导航；同时在目录最顶部保留 `🏠 封面 / 引导页 (aaa.htm)`。
  3. **语义级占位目录美化（消除 9,974 个 `"ggz"` 乱象）(`chm_parser.py`)**：
     - 识别并动态美化占位标题：`aaa.htm` $\rightarrow$ `🏠 封面 / 引导页`，`bbb.htm` $\rightarrow$ `📑 导航主页 (总索引)`，子目录 `H1/bbb.htm` $\rightarrow$ `📑 H1 分类索引`，`index.htm` $\rightarrow$ `📖 {dir} 目录`，散页 $\rightarrow$ `{dir} - {stem}`，彻底消除抽屉全白或通篇 `"ggz"` 的问题。
  4. **纯文本解析防丢页兜底 (`chm_parser.py`)**：
     - 纯图片/表格构成的索引页（提取纯文本长度为 0）不再被简单丢弃，自动生成图文导航提示块并保留在章节列表中，引导用户一键切换原版网页模式。
  5. **元数据接口极速秒开与防 504 熔断 (`main.py`)**：
     - `/api/chm/info` 避免在非必要时阻塞等待全量扫描 9,978 页文本，元数据接口耗时由 20+ 秒降至 0.54 秒；同时将 `/api/read/text` 的熔断超时由 15 秒放宽至 45 秒，杜绝云盘超大归档冷启动 504 错误。
  6. **前端历史状态自愈与极速入场 (`static/js/app.js`)**：
     - 修复历史自动回退记录锁死 `chmWeb: false` 的漏洞：在 `recommend_web = True` 且未在网页模式浏览具体页面时优先进入原版网页；原版模式下跳过纯文本首屏加载，秒级渲染图片超链接总索引。
