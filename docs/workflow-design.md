# 原生工作台流程设计（0.16.0）

目标：一首歌只维护一个工作区，导入之后不再为了试听、校准、保存或输出，在制作页、
歌词编辑器和结果页之间手动交接工程。保留专业操作，但让低频选项按需出现。

## 官方产品流程对比

以下根据 2026-09-30 查阅的官方页面与手册整理，未安装或实测这些第三方产品。
“借鉴”列是本项目的设计判断，不意味着各产品实现完全相同。

| 产品 | 官方描述的流程 | 本项目借鉴 |
| --- | --- | --- |
| Karaoke Builder Studio | 随音乐按空格同步、减速与回退；独立精修窗口把词级时间条与屏幕预览放在一起；同步后可直接构建。 | 初步制作和深度精修可以分层；精修时预览跟着时间轴。 |
| AI Karaoke Video Creator | 起步可以使用向导；编辑器在同一工程中结合歌词、波形标签与预览，之后设置样式并导出。 | 起步短，工程连续；不把每个处理阶段都变成主导航页。 |
| Kanto Syncro | 主界面导入音频和文本、开始同步、撤销、变速与预览，Finish & Save 才进入输出窗口。 | 素材只导入一次；校准、试听留在原位，统一导出入口。 |
| KaraokeClip | 上传、可选转写纠正、编辑器；提供准确歌词时跳过转写纠正，编辑器内调整时间、样式、翻译并导出。 | 根据已提供的素材跳过不必要步骤；本项目继续使用本地原生处理。 |

来源：[Karaoke Builder 同步](https://www.karaokebuilder.com/kbstudio.php?pg=2)、
[精修](https://www.karaokebuilder.com/kbstudio.php?pg=6)、
[AI Karaoke Video Creator 官方流程](https://www.powerkaraoke.com/src/prod-ai-karaoke-video-creator.php)、
[手动创建向导](https://docs.powerkaraoke.com/books/common-pages/page/create-song-manually)、
[Kanto Syncro 官方教程](https://www.kantoeditor.com/kanto-syncro/)、
[KaraokeClip 官方流程](https://docs.karaokeclip.video/how-it-works/)。

## 在 Karaoke Forge 中的实现约束

- 工作台统一素材、工程名称、歌词状态和固定的保存/导出入口。
- 自动准备完成后，在现有工作台载入歌词；渲染结果内联展示，不改变编辑位置。
- 不再要求用户点击“交给制作页”。每次保存、渲染时从编辑器读取并验证最新草稿。
- 切换工程才恢复其他工程的素材与配置；当前工程的歌词快照不得覆盖用户刚改的样式。
- 保存保留撤销历史，错误草稿不得作为成功版本输出，未保存修改仍有离开保护。
- 处理期间锁定相关输入以保持任务快照一致，界面继续处理窗口、日志和状态事件。
- 低频工具和环境设置独立存在，日常制作无需经过它们。
