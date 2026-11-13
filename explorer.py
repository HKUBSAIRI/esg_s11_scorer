# -*- coding: utf-8 -*-
"""段落浏览器：上传一份 PDF → 看见「每个指标对应的段落」。

它回答的问题只有一个
--------------------
打分表里 S1.1.1 给了 50 分，是哪一段话支撑的？这份报告到底写没写「年休假执行数据」？
把报告丢进来，左边选指标，右边就是**带页码的原文段落 + 命中的检索词**，点一下跳到 PDF 那一页。

与打分管道的边界
----------------
- 文本解析：``s11_text.load_pdf``（与 CLI 同一份实现，不含 OCR）
- 段落切分：``s11_text.sentences``（同一套 380 字分块）
- 召回：``qualitative.recall``（同一份词表、同一条排序：命中数 → 是否带数据 → 页码）
- 数值：``extract.extract``（流失率/薪酬差距等，带页码与方法）

**不做的**：不判档、不算分、不写 ``build/``、不碰 ``good_sample/``。这里只做「证据可视」，
所以同一份报告在页面里看到的段落，与 CLI 召回给模型/兜底的段落是同一批。

启动（工作目录＝``50-应用``）::

    python -m esg_s11_scorer.explorer               # http://127.0.0.1:8765
    python -m esg_s11_scorer.explorer --port 9000 --open
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import threading
import time
import webbrowser
from collections import OrderedDict
from functools import lru_cache
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from s11_text import PDFLoadError, load_pdf

from . import keywords_io
from .config import IND_NAMES, IND_WEIGHTS, MODEL_VERSION, SPECS
from .extract import extract
from .qualitative import match_word, recall
from .rubric import MATCH_VERSION, RUBRIC_VERSION, RUBRICS

WEB_DIR = Path(__file__).with_name("web")
INDEX_FILE = "explorer.html"

MAX_UPLOAD = 120 * 1024 * 1024     # 单份 120 MB：本批最大的报告 44 MB，留足余量
MAX_PASSAGES = 40                  # 单指标最多返回的段落（页面里明说上限）
CACHE_SIZE = 6                     # 按 sha256 缓存最近几份的解析结果（同一份重复打开秒开）

FIELD_CN = {
    "turnover": "员工流失率",
    "leavers": "离职人数",
    "headcount": "员工总数",
    "gender_gap": "性别薪酬差距（中位）",
    "gender_gap_avg": "性别薪酬差距（平均）",
    "cb": "集体谈判覆盖率",
    "female_pct": "女性员工占比",
}

METHOD_CN = {
    "table:year-align": "表格 · 年份对齐",
    "table:single": "表格 · 单值",
    "sentence:bracket": "正文 · 括注上期",
    "sentence:en": "正文 · 英文句式",
    "sentence:cn": "正文 · 中文句式",
    "sentence": "正文 · 句式",
}

_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_LOCK = threading.Lock()


# ---------------------------------------------------------------- 分析
@lru_cache(maxsize=4096)
def _word_pattern(word: str) -> re.Pattern[str]:
    """英文按词首、中文按子串——与 ``qualitative._head_pattern`` 同一条规则。"""
    head = word[:1]
    prefix = r"(?<![A-Za-z0-9])" if head.isascii() and head.isalpha() else ""
    return re.compile(prefix + re.escape(word), re.IGNORECASE)


def _occurrences(text: str, word: str) -> list[tuple[int, int]]:
    """``word`` 在 ``text`` 里的出现区间（同一段可能命中多次）。"""
    if not word:
        return []
    return [(m.start(), m.end()) for m in _word_pattern(word).finditer(text)]


def _highlight(text: str, words: list[str]) -> list[list]:
    """把命中的词合并成不重叠的区间：``[[start, end, word], …]``，前端直接包 ``<mark>``。

    区间合并是为了「employee benefit」与「benefit」同时命中时不产生嵌套标签；
    合并后保留先出现（较长）的那个词，展示上更好读。
    """
    spans: list[tuple[int, int, str]] = []
    for w in words:
        spans.extend((s, e, w) for s, e in _occurrences(text, w))
    spans.sort(key=lambda s: (s[0], -s[1]))
    merged: list[list] = []
    for s, e, w in spans:
        if merged and s < merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
            continue
        merged.append([s, e, w])
    return merged


def _indicator(rub, passages: list) -> dict:
    """一个定性命中块（S1.1.x-C 或整项）→ 页面要的扁平结构。"""
    word_group: dict[str, str] = {}
    group_words: dict[str, tuple[str, ...]] = {}
    for group, ws in rub.groups:
        group_words[group] = tuple(ws)
        for w in ws:
            word_group.setdefault(w, group)

    rows: list[dict] = []
    group_hits: dict[str, set[str]] = {g: set() for g in group_words}
    group_passages: dict[str, int] = {g: 0 for g in group_words}
    for p in passages:
        low = p.text.lower()
        hit = [w for w in word_group if match_word(low, w)]
        if not hit:
            continue
        groups: list[str] = []
        for w in hit:
            g = word_group[w]
            group_hits[g].add(w)
            if g not in groups:
                groups.append(g)
        for g in groups:
            group_passages[g] += 1
        rows.append({
            "page": p.page,
            "text": p.text,
            "hits": len(hit),
            "words": hit,
            "groups": groups,
            "spans": _highlight(p.text, hit),
        })

    spec = next((s for s in SPECS if s.ind == rub.ind), None)
    return {
        "key": rub.key,
        "ind": rub.ind,
        "part": rub.part,
        "name": rub.name,
        "ind_name": IND_NAMES.get(rub.ind, rub.ind),
        "weight": IND_WEIGHTS.get(rub.ind),
        "kind": spec.kind if spec else "",
        "tiers": {str(t): rubric for t, rubric in rub.tiers.items()},
        "note": rub.note,
        "gate": rub.gate,
        "groups": [
            {"name": g, "words": len(group_words[g]), "list": list(group_words[g]),
             "hits": len(group_hits[g]), "passages": group_passages[g]}
            for g in group_words
        ],
        "n_passages": len(rows),
        "truncated": len(passages) >= MAX_PASSAGES,
        "passages": rows,
    }


def _facts(doc, filename: str) -> dict:
    ex = extract(doc, filename=filename)
    best = {id(f) for y in ex.best.values() for f in y.values()}
    rows = []
    for f in ex.facts:
        row = f.to_dict()
        row["label"] = FIELD_CN.get(f.field, f.field)
        row["unit"] = "人" if f.field in ("leavers", "headcount") else "%"
        row["method_cn"] = METHOD_CN.get(f.method, f.method)
        row["best"] = id(f) in best
        rows.append(row)
    return {
        "report_year": ex.report_year,
        "denom_note": ex.denom_note,
        "warnings": ex.warnings,
        "facts": rows,
    }


def _quality_for_view(doc) -> dict:
    """``doc.quality()`` 的展示版：把「中文层不可用」从告警降为语种说明。

    纯英文报告里偶尔有几个汉字（本批 00013 只有 2 个），``s11_text.quality()`` 会因此
    给出「中文层不可用，建议走英文层判分」——那是对**判分选层**的提醒，对「看段落」是噪声：
    页面只需要知道这是英文报告，以及文本层是否可读。等级只在没有别的原因时才上调。
    """
    q = doc.quality()
    ratio = q.get("cjk_ratio") or 0.0
    q["lang"] = "英文" if ratio < 0.02 else ("中文" if ratio >= 0.5 else "中英混排")
    if ratio < 0.02:
        kept = [r for r in q.get("reasons", []) if not r.startswith("中文层不可用")]
        if kept != q.get("reasons"):
            q["reasons"] = kept
            if not kept:
                q["level"] = "好"
    return q


def analyze(payload: bytes, filename: str = "") -> dict:
    """整条分析：字节 → 页面用的 JSON。失败抛 ``PDFLoadError``（消息可直接展示）。"""
    digest = hashlib.sha256(payload).hexdigest()
    # 缓存键带上文件名：报告年度先从文件名里的 FY20xx 认，同一份文件换个名字上传
    # 可能得到不同年度（``extract.detect_report_year``），只按内容哈希缓存会串年度。
    key = f"{digest}:{filename}"
    with _LOCK:
        hit = _CACHE.get(key)
        if hit is not None:
            _CACHE.move_to_end(key)
            out = dict(hit)
            out["file"] = dict(hit["file"], cached=True)
            return out

    t0 = time.perf_counter()
    doc = load_pdf(payload, name=filename)
    t1 = time.perf_counter()

    indicators = [_indicator(rub, recall(doc, rub, limit=MAX_PASSAGES))
                  for rub in RUBRICS.values()]
    t2 = time.perf_counter()
    try:
        facts = _facts(doc, filename)
    except Exception as exc:        # 数值抽取出问题不该拖垮「看段落」这个主功能
        facts = {"report_year": 0, "denom_note": "", "facts": [],
                 "warnings": [f"数值抽取失败：{type(exc).__name__}: {exc}"]}
    t3 = time.perf_counter()

    quality = _quality_for_view(doc)
    out = {
        "schema": 1,
        "file": {
            "name": filename or doc.path or "<upload>",
            "pages": len(doc.pages),
            "bytes": doc.meta.get("bytes", len(payload)),
            "chars": doc.meta.get("cleaned_chars", len(doc.text)),
            "sha256": digest,
            "quality": quality,
            "parse_seconds": round(t1 - t0, 2),
            "match_seconds": round(t2 - t1, 2),
            "facts_seconds": round(t3 - t2, 2),
            "cached": False,
        },
        "version": {
            "model": MODEL_VERSION,
            "rubric": RUBRIC_VERSION,
            "match": MATCH_VERSION,
            "keyword_source": keywords_io.describe_source(),
            "keywords": sum(len(ws) for rub in RUBRICS.values() for _, ws in rub.groups),
        },
        "indicators": indicators,
        "facts": facts,
    }
    with _LOCK:
        _CACHE[key] = out
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)
    return out


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "esg-s11-explorer/1.0"
    protocol_version = "HTTP/1.1"

    # ---- GET：页面与静态资源
    def do_GET(self) -> None:                       # noqa: N802（http.server 约定）
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html", "/explorer.html"):
            return self._send_file(WEB_DIR / INDEX_FILE, "text/html; charset=utf-8")
        if path == "/api/health":
            return self._send_json({"ok": True, "model": MODEL_VERSION,
                                    "indicators": [s.ind for s in SPECS]})
        if path.startswith("/api/"):
            return self._send_json({"error": f"没有这个接口：{path}"}, HTTPStatus.NOT_FOUND)

        rel = path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._send_json({"error": "路径不合法"}, HTTPStatus.BAD_REQUEST)
        if not target.is_file():
            return self._send_json({"error": "文件不存在"}, HTTPStatus.NOT_FOUND)
        return self._send_file(target, self._guess_type(target))

    # ---- POST：上传 PDF
    def do_POST(self) -> None:                      # noqa: N802
        url = urlparse(self.path)
        if url.path != "/api/analyze":
            self.close_connection = True            # 请求体没读，keep-alive 会串包
            return self._send_json({"error": "没有这个接口"}, HTTPStatus.NOT_FOUND)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self.close_connection = True
            return self._send_json({"error": "请求里没有文件内容"}, HTTPStatus.BAD_REQUEST)
        if length > MAX_UPLOAD:
            self.close_connection = True            # 不读这么大的体，直接断开
            return self._send_json(
                {"error": f"文件 {length / 1048576:.1f} MB 超过上限 "
                          f"{MAX_UPLOAD // 1048576} MB"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)

        name = Path(parse_qs(url.query).get("name", [""])[0]).name
        payload = self._read_body(length)
        if payload is None:
            return self._send_json({"error": "上传中断（未收全文件）"},
                                   HTTPStatus.BAD_REQUEST)
        try:
            data = analyze(payload, name)
        except PDFLoadError as exc:
            print(f"[explorer] 解析失败：{exc}", file=sys.stderr)
            return self._send_json({"error": str(exc)}, HTTPStatus.UNPROCESSABLE_ENTITY)
        except Exception as exc:                     # 兜底：前端只显示一句话，栈打到控制台
            import traceback
            traceback.print_exc()
            return self._send_json({"error": f"解析时出错：{type(exc).__name__}: {exc}"},
                                   HTTPStatus.INTERNAL_SERVER_ERROR)
        print(f"[explorer] {name or '<upload>'} → {data['file']['pages']} 页 / "
              f"{data['file']['chars']:,} 字 / {data['file']['match_seconds']}s")
        return self._send_json(data)

    # ---- 工具
    def _read_body(self, length: int) -> bytes | None:
        chunks: list[bytes] = []
        left = length
        while left > 0:
            chunk = self.rfile.read(min(left, 1 << 20))
            if not chunk:
                return None
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _guess_type(path: Path) -> str:
        return {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }.get(path.suffix.lower(), "application/octet-stream")

    def _send_json(self, obj: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            return self._send_json({"error": f"读不到 {path.name}"}, HTTPStatus.NOT_FOUND)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:  # 静态请求不刷屏，4xx/5xx 仍要看得见
        code = str(args[1]) if len(args) > 1 else ""
        if code[:1] in ("4", "5"):
            super().log_message(fmt, *args)


# ---------------------------------------------------------------- 启动
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="esg_s11_scorer.explorer",
        description="上传 PDF → 看每个指标对应的段落（带页码、命中词与数值出处）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认只在本机）")
    p.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    p.add_argument("--open", action="store_true", help="启动后自动用默认浏览器打开")
    return p


def main(argv: list[str] | None = None) -> int:
    if not (WEB_DIR / INDEX_FILE).is_file():
        print(f"找不到页面文件：{WEB_DIR / INDEX_FILE}", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"端口 {args.port} 起不来（{exc}）。换一个：--port 8766", file=sys.stderr)
        return 2
    httpd.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"
    print(f"S1.1 报告段落浏览器\n"
          f"  地址   {url}\n"
          f"  词表   {keywords_io.default_path().name}"
          f"（{sum(len(ws) for r in RUBRICS.values() for _, ws in r.groups)} 词，"
          f"{keywords_io.describe_source()}）\n"
          f"  口径   {MODEL_VERSION} / {RUBRIC_VERSION} / {MATCH_VERSION}\n"
          f"  退出   Ctrl+C")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())