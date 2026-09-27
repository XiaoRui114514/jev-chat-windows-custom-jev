# -*- coding: utf-8 -*-
"""自定义判断来源这条路的功能测试：起一个本地假接口，全程离线，只用标准库。

跑法（3.11 / 3.12 都行）：python tools/test_patch.py src
它拿的是 src/ 里那几个改过的文件，题目用这里自带的缩水版（三种题型各来一道）。
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "src")
sys.path.insert(0, ROOT)

import core.jev_client as jc                                          # noqa: E402
from core.providers import JEV_CUSTOM, JEV_PROVIDERS                  # noqa: E402

QUESTIONS = {
    "literal_question": {
        "type": "noul",
        "instructions": "Is the latest message meant purely literally?",
        "criteria": {"true": "No subtext at all.", "false": "There is subtext."},
    },
    "true_intent": {
        "type": "choice",
        "instructions": "What is the truest intent?",
        "criteria": {"vent_anger": "They are angry.", "casual_chat": "Light talk.",
                     "close_topic": "Peaceful wrap-up."},
    },
    "danger_level": {
        "type": "score",
        "instructions": "How close is this to a fight?",
        "criteria": ["No complaint.", "Mild tease.", "Mild complaint.", "Unhappy.",
                     "Sarcastic.", "Openly upset.", "Blame.", "Last warning.", "Rupture.",
                     "Over."],
    },
    "best_reply": {
        "type": "choice",
        "instructions": "Which candidate reply fits best?",
        "criteria": {"reply_a": "甲", "reply_b": "乙", "reply_c": "丙"},
    },
}
STATE = {"chat": {"relationship": "friends", "messages": [{"from": "her", "text": "在吗"}]}}
SEEN = {}
MODE = {"value": "ok"}


def fake_answers():
    out = {}
    for name, q in QUESTIONS.items():
        if q["type"] == "noul":
            out[name] = {"noul": 0.8, "confidence": 0.9}
        elif q["type"] == "choice":
            keys = list(q["criteria"])
            out[name] = {"choice": keys[-1], "confidence": 0.7,
                         "probabilities": {k: 1.0 / len(keys) for k in keys}}
        else:
            out[name] = {"score": 6, "confidence": 0.5, "probabilities": {"6": 0.5, "7": 0.5}}
    return out


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code, obj):
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        SEEN["path"] = self.path
        SEEN["auth"] = self.headers.get("Authorization")
        if self.path.endswith("/models"):
            return self._send(200, {"data": [{"id": "mock-b"}, {"id": "mock-a"}, {"id": "mock-a"}]})
        self._send(404, {"error": {"message": "no such path"}})

    def do_POST(self):
        SEEN["path"] = self.path
        SEEN["auth"] = self.headers.get("Authorization")
        SEEN["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
        mode = MODE["value"]
        payload = fake_answers()
        if mode == "garbage":
            body = {"choices": [{"message": {"content": "抱歉，这个我不能回答。"}}]}
        elif mode == "empty":
            body = {"choices": [{"message": {"content": ""}}]}
        elif mode == "fenced":
            text = "```json\n" + json.dumps({"answers": payload}, ensure_ascii=False) + "\n```"
            body = {"choices": [{"message": {"content": text}}]}
        elif mode == "partial":            # 越界的 score + 不存在的选项
            payload["danger_level"] = {"score": 99}
            payload["best_reply"] = {"choice": "reply_zz"}
            body = {"choices": [{"message": {"content": json.dumps({"answers": payload})}}]}
        else:
            body = {"choices": [{"message": {"content": json.dumps(
                {"answers": payload}, ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 123, "completion_tokens": 456}}
        self._send(200 if mode != "http500" else 500, body)


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d/v1" % server.server_address[1]
os.environ["JEV_API_KEY"] = "test-key"


def ask(**kw):
    kw.setdefault("provider", "custom_openai")
    kw.setdefault("model", "mock-model")
    kw.setdefault("base_url", BASE)
    return jc.ask(STATE, QUESTIONS, **kw)


def expect_error(needle, **kw):
    try:
        ask(**kw)
    except jc.JevError as exc:
        assert needle in str(exc), f"{needle!r} 不在 {str(exc)!r} 里"
        return
    raise AssertionError(f"该报错却没报：{needle}")


# 正常一轮：请求形状、三种答案的解析、usage、模型列表
MODE["value"] = "ok"
got = ask()
assert got["usage"] == {"input_tokens": 123, "output_tokens": 456}, got["usage"]
assert set(got["answers"]) == set(QUESTIONS), sorted(got["answers"])
assert got["answers"]["literal_question"] == {"type": "noul", "noul": 0.8}
assert got["answers"]["true_intent"]["choice"] == "close_topic"
assert abs(sum(got["answers"]["true_intent"]["probabilities"].values()) - 1) < 1e-9
assert got["answers"]["danger_level"] == {"type": "score", "score": 6.0, "confidence": 0.5,
                                          "probabilities": {"6": 0.5, "7": 0.5}}
assert got["answers"]["best_reply"]["choice"] == "reply_c"
assert SEEN["path"] == "/v1/chat/completions" and SEEN["auth"] == "Bearer test-key"
assert SEEN["body"]["model"] == "mock-model" and SEEN["body"]["temperature"] == 0
assert SEEN["body"]["stream"] is False
assert SEEN["body"]["messages"][0]["role"] == "system"
sent = json.loads(SEEN["body"]["messages"][1]["content"])
assert sent["questions"]["true_intent"]["criteria"]["close_topic"] == "Peaceful wrap-up."
assert sent["state"]["chat"]["messages"][0]["text"] == "在吗"
assert jc.list_models("custom_openai", "test-key", base_url=BASE) == ["mock-a", "mock-b"]

# 围栏、越界、对不上的选项：能救的救，救不回来的丢掉
MODE["value"] = "fenced"
assert ask()["answers"]["danger_level"]["score"] == 6.0
MODE["value"] = "partial"
partial = ask()
assert partial["answers"]["danger_level"]["score"] == 9.0           # 夹回范围内
assert "best_reply" not in partial["answers"]                       # 选项不存在就丢掉
assert len(partial["answers"]) == len(QUESTIONS) - 1

# 该失败的都失败，而且消息里说人话
MODE["value"] = "ok"
expect_error("先填 Base URL", base_url="")
expect_error("先填模型 id", model="")
expect_error("http:// 或 https://", base_url="localhost:8000")
for mode, needle in (("garbage", "没有回 JSON"), ("empty", "空内容"),
                     ("http500", "Jev HTTP 500")):
    MODE["value"] = mode
    expect_error(needle)
expect_error("Jev request failed", base_url="http://127.0.0.1:1/v1")   # 端口没人听

# OpenRouter 那条路没被顺手改坏：还是打 /api/alpha/decisions
MODE["value"] = "ok"
jc.OPENROUTER_DECISIONS = BASE + "/decisions"
ask(provider="openrouter", model="typesafe/jev-1.13")
assert SEEN["path"] == "/v1/decisions", SEEN["path"]

# 配置层：jev_base_url 只在自定义来源下读得出来，存得进去
import app.settings as settings                                        # noqa: E402

settings._CONFIG = os.path.join(os.environ.get("TEMP", "."), "jev-patch-test-config.json")
with open(settings._CONFIG, "w", encoding="utf-8") as f:
    json.dump({"relationship": "friends", "jev_provider": "custom_openai",
               "jev_model": "mock-a", "jev_base_url": "https://my.example/v1"}, f)
assert settings.jev_base_url() == "https://my.example/v1"
settings.save(jev_provider_text="openrouter")
assert settings.jev_base_url() == ""
settings.save(jev_provider_text="custom_openai", jev_base_url_text="https://other.example/v1")
assert settings.jev_base_url() == "https://other.example/v1"
settings.save(jev_model_text="mock-c")
assert settings.jev_model() == "mock-c" and settings.jev_base_url() == "https://other.example/v1"
os.remove(settings._CONFIG)

assert list(JEV_CUSTOM) == ["custom_openai"]
assert JEV_PROVIDERS["custom_openai"].name == "自定义 · OpenAI 兼容"
print("custom jev path ok")