# -*- coding: utf-8 -*-
"""Jev 判断 API 客户端：OpenRouter、TypeSafe 直连，或自定义 OpenAI 兼容来源。

TypeSafe 直连走官方 `typesafe_sdk`；OpenRouter 和自定义来源自己拼 HTTP——
SDK 把路径写死成 `/v1/systemone`，打不到 OpenRouter 的 `/api/alpha/decisions`。
自定义来源没有 typed decisions 接口，改用一轮 chat/completions，让模型照题目回 JSON。
三条路返回同一个 dict 形状，engine 不关心跑的是哪条。key 只从环境变量读，绝不打进日志。
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.error
import urllib.request
from typing import NoReturn

try:  # 当模块导入 / 当脚本直接跑 都能用
    from .providers import (ENV_VARS, JEV_CUSTOM, JEV_ENV, JEV_PROVIDERS, LEGACY,
                            OPENROUTER_DECISIONS, OPENROUTER_KEY_URL, TYPESAFE_BASE)
except ImportError:
    from providers import (ENV_VARS, JEV_CUSTOM, JEV_ENV, JEV_PROVIDERS, LEGACY,
                           OPENROUTER_DECISIONS, OPENROUTER_KEY_URL, TYPESAFE_BASE)

MAX_RETRIES = 3


class JevError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def redact_secrets(text: str) -> str:
    """Strip every live key from any string before print or disk write."""
    if not isinstance(text, str):
        text = str(text)
    for env in ENV_VARS:
        key = os.environ.get(env) or ""
        if key:
            text = text.replace(key, "[REDACTED]")
    return text


def _status_of(exc: Exception) -> int | None:
    """各家 SDK 放 HTTP 状态码的属性名不一样：openai/anthropic 是 status_code，
    google-genai 是 code（它的 status 是 'NOT_FOUND' 这种字符串），typesafe 是 status。"""
    for name in ("status_code", "code", "status"):
        value = getattr(exc, name, None)
        if isinstance(value, int):
            return value
    return None


def _fail(exc: Exception, what: str) -> NoReturn:
    """SDK 抛的异常 → 一句人话的 JevError。消息过脱敏，绝不把 key 带出来。"""
    if isinstance(exc, JevError):
        raise exc
    status = _status_of(exc)
    hint = {401: "密钥被拒", 403: "没有权限", 404: "模型或地址不对", 422: "请求被拒",
            429: "被限流", 529: "服务过载"}.get(status, "")
    detail = redact_secrets(str(exc)).strip()[:300]
    head = f"{what} HTTP {status}" if status else f"{what}失败"
    raise JevError(f"{head}: {hint or detail or type(exc).__name__}", status) from None


def _api_key(env: str = JEV_ENV) -> str:
    """两把 key 之一（JEV_API_KEY / LLM_API_KEY）。新名字空着就退回老名字，老用户不用重填。"""
    key = ((os.environ.get(env) or "").strip()
           or (os.environ.get(LEGACY.get(env, "")) or "").strip())
    if not key:
        raise JevError(
            f"{env} is not set. Export it in the environment; "
            "do not put the key in a file."
        )
    return key


def _error_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    return redact_secrets(raw)[:800]


def ask(state: dict, questions: dict, timeout: float = 20,
        provider: str = "openrouter", model: str | None = None,
        base_url: str | None = None) -> dict:
    """问 Jev 一轮判断，返回 {"answers": {名字: 答案}, "usage": {...}}。

    provider ∈ JEV_PROVIDERS（openrouter / typesafe 直连 / 自定义 OpenAI 兼容）；
    model=None 用该来源的默认模型，base_url 只有自定义来源要传。
    三条路返回的 dict 形状一模一样，429/529 都会退避重试。绝不打印或写出 key。
    """
    spec = JEV_PROVIDERS.get(provider) or JEV_PROVIDERS["openrouter"]
    key = _api_key(JEV_ENV)  # 各家共用同一把 key，换来源不用重填
    model = model or spec.default
    if provider in JEV_CUSTOM and not model:
        raise JevError("自定义判断来源要先填模型 id")   # 两家自定义都没默认模型
    if provider in ("typesafe", "custom_typesafe"):
        # 同一种协议，区别只是地址：官方直连用写死的，自定义用设置页填的（没填就报错，别偷偷打官方）
        if provider == "custom_typesafe":
            return _ask_typesafe(state, questions, key, model, timeout, _norm_base(base_url))
        return _ask_typesafe(state, questions, key, model, timeout)
    if provider in JEV_CUSTOM:  # custom_openai：聊天接口，没有 typed decisions
        return _ask_custom(state, questions, key, base_url, model, timeout)
    return _ask_openrouter(state, questions, key, model, timeout)


def _answer(answer) -> dict:
    """SDK 的答案对象 → OpenRouter 那条路 JSON 出来的同一个形状。"""
    if answer.type == "noul":
        return {"type": "noul", "noul": answer.noul}
    if answer.type == "choice":
        return {"type": "choice", "choice": answer.choice, "confidence": answer.confidence,
                "probabilities": dict(answer.probabilities)}
    # score：SDK 把概率的 key 转成了 int，这里转回字符串，跟 JSON 那条路对齐
    return {"type": "score", "score": answer.score, "confidence": answer.confidence,
            "probabilities": {str(k): v for k, v in answer.probabilities.items()}}


def _ask_typesafe(state: dict, questions: dict, key: str, model: str, timeout: float,
                  base_url: str | None = None) -> dict:
    """官方 typesafe_sdk。questions 原样传：core/questions.py 里那几个 dict 本身就是 SDK 的
    NoulModel / ChoiceModel / ScoreModel（SDK 的 normalize_questions 认 dict），不用再包一层对象。
    重试用 RetryPolicy 的默认值——它本来就重试 408/429/5xx（含 529）并退避。
    base_url 只有自定义来源才给：TypeSafe 协议的镜像（TeamoRouter 之类）走这里。"""
    import typesafe_sdk

    base = base_url or TYPESAFE_BASE
    try:
        with typesafe_sdk.TypeSafeClient(api_key=key, base_url=base, model=model,
                                         timeout=timeout) as client:
            result = client.system_one(state, questions, model=model)
    except Exception as exc:
        _fail(exc, "Jev 判断")
    return {
        "answers": {name: _answer(a) for name, a in result.answers.items()},
        "usage": {"input_tokens": result.usage.input_tokens,
                  "output_tokens": result.usage.output_tokens},
    }


def _post_json(url: str, key: str, payload: dict, timeout: float) -> dict:
    """POST 一份 JSON，429/529 退避重试 3 次；出错转成一句人话（消息过脱敏）。

    两条自己拼 HTTP 的路（OpenRouter / 自定义来源）共用这一份。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    last_status: int | None = None
    last_body = ""
    for attempt in range(MAX_RETRIES + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json; charset=utf-8",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_status = exc.code
            last_body = _error_body(exc)
            if last_status in (429, 529) and attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            readable = {
                401: f"Jev HTTP 401: API key rejected. Check {JEV_ENV}.",
                403: f"Jev HTTP 403: this key has no access to the model. {last_body}",
                404: f"Jev HTTP 404: endpoint not found: {url}",
                422: f"Jev HTTP 422: request body rejected. {last_body}",
                429: f"Jev HTTP 429: rate limited after {MAX_RETRIES} retries. {last_body}",
                529: f"Jev HTTP 529: provider overloaded after {MAX_RETRIES} retries. {last_body}",
            }.get(last_status, f"Jev HTTP {last_status}: {last_body}")
            raise JevError(readable, last_status) from None
        except (TimeoutError, socket.timeout) as exc:
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise JevError(f"Jev request timed out after {timeout}s") from exc
        except urllib.error.URLError as exc:
            reason = redact_secrets(getattr(exc, "reason", exc))
            if attempt < MAX_RETRIES:
                time.sleep(2**attempt)
                continue
            raise JevError(f"Jev request failed: {reason}") from None

    raise JevError(
        f"Jev HTTP {last_status}: exhausted retries. {last_body}", last_status
    )


def _ask_openrouter(state: dict, questions: dict, key: str, model: str, timeout: float) -> dict:
    """OpenRouter 的 /api/alpha/decisions。"""
    return _post_json(OPENROUTER_DECISIONS, key,
                      {"model": model, "state": state, "questions": questions}, timeout)


def _norm_base(base_url: str | None) -> str:
    """设置页填的 Base URL → 可以直接拼路径的前缀。跟起草那边一样，一般要带 /v1。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise JevError("自定义判断来源要先填 Base URL")
    if not base.startswith(("http://", "https://")):
        raise JevError(f"Base URL 要以 http:// 或 https:// 开头：{base}")
    return base


# 自定义来源没有 typed decisions 接口，把题目整份丢给模型，按这个契约回 JSON。
# 题目本身（instructions / criteria）就是给模型看的，原样传，不做改写的二次加工。
_CUSTOM_SYSTEM = (
    "You are a decision model. You get a conversation and a set of typed questions about it. "
    "Answer every question. Reply with one JSON object and nothing else:\n"
    '{"answers": {"<question name>": <answer>}}\n'
    "Answer shape depends on the question's type field:\n"
    '- noul: {"noul": <0~1>} - probability that the statement in the instructions is true.\n'
    '- choice: {"choice": "<criteria key>", "confidence": <0~1>, "probabilities": '
    '{"<criteria key>": <0~1>}} - probabilities over every criteria key.\n'
    '- score: {"score": <integer>, "confidence": <0~1>, "probabilities": '
    '{"<criteria index>": <0~1>}} - score is the 0-based index of the matching criteria '
    "entry; probabilities are over every index.\n"
    "Weigh the whole thread, not the last line alone. Follow each question's instructions and "
    "criteria strictly, and use the criteria keys exactly as given. No prose, no code fences."
)


def _number(value) -> float | None:
    """模型爱把数字写成字符串；认不出来就给 None，别猜。"""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _unit(value) -> float:
    return max(0.0, min(1.0, value))


def _probabilities(raw, keys: tuple) -> dict:
    """{key: 数字} 里只留认得的 key，顺手夹到 0~1。"""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for name in keys:
        value = _number(raw.get(name))
        if value is not None:
            out[name] = _unit(value)
    return out


def _answer_of(question: dict, raw) -> dict | None:
    """一道题的回答 → 跟 OpenRouter 那条路一模一样的形状；不像话就返回 None。

    宁缺勿错：少一道题只是少一行判断，瞎猜的答案会一路带到起草里去。
    """
    kind = question.get("type")
    criteria = question.get("criteria")
    given = raw if isinstance(raw, dict) else {}
    if kind == "noul":
        value = _number(given.get("noul")) if isinstance(raw, dict) else _number(raw)
        return None if value is None else {"type": "noul", "noul": _unit(value)}
    if kind == "choice" and isinstance(criteria, dict):
        choice = given.get("choice") if isinstance(raw, dict) else raw
        choice = str(choice).strip() if choice is not None else ""
        if choice not in criteria:
            return None
        confidence = _number(given.get("confidence"))
        probabilities = _probabilities(given.get("probabilities"), tuple(criteria))
        if not probabilities and confidence is not None:
            probabilities = {choice: _unit(confidence)}  # 没给概率就拿置信度顶上
        answer = {"type": "choice", "choice": choice, "probabilities": probabilities}
        if confidence is not None:
            answer["confidence"] = _unit(confidence)
        return answer
    if kind == "score" and isinstance(criteria, list) and criteria:
        value = _number(given.get("score")) if isinstance(raw, dict) else _number(raw)
        if value is None:
            return None
        last = float(len(criteria) - 1)
        score = float(int(round(max(0.0, min(last, value)))))
        keys = tuple(str(i) for i in range(len(criteria)))
        answer = {"type": "score", "score": score,
                  "probabilities": _probabilities(given.get("probabilities"), keys)}
        confidence = _number(given.get("confidence"))
        if confidence is not None:
            answer["confidence"] = _unit(confidence)
        return answer
    return None


def _answers_of(questions: dict, body: dict) -> dict:
    """响应正文 → answers。一道题都没答成约定格式就算这次判断失败了。"""
    content = _content_of(body)
    try:
        data = json.loads(content)
    except ValueError:
        block = re.search(r"\{.*\}", content, re.S)  # 有的模型爱套一层 ```json
        if block is None:
            raise JevError("判断模型没有回 JSON，检查对面是不是 OpenAI 兼容接口") from None
        try:
            data = json.loads(block.group(0))
        except ValueError:
            raise JevError("判断模型回的 JSON 解析不了") from None
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise JevError("判断模型的 JSON 里没有 answers 字段")
    out = {}
    for name, question in questions.items():
        answer = _answer_of(question, answers.get(name))
        if answer is not None:
            out[name] = answer
    if not out:
        raise JevError("判断模型一道题都没按约定格式回答")
    return out


def _content_of(body: dict) -> str:
    """OpenAI 兼容响应里取正文。"""
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices:
        raise JevError("判断模型没有返回 choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):  # 少数服务按 block 回
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        finish = choices[0].get("finish_reason") or "?"
        thought = isinstance(message, dict) and (message.get("reasoning")
                                                or message.get("reasoning_content"))
        why = "token 全花在思考上了，换个普通模型或把预算放大" if thought else "模型没给正文"
        raise JevError(f"判断模型返回了空内容（{why}；finish_reason={finish}）")
    return content


def _usage_of(body: dict) -> dict:
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return {}
    out = {}
    for src, dst in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
        value = usage.get(src)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[dst] = value
    return out


def _ask_custom(state: dict, questions: dict, key: str, base_url: str | None,
                model: str, timeout: float) -> dict:
    """自定义来源：一轮 chat/completions，题目和对话原样进 user 回合。

    判断质量取决于对面选的模型——这条路没有 Jev 自己的 typed decisions 接口可用。
    """
    url = _norm_base(base_url) + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _CUSTOM_SYSTEM},
            {"role": "user", "content": json.dumps({"state": state, "questions": questions},
                                                   ensure_ascii=False)},
        ],
        "temperature": 0,
        "max_tokens": 2000,   # 思考型模型会先写一长段 reasoning，预算小了正文就空
        "stream": False,
    }
    body = _post_json(url, key, payload, timeout)
    return {"answers": _answers_of(questions, body), "usage": _usage_of(body)}


def _check_openrouter_key(key: str, timeout: float) -> None:
    """免费的 auth/key 探测：401/403 说明 key 不对，别的错（超时/断网）也如实上报。
    列表本身是写死的，key 对不对只有靠它才知道，别等第一次判断才暴露。"""
    req = urllib.request.Request(OPENROUTER_KEY_URL,
                                 headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        hint = {401: "密钥被拒", 403: "没有权限"}.get(exc.code, _error_body(exc)[:200])
        raise JevError(f"取模型列表 HTTP {exc.code}: {hint}") from None
    except (TimeoutError, socket.timeout):
        raise JevError(f"取模型列表请求超时（{timeout}s）") from None
    except urllib.error.URLError as exc:
        raise JevError(
            f"取模型列表失败: {redact_secrets(getattr(exc, 'reason', exc))}") from None


def _list_custom_models(key: str, base_url: str | None, timeout: float) -> list[str]:
    """自定义来源走标准的 /models 目录；对面给什么就是什么，不做过滤。"""
    url = _norm_base(base_url) + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}",
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        hint = {401: "密钥被拒", 403: "没有权限",
                404: "地址不对（Base URL 一般要带 /v1）"}.get(exc.code, _error_body(exc)[:200])
        raise JevError(f"取模型列表 HTTP {exc.code}: {hint}") from None
    except (TimeoutError, socket.timeout):
        raise JevError(f"取模型列表请求超时（{timeout}s）") from None
    except urllib.error.URLError as exc:
        raise JevError(
            f"取模型列表失败: {redact_secrets(getattr(exc, 'reason', exc))}") from None
    except ValueError:
        raise JevError("取模型列表返回的不是 JSON") from None
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise JevError("接口没给 data 列表（对面不是 OpenAI 兼容的 /models？）")
    ids = {str(item["id"]).strip() for item in data
           if isinstance(item, dict) and item.get("id")}
    if not ids:
        raise JevError("接口返回的模型列表是空的")
    return sorted(ids)


def list_models(provider: str, key: str, timeout: float = 10,
                base_url: str | None = None) -> list[str]:
    """某家能用的 Jev 模型 id，去重排序。失败抛 JevError（设置页直接显示这句话）。"""
    if provider in ("typesafe", "custom_typesafe"):
        import typesafe_sdk

        try:
            with typesafe_sdk.TypeSafeClient(
                    api_key=key,
                    base_url=_norm_base(base_url) if provider == "custom_typesafe" else TYPESAFE_BASE,
                    timeout=timeout) as client:
                return sorted({m.name for m in client.models.list().models})
        except Exception as exc:
            if provider == "custom_typesafe":
                # TypeSafe 镜像多半只实现 /v1/systemone：拿不到列表不算错，手填就行
                raise JevError("这个来源的模型列表不一定有，手填模型 id 即可"
                               "（TeamoRouter 填 jev）") from None
            _fail(exc, "取模型列表")
    if provider in JEV_CUSTOM:  # custom_openai：标准 /models 目录
        return _list_custom_models(key, base_url, timeout)
    # OpenRouter 的 Jev 是 Decisions API 专属模型，不在 /api/v1/models 目录里
    # （也没有列它的专用端点），列表按官方模型页写死，别名列排最前（永远指向最新版）。
    # key 对不对由探测兜着，别让坏密钥等到第一次判断才暴露。
    _check_openrouter_key(key, timeout)
    return ["~typesafe/jev-latest", "typesafe/jev-1.13"]


if __name__ == "__main__":
    # ponytail: 不联网。两条路各测一次：SDK 那条在 typesafe_sdk 边界换成假客户端，
    # urllib 那条 mock urlopen。会坏的地方就一个——答案对象 → dict 的映射得跟 JSON 那条一模一样。
    import io
    import types as _t
    from unittest.mock import patch

    import typesafe_sdk

    try:
        from .questions import JUDGE_QUESTIONS, build_rank_question
    except ImportError:
        from questions import JUDGE_QUESTIONS, build_rank_question

    os.environ.pop(JEV_ENV, None)
    os.environ["OPENROUTER_API_KEY"] = "or-key"  # 老名字：新名字没设时该退回它
    assert _api_key(JEV_ENV) == "or-key"
    os.environ[JEV_ENV] = "ts-key"  # 新名字在就用新的，两家来源共用这一把
    questions = dict(JUDGE_QUESTIONS)
    questions.update(build_rank_question(["甲", "乙", "丙"]))
    seen: dict = {}

    class _FakeClient:
        def __init__(self, **kw):
            seen["init"] = kw

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def system_one(self, state, qs, **kw):
            seen["state"], seen["questions"], seen["kw"] = state, qs, kw
            return _t.SimpleNamespace(
                answers={
                    "literal_question": _t.SimpleNamespace(type="noul", noul=0.9),
                    "best_reply": _t.SimpleNamespace(
                        type="choice", choice="reply_b", confidence=0.7,
                        probabilities={"reply_a": 0.2, "reply_b": 0.7, "reply_c": 0.1}),
                    "danger_level": _t.SimpleNamespace(
                        type="score", score=4.0, confidence=0.6, probabilities={4: 0.6, 5: 0.4}),
                },
                usage=_t.SimpleNamespace(input_tokens=11, output_tokens=22))

        @property
        def models(self):
            return _t.SimpleNamespace(list=lambda: _t.SimpleNamespace(models=(
                _t.SimpleNamespace(name="jev-preview"), _t.SimpleNamespace(name="jev-latest"))))

    with patch.object(typesafe_sdk, "TypeSafeClient", _FakeClient):
        got = ask({"chat": {}}, questions, timeout=15, provider="typesafe", model="jev-1.13.0")
        ask_init = seen["init"]
        assert list_models("typesafe", "ts-key") == ["jev-latest", "jev-preview"]
        assert seen["init"] == {"api_key": "ts-key", "base_url": TYPESAFE_BASE, "timeout": 10}
    assert ask_init == {"api_key": "ts-key", "base_url": TYPESAFE_BASE,
                        "model": "jev-1.13.0", "timeout": 15}
    assert seen["kw"] == {"model": "jev-1.13.0"}
    # 题目原样进 SDK：它们本身就是 NoulModel / ChoiceModel / ScoreModel，不用再包一层
    assert seen["questions"] is questions
    assert seen["questions"]["danger_level"]["type"] == "score"
    assert isinstance(seen["questions"]["danger_level"]["criteria"], list)
    assert seen["questions"]["best_reply"]["criteria"] == {
        "reply_a": "甲", "reply_b": "乙", "reply_c": "丙"}
    # 映射出来的形状跟 OpenRouter 那条路的 JSON 必须一致（engine 不关心跑的是哪条）
    assert got["answers"]["literal_question"] == {"type": "noul", "noul": 0.9}
    assert got["answers"]["best_reply"] == {
        "type": "choice", "choice": "reply_b", "confidence": 0.7,
        "probabilities": {"reply_a": 0.2, "reply_b": 0.7, "reply_c": 0.1}}
    assert got["answers"]["danger_level"] == {
        "type": "score", "score": 4.0, "confidence": 0.6,
        "probabilities": {"4": 0.6, "5": 0.4}}  # score 的概率 key 转回字符串
    assert got["usage"] == {"input_tokens": 11, "output_tokens": 22}

    class _Boom(Exception):
        status = 429

    with patch.object(typesafe_sdk, "TypeSafeClient", lambda **kw: (_ for _ in ()).throw(_Boom("x"))):
        try:
            ask({"chat": {}}, questions, provider="typesafe")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert e.status == 429 and "被限流" in str(e)

    # OpenRouter 那条没动：还是自己拼 body、打 /api/alpha/decisions
    body = {"answers": {"best_reply": {"type": "choice", "choice": "reply_a"}}, "usage": {}}

    def _fake_urlopen(req, timeout=None):
        seen["url"], seen["body"] = req.full_url, json.loads(req.data.decode("utf-8"))
        return io.BytesIO(json.dumps(body).encode("utf-8"))

    with patch.object(urllib.request, "urlopen", _fake_urlopen):
        assert ask({"chat": {}}, questions) == body
    assert seen["url"] == OPENROUTER_DECISIONS
    assert seen["body"]["model"] == "typesafe/jev-1.13" and seen["body"]["questions"] == questions

    # OpenRouter 路的列表是写死的，但 key 要过 auth/key 探测：mock urlopen 验两头
    def _fake_key_ok(req, timeout=None):
        seen["key_url"] = req.full_url
        assert req.headers["Authorization"] == "Bearer or-key"
        return io.BytesIO(b'{"data":{}}')

    with patch.object(urllib.request, "urlopen", _fake_key_ok):
        assert list_models("openrouter", "or-key") == [
            "~typesafe/jev-latest", "typesafe/jev-1.13"]
    assert seen["key_url"] == OPENROUTER_KEY_URL

    def _fake_key_rejected(req, timeout=None):
        raise urllib.error.HTTPError(OPENROUTER_KEY_URL, 401, "Unauthorized", {},
                                     io.BytesIO(b'{"error":{"message":"bad key or-key"}}'))

    with patch.object(urllib.request, "urlopen", _fake_key_rejected):
        try:
            list_models("openrouter", "or-key")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert e.status is None and "密钥被拒" in str(e) and "or-key" not in str(e)

    # 401/403 以外的状态码要把响应体带出来（别提前 read 把流吃空）
    def _fake_key_429(req, timeout=None):
        raise urllib.error.HTTPError(OPENROUTER_KEY_URL, 429, "Too Many", {},
                                     io.BytesIO(b'{"error":{"message":"rate limited"}}'))

    with patch.object(urllib.request, "urlopen", _fake_key_429):
        try:
            list_models("openrouter", "or-key")
            raise SystemExit("应当抛错")
        except JevError as e:
            assert "HTTP 429" in str(e) and "rate limited" in str(e)

    assert redact_secrets("key=ts-key or-key") == "key=[REDACTED] [REDACTED]"
    print("jev_client ok")
