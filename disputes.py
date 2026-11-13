# -*- coding: utf-8 -*-
"""判档分歧页：上传一份 PDF → 看这家公司五个指标的三票分歧；另附全批分歧总汇。

它回答两个问题
--------------
1. **这家公司**的某个指标，三家模型给的档一样吗？不一样时谁高谁低、理由是什么、引了哪一页？
2. **整批 20 家**里，哪些「公司 × 指标」有分歧？（总汇表，可按指标筛、按公司搜）

与打分管道的边界
----------------
- 判档复用 ``ensemble.ballot``：与 ``python -m esg_s11_scorer.ensemble`` **同一套调用、同一条
  判定、同一份产物结构**，所以页面上看到的与 `reports/ensemble.md` 里的是一回事。
- 本页**不写 ``build/``、不写 ``打分结果/``、不算分**：上传的 PDF 只在内存里解析、判完就丢。
- 总汇表只读 ``reports/ensemble.json``（体检产物）；没有就先跑一次体检，页面会给提示。

启动（工作目录＝``50-应用``）::

    python -m esg_s11_scorer.disputes                # http://127.0.0.1:8766
    python -m esg_s11_scorer.disputes --port 9001 --open

一次上传会真的调用模型：**5 个指标 × 3 个投票人 = 15 次**，页面按钮上写明这一点。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from s11_text import PDFLoadError, load_pdf

from . import blind as blind_mod
from . import ensemble
from .board import CN_NAME
from .config import IND_NAMES, IND_WEIGHTS, MODEL_VERSION, SPECS
from .extract import extract
from .llm import LLMError, from_settings
from .qualitative import recall
from .rubric import RUBRIC_VERSION, RUBRICS
from .settings import get_settings

WEB_DIR = Path(__file__).with_name("web")
PAGE = "disputes.html"

MAX_UPLOAD = 120 * 1024 * 1024          # 与 explorer 同一上限
JOBS_KEEP = 20                          # 只留最近几份任务的结果（每份带原文段落，别攒内存）
REPORT = "ensemble.json"                # ``reports/`` 下的体检产物

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_POOL = ThreadPoolExecutor(max_workers=1)   # 一次只跑一份：别把同一把 key 打满


# ---------------------------------------------------------------- 判档
def _identity(filename: str) -> tuple[str, str]:
    """文件名 → （代码, 展示名）。``06821_ASYMCHEM_ESG_FY2025.pdf`` → ``("06821", "凯莱英")``。"""
    stem = Path(filename or "").stem
    head = stem.split("_")[0] if stem else ""
    code = head if head.isdigit() else ""
    name = CN_NAME.get(code)
    if not name:
        name = stem.replace("_", " ").strip() or "未命名报告"
    return code, name


def judge_document(payload: bytes, filename: str, *, voters, llm,
                   progress=None) -> dict:
    """一份 PDF → 页面的 JSON。``progress(百分比, 说明)`` 用来推任务进度。"""
    t0 = time.perf_counter()
    def say(pct: int, step: str) -> None:
        if progress:
            progress(pct, step)

    say(5, "解析 PDF")
    doc = load_pdf(payload, name=filename)
    code, name = _identity(filename)
    say(25, "抽取数值（判断离职率闸门）")
    ex = extract(doc, filename=filename)
    ry = ex.report_year or get_settings().year
    has_turnover = ex.value("turnover", ry) is not None
    blinder = blind_mod.build(code, filename=filename)

    indicators: list[dict] = []
    total = len(RUBRICS)
    for i, (key, rub) in enumerate(RUBRICS.items(), start=1):
        say(30 + int(65 * (i - 1) / total), f"判档 {key}（{i}/{total}）")
        passages = recall(doc, rub)
        gate_hit = bool(rub.gate) and not has_turnover
        votes = [ensemble.ballot(doc, rub, v, passages, blinder, gate_hit)
                 for v in voters]
        label, majority = ensemble.agreement([v.get("tier") for v in votes])
        spec = next((s for s in SPECS if s.ind == rub.ind), None)
        indicators.append({
            "key": key,
            "ind": rub.ind,
            "part": rub.part,
            "name": rub.name,
            "ind_name": IND_NAMES.get(rub.ind, rub.ind),
            "weight": IND_WEIGHTS.get(rub.ind),
            "kind": spec.kind if spec else "",
            "tiers": {str(t): text for t, text in rub.tiers.items()},
            "note": rub.note,
            "gate": {"text": rub.gate, "hit": gate_hit},
            "n_passages": len(passages),
            "votes": votes,
            "agreement": label,
            "majority": majority,
            "split_type": ensemble.split_type(votes),
            "split": label != "3/3",
        })
    say(98, "整理结果")

    judgeable = [x for x in indicators if x["split_type"] not in ("gate", "incomplete")]
    out = {
        "file": {"name": filename or "<upload>", "pages": len(doc.pages),
                 "sha256": hashlib.sha256(payload).hexdigest()[:16],
                 "bytes": len(payload),
                 "seconds": round(time.perf_counter() - t0, 1),
                 "report_year": ry, "has_turnover": has_turnover},
        "company": {"code": code, "name": name},
        "voters": [v.to_dict() for v in voters],
        "indicators": indicators,
        "summary": {"items": len(indicators), "judgeable": len(judgeable),
                    "split": sum(1 for x in judgeable if x["split"]),
                    "gate": sum(1 for x in indicators if x["split_type"] == "gate")},
        "version": {"model": MODEL_VERSION, "rubric": RUBRIC_VERSION},
    }
    say(100, "完成")
    return out


# ---------------------------------------------------------------- 总汇
def load_summary(year: int | None = None, path: str | Path | None = None) -> dict:
    """读 ``reports/ensemble.json``：整批的分歧表 + 统计。文件不存在时返回提示，不抛错。"""
    s = get_settings()
    path = Path(path) if path else Path(__file__).with_name("reports") / REPORT
    if not path.is_file():
        return {"ok": False,
                "hint": "还没有体检产物。先在工作目录跑："
                        "python -m esg_s11_scorer.ensemble"}
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("rows") or []
    if year:
        rows = [r for r in rows if True]      # 单年度产物，年度只做展示
    companies = sorted({r["code"] for r in rows})
    per_company = []
    for code in companies:
        mine = [r for r in rows if r["code"] == code]
        judge = [r for r in mine if r["split_type"] not in ("gate", "incomplete")]
        bad = [r for r in judge if r["agreement"] != "3/3"]
        per_company.append({"code": code, "name": CN_NAME.get(code, code),
                            "judgeable": len(judge), "split": len(bad),
                            "items": [r["key"] for r in bad]})
    return {
        "ok": True,
        "year": data.get("year") or get_settings().year,
        "voters": data.get("voters") or [],
        "summary": data.get("summary") or {},
        "tables": {"rows": [{k: r.get(k) for k in
                             ("code", "key", "agreement", "split_type", "majority",
                              "t1", "t2", "t3", "e1", "e2", "e3", "r1", "r2", "r3")}
                            for r in rows],
                   "companies": per_company,
                   "names": {c: CN_NAME.get(c, c) for c in companies},
                   "indicators": {k: {"name": RUBRICS[k].name,
                                      "ind": RUBRICS[k].ind,
                                      "weight": IND_WEIGHTS.get(RUBRICS[k].ind)}
                                      for k in RUBRICS}},
        "version": {"model": MODEL_VERSION, "rubric": RUBRIC_VERSION},
    }


# ---------------------------------------------------------------- 任务
def start_job(payload: bytes, filename: str) -> str:
    jid = uuid.uuid4().hex[:12]
    with _JOBS_LOCK:
        _JOBS[jid] = {"id": jid, "status": "queued", "pct": 0, "step": "排队",
                      "result": None, "error": "", "started": time.time()}
        while len(_JOBS) > JOBS_KEEP:
            _JOBS.pop(next(iter(_JOBS)))
    _POOL.submit(_run_job, jid, payload, filename)
    return jid


def _run_job(jid: str, payload: bytes, filename: str) -> None:
    def progress(pct: int, step: str) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(jid)
            if job is not None:
                job.update(pct=pct, step=step, status="running")

    def fail(msg: str) -> None:
        with _JOBS_LOCK:
            job = _JOBS.get(jid)
            if job is not None:
                job.update(status="error", error=msg, pct=100)

    try:
        settings = get_settings()
        llm = from_settings(settings)
        if not llm.ready:
            return fail("LLM 未配置（ESG_LLM_BASE_URL / ESG_LLM_MODEL / ESG_LLM_API_KEY）")
        voters = ensemble.parse_voters(getattr(settings, "ensemble_voters", ""), llm)
        data = judge_document(payload, filename, voters=voters, llm=llm,
                              progress=progress)
        with _JOBS_LOCK:
            job = _JOBS.get(jid)
            if job is not None:
                job.update(status="done", pct=100, step="完成", result=data)
        print(f"[disputes] {filename or '<upload>'} → {len(data['indicators'])} 项，"
              f"分歧 {data['summary']['split']} 项，{data['file']['seconds']}s", file=sys.stderr)
    except PDFLoadError as exc:
        fail(f"解析失败：{exc}")
    except LLMError as exc:
        fail(f"判档失败：{exc}")
    except Exception as exc:                     # 兜底：页面只显示一句话，栈打到控制台
        import traceback
        traceback.print_exc()
        fail(f"出错了：{type(exc).__name__}: {exc}")


def job_state(jid: str) -> dict:
    with _JOBS_LOCK:
        job = _JOBS.get(jid)
        if job is None:
            return {"error": "没有这个任务（服务重启过？重新上传一次）"}
        out = {k: job[k] for k in ("id", "status", "pct", "step", "error")}
        if job["status"] == "done":
            out["result"] = job["result"]
        return out


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "esg-s11-disputes/1.0"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:                       # noqa: N802（http.server 约定）
        url = urlparse(self.path)
        path = unquote(url.path)
        if path in ("/", "/index.html", "/" + PAGE):
            return self._send_file(WEB_DIR / PAGE, "text/html; charset=utf-8")
        if path == "/api/summary":
            year = parse_qs(url.query).get("year", [None])[0]
            return self._send_json(load_summary(int(year) if year else None))
        if path == "/api/job":
            jid = parse_qs(url.query).get("id", [""])[0]
            return self._send_json(job_state(jid))
        if path == "/api/health":
            s = get_settings()
            llm = from_settings(s)
            return self._send_json({"ok": True, "model": MODEL_VERSION,
                                    "llm": llm.model if llm.ready else "",
                                    "voters": getattr(s, "ensemble_voters", "")})
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
            self.close_connection = True
            return self._send_json(
                {"error": f"文件 {length / 1048576:.1f} MB 超过上限 "
                          f"{MAX_UPLOAD // 1048576} MB"},
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        name = Path(parse_qs(url.query).get("name", [""])[0]).name
        payload = self._read_body(length)
        if payload is None:
            return self._send_json({"error": "上传中断（未收全文件）"},
                                   HTTPStatus.BAD_REQUEST)
        return self._send_json({"job": start_job(payload, name)})

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

    def log_message(self, fmt: str, *args) -> None:
        code = str(args[1]) if len(args) > 1 else ""
        if code[:1] in ("4", "5"):
            super().log_message(fmt, *args)


# ---------------------------------------------------------------- 启动
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="esg_s11_scorer.disputes",
        description="上传 PDF → 看这家公司每个指标的三票分歧；附带全批分歧总汇")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认只在本机）")
    p.add_argument("--port", type=int, default=8766, help="端口（默认 8766）")
    p.add_argument("--open", action="store_true", help="启动后自动用默认浏览器打开")
    return p


def main(argv: list[str] | None = None) -> int:
    if not (WEB_DIR / PAGE).is_file():
        print(f"找不到页面文件：{WEB_DIR / PAGE}", file=sys.stderr)
        return 2
    args = build_parser().parse_args(argv)
    try:
        httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        print(f"端口 {args.port} 起不来（{exc}）。换一个：--port 8767", file=sys.stderr)
        return 2
    httpd.daemon_threads = True
    s = get_settings()
    llm = from_settings(s)
    voters = ensemble.parse_voters(getattr(s, "ensemble_voters", ""), llm) if llm.ready else []
    url = f"http://{args.host}:{args.port}/"
    print(f"S1.1 判档分歧页\n"
          f"  地址   {url}\n"
          f"  模型   {llm.model if llm.ready else '（未配置，上传会报错）'}\n"
          f"  投票人 {'、'.join(v.name for v in voters) or '（无）'}"
          f"　→ 每份 PDF 调 {len(voters) * len(RUBRICS)} 次模型\n"
          f"  总汇   {(Path(__file__).with_name('reports') / REPORT).name}"
          f"（没有就先跑 python -m esg_s11_scorer.ensemble）\n"
          f"  退出   Ctrl+C")
    if args.open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
        _POOL.shutdown(wait=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())