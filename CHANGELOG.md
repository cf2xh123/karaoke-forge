# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)；格式参考 [Keep a Changelog](https://keepachangelog.com/)。

## [Unreleased]

后续计划见 [TODO.md](TODO.md)。

## [0.2.0] - 2026-07-31

### Added

- 网易云链接支持从本机 Chrome、Edge、Firefox 或 Brave 读取现有登录会话，检测该曲
  实际可用的 VIP/SVIP 音质，并只使用账号本来具有的最高播放权限；Cookie 不落盘。
- 新增多窗口音轨指纹匹配，可自动跳过 MV 片头/片尾剧情并定位歌曲开始位置，同时拒绝
  错误歌曲版本。
- 网易云中文翻译可写入项目 JSON，并在 ASS 中固定显示于画面顶部。
- ASS 原文采用传统 KTV 双行布局：两句成组、左上右下交错，当前句逐字变色。
- 日语汉字可自动生成平假名振假名，英语单词可自动生成片假名读音，并随原文扫色。
- 网易云歌词优先使用 YRC 真实逐字时间；普通行级 LRC/SRT 可根据演唱音频精修句内
  变速时间，并保持原有行首、行尾不动。
- 网页字幕样式区新增 16:9 实时预览，字体、字号、颜色、翻译和位置可即时查看。
- 会员曲目只返回试听片段时，“制作 MV”可自动回退到上传 MV 的完整内嵌音轨。

### Fixed

- 网易云公开信息请求改用 Certifi CA，修复部分 Windows 环境的 `ASN1: NOT_ENOUGH_DATA`
  证书加载错误。
- Chromium 浏览器正在占用 Cookie 数据库时，返回可操作的中文提示。
- 默认关闭面向语音的 VAD，避免伴奏较强的歌曲在中途被误判为无人声而导致歌词对齐覆盖率过低。
- 为纯文本歌词保留段落空行作为 Whisper 提示，改善重复副歌和尾声的识别连续性。

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
