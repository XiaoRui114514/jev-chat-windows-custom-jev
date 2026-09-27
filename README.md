# JevChat-Windows：判断模型自定义来源补丁

给 [JevChat-Windows](https://github.com/jev-chat/jev-chat-windows) v0.1.11 的 exe 补一个来源选项：
「判断 · Jev」除了 OpenRouter 和 TypeSafe 直连，还能选**自定义 · OpenAI 兼容**，
跟起草模型那边的自定义一样，自己填 Base URL 和密钥，用「获取模型」拉列表。

## 用法

1. 下载本仓库（Code → Download ZIP）解压。
2. 双击 `安装补丁.bat`。
   - 脚本按 sha256 找官方 v0.1.11 的 `jev-chat-windows.exe`：桌面快捷方式、下载、文档目录都翻一遍。
   - 找不到就把 exe（或它所在的文件夹）拖到 `安装补丁.bat` 上。
   - 程序开着也没关系，脚本会先关掉它；原 exe 备份成 `jev-chat-windows.exe.bak`。
3. 打开程序 → 设置 → 模型 → 判断 · Jev：
   来源选「自定义 · OpenAI 兼容」→ 填 Base URL（一般要带 `/v1`）→ 填这一家的密钥
   （换来源要重填一次，留空会继续用上一把）→ 点「获取模型」挑一个 → 保存设置。

还原：双击 `卸载补丁.bat`，它会从 `.bak` 拷回来。

## 自定义来源的判断是怎么跑的

起草模型那边能随便接，是因为它本来就只是发一轮 chat/completions。判断模型不是：
Jev 走 TypeSafe 自己的 typed decisions 协议（OpenRouter 那条叫 `/api/alpha/decisions`），
普通 OpenAI 兼容接口没这个路径。所以补丁里的自定义来源改成这一套：

- 7 道判断题（题目和 criteria 原样）加对话，打包成一轮 `POST {Base URL}/chat/completions`；
- system 里写死输出契约：`noul` / `choice` / `score` 三种答案各回什么 JSON；
- 模型回 JSON → 解析 → 校验（choice 必须在 criteria 里，score 越界就夹回范围）→ 交给原来的流程。

也就是说**这条路用的是你自己选的模型，不是 Jev**。判断质量取决于那个模型读不读得懂这套题目。
个别题答不成约定格式会被丢掉，界面上那几行就少显示；一次判断全部失败会退回原来的老路
（盲起草 + 判断排序一次问完），再失败这次分析就报错。想要真 Jev，还是选 OpenRouter 或 TypeSafe。

## 补了什么

| 文件 | 改动 |
| --- | --- |
| `core/providers.py` | `JEV_PROVIDERS` 多一条 `custom_openai`，多一个 `JEV_CUSTOM` |
| `core/jev_client.py` | 自定义来源的判断和取模型；两条手写 HTTP 合成一个 `_post_json` |
| `core/engine.py` | `analyze()` 多一个 `jev_base_url` 参数 |
| `app/settings.py` | 存/读 `config.json` 里的 `jev_base_url` |
| `app/overlay.py` | 设置页给判断这组加 Base URL 行，来源提示和说明文案跟着改 |
| `main.py` | 把 `jev_base_url` 传给 `analyze()` |

改动就是这 6 个文件，`src/` 下按上游路径原样放着，其余代码没动。

## 实测过什么

- 打完补丁的 exe 在 Windows 11 上能正常启动；设置页能选到「自定义 · OpenAI 兼容」，
  Base URL 会显示出来；点「获取模型」，程序真的去请求了这个地址（本地假接口收到了 `GET /v1/models`，列表也填回了下拉框）。
- 判断这条路：本地假接口跑过一遍完整流程（三种题型、带围栏的 JSON、越界 score、不存在的选项、
  坏密钥、连不上、返回不是 JSON）。另外拿 OpenRouter 的 `/v1/chat/completions` 加一把真 key
  实测过一次：8 道题全答出来，字段和选项都对得上。
- 免费模型不一定顶得住这套题目（遇到过上游 429、只回思考不回正文、回的 JSON 解析不了），
  这类情况会按「这次判断失败」处理，不会瞎猜。

## 限制

- 只认官方 v0.1.11 的 exe（sha256 `5fc8d88f2cd75ee899bf22e66a20cb2311b97de3e88d6548d6e9b5d59559a748`），
  脚本会核对，对不上就不动手。你自己的版本想用，拿 `tools/build_patch.py` 重新生成一份补丁。
- 程序更新到新版本后补丁就没了（更新会覆盖 exe），重新打一遍就行。
- exe 本来就没有代码签名，补丁又是直接改字节，杀毒软件或 SmartScreen 可能有意见。

## 自己构建和验证

```
python tools/build_patch.py --exe "路径\jev-chat-windows.exe" --patched-exe out.exe
python tools/test_patch.py src
```

`build_patch.py` 要用 Python 3.11（exe 里的字节码是 3.11 的）。它把 exe 里的 PYZ 解出来、
塞进新字节码、重排 CArchive，最后自己校验一遍；加 `--patched-exe` 会顺便把打好的 exe 写出来先跑一下。
生成的东西就是 `patch/` 里那几个文件，`安装补丁.bat` 只负责按偏移把它们拼回去。

`test_patch.py` 起一个本地假接口，把「发请求 → 解析 JSON → 出答案 → 喂给起草小抄」整条路跑一遍。

## 协议

上游 [jev-chat-windows](https://github.com/jev-chat/jev-chat-windows) 是 MIT，
判断内核和题目口径的版权归 Finderchangchang 与 jev-chat 贡献者，详见 [NOTICE](NOTICE)。
本仓库是基于它二次开发的补丁。