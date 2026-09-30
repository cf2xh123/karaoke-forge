# Windows 原生桌面便携构建

这个构建生成 `onedir` 格式的 PyInstaller Windows x64 应用。用户解压整个 ZIP，
双击 `KaraokeForge.exe` 即可使用，无须安装 Python、Qt 或 FFmpeg。
它使用原生 PySide6 控件，不启动 Gradio 或 WebEngine。

构建脚本不会安装依赖，不会发布 GitHub Release，不会修改系统 PATH。
运行和发布仍须遵守仓库的 release 分支、CI、合并、不可变版本标签流程。

## 构建环境

在 Windows x64 使用干净的 **CPython 3.12 x64** 虚拟环境。不要使用 Anaconda
解释器或包含全部机器学习工具的大环境，以免混入其他 Qt、ICU、CUDA 或 Torch DLL。
可使用仓库 `scripts/bootstrap_windows.ps1` 准备的私有 CPython 创建专用环境，
也可以使用自行安装的官方 CPython 3.12。

下面假设构建解释器位于 `.build-venv\Scripts\python.exe`：

```powershell
# 将下载缓存和临时文件放在有足够空间的项目磁盘。
New-Item -ItemType Directory -Force .build-cache/temp,.build-cache/pip | Out-Null
$env:TEMP = (Resolve-Path .build-cache/temp).Path
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = (Resolve-Path .build-cache/pip).Path
& .\.build-venv\Scripts\python.exe -m pip install --upgrade pip
& .\.build-venv\Scripts\python.exe -m pip install "torch==2.8.0+cpu" "torchaudio==2.8.0+cpu" --index-url https://download.pytorch.org/whl/cpu
& .\.build-venv\Scripts\python.exe -m pip install ".[desktop,align,netease,pronunciation,separate]" "demucs==4.1.0" "soundfile>=0.13,<0.14" "pyinstaller>=6.22,<7" pyinstaller-hooks-contrib

# 准备经过仓库固定哈希校验的 FFmpeg essentials 分发包。
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/bootstrap_ffmpeg_windows.ps1

# 先检查完整命令；此命令不打包，也不下载许可证。
& .\.build-venv\Scripts\python.exe scripts/build_desktop.py --dry-run

# 正式构建。
& .\.build-venv\Scripts\python.exe scripts/build_desktop.py
```

构建脚本会检查私有 FFmpeg 的 `ffmpeg.exe`、`ffprobe.exe`、`LICENSE` 和
`README.txt`。可用 `--ffmpeg-root PATH` 指向已准备的完整 FFmpeg 分发目录，
其子目录必须为 `bin/ffmpeg.exe` 和 `bin/ffprobe.exe`。
脚本还会拒绝不匹配的 Torch/Torchaudio 或 GPU wheel：本便携构建固定为
`torch==2.8.0+cpu`、`torchaudio==2.8.0+cpu`、`demucs==4.1.0`。
Demucs 4.1 的推理使用 sphn/FFmpeg 读取音频；Torchaudio 随包提供匹配的 CPU 音频算子。

默认输出：

```text
dist/desktop/
  KaraokeForge/
    KaraokeForge.exe
    _internal/
      ffmpeg/bin/ffmpeg.exe
      ffmpeg/bin/ffprobe.exe
      ffmpeg/LICENSE
      ffmpeg/README.txt
      ...Python、Qt、第三方模块和应用资源...
    licenses/
    source/src/karaoke_forge/
    source/pyproject.toml
    source/README.md
    LICENSE
    THIRD_PARTY_NOTICES.md
    BUILDING.md
    build-manifest.json
    开始使用.txt
  KaraokeForge-X.Y.Z-windows-x64.zip
  KaraokeForge-X.Y.Z-windows-x64.zip.sha256
```

`--dist-dir PATH` 和 `--work-dir PATH` 可修改产物与中间文件目录。
`--no-zip` 仅创建目录，便于先验收、随后压缩。产物中的 `build-manifest.json`
记录应用版本、解释器版本、构建环境依赖版本，以及两个媒体工具的 SHA-256。
ZIP 必须包含整个 `KaraokeForge` 文件夹，不能只发布 `.exe`。

## 包含与不包含的功能

包含 PySide6、faster-whisper、CTranslate2、ONNX Runtime、PyAV、tokenizers、
Hugging Face Hub、yt-dlp、websocket-client、certifi、pykakasi、alkana 及其必要
资源；包含私有 FFmpeg/ffprobe、Qt 媒体插件、字幕样式和唱片背景资源。
包含 CPU PyTorch、Torchaudio、Demucs、sphn、julius、lameenc、safetensors
以及对应动态库，可直接生成人声轨和无人声伴奏轨，不依赖源码目录或项目 Python 环境。

Whisper 识别模型和 Demucs 预训练权重不预装。第一次使用相应功能时按现有下载
流程获取，之后复用用户缓存。自动设备选择在此 CPU 人声分离运行库中使用 CPU。
需要 NVIDIA 加速的 Demucs 用户可使用源码版安装相应 GPU 运行库。
Gradio 和 WebEngine 从原生便携包排除；网页版仍可通过源码安装入口使用。
不要在便携目录里执行 `pip install`：它不是通用 Python 虚拟环境。

## 用户数据与子进程

冻结启动器设置 `KARAOKE_FORGE_ROOT` 为 exe 所在目录，设置
`KARAOKE_FORGE_FFMPEG_DIR` 为包内私有媒体工具目录。默认可写数据放在：

```text
%LOCALAPPDATA%/KaraokeForge/
  model-cache/       模型缓存
  cache/             预览与中间缓存
    demucs-torch/    人声分离模型缓存
  outputs/           默认工程与输出
  logs/desktop.log   无控制台时的启动日志
```

现有 `KARAOKE_FORGE_SETTINGS_DIR`、`KARAOKE_FORGE_CACHE_DIR` 和
`KARAOKE_FORGE_OUTPUT_DIR` 显式环境配置仍会被尊重。
升级时可替换程序目录，用户数据不会写入或跟随 `_internal` 被替换。

窗口模式下标准输出可能是 `None`。启动器会优先恢复继承的 Windows 重定向管道，
没有管道时才写入用户日志。现有隔离下载协议
`sys.executable -m karaoke_forge.model_worker ...` 以及
`sys.executable -m karaoke_forge model-download ...` 会转发到对应入口；
`sys.executable -m demucs ...` 会转发到包内分离器。它们不会误开第二个桌面窗口。
已经存在的标准流也会明确设置为 UTF-8，保证中文、日文及其他 Unicode 缓存路径
在父子进程之间完整传递。其他 `-m` 模块不作为通用 Python 执行入口开放。
私有 FFmpeg 路径仅在当前程序及其子进程的 PATH 中优先，不修改系统 PATH。
媒体子进程默认不弹控制台；显式指定的子进程创建选项保持不变。

## 成品自检与验收

窗口程序不应依赖终端中的输出。使用 `Start-Process -Wait -PassThru` 等待自检，
同时检查退出码和 JSON 报告：

```powershell
$report = Join-Path $env:TEMP "karaoke-forge-self-test.json"
$process = Start-Process -FilePath ".\dist\desktop\KaraokeForge\KaraokeForge.exe" `
  -ArgumentList @("--self-test", ('"' + $report + '"')) -Wait -PassThru -WindowStyle Hidden
if ($process.ExitCode -ne 0) { throw "Portable self-test failed; read $report" }
$result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
if (-not $result.ok) { throw "Portable self-test reported a failure" }
```

自检不会登录网站或下载 Whisper/Demucs 预训练模型。它检查：

- 所有动态模块能否实际导入；
- Qt Windows 平台插件、媒体后端、PNG 资源和离屏原生绘制；
- 日语/英语注音字典及 TLS 证书包；
- 私有 FFmpeg/ffprobe 路径、libass 过滤器和一次短 H.264 编码；
- PyAV 音频解码及本地 VAD ONNX 模型加载；
- CPU Torch/Torchaudio 音频算子、SoundFile 音频读取；
- 冻结 exe 的隔离模型子进程是否能成功返回含中文和 emoji 的缓存路径；
- Demucs 在 CPU 上以官方内置的小型随机测试网络完成一秒音频的双轨分离，
  并核对 vocals.wav / no_vocals.wav 的采样数与声道数。

路径协议自检使用临时离线 snapshot 目录，不加载模型权重。随机测试网络仅验证
运行库和音频处理链路；正式成品的伴奏质量须用预训练权重与实际音乐另行验收。

发布前还需解压成品到新的目录，双击 exe，执行一个实际素材的“导入 → 校准 →
保存/重开 → 视频导出”流程，并确认后台处理不会弹控制台。
自检验证本地运行时完整性；实际 Whisper 模型首次下载、在线账号授权和 GPU 驱动
兼容性仍应按需要单独验收。优先用 CPU 在未安装 Python 的 Windows 机器或干净
Windows 环境中进行一次完整验收。

## 第三方许可

脚本保留完整 FFmpeg 许可与上游 README，收集构建环境 wheel 中的许可证，
将标准 Qt LGPL/GPL 文本和 CTranslate2 许可补入 `licenses/`，同时保存来源 URL。
补充许可文本首次从上游 HTTPS 地址获取，缓存于 `build/desktop/license-cache`；
构建不会在许可下载失败时静默继续。网络受限时，可先将对应文件放入该缓存目录。

Karaoke Forge 自身的 MIT 许可证不取代依赖项的 LGPL/GPL 等许可证。
`THIRD_PARTY_NOTICES.md`、`licenses/inventory.json`、包内 `source/` 与
`_internal/ffmpeg/LICENSE` 都属于分发内容，不应从发布 ZIP 中删除。
依赖列表属于构建环境清单，并不意味着每个开发工具都被编进 exe。

实现依据：
[PyInstaller 的 onedir、资源与动态模块选项](https://www.pyinstaller.org/en/stable/usage.html)，
[窗口模式标准输入输出说明](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html)，
[PyTorch 2.8 的配对 CPU 安装命令](https://pytorch.org/get-started/previous-versions/#v280)，
[Demucs 4.1.0 上游发行说明](https://pypi.org/project/demucs/4.1.0/)。
