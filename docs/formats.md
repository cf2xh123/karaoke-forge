# 歌词格式约定

所有内部时间均使用秒和浮点数，导出时再转换为目标格式精度。

## 纯文本

一行一句。空行、以 `#` 开头的行，以及 `[Verse]`、`【副歌】` 形式的段落名会被忽略。

## LRC

读取行级时间标签和常见元数据：

```text
[ar:Artist]
[ti:Title]
[00:12.34]第一句歌词
```

增强 LRC 使用内嵌的词开始时间：

```text
[00:12.34]<00:12.340>第一<00:12.800>句歌词
```

普通 LRC 导出为百分之一秒，增强 LRC 词标签导出为千分之一秒。

## SRT / WebVTT

只包含行级时间。为了生成逐词 ASS，高亮时间会在一行的所有词元之间平均分配。需要准确逐词高亮时，应优先使用增强 LRC 或 Karaoke Forge JSON。

## ASS

导出使用 Advanced SubStation Alpha v4+，每个词元通过 `\kf` 标签产生从普通色到高亮色的填充动画。读取第三方 ASS 时目前保留行级时间和可见文本，不保留第三方样式或原始逐词标签。

## Karaoke Forge JSON

JSON 是无损项目格式：

```json
{
  "version": 1,
  "metadata": {
    "language": "zh"
  },
  "lines": [
    {
      "text": "你好",
      "translation": "Hello",
      "pronunciation": null,
      "pronunciation_units": [
        {
          "source": "你",
          "reading": "nǐ",
          "start": 0,
          "end": 1
        }
      ],
      "hidden": false,
      "start": 1.0,
      "end": 2.0,
      "tokens": [
        {
          "text": "你",
          "start": 1.0,
          "end": 1.4,
          "confidence": 0.96
        }
      ]
    }
  ]
}
```

`pronunciation_units` 的 `start`/`end` 是原文中的字符区间，不是时间；逐词字幕时间仍由
`tokens` 保存。`hidden: true` 的行会保留在 JSON 中，但其他字幕格式和视频会跳过该行。
后续手工修订时间轴时建议保留一份 JSON，再从它导出其他格式。
