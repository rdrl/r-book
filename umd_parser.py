import os
import re
import struct
import zlib
from pathlib import Path
from typing import Dict, Any, List


UMD_MAGIC = b"\x89\x9b\x9a\xde"
_SENTENCE_END_CHARS = frozenset("。！？!?….”’\"'」』》】>：:;；—~～")
_CHAPTER_HEAD_RE = re.compile(
    r"^(?:第[0-9零一二三四五六七八九十百千万两]+[章节回卷部篇节话]|Chapter\s+\d+|序[章言]?|楔子|尾声|番外)",
    re.IGNORECASE,
)


def _normalize_umd_text(raw: str) -> str:
    """合并塞班时代 UMD 定宽硬回车断行，保留真正段落边界与缩进分段。"""
    normalized = (
        raw.replace("\u2029", "\n")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .strip("\x00")
    )
    raw_lines = normalized.split("\n")
    paragraphs: List[str] = []
    cur = ""

    for raw_line in raw_lines:
        has_indent = raw_line.startswith(("\u3000\u3000", "  ", "\t"))
        line = raw_line.strip(" \t\u3000\xa0")
        if not line:
            if cur:
                paragraphs.append(cur)
                cur = ""
            continue

        if not cur:
            cur = line
            continue

        last_ch = cur[-1]
        is_list_or_heading = (
            has_indent
            or line.startswith(("•", "·", "-", "*", "—", "【", "（", "("))
            or bool(_CHAPTER_HEAD_RE.match(line))
        )
        # 仅当上一行较长、未以句末标点结尾、且当前行非新段落开头时，无缝拼接回同一自然段
        if len(cur) >= 10 and last_ch not in _SENTENCE_END_CHARS and not is_list_or_heading:
            joiner = " " if (last_ch.isascii() and last_ch.isalnum() and line[0].isascii() and line[0].isalnum()) else ""
            cur = cur + joiner + line
        else:
            paragraphs.append(cur)
            cur = line

    if cur:
        paragraphs.append(cur)
    return "\n".join(paragraphs).strip()


def _fallback_umd_result(file_path: str, reason: str) -> Dict[str, Any]:
    name = Path(file_path).stem if file_path else "未知UMD书籍"
    return {
        "title": name,
        "author": "未知作者",
        "encoding": "umd-binary",
        "chapters": [
            {
                "index": 0,
                "title": "文档解析提示",
                "content": (
                    f"【UMD 容错保护】无法完整解析《{name}》。\n\n"
                    f"原因说明：{reason}\n\n"
                    "系统已自动拦截异常以保护服务稳定运行。请确认该文件是否为标准未损坏的 UMD 电子书格式。"
                ),
            }
        ],
    }


def parse_umd(file_path: str) -> Dict[str, Any]:
    """
    高容错纯 Python UMD 二进制电子书解析器。
    - 校验魔数 \\x89\\x9b\\x9a\\xde
    - 解析 0x02 标题、0x03 作者 (utf-16-le, errors='replace')
    - 解析 0x83 章节偏移与 0x84 章节标题/正文数据块
    - 每个数据块的 zlib.decompress 均单独包裹在 try...except zlib.error 中
    - 最外层包裹 try...except Exception，遇损坏文件返回友好提示单章，绝不抛 500 错误
    """
    try:
        path_obj = Path(file_path)
        if not path_obj.exists() or not path_obj.is_file():
            return _fallback_umd_result(file_path, "目标文件不存在或不是有效普通文件。")

        file_size = path_obj.stat().st_size
        if file_size < 9:
            return _fallback_umd_result(file_path, "文件体积过小，不是合法的 UMD 文件。")

        with open(path_obj, "rb") as f:
            data = f.read()

        if not data.startswith(UMD_MAGIC):
            return _fallback_umd_result(
                file_path,
                f"文件头部魔数校验失败（期望 {UMD_MAGIC.hex()}，实际 {data[:4].hex()}）。",
            )

        pos = 4
        total_len = len(data)

        title = path_obj.stem
        author = "未知作者"
        chapter_offsets: List[int] = []
        chapter_titles: List[str] = []
        raw_text_chunks: List[bytes] = []

        last_func_id = 0

        while pos < total_len:
            marker = data[pos : pos + 1]

            if marker == b"#":
                if pos + 5 > total_len:
                    break
                chunk_type, _flag, chunk_len = struct.unpack("<HBB", data[pos + 1 : pos + 5])
                if chunk_len < 5:
                    pos += 5
                    continue
                payload_start = pos + 5
                payload_end = min(total_len, pos + chunk_len)
                payload = data[payload_start:payload_end]
                pos = payload_end
                last_func_id = chunk_type

                if chunk_type == 0x02 and payload:
                    decoded_title = payload.decode("utf-16-le", errors="replace").strip("\x00").strip()
                    if decoded_title:
                        title = decoded_title
                elif chunk_type == 0x03 and payload:
                    decoded_author = payload.decode("utf-16-le", errors="replace").strip("\x00").strip()
                    if decoded_author:
                        author = decoded_author

            elif marker == b"$":
                if pos + 9 > total_len:
                    break
                _rand_id, block_len = struct.unpack("<II", data[pos + 1 : pos + 9])
                if block_len < 9:
                    pos += 9
                    continue
                payload_start = pos + 9
                payload_end = min(total_len, pos + block_len)
                block_payload = data[payload_start:payload_end]
                pos = payload_end

                if last_func_id == 0x83:
                    # 章节偏移表 (每个偏移为 4 字节 uint32，代表 UTF-16LE 字节偏移)
                    count = len(block_payload) // 4
                    if count > 0:
                        chapter_offsets = list(struct.unpack(f"<{count}I", block_payload[: count * 4]))
                    last_func_id = 0

                elif last_func_id == 0x84:
                    # 0x84 后跟随的 $ 块可能是章节标题表，也可能是 zlib 压缩正文块
                    # 先尝试检测是否为 zlib 压缩数据块
                    decompressed = None
                    try:
                        decompressed = zlib.decompress(block_payload)
                    except zlib.error:
                        decompressed = None

                    if decompressed is not None and chapter_titles:
                        raw_text_chunks.append(decompressed)
                    else:
                        # 解析章节标题表: [1字节长度][UTF-16LE字节]
                        t_pos = 0
                        parsed_titles: List[str] = []
                        while t_pos < len(block_payload):
                            t_len = block_payload[t_pos]
                            t_pos += 1
                            if t_len == 0 or t_pos + t_len > len(block_payload):
                                break
                            ch_title = (
                                block_payload[t_pos : t_pos + t_len]
                                .decode("utf-16-le", errors="replace")
                                .strip("\x00")
                                .strip()
                            )
                            parsed_titles.append(ch_title or f"第 {len(parsed_titles) + 1} 章")
                            t_pos += t_len

                        if parsed_titles:
                            chapter_titles.extend(parsed_titles)
                        elif decompressed is not None:
                            raw_text_chunks.append(decompressed)
                    last_func_id = 0

                elif last_func_id in (0x81, 0x82, 0x87, 0x0A, 0x0C):
                    # 封面、页眉索引或其他非正文附加块，但若其中包含可解压正文也安全跳过
                    last_func_id = 0

                else:
                    # 正文压缩数据块，每个数据块单独 try...except zlib.error
                    try:
                        decompressed = zlib.decompress(block_payload)
                        if decompressed:
                            raw_text_chunks.append(decompressed)
                    except zlib.error:
                        pass
                    last_func_id = 0
            else:
                # 遇到未知字节偏移时向前步进，防止死循环
                pos += 1

        full_bytes = b"".join(raw_text_chunks)
        if not full_bytes:
            return _fallback_umd_result(file_path, "未能在文件中提取到有效的正文压缩数据块。")

        chapters: List[Dict[str, Any]] = []
        if chapter_offsets:
            num_chapters = len(chapter_offsets)
            for idx in range(num_chapters):
                start_byte = chapter_offsets[idx]
                end_byte = chapter_offsets[idx + 1] if idx + 1 < num_chapters else len(full_bytes)
                start_byte = max(0, min(start_byte, len(full_bytes)))
                end_byte = max(start_byte, min(end_byte, len(full_bytes)))
                # 确保按双字节对齐解码 UTF-16LE
                if (end_byte - start_byte) % 2 != 0:
                    end_byte -= 1
                chunk_bytes = full_bytes[start_byte:end_byte]
                text = _normalize_umd_text(chunk_bytes.decode("utf-16-le", errors="replace"))
                ch_title = (
                    chapter_titles[idx]
                    if idx < len(chapter_titles) and chapter_titles[idx]
                    else f"第 {idx + 1} 章"
                )
                chapters.append(
                    {
                        "index": idx,
                        "title": ch_title,
                        "content": text or "（本章无正文内容）",
                    }
                )
        else:
            # 无章节偏移表时按固定字数切片
            full_text = _normalize_umd_text(full_bytes.decode("utf-16-le", errors="replace"))
            if not full_text:
                return _fallback_umd_result(file_path, "解压后的正文内容为空。")
            chunk_size = 6000
            for idx, i in enumerate(range(0, len(full_text), chunk_size)):
                chapters.append(
                    {
                        "index": idx,
                        "title": f"第 {idx + 1} 节",
                        "content": full_text[i : i + chunk_size],
                    }
                )

        if not chapters:
            return _fallback_umd_result(file_path, "未能生成有效章节列表。")

        return {
            "title": title,
            "author": author,
            "encoding": "umd-utf16le",
            "chapters": chapters,
        }

    except Exception as exc:
        return _fallback_umd_result(file_path, f"解析过程触发异常保护: {type(exc).__name__}: {exc}")
