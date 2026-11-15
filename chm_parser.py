import ctypes
import ctypes.util
import html as html_mod
import os
import posixpath
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import unquote_to_bytes

import chardet

# ==============================================================================
# 1. CHM / ITSF 常量与中文编码映射表
# ==============================================================================
ITSF_MAGIC = b"ITSF"
MAX_SINGLE_ENTRY_BYTES = 32 * 1024 * 1024  # 单个内部成员最大保护上限 32MB
MAX_FUSE_STAGE_BYTES = 128 * 1024 * 1024   # 云盘顺序暂存保护上限 128MB

HTML_EXTS = (".htm", ".html", ".xhtml", ".shtml", ".asp", ".jsp")
TEXT_EXTS = (".txt",)
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# 导航/框架噪音文件名（当存在真实正文章节时自动过滤）
NAV_NOISE_STEMS = {
    "index", "default", "contents", "content", "catalog", "catalogue",
    "menu", "toc", "nav", "navigation", "top", "bottom", "left", "right",
    "header", "footer", "head", "foot", "frame", "frameset", "blank",
    "copyright", "readme", "help", "about", "logo", "banner",
}

GB_ALIASES = {
    "gb2312", "gb_2312", "gb_2312-80", "gb2312-80", "chinese",
    "csiso58gb231280", "iso-ir-58", "euc-cn", "euc_cn", "x-euc-cn",
    "cn-gb", "gbk", "cp936", "ms936", "windows-936", "gb18030",
}
BIG5_ALIASES = {
    "big5", "big5-tw", "csbig5", "x-x-big5", "cp950", "ms950",
    "windows-950", "big5-hkscs", "big5_hkscs", "big5hkscs", "hkscs",
}
UTF8_ALIASES = {"utf-8", "utf8", "utf_8", "unicode-1-1-utf-8"}

# 高频简繁小说核心字表（用于无 <meta> 标签时在 GB18030 与 Big5 之间精准判别）
COMMON_CHINESE_CHARS = set(
    "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动"
    "同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自"
    "二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日"
    "那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变"
    "条只没结解问意建月公无系军很情者最立代想已通并提直题章卷回节目录上下页返回作者正"
    "文序言楔子尾声番外笑眼神色声音目光手心中听说走去问答喝叫叹冷微暗忽却便见她它剑江湖"
    "這為們說國時來對著裡過發後會經還進種將見只沒從開點現樣實間關動無長門問頭氣體當應"
    "聲話書電讓給聽覺臉邊師風劍殺內萬與車東馬雲龍鳳頁錄節傳記俠處變雙難戰驚嘆輕語飛落"
)
COMMON_CJK_PUNCT = set("，。！？、；：“”‘’「」『』《》（）【】—…·")

META_CHARSET_RE = re.compile(
    rb"""(?:charset|encoding)\s*=\s*["']?\s*([A-Za-z0-9_\-]+)""",
    re.IGNORECASE,
)

CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第[零一二三四五六七八九十百千万两0-9]+[章节回卷集部篇].*|"
    r"Chapter\s+\d+.*|"
    r"CHAPTER\s+\d+.*|"
    r"[一二三四五六七八九十]+、.{1,40}|"
    r"序[言章幕]?|楔子|引子|尾声|后记|番外.*"
    r")\s*$",
    re.MULTILINE,
)

# 过滤旧版小说网页顶底常见的纯导航链接行
NAV_LINE_RE = re.compile(
    r"^[\s\[\]【】()（）|｜\-—·<>《》/\\]*"
    r"(?:上一[页章篇回节]|下一[页章篇回节]|返回(?:目录|书页|首页|书架)?|回?目录|书签|首页|尾页|末页|"
    r"加入书签|推荐本书|章节错误|点此举报|快捷键.*)"
    r"(?:[\s\[\]【】()（）|｜\-—·<>《》/\\]+"
    r"(?:上一[页章篇回节]|下一[页章篇回节]|返回(?:目录|书页|首页|书架)?|回?目录|书签|首页|尾页|末页|"
    r"加入书签|推荐本书))*[\s\[\]【】()（）|｜\-—·<>《》/\\]*$"
)


# ==============================================================================
# 2. libchm.so.1 原生 ctypes 零拷贝内存读取绑定（保留原始字节路径防中文乱码崩溃）
# ==============================================================================
class _ChmUnitInfo(ctypes.Structure):
    _fields_ = [
        ("start", ctypes.c_ulonglong),
        ("length", ctypes.c_ulonglong),
        ("space", ctypes.c_int),
        ("flags", ctypes.c_int),
        ("path", ctypes.c_char * 513),
    ]


_ENUMERATOR_CB = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.c_void_p,
    ctypes.POINTER(_ChmUnitInfo),
    ctypes.c_void_p,
)

_CHM_ENUMERATE_ALL = 31
_CHM_ENUMERATOR_CONTINUE = 1
_LIBCHM_LOCK_LOADED = False
_LIBCHM: Optional[ctypes.CDLL] = None


def _get_libchm() -> Optional[ctypes.CDLL]:
    global _LIBCHM_LOCK_LOADED, _LIBCHM
    if _LIBCHM_LOCK_LOADED:
        return _LIBCHM
    _LIBCHM_LOCK_LOADED = True
    for candidate in ("libchm.so.1", ctypes.util.find_library("chm") or ""):
        if not candidate:
            continue
        try:
            lib = ctypes.CDLL(candidate)
            lib.chm_open.argtypes = [ctypes.c_char_p]
            lib.chm_open.restype = ctypes.c_void_p

            lib.chm_close.argtypes = [ctypes.c_void_p]
            lib.chm_close.restype = None

            lib.chm_enumerate.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                _ENUMERATOR_CB,
                ctypes.c_void_p,
            ]
            lib.chm_enumerate.restype = ctypes.c_int

            lib.chm_resolve_object.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.POINTER(_ChmUnitInfo),
            ]
            lib.chm_resolve_object.restype = ctypes.c_int

            lib.chm_retrieve_object.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_ChmUnitInfo),
                ctypes.c_void_p,
                ctypes.c_ulonglong,
                ctypes.c_longlong,
            ]
            lib.chm_retrieve_object.restype = ctypes.c_longlong
            _LIBCHM = lib
            return _LIBCHM
        except Exception:
            continue
    return None


# ==============================================================================
# 3. 强鲁棒性中文编码识别与解码器（GB18030 / Big5-HKSCS / UTF-8 / UTF-16 / Meta纠偏）
# ==============================================================================
def _normalize_charset_name(name: Optional[str]) -> Optional[str]:
    if not name:
        return None
    low = name.strip().lower().strip("\"'")
    if low in GB_ALIASES:
        return "gb18030"
    if low in BIG5_ALIASES:
        return "big5hkscs"
    if low in UTF8_ALIASES:
        return "utf-8"
    if low in ("utf-16", "utf16", "unicode"):
        return "utf-16"
    if low in ("utf-16le", "utf-16-le"):
        return "utf-16-le"
    if low in ("utf-16be", "utf-16-be"):
        return "utf-16-be"
    return low


def _extract_html_meta_charset(raw: bytes) -> Optional[str]:
    head = raw[:4096]
    m = META_CHARSET_RE.search(head)
    if not m:
        return None
    try:
        return _normalize_charset_name(m.group(1).decode("ascii", errors="ignore"))
    except Exception:
        return None


def _count_cjk_and_latin_ext(text: str) -> Tuple[int, int]:
    cjk = 0
    latin_ext = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or (0xF900 <= cp <= 0xFAFF):
            cjk += 1
        elif 0x0080 <= cp <= 0x07FF:
            latin_ext += 1
    return cjk, latin_ext


def _score_chinese_text(text: str) -> float:
    if not text:
        return -1e9
    sample = text[:16384]
    score = 0.0
    for ch in sample:
        cp = ord(ch)
        if ch == "\ufffd":
            score -= 18.0
        elif ch in COMMON_CHINESE_CHARS:
            score += 3.5
        elif 0x4E00 <= cp <= 0x9FFF:
            score += 0.6
        elif ch in COMMON_CJK_PUNCT:
            score += 1.5
        elif cp < 0x20 and ch not in ("\r", "\n", "\t"):
            score -= 12.0
        elif 0x0080 <= cp <= 0x009F:
            score -= 8.0
    return score


def decode_chm_bytes(
    raw: bytes,
    hint_enc: Optional[str] = None,
    check_meta: bool = True,
) -> Tuple[str, str]:
    """
    专为中文小说 CHM 设计的确定性 + 启发式混合解码器：
    1. 识别 BOM 与无 BOM UTF-16LE/BE
    2. 严格校验 UTF-8，同时拦截短 GBK 词组（如 "小说"）误撞双字节 UTF-8 拉丁/西里尔区间的假阳性
    3. 自动将 gb2312/gbk/cp936 提升为超集 gb18030，将 big5/cp950 提升为 big5hkscs
    4. 支持单双坏字节容错（旧版盗版手打小说常见截断半个汉字），对比简繁高频字得分择优输出
    """
    if not raw:
        return ("", "utf-8")

    if raw.isascii():
        return (raw.decode("ascii"), "ascii")

    # 1) BOM 精确识别
    if raw.startswith(b"\xef\xbb\xbf"):
        return (raw.decode("utf-8-sig", errors="replace"), "utf-8-sig")
    if raw.startswith(b"\xff\xfe"):
        return (raw.decode("utf-16-le", errors="replace"), "utf-16-le")
    if raw.startswith(b"\xfe\xff"):
        return (raw.decode("utf-16-be", errors="replace"), "utf-16-be")

    # 无 BOM 的 UTF-16 HTML 检测（偶数位或奇数位大量 0x00）
    probe = raw[:256]
    if len(probe) >= 8 and b"\x00" in probe:
        even_zeros = sum(1 for b in probe[0::2] if b == 0)
        odd_zeros = sum(1 for b in probe[1::2] if b == 0)
        half = len(probe) // 2
        if odd_zeros > half * 0.6 and even_zeros < half * 0.1:
            return (raw.decode("utf-16-le", errors="replace"), "utf-16-le")
        if even_zeros > half * 0.6 and odd_zeros < half * 0.1:
            return (raw.decode("utf-16-be", errors="replace"), "utf-16-be")

    meta_enc = _extract_html_meta_charset(raw) if check_meta else None
    norm_hint = _normalize_charset_name(hint_enc)

    # 2) UTF-8 严格校验（排除短 GBK/Big5 字节流落入 U+0080..U+07FF 假阳性）
    try:
        u8_text = raw.decode("utf-8", errors="strict")
        cjk_cnt, latin_ext_cnt = _count_cjk_and_latin_ext(u8_text[:8192])
        if cjk_cnt > 0 or latin_ext_cnt == 0:
            return (u8_text, "utf-8")
        # 若完全没有 CJK 字符却全是双字节拉丁/西里尔扩展字符，对比 GB18030 是否为纯正中文
        try:
            gb_probe = raw.decode("gb18030", errors="strict")
            gb_cjk, _ = _count_cjk_and_latin_ext(gb_probe[:8192])
            if gb_cjk > 0 and _score_chinese_text(gb_probe) > _score_chinese_text(u8_text) + 2.0:
                return (gb_probe, "gb18030")
        except UnicodeDecodeError:
            pass
        return (u8_text, "utf-8")
    except UnicodeDecodeError:
        pass

    # 3) 若 HTML <meta> 明确声明了编码，优先尝试该超集编码
    if meta_enc in ("gb18030", "big5hkscs"):
        try:
            return (raw.decode(meta_enc, errors="strict"), meta_enc)
        except UnicodeDecodeError:
            rep = raw.decode(meta_enc, errors="replace")
            if rep.count("\ufffd") <= max(2, len(rep) // 200):
                return (rep, meta_enc)
    elif meta_enc and meta_enc not in ("utf-8", "ascii"):
        try:
            return (raw.decode(meta_enc, errors="strict"), meta_enc)
        except Exception:
            pass

    # 4) 在 GB18030 与 Big5-HKSCS 之间基于严格性、#SYSTEM LCID 提示与常用汉字频度打分择优
    gb_strict_ok = False
    b5_strict_ok = False
    try:
        gb_text = raw.decode("gb18030", errors="strict")
        gb_strict_ok = True
    except UnicodeDecodeError:
        gb_text = raw.decode("gb18030", errors="replace")

    try:
        b5_text = raw.decode("big5hkscs", errors="strict")
        b5_strict_ok = True
    except UnicodeDecodeError:
        b5_text = raw.decode("big5hkscs", errors="replace")

    gb_score = _score_chinese_text(gb_text) + (6.0 if gb_strict_ok else 0.0)
    b5_score = _score_chinese_text(b5_text) + (6.0 if b5_strict_ok else 0.0)
    if norm_hint == "gb18030":
        gb_score += 5.0
    elif norm_hint == "big5hkscs":
        b5_score += 5.0

    best_cn_text, best_cn_enc, best_cn_score = (
        (b5_text, "big5hkscs", b5_score)
        if b5_score > gb_score + 1.5
        else (gb_text, "gb18030", gb_score)
    )

    if best_cn_score > 3.0:
        return (best_cn_text, best_cn_enc)

    # 5) 非中文或特殊编码（日文 Shift-JIS / 西文 CP1252 / 俄文 CP1251）走 chardet
    try:
        det = chardet.detect(raw[:32768])
        det_enc = _normalize_charset_name(det.get("encoding") if isinstance(det, dict) else None)
        if det_enc:
            return (raw.decode(det_enc, errors="replace"), det_enc)
    except Exception:
        pass

    return (gb_text, "gb18030-replace")


def _decode_internal_path(raw_path: bytes, hint_enc: Optional[str] = None) -> str:
    cleaned = raw_path.replace(b"\\", b"/").strip()
    text, _ = decode_chm_bytes(cleaned, hint_enc=hint_enc, check_meta=False)
    norm = posixpath.normpath("/" + text.lstrip("/"))
    return norm


# ==============================================================================
# 4. CHM 归档统一抽象（主引擎 libchm.so.1 内存直读 + 备选引擎 7z 容错提取）
# ==============================================================================
class _ChmArchiveReader:
    def __init__(self, entries: Dict[str, Any], reader_type: str, handle: Any = None, tmp_dir: Optional[str] = None):
        self.entries = entries  # norm_path (str) -> (unit_info | abs_file_path_bytes)
        self.lower_map: Dict[str, str] = {k.lower(): k for k in entries}
        self.reader_type = reader_type
        self.handle = handle
        self.tmp_dir = tmp_dir
        self.hint_enc: Optional[str] = None

    def redecode_paths_with_hint(self, raw_map: Dict[bytes, Any], hint_enc: Optional[str]) -> None:
        self.hint_enc = hint_enc
        new_entries: Dict[str, Any] = {}
        for raw_p, payload in raw_map.items():
            norm = _decode_internal_path(raw_p, hint_enc=hint_enc)
            new_entries[norm] = payload
        self.entries = new_entries
        self.lower_map = {k.lower(): k for k in new_entries}

    def resolve_key(self, ref_path: str, base_dir: str = "/") -> Optional[str]:
        if not ref_path:
            return None
        clean = ref_path.strip().replace("\\", "/")
        if clean in self.entries:
            return clean
        if clean.lower() in self.lower_map:
            return self.lower_map[clean.lower()]

        # 剥离 URL query 与 HTML 页内锚点（注意保留 CHM 系统内部文件如 /#SYSTEM 的前导 /#）
        if "?" in clean:
            clean = clean.split("?", 1)[0]
        if "#" in clean and not clean.startswith("/#") and not clean.startswith("#"):
            clean = clean.split("#", 1)[0]
        clean = clean.strip()
        if not clean or clean.lower().startswith(("http://", "https://", "mailto:", "javascript:")):
            return None

        candidates: List[str] = []

        def _add_candidate(p: str) -> None:
            if not p:
                return
            if p.startswith("/"):
                norm = posixpath.normpath(p)
            else:
                norm = posixpath.normpath(posixpath.join(base_dir or "/", p))
            if not norm.startswith("/"):
                norm = "/" + norm
            candidates.append(norm)
            # 同时尝试相对于归档根目录
            root_norm = posixpath.normpath("/" + p.lstrip("./"))
            if root_norm not in candidates:
                candidates.append(root_norm)

        _add_candidate(clean)

        # 处理 URL 百分号编码（可能是 UTF-8 或 GB18030 或 Big5 字节序列）
        if "%" in clean:
            try:
                raw_unq = unquote_to_bytes(clean)
                for enc in ("utf-8", self.hint_enc or "gb18030", "gb18030", "big5hkscs"):
                    try:
                        decoded_p = raw_unq.decode(enc, errors="strict")
                        _add_candidate(decoded_p)
                    except Exception:
                        continue
            except Exception:
                pass

        for cand in candidates:
            if cand in self.entries:
                return cand
            low = cand.lower()
            if low in self.lower_map:
                return self.lower_map[low]

        # 最后兜底：若路径层级在制作工具中错位，且唯一同名文件存在，按 basename 匹配
        target_base = posixpath.basename(clean).lower()
        if target_base:
            basename_hits = [k for k in self.entries if posixpath.basename(k).lower() == target_base]
            if len(basename_hits) == 1:
                return basename_hits[0]

        return None

    def read_bytes(self, key: str) -> Optional[bytes]:
        payload = self.entries.get(key)
        if payload is None:
            return None

        if self.reader_type == "libchm":
            lib = _get_libchm()
            if lib is None or not self.handle:
                return None
            start, length, space, flags, raw_path = payload
            if length <= 0:
                return b""
            read_len = min(int(length), MAX_SINGLE_ENTRY_BYTES)
            ui = _ChmUnitInfo(
                start=start,
                length=length,
                space=space,
                flags=flags,
                path=raw_path[:512],
            )
            buf = ctypes.create_string_buffer(read_len)
            got = lib.chm_retrieve_object(
                self.handle,
                ctypes.byref(ui),
                ctypes.cast(buf, ctypes.c_void_p),
                0,
                read_len,
            )
            if got <= 0:
                return b""
            return buf.raw[: int(got)]

        # 7z fallback: payload is bytes path on local tmp_dir
        try:
            st_size = os.path.getsize(payload)
            if st_size <= 0:
                return b""
            with open(payload, "rb") as f:
                return f.read(min(st_size, MAX_SINGLE_ENTRY_BYTES))
        except Exception:
            return None

    def close(self) -> None:
        if self.reader_type == "libchm" and self.handle:
            lib = _get_libchm()
            if lib is not None:
                try:
                    lib.chm_close(self.handle)
                except Exception:
                    pass
            self.handle = None
        if self.tmp_dir and os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
            self.tmp_dir = None


def _is_fuse_or_cloud_path(file_path: str) -> bool:
    real_p = os.path.abspath(file_path)
    if real_p.startswith("/srv/oc/gdrive"):
        return True
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3 and "fuse" in parts[2].lower():
                    mnt = parts[1]
                    if mnt != "/" and (real_p == mnt or real_p.startswith(mnt + "/")):
                        return True
    except Exception:
        pass
    return False


def _open_chm_archive(local_path: str) -> Tuple[Optional[_ChmArchiveReader], Dict[bytes, Any]]:
    # 1) 主引擎：libchm.so.1 纯内存枚举与按需解压
    lib = _get_libchm()
    if lib is not None:
        handle = lib.chm_open(os.fsencode(local_path))
        if handle:
            raw_map: Dict[bytes, Any] = {}

            def _cb(_h: int, ui_ptr: Any, _ctx: int) -> int:
                ui = ui_ptr.contents
                raw_p = bytes(ui.path).split(b"\x00", 1)[0]
                if raw_p and not raw_p.endswith(b"/"):
                    raw_map[raw_p] = (
                        int(ui.start),
                        int(ui.length),
                        int(ui.space),
                        int(ui.flags),
                        raw_p,
                    )
                return _CHM_ENUMERATOR_CONTINUE

            cb_c = _ENUMERATOR_CB(_cb)
            ok = lib.chm_enumerate(handle, _CHM_ENUMERATE_ALL, cb_c, None)
            has_content = any(
                raw_p.lower().endswith(tuple(e.encode() for e in HTML_EXTS + TEXT_EXTS + (".hhc",)))
                for raw_p in raw_map
            )
            if ok and has_content:
                init_entries = {_decode_internal_path(rp): val for rp, val in raw_map.items()}
                return (_ChmArchiveReader(init_entries, "libchm", handle=handle), raw_map)
            lib.chm_close(handle)

    # 2) 备选引擎：7z 解包至 /tmp 临时沙箱（处理魔改 ITSF 头部或损坏索引）
    seven_zip = shutil.which("7z") or shutil.which("7zz")
    if seven_zip:
        tmp_dir = tempfile.mkdtemp(prefix=".rbook_chm7z_")
        try:
            subprocess.run(
                [seven_zip, "x", "-y", f"-o{tmp_dir}", local_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            raw_map_7z: Dict[bytes, Any] = {}
            root_b = os.fsencode(tmp_dir)
            for cur_root, _dirs, files in os.walk(root_b):
                for fn in files:
                    full_b = os.path.join(cur_root, fn)
                    rel_b = b"/" + os.path.relpath(full_b, root_b).replace(b"\\", b"/")
                    raw_map_7z[rel_b] = full_b
            if raw_map_7z:
                init_entries = {_decode_internal_path(rp): val for rp, val in raw_map_7z.items()}
                return (_ChmArchiveReader(init_entries, "7z", tmp_dir=tmp_dir), raw_map_7z)
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return (None, {})


# ==============================================================================
# 5. #SYSTEM 元数据、.hhc 目录树、索引页链接与自然排序解析
# ==============================================================================
def _parse_system_metadata(raw_sys: Optional[bytes]) -> Dict[str, Any]:
    meta: Dict[str, Any] = {
        "hhc_raw": None,
        "default_topic_raw": None,
        "title_raw": None,
        "hint_enc": None,
    }
    if not raw_sys or len(raw_sys) < 4:
        return meta

    pos = 4
    total = len(raw_sys)
    while pos + 4 <= total:
        code, length = struct.unpack_from("<HH", raw_sys, pos)
        pos += 4
        if pos + length > total:
            break
        chunk = raw_sys[pos : pos + length]
        pos += length

        if code == 0:
            meta["hhc_raw"] = chunk.split(b"\x00", 1)[0]
        elif code == 2:
            meta["default_topic_raw"] = chunk.split(b"\x00", 1)[0]
        elif code == 3:
            meta["title_raw"] = chunk.split(b"\x00", 1)[0]
        elif code == 4 and len(chunk) >= 4:
            (lcid,) = struct.unpack_from("<I", chunk, 0)
            lang_id = lcid & 0xFFFF
            if lang_id in (0x0404, 0x0C04, 0x1404):
                meta["hint_enc"] = "big5hkscs"
            elif lang_id in (0x0804, 0x1004):
                meta["hint_enc"] = "gb18030"
    return meta


_OBJECT_BLOCK_RE = re.compile(r"<object\b[^>]*>(.*?)</object>", re.IGNORECASE | re.DOTALL)
_PARAM_TAG_RE = re.compile(r"<param\b([^>]*?)/?>", re.IGNORECASE | re.DOTALL)
_ATTR_KV_RE = re.compile(
    r"""([a-zA-Z_][a-zA-Z0-9_\-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]+))"""
)


def _parse_hhc_toc(hhc_text: str) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    for obj_m in _OBJECT_BLOCK_RE.finditer(hhc_text):
        block = obj_m.group(1)
        name_val = ""
        local_val = ""
        for pm in _PARAM_TAG_RE.finditer(block):
            attrs_raw = pm.group(1)
            attrs: Dict[str, str] = {}
            for am in _ATTR_KV_RE.finditer(attrs_raw):
                k = am.group(1).lower()
                v = am.group(2) if am.group(2) is not None else (
                    am.group(3) if am.group(3) is not None else (am.group(4) or "")
                )
                attrs[k] = html_mod.unescape(v).strip()
            p_name = attrs.get("name", "").lower()
            p_val = attrs.get("value", "")
            if p_name == "name" and p_val:
                name_val = p_val
            elif p_name in ("local", "url") and p_val:
                local_val = p_val
        if local_val:
            items.append((name_val, local_val))
    return items


_ANCHOR_LINK_RE = re.compile(
    r"""<a\b[^>]*?\bhref\s*=\s*(?:"([^"]+)"|'([^']+)'|([^\s"'>]+))[^>]*>(.*?)</a>""",
    re.IGNORECASE | re.DOTALL,
)
_TAG_STRIP_RE = re.compile(r"<[^>]+>")


def _parse_index_html_links(html_text: str) -> List[Tuple[str, str]]:
    links: List[Tuple[str, str]] = []
    for m in _ANCHOR_LINK_RE.finditer(html_text):
        href = (m.group(1) or m.group(2) or m.group(3) or "").strip()
        label_html = m.group(4) or ""
        label = html_mod.unescape(_TAG_STRIP_RE.sub("", label_html))
        label = re.sub(r"\s+", " ", label).strip()
        if not href:
            continue
        links.append((label, href))
    return links


def _natural_sort_key(path_str: str) -> List[Any]:
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", path_str)]


# ==============================================================================
# 6. HTML 清洗与小说正文段落提纯
# ==============================================================================
_REMOVE_BLOCKS_RE = re.compile(
    r"<(script|style|noscript|iframe|object|svg)\b[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_HEAD_BLOCK_RE = re.compile(r"<head\b[^>]*>.*?</head>", re.IGNORECASE | re.DOTALL)
_TITLE_TAG_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H_HEADING_RE = re.compile(r"<h[1-3]\b[^>]*>(.*?)</h[1-3]>", re.IGNORECASE | re.DOTALL)
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLOCK_BREAK_RE = re.compile(
    r"(?:<br\s*/?>|</p>|</div>|</tr>|</li>|</dt>|</dd>|</h[1-6]>|</blockquote>|</pre>|<hr\s*/?>|<p\b[^>]*>|<div\b[^>]*>|<h[1-6]\b[^>]*>|<li\b[^>]*>)",
    re.IGNORECASE,
)
_PRE_BLOCK_RE = re.compile(r"<pre\b[^>]*>.*?</pre>", re.IGNORECASE | re.DOTALL)
_CHM_SENTENCE_END_CHARS = frozenset("。！？!?….”’\"'」』》】>：:;；—~～")


def _unwrap_hard_lines(lines: List[str], min_wrap_len: int = 10) -> List[str]:
    """合并老 CHM 里由固定列宽 <br> 硬断行导致的半句换行，保留完整自然段与列表/标题。"""
    if len(lines) < 2:
        return lines
    merged: List[str] = []
    for line in lines:
        if not merged:
            merged.append(line)
            continue
        prev = merged[-1]
        last_ch = prev[-1]
        is_special = (
            line.startswith(("•", "·", "-", "*", "—", "【", "（", "("))
            or bool(CHAPTER_HEADING_RE.match(line))
            or bool(CHAPTER_HEADING_RE.match(prev))
        )
        if len(prev) >= min_wrap_len and last_ch not in _CHM_SENTENCE_END_CHARS and not is_special:
            joiner = " " if (last_ch.isascii() and last_ch.isalnum() and line[0].isascii() and line[0].isalnum()) else ""
            merged[-1] = prev + joiner + line
        else:
            merged.append(line)
    return merged


def _clean_heading_text(raw_html: str) -> str:
    txt = html_mod.unescape(_TAG_STRIP_RE.sub("", raw_html))
    txt = re.sub(r"[\r\n\t\u3000\xa0]+", " ", txt).strip()
    return txt[:80]


def _extract_html_title_and_body(html_text: str, fallback_title: str) -> Tuple[str, str]:
    page_title = ""
    tm = _TITLE_TAG_RE.search(html_text[:8192])
    if tm:
        page_title = _clean_heading_text(tm.group(1))

    h1_title = ""
    hm = _H_HEADING_RE.search(html_text[:12288])
    if hm:
        h1_title = _clean_heading_text(hm.group(1))

    cleaned = _HTML_COMMENT_RE.sub("", html_text)
    cleaned = _REMOVE_BLOCKS_RE.sub("", cleaned)
    cleaned = _HEAD_BLOCK_RE.sub("", cleaned)
    # 保留 <pre> 代码块内部换行，其余 HTML 源码本身的编辑器软回车按中英文规则折叠（中文间无缝拼接，英文间保留空格）
    cleaned = _PRE_BLOCK_RE.sub(lambda m: m.group(0).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>"), cleaned)
    cleaned = re.sub(r"(?<=[^\x00-\x7f])[ \t]*[\r\n]+[ \t]*(?=[^\x00-\x7f])", "", cleaned)
    cleaned = re.sub(r"[\r\n]+", " ", cleaned)
    cleaned = _BLOCK_BREAK_RE.sub("\n", cleaned)
    cleaned = _TAG_STRIP_RE.sub("", cleaned)
    cleaned = html_mod.unescape(cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")

    lines: List[str] = []
    for raw_line in cleaned.split("\n"):
        line = re.sub(r"[ \t\f\v]+", " ", raw_line).strip(" \t\u3000")
        if not line:
            continue
        if len(line) <= 45 and NAV_LINE_RE.match(line):
            continue
        lines.append(line)

    # 若无 <h1> 且首行/次行本身就是标准章节标题（如 "第一章 xxx"），优先提升为章节标题（避免全书 <title> 同名）
    body_heading = ""
    for probe_line in lines[:2]:
        if len(probe_line) <= 60 and CHAPTER_HEADING_RE.match(probe_line):
            body_heading = probe_line
            break

    chosen_title = h1_title or body_heading or page_title or fallback_title
    # 若正文首行与最终章节标题（或目录传入标题）重复，移除重复首行避免阅读器双重标题
    if lines:
        norm_first = re.sub(r"[\s\u3000·\-—_:：|｜]+", "", lines[0])
        for cand_t in (fallback_title, chosen_title, h1_title, body_heading):
            if not cand_t:
                continue
            norm_t = re.sub(r"[\s\u3000·\-—_:：|｜]+", "", cand_t)
            if norm_first and norm_t and (
                norm_first == norm_t
                or (len(lines[0]) <= 50 and norm_t.endswith(norm_first) and len(norm_first) >= 4)
            ):
                if len(lines) > 1:
                    lines.pop(0)
                break

    lines = _unwrap_hard_lines(lines)
    return (chosen_title or fallback_title, "\n".join(lines).strip())


def _split_single_giant_chapter(title: str, content: str) -> List[Dict[str, Any]]:
    matches = list(CHAPTER_HEADING_RE.finditer(content))
    if len(matches) < 2:
        return [{"index": 0, "title": title, "content": content}]

    chapters: List[Dict[str, Any]] = []
    if matches[0].start() > 60:
        preface = content[: matches[0].start()].strip()
        if preface:
            chapters.append({"index": 0, "title": "导读 / 序言", "content": preface})

    for i, m in enumerate(matches):
        ch_title = m.group(0).strip()[:60]
        start_pos = m.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        body = content[start_pos:end_pos].strip()
        chapters.append(
            {
                "index": len(chapters),
                "title": ch_title or f"第 {len(chapters) + 1} 章",
                "content": body or ch_title,
            }
        )
    return chapters


# ==============================================================================
# 7. 对外核心接口：parse_chm & extract_chm_cover_bytes（零 500 崩溃保证）
# ==============================================================================
def extract_chm_cover_bytes(file_path: str) -> Optional[bytes]:
    """按需尝试从 CHM 内提取封面图片（仅在显式请求封面或本地满血模式时调用）。"""
    p = Path(file_path)
    staged_tmp: Optional[str] = None
    reader: Optional[_ChmArchiveReader] = None
    try:
        if not p.exists() or not p.is_file():
            return None
        with open(p, "rb") as f:
            if f.read(4) != ITSF_MAGIC:
                return None

        target_path = str(p)
        st_size = p.stat().st_size
        if _is_fuse_or_cloud_path(target_path) and 0 < st_size <= MAX_FUSE_STAGE_BYTES:
            fd, staged_tmp = tempfile.mkstemp(prefix=".rbook_chmcov_", suffix=".chm")
            with os.fdopen(fd, "wb") as out_f, open(p, "rb") as in_f:
                shutil.copyfileobj(in_f, out_f, length=262144)
            target_path = staged_tmp

        reader, _ = _open_chm_archive(target_path)
        if reader is None:
            return None

        img_keys = [
            k for k in reader.entries
            if not posixpath.basename(k).startswith(("#", "$"))
            and k.lower().endswith(IMAGE_EXTS)
        ]
        if not img_keys:
            return None

        preferred = [
            k for k in img_keys
            if any(tag in posixpath.basename(k).lower() for tag in ("cover", "folder", "front", "book", "fengmian", "封面"))
        ]
        for cand in (preferred + img_keys)[:6]:
            data = reader.read_bytes(cand)
            if data and len(data) >= 1024:
                return data
    except Exception:
        return None
    finally:
        if reader is not None:
            reader.close()
        if staged_tmp and os.path.exists(staged_tmp):
            try:
                os.unlink(staged_tmp)
            except Exception:
                pass
    return None


def parse_chm(file_path: str) -> Dict[str, Any]:
    """
    解析 Microsoft Compiled HTML Help (.chm) 电子书，返回与 umd_parser.py 完全兼容的结构：
    {
        "title": str,
        "author": str,
        "encoding": str,
        "chapters": [{"index": int, "title": str, "content": str}]
    }
    包含完整的中文编码自适应（GB18030 / Big5-HKSCS / UTF-8 / UTF-16）与零崩溃兜底保护。
    """
    p = Path(file_path)
    default_title = p.stem or "未命名书籍"
    result: Dict[str, Any] = {
        "title": default_title,
        "author": "CHM 藏书",
        "encoding": "gb18030",
        "chapters": [],
    }

    staged_tmp: Optional[str] = None
    reader: Optional[_ChmArchiveReader] = None

    try:
        if not p.exists() or not p.is_file():
            result["chapters"] = [{"index": 0, "title": default_title, "content": "（目标 CHM 文件不存在）"}]
            return result

        st_size = p.stat().st_size
        if st_size < 32:
            result["chapters"] = [{"index": 0, "title": default_title, "content": "（CHM 文件过小或为空文件）"}]
            return result

        with open(p, "rb") as f:
            head_magic = f.read(4)
        if head_magic != ITSF_MAGIC:
            result["chapters"] = [
                {
                    "index": 0,
                    "title": default_title,
                    "content": "（文件头缺少 ITSF 签名，该文件可能并非有效的 CHM 格式或已损坏）",
                }
            ]
            return result

        target_path = str(p)
        if _is_fuse_or_cloud_path(target_path) and st_size <= MAX_FUSE_STAGE_BYTES:
            fd, staged_tmp = tempfile.mkstemp(prefix=".rbook_chm_", suffix=".chm")
            with os.fdopen(fd, "wb") as out_f, open(p, "rb") as in_f:
                shutil.copyfileobj(in_f, out_f, length=262144)
            target_path = staged_tmp

        reader, raw_map = _open_chm_archive(target_path)
        if reader is None or not reader.entries:
            result["chapters"] = [
                {
                    "index": 0,
                    "title": default_title,
                    "content": "（无法解包该 CHM 归档内容，文件索引可能已损坏）",
                }
            ]
            return result

        # 1. 解析 /#SYSTEM 获取语言 LCID 编码提示、书名、.hhc 路径与默认首页
        sys_key = reader.resolve_key("/#SYSTEM")
        sys_meta = _parse_system_metadata(reader.read_bytes(sys_key) if sys_key else None)
        hint_enc: Optional[str] = sys_meta.get("hint_enc")

        if hint_enc and raw_map:
            reader.redecode_paths_with_hint(raw_map, hint_enc)

        detected_encodings: List[str] = []

        if sys_meta.get("title_raw"):
            sys_title, t_enc = decode_chm_bytes(sys_meta["title_raw"], hint_enc=hint_enc, check_meta=False)
            sys_title = sys_title.strip()
            if sys_title and not sys_title.lower().endswith(".chm") and len(sys_title) <= 80:
                result["title"] = sys_title
                detected_encodings.append(t_enc)

        # 2. 提取章节顺序与标题（优先级：.hhc 目录树 -> 默认首页/目录页 <a> 链接 -> 内部文件自然排序）
        ordered_targets: List[Tuple[str, str]] = []  # [(toc_title, resolved_key), ...]
        seen_keys: Set[str] = set()

        def _append_target(label: str, key: Optional[str]) -> None:
            if not key or key in seen_keys:
                return
            low_k = key.lower()
            if not low_k.endswith(HTML_EXTS + TEXT_EXTS):
                return
            if posixpath.basename(key).startswith(("#", "$")):
                return
            seen_keys.add(key)
            ordered_targets.append((label.strip(), key))

        hhc_candidates: List[str] = []
        if sys_meta.get("hhc_raw"):
            hhc_rel, _ = decode_chm_bytes(sys_meta["hhc_raw"], hint_enc=hint_enc, check_meta=False)
            rk = reader.resolve_key(hhc_rel)
            if rk:
                hhc_candidates.append(rk)
        for k in sorted(reader.entries.keys(), key=_natural_sort_key):
            if k.lower().endswith(".hhc") and k not in hhc_candidates:
                hhc_candidates.append(k)

        for hhc_key in hhc_candidates:
            hhc_bytes = reader.read_bytes(hhc_key)
            if not hhc_bytes:
                continue
            hhc_text, hhc_enc = decode_chm_bytes(hhc_bytes, hint_enc=hint_enc, check_meta=True)
            detected_encodings.append(hhc_enc)
            if not hint_enc and hhc_enc in ("gb18030", "big5hkscs", "utf-8"):
                hint_enc = hhc_enc
                reader.hint_enc = hint_enc
            hhc_dir = posixpath.dirname(hhc_key) or "/"
            for name_val, local_val in _parse_hhc_toc(hhc_text):
                rk = reader.resolve_key(local_val, base_dir=hhc_dir)
                _append_target(name_val, rk)
            if len(ordered_targets) >= 2:
                break

        # 若 .hhc 缺失或仅指向单个目录页，解析默认首页 / index.htm 中的章节超链接
        all_content_keys = sorted(
            [
                k for k in reader.entries
                if k.lower().endswith(HTML_EXTS + TEXT_EXTS)
                and not posixpath.basename(k).startswith(("#", "$"))
            ],
            key=_natural_sort_key,
        )

        index_page_used: Optional[str] = None
        if len(ordered_targets) < 2:
            index_candidates: List[str] = []
            if len(ordered_targets) == 1:
                index_candidates.append(ordered_targets[0][1])
            if sys_meta.get("default_topic_raw"):
                dt_rel, _ = decode_chm_bytes(sys_meta["default_topic_raw"], hint_enc=hint_enc, check_meta=False)
                dt_key = reader.resolve_key(dt_rel)
                if dt_key and dt_key not in index_candidates:
                    index_candidates.append(dt_key)
            for k in all_content_keys:
                stem_low = Path(posixpath.basename(k)).stem.lower()
                if stem_low in ("index", "default", "contents", "catalog", "menu", "toc") and k not in index_candidates:
                    index_candidates.append(k)

            for idx_key in index_candidates[:4]:
                idx_bytes = reader.read_bytes(idx_key)
                if not idx_bytes:
                    continue
                idx_html, idx_enc = decode_chm_bytes(idx_bytes, hint_enc=hint_enc, check_meta=True)
                idx_dir = posixpath.dirname(idx_key) or "/"
                parsed_links = _parse_index_html_links(idx_html)
                link_targets: List[Tuple[str, str]] = []
                link_seen: Set[str] = set()
                for lbl, href in parsed_links:
                    rk = reader.resolve_key(href, base_dir=idx_dir)
                    if rk and rk != idx_key and rk not in link_seen and rk.lower().endswith(HTML_EXTS + TEXT_EXTS):
                        link_seen.add(rk)
                        link_targets.append((lbl, rk))
                if len(link_targets) >= 2:
                    detected_encodings.append(idx_enc)
                    ordered_targets = link_targets
                    seen_keys = set(link_seen)
                    index_page_used = idx_key
                    break

        # 若仍不足或存在大量未收录的独立正文章节（无目录野生小说 CHM），用自然排序补齐
        if len(ordered_targets) < 2 and len(all_content_keys) >= 2:
            non_noise_keys = [
                k for k in all_content_keys
                if Path(posixpath.basename(k)).stem.lower() not in NAV_NOISE_STEMS
                and k != index_page_used
            ]
            chosen_pool = non_noise_keys if non_noise_keys else all_content_keys
            ordered_targets = []
            seen_keys = set()
            for k in chosen_pool:
                _append_target("", k)
        elif not ordered_targets and all_content_keys:
            for k in all_content_keys:
                _append_target("", k)

        # 3. 逐章解码与 HTML 正文提纯
        chapters: List[Dict[str, Any]] = []
        for raw_toc_title, key in ordered_targets:
            raw_data = reader.read_bytes(key)
            if not raw_data:
                continue
            text_decoded, ch_enc = decode_chm_bytes(
                raw_data,
                hint_enc=hint_enc,
                check_meta=key.lower().endswith(HTML_EXTS),
            )
            detected_encodings.append(ch_enc)
            if not hint_enc and ch_enc in ("gb18030", "big5hkscs", "utf-8"):
                hint_enc = ch_enc

            file_stem = Path(posixpath.basename(key)).stem
            fallback_ch_title = raw_toc_title or file_stem or f"第 {len(chapters) + 1} 章"

            if key.lower().endswith(TEXT_EXTS):
                body = text_decoded.replace("\r\n", "\n").replace("\r", "\n").strip()
                ch_title = raw_toc_title or file_stem
            else:
                extracted_title, body = _extract_html_title_and_body(text_decoded, fallback_ch_title)
                ch_title = raw_toc_title or extracted_title

            if not body:
                # 若正文无纯文本段落但包含图片或超链接（典型的 CHM 图文导航大厅/索引页）
                links = _parse_index_html_links(text_decoded)
                has_imgs = bool(re.search(r"<img\b", text_decoded, re.IGNORECASE))
                if links or has_imgs:
                    body = f"【本页为图片超链接导航索引页（含 {len(links)} 个超链接与多张封面图片）。建议点击顶部「🌐 原版网页」按钮以完整图文排版浏览。】"
                    if not ch_title or ch_title.lower() in ("ggz", "new page", "new page 1"):
                        ch_title = "目录导航索引"
                else:
                    continue
            # 过滤纯噪音极短空壳页（如仅有几个字版权水印的附加页，前提是全书有多章）
            if len(ordered_targets) > 3 and len(body) < 12 and Path(file_stem).stem.lower() in NAV_NOISE_STEMS:
                continue

            # 智能修正 Visual CHM 等工具产生的千篇一律 ggz / New Page 1 噪音标题
            if ch_title.lower() in ("ggz", "new page", "new page 1"):
                p_base = posixpath.basename(key).lower()
                d_name = posixpath.basename(posixpath.dirname(key))
                if p_base in ("aaa.htm", "aaa.html"):
                    ch_title = "封面 / 引导页"
                elif p_base in ("bbb.htm", "bbb.html"):
                    ch_title = f"{d_name} 分类索引" if d_name else "总目录导航索引"
                elif p_base in ("index.htm", "index.html") and d_name:
                    ch_title = f"{d_name} 目录"

            chapters.append(
                {
                    "index": len(chapters),
                    "title": (ch_title or f"第 {len(chapters) + 1} 章")[:80],
                    "content": body,
                    "src": key,  # CHM 内部源页面路径（供「原版网页模式」与纯文本章节互相定位）
                }
            )

        # 4. 单文件巨型章节自动切章（部分 CHM 将整部小说压入单个大 HTML 文件）
        if len(chapters) == 1 and len(chapters[0]["content"]) > 8000:
            chapters = _split_single_giant_chapter(chapters[0]["title"], chapters[0]["content"])

        if detected_encodings:
            non_ascii = [e for e in detected_encodings if e != "ascii"]
            chosen_enc = (non_ascii or detected_encodings)[0]
            result["encoding"] = chosen_enc

        if not chapters:
            result["chapters"] = [
                {
                    "index": 0,
                    "title": result["title"],
                    "content": "（该 CHM 文件内未检测到可阅读的文本章节）",
                }
            ]
        else:
            result["chapters"] = chapters

    except Exception as exc:
        result["chapters"] = [
            {
                "index": 0,
                "title": default_title,
                "content": f"（CHM 解析过程中遇到异常，已启用安全降级保护：{exc}）",
            }
        ]
    finally:
        if reader is not None:
            reader.close()
        if staged_tmp and os.path.exists(staged_tmp):
            try:
                os.unlink(staged_tmp)
            except Exception:
                pass

    return result


# ==============================================================================
# 8. 「原版网页模式」：按需读取 CHM 内部成员（HTML / 图片 / CSS / JS），
#    让带图片导航主页的 CHM 能在 iframe 中按原始网页结构浏览
# ==============================================================================
import mimetypes
import threading
import time
from collections import OrderedDict

CHM_SESSION_MAX = 3            # 同时保持打开的 CHM 归档数量上限
CHM_SESSION_IDLE_SEC = 15 * 60  # 空闲超过 15 分钟自动关闭并清理暂存文件

_TRANSCODE_EXTS = (".css", ".js", ".txt", ".hhc", ".hhk")


class _ChmSession:
    def __init__(self, file_path: str, mtime: float):
        self.file_path = file_path
        self.mtime = mtime
        self.reader: Optional[_ChmArchiveReader] = None
        self.staged_tmp: Optional[str] = None
        self.hint_enc: Optional[str] = None
        self.sys_meta: Dict[str, Any] = {}
        self.lock = threading.RLock()  # libchm 句柄非线程安全，串行访问
        self.last_used = time.time()
        self.info: Optional[Dict[str, Any]] = None

    def open(self) -> bool:
        p = Path(self.file_path)
        with open(p, "rb") as f:
            if f.read(4) != ITSF_MAGIC:
                return False
        target_path = str(p)
        st_size = p.stat().st_size
        if _is_fuse_or_cloud_path(target_path) and 0 < st_size <= MAX_FUSE_STAGE_BYTES:
            fd, self.staged_tmp = tempfile.mkstemp(prefix=".rbook_chmweb_", suffix=".chm")
            with os.fdopen(fd, "wb") as out_f, open(p, "rb") as in_f:
                shutil.copyfileobj(in_f, out_f, length=262144)
            target_path = self.staged_tmp
        reader, raw_map = _open_chm_archive(target_path)
        if reader is None or not reader.entries:
            self.close()
            return False
        self.reader = reader
        sys_key = reader.resolve_key("/#SYSTEM")
        self.sys_meta = _parse_system_metadata(reader.read_bytes(sys_key) if sys_key else None)
        self.hint_enc = self.sys_meta.get("hint_enc")
        if self.hint_enc and raw_map:
            reader.redecode_paths_with_hint(raw_map, self.hint_enc)
        return True

    def close(self) -> None:
        with self.lock:
            if self.reader is not None:
                try:
                    self.reader.close()
                except Exception:
                    pass
                self.reader = None
            if self.staged_tmp and os.path.exists(self.staged_tmp):
                try:
                    os.unlink(self.staged_tmp)
                except Exception:
                    pass
            self.staged_tmp = None


_CHM_SESSIONS: "OrderedDict[str, _ChmSession]" = OrderedDict()
_CHM_SESSIONS_LOCK = threading.Lock()


def _evict_idle_sessions_locked() -> None:
    now = time.time()
    for k in list(_CHM_SESSIONS.keys()):
        s = _CHM_SESSIONS[k]
        if now - s.last_used > CHM_SESSION_IDLE_SEC:
            _CHM_SESSIONS.pop(k, None)
            s.close()
    while len(_CHM_SESSIONS) > CHM_SESSION_MAX:
        _k, s = _CHM_SESSIONS.popitem(last=False)
        s.close()


def _get_chm_session(file_path: str) -> Optional[_ChmSession]:
    abs_p = os.path.abspath(file_path)
    try:
        mtime = os.stat(abs_p).st_mtime
    except Exception:
        return None
    with _CHM_SESSIONS_LOCK:
        _evict_idle_sessions_locked()
        sess = _CHM_SESSIONS.get(abs_p)
        if sess is not None and sess.mtime == mtime and sess.reader is not None:
            _CHM_SESSIONS.move_to_end(abs_p)
            sess.last_used = time.time()
            return sess
        if sess is not None:
            _CHM_SESSIONS.pop(abs_p, None)
            sess.close()
        sess = _ChmSession(abs_p, mtime)
        _CHM_SESSIONS[abs_p] = sess
    # 打开（可能需要从云盘暂存）放在全局锁外，避免阻塞其它书籍
    with sess.lock:
        if sess.reader is None:
            try:
                ok = sess.open()
            except Exception:
                ok = False
            if not ok:
                with _CHM_SESSIONS_LOCK:
                    if _CHM_SESSIONS.get(abs_p) is sess:
                        _CHM_SESSIONS.pop(abs_p, None)
                sess.close()
                return None
    with _CHM_SESSIONS_LOCK:
        _evict_idle_sessions_locked()
    return sess


_HHC_TOKEN_RE = re.compile(r"<(/?)ul\b[^>]*>|<object\b[^>]*>(.*?)</object>", re.IGNORECASE | re.DOTALL)


def _parse_hhc_tree(hhc_text: str) -> List[Dict[str, Any]]:
    """解析 .hhc 为带层级的扁平列表 [{title, local, level}]，保留无链接的分组节点。"""
    items: List[Dict[str, Any]] = []
    depth = 0
    for m in _HHC_TOKEN_RE.finditer(hhc_text):
        if m.group(2) is None:
            if m.group(1):
                depth = max(0, depth - 1)
            else:
                depth += 1
            continue
        block = m.group(2)
        name_val = ""
        local_val = ""
        is_sitemap_props = False
        for pm in _PARAM_TAG_RE.finditer(block):
            attrs: Dict[str, str] = {}
            for am in _ATTR_KV_RE.finditer(pm.group(1)):
                k = am.group(1).lower()
                v = am.group(2) if am.group(2) is not None else (
                    am.group(3) if am.group(3) is not None else (am.group(4) or "")
                )
                attrs[k] = html_mod.unescape(v).strip()
            p_name = attrs.get("name", "").lower()
            p_val = attrs.get("value", "")
            if p_name == "name" and p_val and not name_val:
                name_val = p_val
            elif p_name in ("local", "url") and p_val and not local_val:
                local_val = p_val
            elif p_name in ("window styles", "imagetype", "font"):
                is_sitemap_props = True
        if not name_val and not local_val:
            continue
        if is_sitemap_props and not local_val and depth == 0:
            continue
        items.append({"title": name_val, "local": local_val, "level": max(1, depth)})
    return items


def chm_web_info(file_path: str) -> Optional[Dict[str, Any]]:
    """返回原版网页模式所需的元数据：首页、层级目录、页面统计。"""
    sess = _get_chm_session(file_path)
    if sess is None:
        return None
    with sess.lock:
        if sess.info is not None:
            return sess.info
        reader = sess.reader
        if reader is None:
            return None
        hint_enc = sess.hint_enc
        meta = sess.sys_meta

        content_keys = [
            k for k in reader.entries
            if not posixpath.basename(k).startswith(("#", "$"))
        ]
        html_keys = [k for k in content_keys if k.lower().endswith(HTML_EXTS + TEXT_EXTS)]
        image_count = sum(1 for k in content_keys if k.lower().endswith(IMAGE_EXTS))

        # 1. 首页：#SYSTEM 默认主题 -> index/default 等常见首页 -> 首个 HTML
        home: Optional[str] = None
        if meta.get("default_topic_raw"):
            dt_rel, _ = decode_chm_bytes(meta["default_topic_raw"], hint_enc=hint_enc, check_meta=False)
            home = reader.resolve_key(dt_rel.strip())
        if not home:
            for stem in ("index", "default", "main", "home", "cover", "contents", "toc", "bbb"):
                for ext in (".htm", ".html"):
                    k = reader.resolve_key("/" + stem + ext)
                    if k:
                        home = k
                        break
                if home:
                    break
        if not home and html_keys:
            home = sorted(html_keys, key=_natural_sort_key)[0]

        # 智能主页优化：若默认主题为过渡页/免责引导页（如 aaa.htm），且存在分类总索引页（如 bbb.htm），
        # 优先将真正的图文索引大厅设为主页
        if home and posixpath.basename(home).lower() in ("aaa.htm", "aaa.html"):
            bbb_key = reader.resolve_key("/bbb.htm") or reader.resolve_key("/bbb.html")
            if bbb_key:
                home = bbb_key

        def _beautify_toc_title(raw_title: str, r_key: Optional[str]) -> str:
            t = (raw_title or "").strip()
            low_t = t.lower()
            if not r_key:
                return t or "（无标题）"
            p_base = posixpath.basename(r_key).lower()
            p_stem = posixpath.splitext(p_base)[0]
            d_name = posixpath.basename(posixpath.dirname(r_key))
            if not t or low_t in ("ggz", "new page", "new page 1", "untitled", "未命名", "无标题", "doc", "document"):
                if p_base in ("aaa.htm", "aaa.html"):
                    return "🏠 封面 / 引导页"
                if p_base in ("bbb.htm", "bbb.html"):
                    return f"📑 {d_name} 分类索引" if d_name else "📑 导航主页 (总索引)"
                if p_base in ("index.htm", "index.html", "default.htm", "default.html") and d_name:
                    return f"📖 {d_name} 目录"
                if d_name:
                    return f"{d_name} - {p_stem}"
                return p_stem or t or "（无标题）"
            return t

        # 2. 层级目录（.hhc）
        toc: List[Dict[str, Any]] = []
        raw_titles_list: List[str] = []
        is_garbage_toc = False
        hhc_key: Optional[str] = None
        if meta.get("hhc_raw"):
            hhc_rel, _ = decode_chm_bytes(meta["hhc_raw"], hint_enc=hint_enc, check_meta=False)
            hhc_key = reader.resolve_key(hhc_rel.strip())
        if not hhc_key:
            hhc_list = sorted([k for k in content_keys if k.lower().endswith(".hhc")], key=_natural_sort_key)
            hhc_key = hhc_list[0] if hhc_list else None
        if hhc_key:
            raw = reader.read_bytes(hhc_key)
            if raw:
                hhc_text, _enc = decode_chm_bytes(raw, hint_enc=hint_enc, check_meta=True)
                hhc_dir = posixpath.dirname(hhc_key) or "/"
                for it in _parse_hhc_tree(hhc_text):
                    key = reader.resolve_key(it["local"], base_dir=hhc_dir) if it["local"] else None
                    if key and it["title"]:
                        raw_titles_list.append(it["title"].strip())
                    # 保留页内锚点
                    anchor = ""
                    if key and "#" in it["local"] and not it["local"].startswith("#"):
                        anchor = "#" + it["local"].split("#", 1)[1]
                    title = _beautify_toc_title(it["title"], key)
                    toc.append({
                        "title": title[:120],
                        "key": key,
                        "anchor": anchor,
                        "level": min(it["level"], 6),
                    })

        if len(raw_titles_list) >= 4:
            from collections import Counter
            c = Counter(raw_titles_list)
            most_common_cnt = c.most_common(1)[0][1]
            if (most_common_cnt / len(raw_titles_list)) > 0.35:
                is_garbage_toc = True
            else:
                placeholder_stems = {"ggz", "new page", "new page 1", "untitled", "未命名", "无标题", "doc", "document", "default", "index"}
                ph_count = sum(cnt for t, cnt in c.items() if t.lower() in placeholder_stems)
                if (ph_count / len(raw_titles_list)) > 0.4:
                    is_garbage_toc = True

        if home and not any(t.get("key") == home for t in toc):
            toc.insert(0, {"title": "🏠 导航主页", "key": home, "anchor": "", "level": 1})

        toc_keys = {t["key"] for t in toc if t.get("key")}
        sess.info = {
            "home": home,
            "toc": toc,
            "html_count": len(html_keys),
            "image_count": image_count,
            "toc_count": len(toc_keys),
            "is_garbage_toc": is_garbage_toc,
            "hint_enc": hint_enc,
        }
        return sess.info


_META_CHARSET_TAG_RE = re.compile(
    r"<meta\b[^>]*?(?:charset\s*=|http-equiv\s*=\s*[\"']?content-type)[^>]*>",
    re.IGNORECASE,
)
_HEAD_OPEN_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HTML_OPEN_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_MSITS_RE = re.compile(r"(?:mk:@MSITStore:|ms-its:|its:)[^\"'>\s]*?::", re.IGNORECASE)
_ROOT_ABS_ATTR_RE = re.compile(r"""(\b(?:href|src|background|action)\s*=\s*["']?)/(?!/)""", re.IGNORECASE)
_TARGET_TOP_RE = re.compile(r"""(\btarget\s*=\s*["']?)_(?:top|blank)\b""", re.IGNORECASE)


def _rewrite_chm_html(text: str, base_prefix: str) -> str:
    """将 CHM 页面改写为可在浏览器 iframe 中直接浏览的 UTF-8 网页。"""
    out = _META_CHARSET_TAG_RE.sub("", text)
    # 归档根路径（/xxx.htm）与 ms-its:xxx.chm::/xxx.htm 外链 -> /api/chm/view/<token>/xxx.htm
    out = _ROOT_ABS_ATTR_RE.sub(lambda m: m.group(1) + base_prefix + "/", out)
    out = _MSITS_RE.sub(base_prefix, out)
    # 防止 _top / _blank 跳出阅读器：改为在 R-Book 承载 iframe（name=rbook-chm）内打开
    out = _TARGET_TOP_RE.sub(lambda m: m.group(1) + "rbook-chm", out)
    inject = (
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<style>'
        'html,body{max-width:100%!important;overflow-x:hidden!important;box-sizing:border-box!important;word-wrap:break-word!important;overflow-wrap:anywhere!important;}'
        'body{padding:10px 12px!important;margin:0 auto!important;}'
        'table,tbody,thead,tr,td,th,div,p,blockquote{max-width:100%!important;box-sizing:border-box!important;overflow-wrap:anywhere!important;}'
        'table[width],td[width],th[width],div[width]{width:auto!important;min-width:0!important;}'
        'td[nowrap],th[nowrap]{white-space:normal!important;}'
        'img{max-width:100%!important;height:auto!important;}'
        'pre,code{max-width:100%!important;white-space:pre-wrap!important;word-break:break-word!important;}'
        '</style>'
    )
    hm = _HEAD_OPEN_RE.search(out)
    if hm:
        return out[: hm.end()] + inject + out[hm.end():]
    hm = _HTML_OPEN_RE.search(out)
    if hm:
        return out[: hm.end()] + "<head>" + inject + "</head>" + out[hm.end():]
    return inject + out


def chm_read_member(file_path: str, inner_path: str, base_prefix: str) -> Optional[Tuple[bytes, str]]:
    """读取 CHM 内部成员，返回 (body_bytes, content_type)；HTML/CSS/JS/TXT 统一转码为 UTF-8。"""
    sess = _get_chm_session(file_path)
    if sess is None:
        return None
    with sess.lock:
        reader = sess.reader
        if reader is None:
            return None
        key = reader.resolve_key(inner_path or "/")
        if not key or posixpath.basename(key).startswith(("#", "$")):
            return None
        data = reader.read_bytes(key)
        hint_enc = sess.hint_enc
    if data is None:
        return None
    low = key.lower()
    if low.endswith(HTML_EXTS):
        text, _enc = decode_chm_bytes(data, hint_enc=hint_enc, check_meta=True)
        return (_rewrite_chm_html(text, base_prefix).encode("utf-8"), "text/html; charset=utf-8")
    if low.endswith(_TRANSCODE_EXTS):
        text, _enc = decode_chm_bytes(data, hint_enc=hint_enc, check_meta=False)
        if low.endswith(".css"):
            text = re.sub(r"@charset\s+[\"'][^\"']*[\"']\s*;?", "", text, flags=re.IGNORECASE)
            ctype = "text/css; charset=utf-8"
        elif low.endswith(".js"):
            ctype = "application/javascript; charset=utf-8"
        else:
            ctype = "text/plain; charset=utf-8"
        return (text.encode("utf-8"), ctype)
    guessed, _ = mimetypes.guess_type(posixpath.basename(key))
    return (data, guessed or "application/octet-stream")
