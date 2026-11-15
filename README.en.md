# Custom judging provider patch for JevChat-Windows

[简体中文](README.md) · English

A version-specific Windows binary patch for the official JevChat-Windows **v0.1.11** executable. It adds custom OpenAI-compatible and TypeSafe-compatible judging providers. It is not a standalone chat application and must not be applied to arbitrary executable versions.

For a maintained source-based application with these providers integrated, see [JevChat-Windows Custom](https://github.com/XiaoRui114514/jev-chat-windows-custom). Use this repository when you specifically need the v0.1.11 patch workflow.

## Apply and restore

1. Download this repository as a ZIP and extract it.
2. Run `安装补丁.bat`, or drag the supported executable/folder onto it if automatic discovery fails. The installer checks the expected executable SHA-256 and creates a `.bak` backup. It may close the running application.
3. Open model settings and choose the custom judging provider. Use your own endpoint, key and model; consult the [Chinese protocol guide](README.md) for the different Base URL requirements.
4. Run `检查补丁.bat` to inspect patch status. To restore, run `卸载补丁.bat`, which uses the backup. Preserve that backup.

The OpenAI-compatible path sends structured judging questions through chat completions; an arbitrary chat model is not Jev. TypeSafe-compatible services use their own protocol. Quality, availability and cost depend on the service. Model calls can send recognized conversation text to your configured provider.

## Validation

Python regression tests use protocol fixtures and synthetic answers:

```sh
python -m pip install typesafe-sdk openai httpx
python -m unittest discover -s tests -v
python tools/test_patch.py src
```

Installer acceptance additionally requires Windows PowerShell/PowerShell and the supported executable. The [regression workflow](.github/workflows/tests.yml) records the Windows matrix. Never attach real conversations, provider keys or proprietary executables to public bug reports.

Upstream attribution and redistribution terms are retained in [LICENSE](LICENSE) and [NOTICE](NOTICE).
