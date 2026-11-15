import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple
from urllib.parse import quote, unquote

import chardet
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from chm_parser import chm_read_member, chm_web_info, extract_chm_cover_bytes, parse_chm
from umd_parser import parse_umd

# ==============================================================================
# 1. 动态路径定义（严禁硬编码绝对路径，全部基于 BASE_DIR 派生）
# ==============================================================================
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
COVERS_DIR = DATA_DIR / "covers"
STATIC_DIR = BASE_DIR / "static"
SAMPLE_DIR = BASE_DIR / "sample_docs"

CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "reading_state.json"
LIBRARY_CACHE_FILE = DATA_DIR / "library_cache.json"

SUPPORTED_EXTENSIONS = {".pdf", ".epub", ".md", ".txt", ".umd", ".chm"}
BOOK_CATEGORY_EXTS = {"epub", "umd", "txt", "chm"}
VIRTUAL_SYSTEM_DIRS = {"/proc", "/sys", "/dev", "/run", "/snap", "/lost+found"}
DEFAULT_GDRIVE_DOC_PATH = "/srv/oc/gdrive/documents"

# 线程锁与内存缓存
CONFIG_LOCK = threading.RLock()
STATE_LOCK = threading.RLock()
CACHE_LOCK = threading.RLock()
TEXT_LRU_LOCK = threading.RLock()

# 2 万册大书库常驻内存缓存（生成后永久有效，除非用户显式点击刷新 ?refresh=1）
MEMORY_LIBRARY_CACHE: Dict[str, Any] = {
    "loaded": False,
    "updated_at": 0.0,
    "collections": [],
    "books": [],
}

# TXT / UMD / MD 解析结果 LRU 缓存（上限 5 本）
TEXT_BOOK_LRU: "OrderedDict[Tuple[str, float], Dict[str, Any]]" = OrderedDict()
TEXT_BOOK_LRU_MAX = 5

# 后台扫描与封面预热状态
SCAN_STATE: Dict[str, Any] = {
    "scanning": False,
    "scanned_dirs": 0,
    "found_books": 0,
    "last_scan_time": 0.0,
    "warmer_running": False,
    "error": None,
}
SCAN_THREAD_LOCK = threading.Lock()


# ==============================================================================
# 2. 工具函数：安全目录检测、原子 JSON 读写、样本数据自愈生成
# ==============================================================================
def _is_dir_accessible(path_str: str) -> bool:
    if not path_str or not isinstance(path_str, str):
        return False
    try:
        p = Path(path_str)
        if not p.exists() or not p.is_dir():
            return False
        if not os.access(str(p), os.R_OK | os.X_OK):
            return False
        with os.scandir(str(p)) as it:
            next(it, None)
        return True
    except Exception:
        return False


def _cleanup_tmp_files() -> None:
    for folder in (DATA_DIR, COVERS_DIR):
        try:
            if folder.exists():
                for entry in os.scandir(str(folder)):
                    if entry.is_file() and entry.name.endswith(".tmp"):
                        try:
                            os.unlink(entry.path)
                        except Exception:
                            pass
        except Exception:
            pass


def _atomic_read_json(file_path: Path, default_val: Any) -> Any:
    try:
        if not file_path.exists():
            return default_val
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default_val


def _atomic_write_json(file_path: Path, data: Any) -> bool:
    tmp_path: Optional[Path] = None
    try:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{file_path.name}.",
            suffix=".tmp",
            dir=str(file_path.parent),
        )
        tmp_path = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp_path), str(file_path))
        return True
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass
        return False


def _ensure_sample_docs() -> None:
    """当 SAMPLE_DIR 为空时，自动生成测试子文件夹与 4 篇测试文档。"""
    try:
        SAMPLE_DIR.mkdir(parents=True, exist_ok=True)
        has_any_book = False
        for _root, _dirs, files in os.walk(str(SAMPLE_DIR)):
            for fname in files:
                if Path(fname).suffix.lower() in SUPPORTED_EXTENSIONS:
                    has_any_book = True
                    break
            if has_any_book:
                break

        if has_any_book:
            return

        dir_history = SAMPLE_DIR / "人文历史"
        dir_invest = SAMPLE_DIR / "投资经典"
        dir_hardware = SAMPLE_DIR / "硬件手册"

        dir_history.mkdir(parents=True, exist_ok=True)
        dir_invest.mkdir(parents=True, exist_ok=True)
        dir_hardware.mkdir(parents=True, exist_ok=True)

        tangshi_content = (
            "第一章 五言古诗精选\n\n"
            "【感遇四首·其一】张九龄\n"
            "孤鸿海上来，池潢不敢顾。\n"
            "侧见双翠鸟，巢在三珠树。\n"
            "矫矫珍木巅，得无金丸惧？\n"
            "美服患人指，高明逼神恶。\n"
            "今我游冥冥，弋者何所慕！\n\n"
            "【下终南山过斛斯山人宿置酒】李白\n"
            "暮从碧山下，山月随人归。\n"
            "却顾所来径，苍苍横翠微。\n"
            "相携及田家，童稚开荆扉。\n"
            "绿竹入幽径，青萝拂行衣。\n"
            "欢言得所憩，美酒聊共挥。\n"
            "长歌吟松风，曲尽河星稀。\n"
            "我醉君复乐，陶然共忘机。\n\n"
            "第二章 七言律诗精选\n\n"
            "【登高】杜甫\n"
            "风急天高猿啸哀，渚清沙白鸟飞回。\n"
            "无边落木萧萧下，不尽长江滚滚来。\n"
            "万里悲秋常作客，百年多病独登台。\n"
            "艰难苦恨繁霜鬓，潦倒新停浊酒杯。\n\n"
            "【锦瑟】李商隐\n"
            "锦瑟无端五十弦，一弦一柱思华年。\n"
            "庄生晓梦迷蝴蝶，望帝春心托杜鹃。\n"
            "沧海月明珠有泪，蓝田日暖玉生烟。\n"
            "此情可待成追忆，只是当时已惘然。\n\n"
            "第三章 五言绝句与七言绝句\n\n"
            "【鹿柴】王维\n"
            "空山不见人，但闻人语响。\n"
            "返景入深林，复照青苔上。\n\n"
            "【早发白帝城】李白\n"
            "朝辞白帝彩云间，千里江陵一日还。\n"
            "两岸猿声啼不住，轻舟已过万重山。\n"
        )
        (dir_history / "唐诗三百首.txt").write_text(tangshi_content, encoding="utf-8")

        backpack_md = (
            "# 背包十年：我的职业是旅行\n\n"
            "> 走过越多的路，越明白世界不是由坐标构成的，而是由一次次出发与重逢构成的。\n\n"
            "## 第一章 出发：从一张单程票开始\n\n"
            "十年前第一次背起65升的登山包时，我并不知道这段旅程会持续整整十年。那时候行囊里塞满了不必要的指南针、厚重的词典和多余的衣物，却唯独缺少面对未知的从容。\n\n"
            "### 1.1 青年旅舍里的世界地图\n\n"
            "在清迈老城的一家木结构青旅里，墙上贴满了来自世界各地旅行者留下的便签。有人用铅笔写下穿越陆路边境的路线，有人画下自己在安纳普尔纳大环线看到的星空。\n\n"
            "- **极简行囊法则**：凡是三天内没有用到的物品，大概率整趟旅途都不需要。\n"
            "- **慢速移动**：与其在十天内打卡八座城市，不如在一个清晨跟着当地人去一次菜市场。\n\n"
            "## 第二章 跨越山海的独行时刻\n\n"
            "当火车穿过西伯利亚无边的白桦林，窗外的风景在数日之内几乎保持着同样的节奏。正是在这种漫长的空白里，阅读与记录成为了旅途中最忠实的陪伴。\n\n"
            "```text\n"
            "路线记录：北京 -> 二连浩特 -> 乌兰巴托 -> 伊尔库茨克（贝加尔湖） -> 莫斯科\n"
            "历时：19天 | 方式：国际列车 + 徒步\n"
            "```\n\n"
            "## 第三章 归来仍是出发者\n\n"
            "真正的旅行从来不是为了逃离生活，而是为了在看过足够辽阔的坐标系之后，依然热爱眼前的每一顿早餐与每一本好书。\n"
        )
        (dir_history / "背包十年：我的职业是旅行.md").write_text(backpack_md, encoding="utf-8")

        china_future_txt = (
            "第一章 超大规模市场的内生韧性\n\n"
            "理解现代经济体的长期演进，核心在于观察其人口规模、产业配套深度以及基础设施网络所构成的复合乘数效应。当数以亿计的消费者与全球最完整的工业门类处在同一语言、同一物流网络与同一数字支付体系之下时，任何微小的技术创新都会被迅速放大为规模经济。\n\n"
            "从长三角的精密制造集群到珠三角的消费电子供应链，早晨产生的设计图纸往往在当天傍晚就能完成打样。这种敏捷协同能力并非一朝一夕形成，而是数十年工程师红利与产业基础设施持续投入的结晶。\n\n"
            "第二章 工程师红利与产业升级浪潮\n\n"
            "每年数百万理工科毕业生的持续涌入，正在将传统的成本优势转化为研发迭代优势。无论是在新能源电力装备、智能网联硬件，还是在开源软件生态与自动化控制领域，密集的研发投入正推动产业链向高附加值环节稳步攀升。\n\n"
            "第三章 长期主义视角下的价值发现\n\n"
            "对于理性观察者而言，穿越短期周期波动的关键在于把握结构性趋势：城市群数字化治理、绿色能源转型以及本土品牌的品质崛起，共同构成了未来数十年最具确定性的增长底座。\n"
        )
        (dir_invest / "中国为什么有前途.txt").write_text(china_future_txt, encoding="utf-8")

        hardware_md = (
            "# GA-B75M-S 主板硬件维护与技术手册\n\n"
            "本手册提供 **GA-B75M-S (Rev 1.0)** 主板的核心规格参数、跳线定义、存储接口说明及常见故障排查流程。\n\n"
            "## 1. 核心硬件规格总览\n\n"
            "| 模块名称 | 规格参数说明 |\n"
            "| :--- | :--- |\n"
            "| **处理器接口** | 支持 LGA1155 封装 Intel 第 2 代 / 第 3 代 Core i7 / i5 / i3 / Pentium / Celeron 处理器 |\n"
            "| **芯片组** | Intel B75 高速芯片组 |\n"
            "| **内存支持** | 2 x DDR3 DIMM 插槽，最高支持 16 GB 双通道 DDR3 1600/1333/1066 MHz |\n"
            "| **存储接口** | 1 x SATA 6Gb/s (SATA3_0 原生高速口) + 3 x SATA 3Gb/s (SATA2_1~3) |\n"
            "| **扩展插槽** | 1 x PCI Express x16 (支持 PCIe 3.0) + 1 x PCI Express x1 |\n"
            "| **板载网卡** | Realtek GbE 千兆以太网控制芯片 (10/100/1000 Mbit) |\n\n"
            "## 2. 关键接口与装机布线指南\n\n"
            "### 2.1 SATA 接口速率区分\n\n"
            "装机接入固态硬盘（SSD）时，请务必将系统盘连接至白色 **`SATA3_0`** 接口，以获得完整的 6Gb/s 持续读写带宽；机械硬盘（HDD）可连接至其余黑色 `SATA2` 接口。\n\n"
            "### 2.2 前置面板跳线 (`F_PANEL`) 定义\n\n"
            "```text\n"
            "  +PWR_LED-   +PWR_BTN-\n"
            "  [ 1 ][ 3 ]  [ 5 ][ 7 ]\n"
            "  [ 2 ][ 4 ]  [ 6 ][ 8 ]  [ 10 (NC) ]\n"
            "  +HDD_LED-   +RESET-\n"
            "```\n\n"
            "## 3. BIOS 重置与常见故障诊断\n\n"
            "1. **开机无显示 / 内存自检未通过**：请先断开 220V 电源线，拔下内存条使用橡皮擦拭金手指，优先单条插入 `DDR3_1` 插槽测试。\n"
            "2. **清除 CMOS 参数 (`CLR_CMOS`)**：在断电状态下，使用金属镊子短接主板右下角 `CLR_CMOS` 两针跳线 5 秒，或取下 CR2032 纽扣电池静置 1 分钟即可恢复出厂设置。\n"
        )
        (dir_hardware / "GA-B75M-S.md").write_text(hardware_md, encoding="utf-8")
    except Exception:
        pass


# ==============================================================================
# 3. 配置中心加载、路径迁移自愈与保存
# ==============================================================================
def load_and_heal_config() -> Dict[str, Any]:
    with CONFIG_LOCK:
        raw = _atomic_read_json(CONFIG_FILE, {})
        if not isinstance(raw, dict):
            raw = {}

        raw_dirs = raw.get("media_dirs", [])
        if not isinstance(raw_dirs, list):
            raw_dirs = []

        healed_dirs: List[str] = []
        changed = False
        sample_dir_str = str(SAMPLE_DIR.resolve())

        for d in raw_dirs:
            if not isinstance(d, str) or not d.strip():
                changed = True
                continue
            d_clean = d.strip()
            try:
                p_obj = Path(d_clean)
                if p_obj.name == "sample_docs" and str(p_obj.resolve()) != sample_dir_str:
                    target = sample_dir_str
                    changed = True
                elif _is_dir_accessible(d_clean):
                    target = str(p_obj.resolve())
                else:
                    target = sample_dir_str
                    changed = True
            except Exception:
                target = sample_dir_str
                changed = True

            if target not in healed_dirs:
                healed_dirs.append(target)

        if not healed_dirs:
            changed = True
            if _is_dir_accessible(DEFAULT_GDRIVE_DOC_PATH):
                healed_dirs = [str(Path(DEFAULT_GDRIVE_DOC_PATH).resolve())]
            else:
                healed_dirs = [sample_dir_str]

        cloud_restricted_mode = raw.get("cloud_restricted_mode")
        if not isinstance(cloud_restricted_mode, bool):
            cloud_restricted_mode = True
            changed = True

        peer_sync_url = raw.get("peer_sync_url")
        if not isinstance(peer_sync_url, str):
            peer_sync_url = ""
            changed = True
        else:
            peer_sync_url = peer_sync_url.strip().rstrip("/")

        config_data = {
            "media_dirs": healed_dirs,
            "cloud_restricted_mode": cloud_restricted_mode,
            "peer_sync_url": peer_sync_url,
        }

        if changed or not CONFIG_FILE.exists():
            _atomic_write_json(CONFIG_FILE, config_data)

        return config_data


def save_config(new_config: Dict[str, Any]) -> Dict[str, Any]:
    with CONFIG_LOCK:
        current = load_and_heal_config()

        if "media_dirs" in new_config and isinstance(new_config["media_dirs"], list):
            valid_dirs: List[str] = []
            for d in new_config["media_dirs"]:
                if isinstance(d, str) and d.strip():
                    d_str = d.strip()
                    if _is_dir_accessible(d_str):
                        resolved = str(Path(d_str).resolve())
                        if resolved not in valid_dirs:
                            valid_dirs.append(resolved)
            if not valid_dirs:
                valid_dirs = [str(SAMPLE_DIR.resolve())]
            current["media_dirs"] = valid_dirs

        if "cloud_restricted_mode" in new_config and isinstance(
            new_config["cloud_restricted_mode"], bool
        ):
            current["cloud_restricted_mode"] = new_config["cloud_restricted_mode"]

        if "peer_sync_url" in new_config and isinstance(new_config["peer_sync_url"], str):
            current["peer_sync_url"] = new_config["peer_sync_url"].strip().rstrip("/")

        _atomic_write_json(CONFIG_FILE, current)
        return current


# ==============================================================================
# 4. 相对路径主键 (rel_key) 解析、状态存储与多书签 (Bookmarks) 系统
# ==============================================================================
def _normalize_rel_key_and_abs_path(path_param: str) -> Tuple[str, Optional[Path], Optional[Path]]:
    """
    将前端传入的 rel_key 或绝对路径解析为 (rel_key, abs_path, matched_media_dir)。
    确保所有配置的 media_dirs 均经过安全边界校验，杜绝越权访问。
    """
    if not path_param:
        return ("", None, None)

    cleaned = unquote(path_param).strip()
    cfg = load_and_heal_config()
    media_dirs = [Path(d).resolve() for d in cfg.get("media_dirs", []) if _is_dir_accessible(d)]
    if SAMPLE_DIR.resolve() not in media_dirs and _is_dir_accessible(str(SAMPLE_DIR)):
        media_dirs.append(SAMPLE_DIR.resolve())

    # 1. 若传入的是绝对路径，检查其是否位于某个 media_dir 下
    try:
        candidate = Path(cleaned)
        if candidate.is_absolute():
            resolved_cand = candidate.resolve()
            for mdir in media_dirs:
                try:
                    rel = resolved_cand.relative_to(mdir)
                    rel_key = rel.as_posix()
                    return (rel_key, resolved_cand, mdir)
                except ValueError:
                    continue
    except Exception:
        pass

    # 2. 作为相对路径 rel_key 在各个 media_dir 下查找
    rel_clean = cleaned.lstrip("/").replace("\\", "/")
    for mdir in media_dirs:
        try:
            resolved_cand = (mdir / rel_clean).resolve()
            rel = resolved_cand.relative_to(mdir)
            if resolved_cand.exists():
                return (rel.as_posix(), resolved_cand, mdir)
        except Exception:
            continue

    # 3. 即使文件尚未落盘（例如仅更新阅读状态或书签），也返回规范化的 rel_key
    safe_rel = Path(rel_clean).as_posix() if rel_clean else ""
    if safe_rel.startswith(".."):
        safe_rel = safe_rel.lstrip("./")
    default_mdir = media_dirs[0] if media_dirs else SAMPLE_DIR.resolve()
    return (safe_rel, (default_mdir / safe_rel) if safe_rel else None, default_mdir)


def _cover_md5_for_rel_key(rel_key: str) -> str:
    return hashlib.md5(rel_key.encode("utf-8", errors="ignore")).hexdigest()


def _find_cached_or_sidecar_cover(
    rel_key: str,
    abs_path: Optional[Path],
    check_sidecar: bool = True,
) -> Optional[Path]:
    """
    被动检查本地缓存封面 data/covers/<rel_key_md5>.jpg 或同级目录下的同名图片。
    绝不打开 EPUB/PDF 文件！
    """
    try:
        md5_key = _cover_md5_for_rel_key(rel_key)
        cached = COVERS_DIR / f"{md5_key}.jpg"
        if cached.exists() and cached.is_file() and cached.stat().st_size > 0:
            return cached

        if abs_path is not None:
            abs_md5 = hashlib.md5(str(abs_path).encode("utf-8", errors="ignore")).hexdigest()
            cached_abs = COVERS_DIR / f"{abs_md5}.jpg"
            if cached_abs.exists() and cached_abs.is_file() and cached_abs.stat().st_size > 0:
                return cached_abs

            if check_sidecar:
                parent = abs_path.parent
                stem = abs_path.stem
                for ext in (".jpg", ".jpeg", ".png", ".webp"):
                    sidecar = parent / f"{stem}{ext}"
                    if sidecar.exists() and sidecar.is_file():
                        return sidecar
    except Exception:
        pass
    return None


def _normalize_bookmarks_list(raw_list: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_list, list):
        return []
    cleaned: List[Dict[str, Any]] = []
    seen_ids = set()
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        bm_id = str(item.get("id") or "").strip()
        if not bm_id:
            bm_id = hashlib.md5(
                json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8", errors="ignore")
            ).hexdigest()[:12]
        if bm_id in seen_ids:
            continue
        seen_ids.add(bm_id)
        created_at = float(item.get("created_at") or time.time())
        progress_val = float(item.get("progress") or 0.0)
        cleaned.append(
            {
                "id": bm_id,
                "title": str(item.get("title") or item.get("label") or "书签"),
                "label": str(item.get("label") or item.get("title") or "书签"),
                "snippet": str(item.get("snippet") or "")[:160],
                "locator": item.get("locator") if item.get("locator") is not None else {},
                "progress": round(progress_val, 1),
                "created_at": created_at,
            }
        )
    cleaned.sort(key=lambda x: float(x.get("created_at", 0.0)), reverse=True)
    return cleaned


def load_reading_state() -> Dict[str, Dict[str, Any]]:
    with STATE_LOCK:
        data = _atomic_read_json(STATE_FILE, {})
        return data if isinstance(data, dict) else {}


def update_reading_state_entry(
    rel_key: str,
    progress: Optional[float] = None,
    status: Optional[str] = None,
    locator: Optional[Any] = None,
    bookmarks: Optional[List[Dict[str, Any]]] = None,
    updated_at: Optional[float] = None,
) -> Dict[str, Any]:
    with STATE_LOCK:
        state_all = load_reading_state()
        entry = state_all.get(rel_key, {})
        if not isinstance(entry, dict):
            entry = {}

        if progress is not None:
            p_val = max(0.0, min(100.0, float(progress)))
            entry["progress"] = round(p_val, 1) if p_val % 1 != 0 else int(p_val)
            if status is None:
                if p_val >= 99.5:
                    entry["status"] = "finished"
                elif p_val > 0 and entry.get("status", "new") == "new":
                    entry["status"] = "reading"

        if status is not None and status in ("new", "reading", "want", "finished"):
            entry["status"] = status
            if status == "new" and progress is None:
                entry["progress"] = 0
                entry["locator"] = ""
            elif status == "finished" and progress is None:
                entry["progress"] = 100

        if locator is not None:
            entry["locator"] = locator

        if bookmarks is not None:
            entry["bookmarks"] = _normalize_bookmarks_list(bookmarks)
        else:
            entry["bookmarks"] = _normalize_bookmarks_list(entry.get("bookmarks", []))

        entry.setdefault("progress", 0)
        entry.setdefault("status", "new")
        entry.setdefault("locator", "")
        entry["updated_at"] = float(updated_at) if updated_at is not None else time.time()

        state_all[rel_key] = entry
        _atomic_write_json(STATE_FILE, state_all)
        return entry


def mutate_book_bookmarks(
    rel_key: str,
    action: str,
    bookmark_payload: Dict[str, Any],
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
    """
    对指定 rel_key 的书籍执行书签增删改操作，持久化至 data/reading_state.json。
    返回 (updated_state_entry, affected_bookmark)。
    """
    act = (action or "add").strip().lower()
    with STATE_LOCK:
        state_all = load_reading_state()
        entry = state_all.get(rel_key, {})
        if not isinstance(entry, dict):
            entry = {}

        bookmarks = _normalize_bookmarks_list(entry.get("bookmarks", []))
        target_id = str(
            bookmark_payload.get("id") or bookmark_payload.get("bookmark_id") or ""
        ).strip()
        affected: Optional[Dict[str, Any]] = None

        if act in ("remove", "delete"):
            new_list: List[Dict[str, Any]] = []
            for bm in bookmarks:
                if target_id and bm.get("id") == target_id:
                    affected = bm
                    continue
                new_list.append(bm)
            bookmarks = new_list
        elif act == "clear":
            bookmarks = []
        else:
            now_ts = float(bookmark_payload.get("created_at") or time.time())
            if not target_id:
                raw_seed = f"{rel_key}:{now_ts}:{os.urandom(4).hex()}"
                target_id = hashlib.md5(raw_seed.encode("utf-8", errors="ignore")).hexdigest()[:12]

            label_str = str(
                bookmark_payload.get("label")
                or bookmark_payload.get("title")
                or "书签"
            ).strip()
            title_str = str(
                bookmark_payload.get("title")
                or label_str
                or "书签"
            ).strip()
            snippet_str = str(bookmark_payload.get("snippet") or "").strip()[:160]
            locator_val = (
                bookmark_payload.get("locator")
                if bookmark_payload.get("locator") is not None
                else {}
            )
            prog_val = float(bookmark_payload.get("progress") or entry.get("progress") or 0.0)
            prog_val = max(0.0, min(100.0, prog_val))

            new_bm = {
                "id": target_id,
                "title": title_str,
                "label": label_str,
                "snippet": snippet_str,
                "locator": locator_val,
                "progress": round(prog_val, 1),
                "created_at": now_ts,
            }
            affected = new_bm
            bookmarks = [b for b in bookmarks if b.get("id") != target_id]
            bookmarks.insert(0, new_bm)
            bookmarks.sort(key=lambda x: float(x.get("created_at", 0.0)), reverse=True)

        entry.setdefault("progress", 0)
        entry.setdefault("status", "new")
        entry.setdefault("locator", "")
        entry["bookmarks"] = bookmarks
        entry["updated_at"] = time.time()

        state_all[rel_key] = entry
        _atomic_write_json(STATE_FILE, state_all)
        return (entry, affected)


# ==============================================================================
# 5. 云盘同步进程避让检测、后台目录扫描与常驻内存书库管理
# ==============================================================================
def _is_cloud_sync_process_active() -> bool:
    """检测系统是否存在 flush_gdrive.sh 或 rclone move 进程。"""
    try:
        res = subprocess.run(
            ["ps", "-eo", "args"],
            capture_output=True,
            text=True,
            timeout=1.5,
        )
        out = res.stdout or ""
        if "flush_gdrive.sh" in out or "rclone move" in out:
            return True
    except Exception:
        pass
    return False


def _extract_epub_cover_bytes(epub_path: Path) -> Optional[bytes]:
    """从 EPUB (ZIP) 容器中安全提取封面图片字节。"""
    try:
        if not epub_path.exists() or not epub_path.is_file():
            return None
        with zipfile.ZipFile(str(epub_path), "r") as zf:
            namelist = zf.namelist()
            image_exts = (".jpg", ".jpeg", ".png", ".webp")

            cover_candidates = [
                n
                for n in namelist
                if n.lower().endswith(image_exts) and "cover" in Path(n).name.lower()
            ]
            if not cover_candidates:
                opf_files = [n for n in namelist if n.lower().endswith(".opf")]
                for opf_name in opf_files[:2]:
                    try:
                        opf_text = zf.read(opf_name).decode("utf-8", errors="ignore")
                        m = re.search(
                            r'href=["\']([^"\']+\.(?:jpg|jpeg|png|webp))["\']',
                            opf_text,
                            re.IGNORECASE,
                        )
                        if m:
                            href = m.group(1)
                            opf_dir = Path(opf_name).parent.as_posix()
                            joined = f"{opf_dir}/{href}" if opf_dir != "." else href
                            for n in namelist:
                                if n.endswith(href) or n == joined:
                                    cover_candidates.append(n)
                                    break
                    except Exception:
                        pass

            if not cover_candidates:
                all_imgs = [n for n in namelist if n.lower().endswith(image_exts)]
                if all_imgs:
                    cover_candidates.append(all_imgs[0])

            if cover_candidates:
                data = zf.read(cover_candidates[0])
                if data and len(data) > 128:
                    return data
    except Exception:
        pass
    return None


def _mark_cover_in_memory_cache(rel_key: str) -> None:
    with CACHE_LOCK:
        for b in MEMORY_LIBRARY_CACHE.get("books", []):
            if b.get("rel_key") == rel_key:
                b["has_cover"] = True
                break


def _save_cover_cache(rel_key: str, image_bytes: bytes) -> Optional[Path]:
    try:
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        md5_key = _cover_md5_for_rel_key(rel_key)
        target_path = COVERS_DIR / f"{md5_key}.jpg"
        fd, tmp_name = tempfile.mkstemp(prefix=".cover.", suffix=".tmp", dir=str(COVERS_DIR))
        with os.fdopen(fd, "wb") as f:
            f.write(image_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(target_path))
        _mark_cover_in_memory_cache(rel_key)
        return target_path
    except Exception:
        return None


def _local_cover_warmer_worker(books: List[Dict[str, Any]]) -> None:
    """当 cloud_restricted_mode == False 时，后台低优先级静默提取未缓存的 EPUB 封面。"""
    SCAN_STATE["warmer_running"] = True
    try:
        for book in books:
            cfg = load_and_heal_config()
            if cfg.get("cloud_restricted_mode", True):
                break
            ext_low = (book.get("ext") or "").lower()
            if ext_low not in ("epub", "chm"):
                continue
            rel_key = book.get("rel_key", "")
            abs_p = Path(book.get("abs_path", "")) if book.get("abs_path") else None
            if not rel_key or not abs_p:
                continue
            if _find_cached_or_sidecar_cover(rel_key, abs_p) is not None:
                continue
            img_bytes = (
                _extract_epub_cover_bytes(abs_p)
                if ext_low == "epub"
                else extract_chm_cover_bytes(str(abs_p))
            )
            if img_bytes:
                _save_cover_cache(rel_key, img_bytes)
            time.sleep(0.05)
    except Exception:
        pass
    finally:
        SCAN_STATE["warmer_running"] = False


def _build_book_record(
    entry_path: Path,
    stat_res: os.stat_result,
    media_dir: Path,
    cloud_restricted: bool = True,
) -> Dict[str, Any]:
    rel_path = entry_path.relative_to(media_dir)
    rel_key = rel_path.as_posix()
    parts = rel_path.parts
    category = parts[0] if len(parts) > 1 else "根目录"
    folder = rel_path.parent.as_posix()
    if folder == ".":
        folder = ""

    ext = entry_path.suffix.lower().lstrip(".")
    has_cover = (
        _find_cached_or_sidecar_cover(
            rel_key,
            entry_path,
            check_sidecar=not cloud_restricted,
        )
        is not None
    )

    return {
        "id": _cover_md5_for_rel_key(rel_key),
        "rel_key": rel_key,
        "path": rel_key,
        "abs_path": str(entry_path),
        "media_dir": str(media_dir),
        "name": entry_path.name,
        "title": entry_path.stem,
        "ext": ext,
        "format": ext.upper(),
        "category": category,
        "folder": folder,
        "size": stat_res.st_size,
        "mtime": stat_res.st_mtime,
        "has_cover": has_cover,
        "cover_url": f"/api/cover?path={quote(rel_key)}",
    }


def _set_memory_library_cache(snapshot: Dict[str, Any]) -> None:
    with CACHE_LOCK:
        books = snapshot.get("books", [])
        collections = snapshot.get("collections", [])
        updated_at = float(snapshot.get("updated_at", 0.0) or 0.0)
        MEMORY_LIBRARY_CACHE["loaded"] = True
        MEMORY_LIBRARY_CACHE["updated_at"] = updated_at
        MEMORY_LIBRARY_CACHE["collections"] = list(collections) if isinstance(collections, list) else []
        MEMORY_LIBRARY_CACHE["books"] = list(books) if isinstance(books, list) else []


def _ensure_memory_library_loaded() -> Dict[str, Any]:
    """
    确保 MEMORY_LIBRARY_CACHE 已从 data/library_cache.json 加载到内存。
    一旦加载，永久常驻内存，绝不因时间流逝自动触发云盘重扫！
    """
    with CACHE_LOCK:
        if MEMORY_LIBRARY_CACHE.get("loaded") and MEMORY_LIBRARY_CACHE.get("books"):
            return MEMORY_LIBRARY_CACHE

        cache_data = _atomic_read_json(LIBRARY_CACHE_FILE, {})
        if isinstance(cache_data, dict) and isinstance(cache_data.get("books"), list) and cache_data.get("books"):
            _set_memory_library_cache(cache_data)
            SCAN_STATE["last_scan_time"] = MEMORY_LIBRARY_CACHE["updated_at"]
            return MEMORY_LIBRARY_CACHE

    # 仅当磁盘缓存文件不存在或为空（首次初始化）时同步扫描一次
    _scan_library_worker()
    return MEMORY_LIBRARY_CACHE


def _scan_library_worker() -> None:
    if not SCAN_THREAD_LOCK.acquire(blocking=False):
        return
    try:
        SCAN_STATE["scanning"] = True
        SCAN_STATE["scanned_dirs"] = 0
        SCAN_STATE["found_books"] = 0
        SCAN_STATE["error"] = None

        cfg = load_and_heal_config()
        media_dirs = cfg.get("media_dirs", [])
        cloud_restricted = cfg.get("cloud_restricted_mode", True)
        avoid_cloud = _is_cloud_sync_process_active()

        books_list: List[Dict[str, Any]] = []
        collections_set: List[str] = []
        seen_rel_keys = set()

        for mdir_str in media_dirs:
            if not _is_dir_accessible(mdir_str):
                continue
            mdir = Path(mdir_str).resolve()

            # 收集一级子目录作为 My Collections
            try:
                with os.scandir(str(mdir)) as it:
                    for top_entry in sorted(it, key=lambda e: e.name.lower()):
                        if top_entry.name.startswith("."):
                            continue
                        try:
                            if top_entry.is_dir(follow_symlinks=False):
                                if top_entry.name not in collections_set:
                                    collections_set.append(top_entry.name)
                        except Exception:
                            continue
            except Exception:
                pass

            # 递归扫描目录树（仅读取文件名与 stat 元信息，绝不打开任何文件内容）
            stack = [mdir]
            while stack:
                curr_dir = stack.pop()
                SCAN_STATE["scanned_dirs"] += 1
                if avoid_cloud:
                    time.sleep(0.05)

                try:
                    with os.scandir(str(curr_dir)) as it:
                        entries = sorted(list(it), key=lambda e: e.name.lower())
                except Exception:
                    continue

                subdirs: List[Path] = []
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            subdirs.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            suffix = Path(entry.name).suffix.lower()
                            if suffix in SUPPORTED_EXTENSIONS:
                                st = entry.stat(follow_symlinks=False)
                                book_rec = _build_book_record(
                                    Path(entry.path),
                                    st,
                                    mdir,
                                    cloud_restricted=cloud_restricted,
                                )
                                rk = book_rec["rel_key"]
                                if rk not in seen_rel_keys:
                                    seen_rel_keys.add(rk)
                                    books_list.append(book_rec)
                                    SCAN_STATE["found_books"] = len(books_list)
                    except Exception:
                        continue

                for sd in reversed(subdirs):
                    stack.append(sd)

        snapshot = {
            "updated_at": time.time(),
            "collections": collections_set,
            "books": books_list,
        }
        with CACHE_LOCK:
            _atomic_write_json(LIBRARY_CACHE_FILE, snapshot)
            _set_memory_library_cache(snapshot)

        SCAN_STATE["last_scan_time"] = snapshot["updated_at"]

        # 若处于本地满血模式 (cloud_restricted_mode == False)，启动后台 EPUB 封面预热线程
        if not cloud_restricted and books_list:
            t = threading.Thread(
                target=_local_cover_warmer_worker,
                args=(books_list,),
                daemon=True,
            )
            t.start()

    except Exception as exc:
        SCAN_STATE["error"] = str(exc)
    finally:
        SCAN_STATE["scanning"] = False
        SCAN_THREAD_LOCK.release()


def trigger_background_scan() -> bool:
    if SCAN_STATE.get("scanning"):
        return False
    t = threading.Thread(target=_scan_library_worker, daemon=True)
    t.start()
    return True


def _merge_single_book_with_state(
    b: Dict[str, Any],
    reading_state: Dict[str, Dict[str, Any]],
    cloud_restricted: bool,
    verify_cover_on_disk: bool = False,
) -> Dict[str, Any]:
    item = dict(b)
    rk = item.get("rel_key", "")
    st = reading_state.get(rk, {})
    if not isinstance(st, dict):
        st = {}
    item["progress"] = st.get("progress", 0)
    item["status"] = st.get("status", "new")
    item["locator"] = st.get("locator", "")
    item["bookmarks"] = _normalize_bookmarks_list(st.get("bookmarks", []))
    item["updated_at"] = st.get("updated_at", 0)
    item["cloud_restricted_mode"] = cloud_restricted
    if verify_cover_on_disk:
        abs_p = Path(item["abs_path"]) if item.get("abs_path") else None
        item["has_cover"] = (
            _find_cached_or_sidecar_cover(
                rk,
                abs_p,
                check_sidecar=not cloud_restricted,
            )
            is not None
        )
    else:
        item["has_cover"] = bool(item.get("has_cover", False))
    return item


def _merge_books_with_state(
    books: List[Dict[str, Any]],
    reading_state: Dict[str, Dict[str, Any]],
    cloud_restricted: bool,
    verify_cover_on_disk: bool = True,
) -> List[Dict[str, Any]]:
    return [
        _merge_single_book_with_state(b, reading_state, cloud_restricted, verify_cover_on_disk)
        for b in books
    ]


def _query_memory_library_paginated(
    page: int,
    page_size: int,
    category: str,
    search: str,
    folder: str,
    sort_by: str,
    cfg: Dict[str, Any],
) -> Dict[str, Any]:
    """
    在常驻内存的 2 万册书库列表上毫秒级完成：
    1. 全局分类统计计数 (counts)
    2. 分类 / 一级目录 / 文件夹 / 搜索词过滤
    3. 排序与分页切片（仅对当前页的 60 本检查本地封面缓存，零云盘 I/O）
    """
    mem = _ensure_memory_library_loaded()
    with CACHE_LOCK:
        raw_books = list(mem.get("books", []))
        collections = list(mem.get("collections", []))
        updated_at = float(mem.get("updated_at", 0.0) or 0.0)

    reading_state = load_reading_state()
    cloud_restricted = cfg.get("cloud_restricted_mode", True)

    counts: Dict[str, int] = {
        "all": len(raw_books),
        "want": 0,
        "finished": 0,
        "books": 0,
        "pdfs": 0,
        "markdown": 0,
    }

    cat_key = (category or "all").strip()
    cat_lower = cat_key.lower()
    folder_clean = (folder or "").strip().strip("/")
    q_lower = (search or "").strip().lower()

    filtered_entries: List[Dict[str, Any]] = []

    for b in raw_books:
        rk = b.get("rel_key", "")
        ext = (b.get("ext") or "").lower()
        st = reading_state.get(rk)
        status = st.get("status", "new") if isinstance(st, dict) else "new"

        if status == "want":
            counts["want"] += 1
        if status == "finished":
            counts["finished"] += 1
        if ext in BOOK_CATEGORY_EXTS:
            counts["books"] += 1
        elif ext == "pdf":
            counts["pdfs"] += 1
        elif ext == "md":
            counts["markdown"] += 1

        # 1) 分类过滤
        if cat_lower == "all" or not cat_lower:
            pass
        elif cat_lower == "want":
            if status != "want":
                continue
        elif cat_lower == "finished":
            if status != "finished":
                continue
        elif cat_lower == "books":
            if ext not in BOOK_CATEGORY_EXTS:
                continue
        elif cat_lower in ("pdfs", "pdf"):
            if ext != "pdf":
                continue
        elif cat_lower in ("markdown", "md"):
            if ext != "md":
                continue
        else:
            # 支持按一级 Collection 名称或分类名过滤
            b_cat = b.get("category", "")
            b_folder = b.get("folder", "")
            if b_cat != cat_key and not (
                b_folder == cat_key or b_folder.startswith(cat_key + "/")
            ):
                continue

        # 2) 指定文件夹过滤
        if folder_clean:
            b_folder = b.get("folder", "")
            if b_folder != folder_clean and not b_folder.startswith(folder_clean + "/"):
                continue

        # 3) 关键词搜索过滤
        if q_lower:
            haystack = f"{b.get('title', '')} {b.get('name', '')} {rk} {b.get('category', '')}".lower()
            if q_lower not in haystack:
                continue

        merged_item = _merge_single_book_with_state(
            b,
            reading_state,
            cloud_restricted,
            verify_cover_on_disk=False,
        )
        filtered_entries.append(merged_item)

    # 4) 排序：优先按最近阅读/状态更新时间倒序，其次按标题排序（或根据 sort 参数）
    sort_mode = (sort_by or "").strip().lower()
    if sort_mode == "title":
        filtered_entries.sort(key=lambda x: (x.get("title") or "").lower())
    elif sort_mode == "mtime":
        filtered_entries.sort(key=lambda x: float(x.get("mtime") or 0.0), reverse=True)
    elif sort_mode == "size":
        filtered_entries.sort(key=lambda x: int(x.get("size") or 0), reverse=True)
    else:
        filtered_entries.sort(
            key=lambda x: (
                -float(x.get("updated_at") or 0.0),
                (x.get("title") or "").lower(),
            )
        )

    total_matched = len(filtered_entries)
    safe_page = max(1, int(page or 1))
    safe_page_size = max(1, min(500, int(page_size or 60)))
    start_idx = (safe_page - 1) * safe_page_size
    end_idx = start_idx + safe_page_size
    page_slice = filtered_entries[start_idx:end_idx]

    # 仅对当前页返回的书籍检查本地 data/covers/ 缓存，确保零云盘阻塞
    for item in page_slice:
        rk = item.get("rel_key", "")
        abs_p = Path(item["abs_path"]) if item.get("abs_path") else None
        has_cov = (
            _find_cached_or_sidecar_cover(
                rk,
                abs_p,
                check_sidecar=not cloud_restricted,
            )
            is not None
        )
        item["has_cover"] = has_cov

    total_pages = max(1, (total_matched + safe_page_size - 1) // safe_page_size)
    return {
        "books": page_slice,
        "collections": collections,
        "total": total_matched,
        "page": safe_page,
        "page_size": safe_page_size,
        "total_pages": total_pages,
        "has_more": end_idx < total_matched,
        "counts": counts,
        "updated_at": updated_at,
        "config": cfg,
        "scan_status": dict(SCAN_STATE),
    }


# ==============================================================================
# 6. TXT / MD 确定性编码回退链与按章切片引擎
# ==============================================================================
CHAPTER_HEADING_RE = re.compile(
    r"^\s*(?:"
    r"第[零一二三四五六七八九十百千万0-9]+[章节回卷集部篇].*|"
    r"Chapter\s+\d+.*|"
    r"CHAPTER\s+\d+.*|"
    r"[一二三四五六七八九十]+、.{1,40}|"
    r"序[言章幕]?|楔子|引子|尾声|后记|番外.*"
    r")\s*$",
    re.MULTILINE,
)


def _decode_text_deterministic(raw_bytes: bytes) -> Tuple[str, str]:
    """
    确定性编码识别：
    仅读取前 64KB 字节，优先按顺序尝试严格解码：
    utf-8-sig -> utf-8 -> gb18030 -> big5
    均失败时调用 chardet，最后用 gb18030 (errors='replace') 终极兜底。
    """
    head = raw_bytes[:65536]
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            head.decode(enc, errors="strict")
            full_text = raw_bytes.decode(enc, errors="strict")
            return (full_text, enc)
        except UnicodeDecodeError:
            continue

    try:
        detected = chardet.detect(head)
        det_enc = detected.get("encoding") if isinstance(detected, dict) else None
        if det_enc:
            full_text = raw_bytes.decode(det_enc, errors="strict")
            return (full_text, det_enc.lower())
    except Exception:
        pass

    return (raw_bytes.decode("gb18030", errors="replace"), "gb18030-replace")


def _split_txt_into_chapters(full_text: str, default_title: str) -> List[Dict[str, Any]]:
    normalized = full_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return [{"index": 0, "title": default_title, "content": "（文档内容为空）"}]

    matches = list(CHAPTER_HEADING_RE.finditer(normalized))
    chapters: List[Dict[str, Any]] = []

    if matches:
        first_start = matches[0].start()
        if first_start > 60:
            preface = normalized[:first_start].strip()
            if preface:
                chapters.append(
                    {
                        "index": 0,
                        "title": "导读 / 序言",
                        "content": preface,
                    }
                )

        for i, m in enumerate(matches):
            ch_title = m.group(0).strip()
            start_pos = m.end()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(normalized)
            body = normalized[start_pos:end_pos].strip()
            if not body and i + 1 < len(matches):
                body = ch_title
            chapters.append(
                {
                    "index": len(chapters),
                    "title": ch_title[:60] or f"第 {len(chapters) + 1} 章",
                    "content": body or "（本章无正文）",
                }
            )
    else:
        chunk_size = 6000
        if len(normalized) <= chunk_size:
            chapters.append({"index": 0, "title": default_title, "content": normalized})
        else:
            for idx, pos in enumerate(range(0, len(normalized), chunk_size)):
                chapters.append(
                    {
                        "index": idx,
                        "title": f"第 {idx + 1} 节",
                        "content": normalized[pos : pos + chunk_size],
                    }
                )

    return chapters


def _parse_and_cache_text_book(abs_path: Path) -> Dict[str, Any]:
    st = abs_path.stat()
    cache_key = (str(abs_path), st.st_mtime)

    with TEXT_LRU_LOCK:
        if cache_key in TEXT_BOOK_LRU:
            TEXT_BOOK_LRU.move_to_end(cache_key)
            return TEXT_BOOK_LRU[cache_key]

    ext = abs_path.suffix.lower().lstrip(".")
    if ext == "umd":
        parsed = parse_umd(str(abs_path))
        parsed["ext"] = "umd"
    elif ext == "chm":
        parsed = parse_chm(str(abs_path))
        parsed["ext"] = "chm"
    elif ext == "md":
        raw_bytes = abs_path.read_bytes()
        full_text, enc = _decode_text_deterministic(raw_bytes)
        normalized = full_text.replace("\r\n", "\n").replace("\r", "\n")
        toc_list: List[Dict[str, Any]] = []
        for line in normalized.split("\n"):
            m = re.match(r"^(#{1,3})\s+(.+)$", line.strip())
            if m:
                toc_list.append(
                    {
                        "level": len(m.group(1)),
                        "title": m.group(2).strip(),
                    }
                )
        parsed = {
            "title": abs_path.stem,
            "author": "Markdown 文档",
            "encoding": enc,
            "ext": "md",
            "toc": toc_list,
            "chapters": [
                {
                    "index": 0,
                    "title": abs_path.stem,
                    "content": normalized,
                }
            ],
        }
    else:
        raw_bytes = abs_path.read_bytes()
        full_text, enc = _decode_text_deterministic(raw_bytes)
        chapters = _split_txt_into_chapters(full_text, abs_path.stem)
        parsed = {
            "title": abs_path.stem,
            "author": "本地藏书",
            "encoding": enc,
            "ext": ext,
            "chapters": chapters,
        }

    with TEXT_LRU_LOCK:
        TEXT_BOOK_LRU[cache_key] = parsed
        TEXT_BOOK_LRU.move_to_end(cache_key)
        while len(TEXT_BOOK_LRU) > TEXT_BOOK_LRU_MAX:
            TEXT_BOOK_LRU.popitem(last=False)

    return parsed


# ==============================================================================
# 7. FastAPI 生命周期与路由定义
# ==============================================================================
@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        COVERS_DIR.mkdir(parents=True, exist_ok=True)
        STATIC_DIR.mkdir(parents=True, exist_ok=True)
        SAMPLE_DIR.mkdir(parents=True, exist_ok=True)

        _cleanup_tmp_files()
        _ensure_sample_docs()
        load_and_heal_config()

        if not STATE_FILE.exists():
            _atomic_write_json(STATE_FILE, {})

        cache_data = _atomic_read_json(LIBRARY_CACHE_FILE, {})
        if not isinstance(cache_data, dict) or not cache_data.get("books"):
            _scan_library_worker()
        else:
            _set_memory_library_cache(cache_data)
            SCAN_STATE["last_scan_time"] = float(cache_data.get("updated_at", 0.0))
    except Exception:
        pass
    yield


app = FastAPI(
    title="R-Book (Apple Books Library Edition)",
    version="1.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------------------
# Pydantic 请求模型
# ------------------------------------------------------------------------------
class ConfigUpdateRequest(BaseModel):
    media_dirs: Optional[List[str]] = None
    cloud_restricted_mode: Optional[bool] = None
    peer_sync_url: Optional[str] = None


class StateUpdateRequest(BaseModel):
    path: Optional[str] = None
    rel_key: Optional[str] = None
    progress: Optional[float] = None
    status: Optional[str] = None
    locator: Optional[Any] = None
    bookmarks: Optional[List[Dict[str, Any]]] = None
    updated_at: Optional[float] = None


class BookmarkActionRequest(BaseModel):
    action: Optional[str] = "add"
    path: Optional[str] = None
    rel_key: Optional[str] = None
    id: Optional[str] = None
    bookmark_id: Optional[str] = None
    title: Optional[str] = None
    label: Optional[str] = None
    snippet: Optional[str] = None
    locator: Optional[Any] = None
    progress: Optional[float] = None
    created_at: Optional[float] = None


class CoverExtractRequest(BaseModel):
    path: str


class SyncTriggerRequest(BaseModel):
    peer_sync_url: Optional[str] = None


# ------------------------------------------------------------------------------
# 7.1 配置与宿主机远程目录树浏览接口
# ------------------------------------------------------------------------------
@app.get("/api/config")
async def api_get_config():
    cfg = await asyncio.to_thread(load_and_heal_config)
    return cfg


@app.post("/api/config")
async def api_save_config(req: ConfigUpdateRequest):
    payload = req.model_dump(exclude_none=True)
    updated = await asyncio.to_thread(save_config, payload)
    await asyncio.to_thread(_scan_library_worker)
    return {
        "status": "ok",
        "config": updated,
        **updated,
    }


def _list_host_directories_sync(target_path_str: str) -> Dict[str, Any]:
    shortcuts = ["/", "/srv/oc/gdrive", "/mnt", "/home", str(BASE_DIR)]

    raw_path = (target_path_str or "/").strip()
    try:
        target = Path(raw_path).resolve()
        if not target.exists() or not target.is_dir():
            target = BASE_DIR
    except Exception:
        target = BASE_DIR

    current_str = str(target)
    parent_str = str(target.parent) if target != target.parent else None

    dirs: List[Dict[str, Any]] = []
    try:
        with os.scandir(current_str) as it:
            for entry in it:
                name = entry.name
                if name.startswith("."):
                    continue
                full_p = os.path.join(current_str, name) if current_str != "/" else f"/{name}"
                if full_p in VIRTUAL_SYSTEM_DIRS:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if not os.access(full_p, os.R_OK | os.X_OK):
                        continue
                    has_subdirs = False
                    try:
                        with os.scandir(full_p) as sub_it:
                            for sub_e in sub_it:
                                if not sub_e.name.startswith(".") and sub_e.is_dir(
                                    follow_symlinks=False
                                ):
                                    has_subdirs = True
                                    break
                    except Exception:
                        has_subdirs = False

                    dirs.append(
                        {
                            "name": name,
                            "path": full_p,
                            "has_subdirs": has_subdirs,
                        }
                    )
                except (PermissionError, OSError):
                    continue
    except (PermissionError, OSError):
        pass

    dirs.sort(key=lambda d: d["name"].lower())
    return {
        "current_path": current_str,
        "parent_path": parent_str,
        "shortcuts": shortcuts,
        "dirs": dirs,
    }


@app.get("/api/fs/dirs")
async def api_fs_dirs(path: str = Query("/")):
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_list_host_directories_sync, path),
            timeout=6.0,
        )
        return result
    except asyncio.TimeoutError:
        return {
            "current_path": path,
            "parent_path": "/",
            "shortcuts": ["/", "/srv/oc/gdrive", "/mnt", "/home", str(BASE_DIR)],
            "dirs": [],
            "warning": "读取目录超时（已受 6 秒防阻塞熔断保护）",
        }
    except Exception as exc:
        return {
            "current_path": str(BASE_DIR),
            "parent_path": str(BASE_DIR.parent),
            "shortcuts": ["/", "/srv/oc/gdrive", "/mnt", "/home", str(BASE_DIR)],
            "dirs": [],
            "warning": str(exc),
        }


# ------------------------------------------------------------------------------
# 7.2 常驻内存书库分页过滤接口、后台扫描状态与实时文件夹漫游接口
# ------------------------------------------------------------------------------
@app.get("/api/library")
async def api_get_library(
    refresh: int = Query(0),
    page: int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=500),
    category: str = Query("all"),
    search: str = Query(""),
    folder: str = Query(""),
    sort: str = Query(""),
):
    cfg = await asyncio.to_thread(load_and_heal_config)

    # 仅当用户显式传入 ?refresh=1 时才重新扫描磁盘/云盘；绝不按 600 秒自动重扫！
    if refresh == 1:
        await asyncio.to_thread(_scan_library_worker)

    result = await asyncio.to_thread(
        _query_memory_library_paginated,
        page,
        page_size,
        category,
        search,
        folder,
        sort,
        cfg,
    )
    return result


@app.get("/api/library/status")
async def api_library_status():
    return dict(SCAN_STATE)


def _browse_directory_sync(path_param: str) -> Dict[str, Any]:
    cfg = load_and_heal_config()
    media_dirs = [Path(d).resolve() for d in cfg.get("media_dirs", []) if _is_dir_accessible(d)]
    if not media_dirs:
        media_dirs = [SAMPLE_DIR.resolve()]

    reading_state = load_reading_state()
    cloud_restricted = cfg.get("cloud_restricted_mode", True)

    cleaned = unquote(path_param or "").strip().strip("/")

    if not cleaned:
        subdirs_map: Dict[str, Dict[str, Any]] = {}
        root_books: List[Dict[str, Any]] = []
        seen_rk = set()

        for mdir in media_dirs:
            try:
                with os.scandir(str(mdir)) as it:
                    entries = sorted(list(it), key=lambda e: e.name.lower())
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in subdirs_map:
                                subdirs_map[entry.name] = {
                                    "name": entry.name,
                                    "path": entry.name,
                                    "rel_path": entry.name,
                                }
                        elif entry.is_file(follow_symlinks=False):
                            if Path(entry.name).suffix.lower() in SUPPORTED_EXTENSIONS:
                                st = entry.stat(follow_symlinks=False)
                                rec = _build_book_record(
                                    Path(entry.path),
                                    st,
                                    mdir,
                                    cloud_restricted=cloud_restricted,
                                )
                                if rec["rel_key"] not in seen_rk:
                                    seen_rk.add(rec["rel_key"])
                                    root_books.append(rec)
                    except Exception:
                        continue
            except Exception:
                continue

        return {
            "current_path": "",
            "rel_path": "",
            "parent_path": None,
            "breadcrumbs": [{"name": "全部文件夹", "path": ""}],
            "dirs": list(subdirs_map.values()),
            "books": _merge_books_with_state(root_books, reading_state, cloud_restricted),
        }

    target_dir: Optional[Path] = None
    matched_mdir: Optional[Path] = None
    rel_path_str = cleaned

    cand_abs = Path(unquote(path_param).strip())
    if cand_abs.is_absolute():
        try:
            res_abs = cand_abs.resolve()
            for mdir in media_dirs:
                try:
                    rel = res_abs.relative_to(mdir)
                    if res_abs.exists() and res_abs.is_dir():
                        target_dir = res_abs
                        matched_mdir = mdir
                        rel_path_str = "" if rel.as_posix() == "." else rel.as_posix()
                        break
                except ValueError:
                    continue
        except Exception:
            pass

    if target_dir is None:
        for mdir in media_dirs:
            try:
                cand = (mdir / cleaned).resolve()
                rel = cand.relative_to(mdir)
                if cand.exists() and cand.is_dir():
                    target_dir = cand
                    matched_mdir = mdir
                    rel_path_str = "" if rel.as_posix() == "." else rel.as_posix()
                    break
            except Exception:
                continue

    if target_dir is None or matched_mdir is None:
        raise HTTPException(status_code=404, detail="目标目录不存在或不在已授权的媒体目录中")

    dirs_list: List[Dict[str, Any]] = []
    books_list: List[Dict[str, Any]] = []

    try:
        with os.scandir(str(target_dir)) as it:
            entries = sorted(list(it), key=lambda e: e.name.lower())
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    sub_rel = (
                        Path(entry.path).resolve().relative_to(matched_mdir).as_posix()
                    )
                    dirs_list.append(
                        {
                            "name": entry.name,
                            "path": sub_rel,
                            "rel_path": sub_rel,
                        }
                    )
                elif entry.is_file(follow_symlinks=False):
                    if Path(entry.name).suffix.lower() in SUPPORTED_EXTENSIONS:
                        st = entry.stat(follow_symlinks=False)
                        rec = _build_book_record(
                            Path(entry.path).resolve(),
                            st,
                            matched_mdir,
                            cloud_restricted=cloud_restricted,
                        )
                        books_list.append(rec)
            except Exception:
                continue
    except Exception:
        pass

    parts = [p for p in rel_path_str.split("/") if p]
    breadcrumbs = [{"name": "全部文件夹", "path": ""}]
    acc = []
    for p in parts:
        acc.append(p)
        breadcrumbs.append({"name": p, "path": "/".join(acc)})

    parent_rel = "/".join(parts[:-1]) if parts else None

    return {
        "current_path": rel_path_str,
        "rel_path": rel_path_str,
        "parent_path": parent_rel,
        "breadcrumbs": breadcrumbs,
        "dirs": dirs_list,
        "books": _merge_books_with_state(books_list, reading_state, cloud_restricted),
    }


@app.get("/api/browse")
async def api_browse(path: str = Query("")):
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_browse_directory_sync, path),
            timeout=6.0,
        )
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="浏览目录超时")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ------------------------------------------------------------------------------
# 7.3 阅读进度与状态更新接口 + 多书签 (Bookmarks) 接口 + 跨设备双向同步接口
# ------------------------------------------------------------------------------
@app.post("/api/state")
async def api_update_state(req: StateUpdateRequest):
    raw_key = req.rel_key or req.path or ""
    if not raw_key:
        raise HTTPException(status_code=400, detail="缺少 path 或 rel_key 参数")

    rel_key, _abs_p, _mdir = await asyncio.to_thread(_normalize_rel_key_and_abs_path, raw_key)
    if not rel_key:
        rel_key = raw_key.strip().lstrip("/")

    entry = await asyncio.to_thread(
        update_reading_state_entry,
        rel_key,
        req.progress,
        req.status,
        req.locator,
        req.bookmarks,
        req.updated_at,
    )
    return {
        "status": "ok",
        "rel_key": rel_key,
        "state": entry,
    }


@app.get("/api/bookmarks")
async def api_get_bookmarks(path: str = Query(...)):
    rel_key, _abs_p, _mdir = await asyncio.to_thread(_normalize_rel_key_and_abs_path, path)
    if not rel_key:
        rel_key = path.strip().lstrip("/")
    state_all = await asyncio.to_thread(load_reading_state)
    entry = state_all.get(rel_key, {}) if isinstance(state_all, dict) else {}
    bookmarks = _normalize_bookmarks_list(entry.get("bookmarks", [])) if isinstance(entry, dict) else []
    return {
        "status": "ok",
        "rel_key": rel_key,
        "bookmarks": bookmarks,
    }


@app.post("/api/bookmarks")
async def api_post_bookmarks(req: BookmarkActionRequest):
    raw_key = req.rel_key or req.path or ""
    if not raw_key:
        raise HTTPException(status_code=400, detail="缺少 path 或 rel_key 参数")

    rel_key, _abs_p, _mdir = await asyncio.to_thread(_normalize_rel_key_and_abs_path, raw_key)
    if not rel_key:
        rel_key = raw_key.strip().lstrip("/")

    action_str = (req.action or "add").strip().lower()
    payload = req.model_dump(exclude_none=True)
    entry, affected = await asyncio.to_thread(
        mutate_book_bookmarks,
        rel_key,
        action_str,
        payload,
    )
    return {
        "status": "ok",
        "action": action_str,
        "rel_key": rel_key,
        "bookmark": affected,
        "bookmarks": entry.get("bookmarks", []),
        "state": entry,
    }


@app.get("/api/sync/state")
async def api_sync_get_state():
    state_data = await asyncio.to_thread(load_reading_state)
    return {
        "status": "ok",
        "timestamp": time.time(),
        "state": state_data,
    }


def _merge_sync_states_sync(incoming_payload: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
    incoming_map = incoming_payload.get("state") if "state" in incoming_payload else incoming_payload
    if not isinstance(incoming_map, dict):
        return (load_reading_state(), 0)

    updated_count = 0
    with STATE_LOCK:
        local_state = load_reading_state()
        for rk, rem_val in incoming_map.items():
            if not isinstance(rk, str) or not isinstance(rem_val, dict):
                continue
            rem_ts = float(rem_val.get("updated_at", 0.0) or 0.0)
            loc_val = local_state.get(rk)
            loc_ts = (
                float(loc_val.get("updated_at", 0.0) or 0.0)
                if isinstance(loc_val, dict)
                else -1.0
            )
            loc_bms = (
                _normalize_bookmarks_list(loc_val.get("bookmarks", []))
                if isinstance(loc_val, dict)
                else []
            )
            rem_bms = _normalize_bookmarks_list(rem_val.get("bookmarks", []))

            if rem_ts > loc_ts:
                merged_bms = _normalize_bookmarks_list(rem_bms + loc_bms) if rem_bms else loc_bms
                local_state[rk] = {
                    "progress": rem_val.get("progress", 0),
                    "status": rem_val.get("status", "new"),
                    "locator": rem_val.get("locator", ""),
                    "bookmarks": merged_bms,
                    "updated_at": rem_ts,
                }
                updated_count += 1
            elif isinstance(loc_val, dict) and rem_bms:
                merged_bms = _normalize_bookmarks_list(loc_bms + rem_bms)
                if len(merged_bms) != len(loc_bms):
                    loc_val["bookmarks"] = merged_bms
                    local_state[rk] = loc_val
                    updated_count += 1

        if updated_count > 0:
            _atomic_write_json(STATE_FILE, local_state)

        return (local_state, updated_count)


@app.post("/api/sync/merge")
async def api_sync_merge(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    merged_state, updated_count = await asyncio.to_thread(_merge_sync_states_sync, payload)
    return {
        "status": "ok",
        "updated_count": updated_count,
        "total_keys": len(merged_state),
        "state": merged_state,
    }


def _trigger_peer_sync_sync(override_url: Optional[str] = None) -> Dict[str, Any]:
    cfg = load_and_heal_config()
    peer_url = (override_url or cfg.get("peer_sync_url") or "").strip().rstrip("/")
    if not peer_url:
        return {
            "status": "skipped",
            "message": "未配置对端同步服务器 URL (peer_sync_url)",
            "updated_local": 0,
        }

    try:
        req_get = urllib.request.Request(
            f"{peer_url}/api/sync/state",
            headers={"Accept": "application/json"},
            method="GET",
        )
        with urllib.request.urlopen(req_get, timeout=5.0) as resp:
            peer_data = json.loads(resp.read().decode("utf-8"))

        merged_state, updated_local = _merge_sync_states_sync(peer_data)

        push_body = json.dumps({"state": merged_state}).encode("utf-8")
        req_post = urllib.request.Request(
            f"{peer_url}/api/sync/merge",
            data=push_body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_post, timeout=5.0) as resp_post:
            peer_merge_res = json.loads(resp_post.read().decode("utf-8"))

        return {
            "status": "ok",
            "peer_url": peer_url,
            "updated_local": updated_local,
            "updated_remote": peer_merge_res.get("updated_count", 0),
            "total_synced_keys": len(merged_state),
        }
    except Exception as exc:
        return {
            "status": "error",
            "peer_url": peer_url,
            "message": f"连接对端服务器失败: {exc}",
        }


@app.post("/api/sync/trigger")
async def api_sync_trigger(req: Optional[SyncTriggerRequest] = None):
    override_url = req.peer_sync_url if req else None
    result = await asyncio.to_thread(_trigger_peer_sync_sync, override_url)
    return result


# ------------------------------------------------------------------------------
# 7.4 封面接口：被动读取、EPUB 4秒超时提取、前端 PDF 封面上传
# ------------------------------------------------------------------------------
@app.get("/api/cover")
async def api_get_cover(path: str = Query(...)):
    rel_key, abs_path, _mdir = await asyncio.to_thread(_normalize_rel_key_and_abs_path, path)
    if not rel_key:
        raise HTTPException(status_code=404, detail="未找到封面")

    cover_file = await asyncio.to_thread(_find_cached_or_sidecar_cover, rel_key, abs_path)
    if cover_file is None or not cover_file.exists():
        raise HTTPException(status_code=404, detail="本地暂无缓存封面")

    media_type = "image/png" if cover_file.suffix.lower() == ".png" else "image/jpeg"
    return FileResponse(
        path=str(cover_file),
        media_type=media_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.post("/api/cover/extract")
async def api_extract_cover(req: CoverExtractRequest):
    rel_key, abs_path, _mdir = await asyncio.to_thread(_normalize_rel_key_and_abs_path, req.path)
    if not rel_key or abs_path is None or not abs_path.exists():
        raise HTTPException(status_code=404, detail="目标书籍文件不存在")

    existing = await asyncio.to_thread(_find_cached_or_sidecar_cover, rel_key, abs_path)
    if existing is not None:
        _mark_cover_in_memory_cache(rel_key)
        return {
            "status": "ok",
            "cached": True,
            "has_cover": True,
            "cover_url": f"/api/cover?path={quote(rel_key)}",
        }

    ext_low = abs_path.suffix.lower()
    if ext_low not in (".epub", ".chm"):
        return {"status": "ok", "has_cover": False}

    try:
        img_bytes = await asyncio.wait_for(
            asyncio.to_thread(
                _extract_epub_cover_bytes if ext_low == ".epub" else extract_chm_cover_bytes,
                abs_path if ext_low == ".epub" else str(abs_path),
            ),
            timeout=6.0,
        )
        if img_bytes:
            saved = await asyncio.to_thread(_save_cover_cache, rel_key, img_bytes)
            if saved is not None:
                return {
                    "status": "ok",
                    "cached": False,
                    "has_cover": True,
                    "cover_url": f"/api/cover?path={quote(rel_key)}&t={int(time.time())}",
                }
    except asyncio.TimeoutError:
        return {
            "status": "timeout",
            "has_cover": False,
            "message": "提取封面超过 4 秒，已触发超时熔断保护",
        }
    except Exception as exc:
        return {"status": "error", "has_cover": False, "message": str(exc)}

    return {"status": "ok", "has_cover": False}


def _parse_multipart_cover_payload(
    raw_body: bytes, content_type: str
) -> Tuple[Optional[str], Optional[bytes]]:
    """无第三方依赖解析 multipart/form-data 中的 path 字段与图片二进制块。"""
    m = re.search(r"boundary=([^\s;]+)", content_type)
    if not m:
        return (None, None)
    boundary = m.group(1).strip('"').encode("utf-8", errors="ignore")
    parts = raw_body.split(b"--" + boundary)
    extracted_path: Optional[str] = None
    extracted_bytes: Optional[bytes] = None

    for part in parts:
        if not part or part in (b"--\r\n", b"--", b"\r\n"):
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_block, body_block = part.split(b"\r\n\r\n", 1)
        if body_block.endswith(b"\r\n"):
            body_block = body_block[:-2]
        header_str = header_block.decode("utf-8", errors="ignore")
        if 'name="path"' in header_str:
            extracted_path = body_block.decode("utf-8", errors="ignore").strip()
        elif "filename=" in header_str or 'name="file"' in header_str or "image/" in header_str:
            extracted_bytes = body_block

    return (extracted_path, extracted_bytes)


@app.post("/api/cover/upload")
async def api_upload_cover(
    request: Request,
    path: Optional[str] = Query(None),
):
    target_path_param = path
    img_bytes: Optional[bytes] = None

    content_type = request.headers.get("content-type", "")
    raw_body = await request.body()

    if "multipart/form-data" in content_type:
        mp_path, mp_bytes = _parse_multipart_cover_payload(raw_body, content_type)
        if not target_path_param and mp_path:
            target_path_param = mp_path
        if mp_bytes:
            img_bytes = mp_bytes
    elif "application/json" in content_type:
        try:
            body_json = json.loads(raw_body.decode("utf-8"))
            if not target_path_param:
                target_path_param = body_json.get("path")
            b64_str = body_json.get("image") or body_json.get("data") or ""
            if isinstance(b64_str, str) and b64_str:
                if "," in b64_str:
                    b64_str = b64_str.split(",", 1)[1]
                img_bytes = base64.b64decode(b64_str)
        except Exception:
            pass
    else:
        if raw_body:
            img_bytes = raw_body

    if not target_path_param or not img_bytes:
        raise HTTPException(status_code=400, detail="缺少 path 参数或封面图片数据")

    rel_key, _abs_p, _mdir = await asyncio.to_thread(
        _normalize_rel_key_and_abs_path, target_path_param
    )
    if not rel_key:
        rel_key = target_path_param.strip().lstrip("/")

    saved = await asyncio.to_thread(_save_cover_cache, rel_key, img_bytes)
    if saved is None:
        raise HTTPException(status_code=500, detail="保存封面文件失败")

    return {
        "status": "ok",
        "rel_key": rel_key,
        "has_cover": True,
        "cover_url": f"/api/cover?path={quote(rel_key)}&t={int(time.time())}",
    }


# ------------------------------------------------------------------------------
# 7.5 TXT / UMD / MD 按章切片阅读接口与 Range (206) 精准 64KB 流式接口
# ------------------------------------------------------------------------------
@app.get("/api/read/text")
async def api_read_text(
    path: str = Query(...),
    chapter: int = Query(0, ge=0),
):
    rel_key, abs_path, _mdir = await asyncio.to_thread(_normalize_rel_key_and_abs_path, path)
    if abs_path is None or not abs_path.exists() or not abs_path.is_file():
        raise HTTPException(status_code=404, detail="目标文档不存在")

    try:
        book_data = await asyncio.wait_for(
            asyncio.to_thread(_parse_and_cache_text_book, abs_path),
            timeout=45.0,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="读取文档超时（15秒熔断保护）")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"解析文档失败: {exc}")

    chapters = book_data.get("chapters", [])
    total_chapters = len(chapters)
    if total_chapters == 0:
        return {
            "rel_key": rel_key,
            "title": abs_path.stem,
            "author": "未知",
            "encoding": "utf-8",
            "ext": abs_path.suffix.lower().lstrip("."),
            "total_chapters": 1,
            "current_chapter": 0,
            "chapter_title": abs_path.stem,
            "content": "（文档无内容）",
            "chapters": [{"index": 0, "title": abs_path.stem}],
            "toc": [],
        }

    safe_idx = max(0, min(chapter, total_chapters - 1))
    current_ch = chapters[safe_idx]

    return {
        "rel_key": rel_key,
        "title": book_data.get("title", abs_path.stem),
        "author": book_data.get("author", ""),
        "encoding": book_data.get("encoding", "utf-8"),
        "ext": book_data.get("ext", abs_path.suffix.lower().lstrip(".")),
        "total_chapters": total_chapters,
        "current_chapter": safe_idx,
        "chapter_title": current_ch.get("title", f"第 {safe_idx + 1} 章"),
        "content": current_ch.get("content", ""),
        "chapters": [
            {"index": i, "title": ch.get("title", f"第 {i + 1} 章")}
            for i, ch in enumerate(chapters)
        ],
        "toc": book_data.get("toc", []),
    }


def _guess_mime_type(file_path: Path) -> str:
    ext = file_path.suffix.lower()
    if ext == ".pdf":
        return "application/pdf"
    if ext == ".epub":
        return "application/epub+zip"
    if ext == ".md":
        return "text/markdown; charset=utf-8"
    if ext == ".txt":
        return "text/plain; charset=utf-8"
    if ext == ".chm":
        return "application/vnd.ms-htmlhelp"
    guessed, _enc = mimetypes.guess_type(str(file_path))
    if guessed:
        return guessed
    return "application/octet-stream"


def _stream_file_range(
    file_path: Path, start: int, end: int, chunk_size: int = 65536
) -> Generator[bytes, None, None]:
    """
    严格使用 f.seek(start) 并按 65536 (64KB) 块大小循环分片读取，
    严禁一次性读取整个大文件到内存！
    """
    remaining = end - start + 1
    with open(file_path, "rb") as f:
        f.seek(start)
        while remaining > 0:
            read_size = min(chunk_size, remaining)
            chunk = f.read(read_size)
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@app.get("/api/raw")
async def api_get_raw(request: Request, path: str = Query(...)):
    _rel_key, abs_path, _mdir = await asyncio.to_thread(_normalize_rel_key_and_abs_path, path)
    if abs_path is None or not abs_path.exists() or not abs_path.is_file():
        raise HTTPException(status_code=404, detail="目标文件不存在")

    file_size = abs_path.stat().st_size
    media_type = _guess_mime_type(abs_path)
    encoded_filename = quote(abs_path.name)

    range_header = request.headers.get("range") or request.headers.get("Range")
    common_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"inline; filename*=UTF-8''{encoded_filename}",
    }

    if range_header:
        m = re.match(r"bytes=(\d*)-(\d*)", range_header.strip())
        if m:
            start_str, end_str = m.group(1), m.group(2)
            if start_str == "" and end_str != "":
                suffix_len = int(end_str)
                start = max(0, file_size - suffix_len)
                end = file_size - 1
            else:
                start = int(start_str) if start_str else 0
                end = int(end_str) if end_str else file_size - 1

            if start >= file_size or start < 0:
                return Response(
                    status_code=416,
                    headers={"Content-Range": f"bytes */{file_size}", **common_headers},
                )

            end = min(end, file_size - 1)
            content_length = end - start + 1
            headers = {
                **common_headers,
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Content-Length": str(content_length),
            }
            return StreamingResponse(
                _stream_file_range(abs_path, start, end, 65536),
                status_code=206,
                media_type=media_type,
                headers=headers,
            )

    if file_size == 0:
        return Response(content=b"", status_code=200, media_type=media_type, headers=common_headers)

    headers = {
        **common_headers,
        "Content-Length": str(file_size),
    }
    return StreamingResponse(
        _stream_file_range(abs_path, 0, file_size - 1, 65536),
        status_code=200,
        media_type=media_type,
        headers=headers,
    )


# ------------------------------------------------------------------------------
# 7.6 静态文件服务与首页入口
# ------------------------------------------------------------------------------
@app.get("/")
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file), media_type="text/html; charset=utf-8")
    return JSONResponse(
        {
            "app": "R-Book",
            "status": "running",
            "message": "Backend API ready on port 8001.",
        }
    )


# ==============================================================================
# CHM「原版网页模式」：保留 CHM 自带的图片导航主页与超链接结构，在 iframe 中原样浏览
# ==============================================================================
CHM_VIEW_PREFIX = "/api/chm/view/"


def _chm_token_encode(rel_key: str) -> str:
    return base64.urlsafe_b64encode(rel_key.encode("utf-8")).decode("ascii").rstrip("=")


def _chm_token_decode(token: str) -> str:
    pad = "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode((token + pad).encode("ascii")).decode("utf-8")


def _resolve_chm_abs_path(path_param: str) -> Tuple[str, Path]:
    rel_key, abs_path, _mdir = _normalize_rel_key_and_abs_path(path_param)
    if abs_path is None or not abs_path.exists() or not abs_path.is_file() or abs_path.suffix.lower() != ".chm":
        raise HTTPException(status_code=404, detail="目标 CHM 文件不存在")
    return rel_key, abs_path


@app.get("/api/chm/info")
async def api_chm_info(path: str = Query(...)):
    rel_key, abs_path = await asyncio.to_thread(_resolve_chm_abs_path, path)
    try:
        info = await asyncio.wait_for(asyncio.to_thread(chm_web_info, str(abs_path)), timeout=60.0)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="打开 CHM 超时（云盘文件较大时请稍后重试）")
    if not info or not info.get("home"):
        raise HTTPException(status_code=422, detail="该 CHM 未找到可浏览的网页首页")

    is_garbage_toc = bool(info.get("is_garbage_toc"))

    html_count = int(info.get("html_count") or 0)
    toc_count = int(info.get("toc_count") or 0)

    # 纯文本章节 <-> 原始网页 互相定位映射（优先复用文本解析 LRU 缓存，未命中时在非原版模式下快速尝试，绝不阻塞网页端）
    chapter_map: Dict[str, int] = {}
    try:
        cached_book = None
        with TEXT_LRU_LOCK:
            cached_book = TEXT_BOOK_LRU.get((str(abs_path), abs_path.stat().st_mtime))
        if cached_book is not None:
            for ch in cached_book.get("chapters", []):
                src = ch.get("src")
                if src and src not in chapter_map:
                    chapter_map[src] = int(ch.get("index", 0))
        elif not is_garbage_toc:
            book_data = await asyncio.wait_for(
                asyncio.to_thread(_parse_and_cache_text_book, abs_path), timeout=3.0
            )
            for ch in book_data.get("chapters", []):
                src = ch.get("src")
                if src and src not in chapter_map:
                    chapter_map[src] = int(ch.get("index", 0))
    except Exception:
        pass

    covered = max(len(chapter_map), toc_count)
    # 正文页面中有大量页面未被目录/章节收录 或 目录全为无意义占位符 => 自动推荐原版网页模式
    recommend_web = (html_count >= 4 and covered < html_count * 0.7) or is_garbage_toc

    token = _chm_token_encode(rel_key)
    return {
        "rel_key": rel_key,
        "base": CHM_VIEW_PREFIX + token,
        "home": info["home"],
        "toc": info.get("toc", []),
        "html_count": html_count,
        "image_count": int(info.get("image_count") or 0),
        "chapter_map": chapter_map,
        "recommend_web": recommend_web,
    }


@app.get("/api/chm/view/{token}/{inner:path}")
async def api_chm_view(request: Request, token: str, inner: str = ""):
    try:
        rel_key = _chm_token_decode(token)
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 CHM 令牌")
    _rk, abs_path = await asyncio.to_thread(_resolve_chm_abs_path, rel_key)

    # 优先使用原始（未解码）请求路径：兼容老 CHM 里 GBK/Big5 百分号编码的超链接
    inner_ref = "/" + inner.lstrip("/")
    try:
        raw_path = request.scope.get("raw_path") or b""
        raw_str = raw_path.decode("latin-1")
        marker = CHM_VIEW_PREFIX + token + "/"
        pos = raw_str.find(marker)
        if pos >= 0:
            raw_inner = raw_str[pos + len(marker):].split("?", 1)[0]
            if "%" in raw_inner:
                try:
                    inner_ref = "/" + unquote(raw_inner, encoding="utf-8", errors="strict").lstrip("/")
                except Exception:
                    inner_ref = "/" + raw_inner.lstrip("/")
    except Exception:
        pass

    base_prefix = CHM_VIEW_PREFIX + token
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(chm_read_member, str(abs_path), inner_ref, base_prefix), timeout=60.0
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="读取 CHM 页面超时")
    if result is None:
        return Response(
            content=f"<html><head><meta charset='utf-8'></head><body style='font-family:sans-serif;padding:2em;color:#888'>"
            f"CHM 内未找到页面：{inner_ref}</body></html>".encode("utf-8"),
            status_code=404,
            media_type="text/html; charset=utf-8",
        )
    body, ctype = result
    return Response(content=body, media_type=ctype, headers={"Cache-Control": "private, max-age=3600"})


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)
