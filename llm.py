# -*- coding: utf-8 -*-
"""LLM 客户端：只要一个 OpenAI 兼容的 ``/chat/completions``，不引第三方依赖。

为什么自己写而不是装 SDK
-----------------------
1. 本包目前的运行时依赖只有 ``pypdf`` + ``openpyxl``，为 60 行 HTTP 加一个 SDK（及其
   传递依赖、鉴权插件、遥测开关）不划算，且会让「装了没装行为不一样」重新出现。
2. 判档只需要**一个确定性调用**：温度 0、单轮、JSON 出参、超时可控、失败可重试。
   SDK 的流式、函数调用、会话管理等能力在这里都是噪声。
3. 端点是配置项（``ESG_LLM_BASE_URL``），换供应商不该改代码。阿里百炼、vLLM、
   Ollama 的 OpenAI 兼容层都能直接指过来。

可复现性
--------
``fingerprint()`` 把「端点 + 模型」压成一个短哈希，进打分层指纹。同一份报告 + 同一套
rubric + 同一个模型 → 缓存命中；换了模型就该重算，否则两次运行的分数没法解释。

密钥只从 ``.env`` / 环境变量来（``settings.Settings.llm_api_key``），从不落盘到产物、
从不进日志、``--show-config`` 里是掩码。
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

LLM_VERSION = "llm-client-1.0"

# 这些状态码值得重试（限流 / 服务端抖动）；4xx 的其余值重试也没用
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    """调用失败（网络、鉴权、返回不是 JSON）。调用方应退回关键词兜底，而不是崩批。"""


@dataclass(frozen=True)
class LLM:
    """一次运行生效的 LLM 配置（不可变，便于进指纹）。"""

    base_url: str = ""
    model: str = ""
    api_key: str = ""
    thinking: bool = False          # 思维链开关；deepseek-v4.1-flash 这类模型默认会先刷推理
    timeout: int = 90
    retries: int = 2
    temperature: float = 0.0

    @property
    def ready(self) -> bool:
        """三项齐备才算可用；缺一项就由调用方决定降级还是报错。"""
        return bool(self.base_url.strip() and self.model.strip() and self.api_key.strip())

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def fingerprint(self) -> str:
        # 思维链开关会改变输出分布，必须一起进指纹，否则换开关还在用旧结论
        raw = f"{self.base_url.rstrip('/')}|{self.model}|{int(self.thinking)}|{LLM_VERSION}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]

    def __repr__(self) -> str:                      # 避免密钥随异常信息扩散
        return (f"LLM(model={self.model!r}, base_url={self.base_url!r}, "
                f"thinking={self.thinking}, key={'已配置' if self.api_key else '未配置'})")


def from_settings(settings=None) -> LLM:
    """从配置层取 LLM 参数（未配置时返回 ``ready=False`` 的空对象）。"""
    if settings is None:
        from .settings import get_settings
        settings = get_settings()
    return LLM(base_url=settings.llm_base_url or "",
               model=settings.llm_model or "",
               api_key=settings.llm_api_key or "",
               thinking=bool(getattr(settings, "llm_thinking", False)))


# ---------------------------------------------------------------- JSON 出参
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def parse_json(text: str) -> dict:
    """从模型回复里取 JSON 对象。

    实测 qwen 有时会在 JSON 前后补一句「好的，以下是评分结果：」，或包一层 ```json。
    这里**不**相信模型一定守格式，但也不做宽松的启发式修补：先剥围栏，再取第一个
    ``{`` 到最后一个 ``}``；仍解析不了就抛 ``LLMError``（→ 调用方降级），
    绝不猜一个分数出来。
    """
    t = _FENCE.sub("", text.strip())
    try:
        obj = json.loads(t)
    except json.JSONDecodeError:
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            raise LLMError(f"模型回复不是 JSON：{text[:120]!r}") from None
        try:
            obj = json.loads(t[i:j + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"模型回复的 JSON 解析失败：{exc}") from None
    if not isinstance(obj, dict):
        raise LLMError(f"模型回复不是 JSON 对象：{type(obj).__name__}")
    return obj


# ---------------------------------------------------------------- 主调用
def chat(llm: LLM, system: str, user: str, *, max_tokens: int = 1600,
         json_mode: bool = True) -> str:
    """发一轮对话，返回正文。失败抛 ``LLMError``（调用方负责降级）。

    **空回复的两种成因都自己处理**：思维链型模型（如 deepseek-v4.1-flash）会先把
    ``reasoning_content`` 写满 token 预算，导致 ``content`` 为空且 ``finish_reason=length``。
    遇到空回复先尝试 ``enable_thinking=false``（不支持的端点会 400，再退回加大预算），
    都拿不到正文才抛错——否则每一家都会静默掉进关键词兜底，分数看着"正常"其实没判过。
    """
    if not llm.ready:
        raise LLMError("LLM 未配置（缺 ESG_LLM_BASE_URL / ESG_LLM_MODEL / ESG_LLM_API_KEY）")

    payload: dict = {
        "model": llm.model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": llm.temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if not llm.thinking:
        # 显式关掉思维链：判档是「照标准选一档」，推理链只会吃预算、让输出被截断。
        # 端点若不认这个参数会回 400，下面的 HTTPError 分支会摘掉它重试。
        payload["enable_thinking"] = False

    last = ""
    tried_no_think = False
    bumped = False
    for attempt in range(llm.retries + 1):
        req = urllib.request.Request(llm.endpoint, data=json.dumps(
            payload, ensure_ascii=False).encode("utf-8"), method="POST")
        req.add_header("Authorization", f"Bearer {llm.api_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=llm.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            content = (data["choices"][0]["message"].get("content") or "").strip()
            finish = data["choices"][0].get("finish_reason")
            if content and finish != "length":
                return content
            # 空正文或被截断（JSON 只吐了一半）：先把思维链关掉再试，其次翻倍预算
            last = ("回答不完整（finish=%s，输出 %s token，正文 %d 字）"
                    % (finish, data.get("usage", {}).get("completion_tokens"),
                       len(content)))
            if not tried_no_think and payload.get("enable_thinking") is not False:
                payload["enable_thinking"] = False
                tried_no_think = True
                continue
            if not bumped:
                payload["max_tokens"] = max_tokens * 2
                bumped = True
                continue
            if content:
                return content                     # 预算翻倍后仍被截断，但至少有正文
            raise LLMError(last)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            last = f"HTTP {exc.code}: {detail}"
            if exc.code == 400:
                # 端点不认某些可选参数：逐个摘掉再试（兼容本地 vLLM / 旧网关）
                if payload.pop("enable_thinking", None) is not None:
                    continue
                if payload.pop("response_format", None) is not None:
                    continue
            if exc.code not in RETRY_STATUS:
                raise LLMError(last) from None
        except Exception as exc:                        # noqa: BLE001 网络层什么都可能抛
            last = f"{type(exc).__name__}: {exc}"
        if attempt < llm.retries:
            time.sleep(1.5 * (attempt + 1))
    raise LLMError(f"LLM 调用失败（已重试 {llm.retries} 次）：{last}")


def chat_json(llm: LLM, system: str, user: str, *, max_tokens: int = 1600) -> dict:
    return parse_json(chat(llm, system, user, max_tokens=max_tokens))


__all__ = ["LLM", "LLMError", "LLM_VERSION", "from_settings", "chat", "chat_json",
           "parse_json"]