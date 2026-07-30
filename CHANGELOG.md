# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)；格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]

### Fixed

- 默认关闭面向语音的 VAD，避免伴奏较强的歌曲在中途被误判为无人声而导致歌词对齐覆盖率过低。
- 为纯文本歌词保留段落空行作为 Whisper 提示，改善重复副歌和尾声的识别连续性。

### Planned

- 可视化时间轴编辑器；
- 音素级强制对齐后端；
- 双语与双行字幕；
- Docker 镜像和可复现模型配置；
- 批量项目清单。

## [0.1.0] - 2026-07-30

### Added

- `align`：使用 faster-whisper 时间戳对齐用户歌词；
- `convert`：LRC、增强 LRC、SRT、VTT、ASS、JSON 相互转换；
- `render`：通过 FFmpeg 烧录逐词高亮字幕并可替换音轨；
- `make`：串联识别、对齐、格式导出和视频渲染；
- `web`：面向非技术用户的本地可视化工作台；
- Windows 首次安装与双击启动脚本；
- 网易云公开单曲链接解析、LRC 获取与匿名公开音频适配器；
- 会员歌曲可配合用户合法导出的本地标准音频，不读取 Cookie、不处理 NCM；
- 一键 MV 流程可直接采用网易云公开歌词；
- 可选 Demucs 人声分离；
- `doctor` 环境检查；
- 中英文 README、算法/格式文档、测试与 GitHub Actions。
