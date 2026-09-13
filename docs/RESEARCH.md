# 技术选择与来源研究

## 下载来源

- [gallery-dl 官方项目](https://github.com/mikf/gallery-dl) 提供大量站点的现有提取器，以及下载与失败重试机制。本项目通过独立命令调用已安装的固定版本，不把整个框架复制进业务代码。已查看安装包里的 directlink 和 webtoons 提取器源码。直链下载已实测；不能据此保证每个韩漫站点都可用。
- [gallery-dl 配置文档](https://gdl-org.github.io/docs/configuration.html) 用于核对配置隔离；本项目使用 `--ignore-config --no-input`，避免意外加载现有账户配置。
- [manga-downloader](https://github.com/elboletaire/manga-downloader) 和 [AIO-Webtoon-Downloader](https://github.com/zzyil/AIO-Webtoon-Downloader) 也提供多来源方案。本轮没有 clone 或运行这些替代项目，避免给 MVP 增加多个未验证依赖。

本次没有验证到「可直接可靠完成任意漫画与小说精确章节匹配」的现成项目。能同时搜索漫画和小说，与能判定改编对应章节是不同能力；没有将搜索结果包装成已完成的匹配系统。

## 图像文字与配音

- [RapidOCR 官方快速开始](https://rapidai.github.io/RapidOCRDocs/main/quickstart/)：用于本机中文文字识别，运行 ONNX CPU 模型，模型随当前安装包提供。本项目没有训练或重新发布模型。
- [Edge TTS 项目](https://github.com/rany2/edge-tts)：用于无需密钥的在线中文配音。此为社区客户端；服务可用性取决于网络与上游。
- Windows System.Speech：调用本机已安装中文语音。本次在正常桌面权限下已实际生成中文 WAV；受限沙箱不能正常枚举系统语音，因此离线完整链路测试在正常桌面环境运行。
- [FFmpeg 官方文档](https://ffmpeg.org/ffmpeg-filters.html)：剪辑、重采样、响度处理、字幕、编码和完整解码校验。

## 示例视频观察

用户提供的 MP4：768×576，30 fps，约 1663.806 秒（27 分 43.8 秒），H.264 + AAC。

典型表现为漫画画面主体、底部白色描边短句字幕、跟随解说推进的镜头切换。MVP 采用 4:3 默认画面、轻微推近、独立连续配音和短句字幕；不添加背景音乐。没有复刻原片的逐帧剪辑节奏或声音。

参考抽帧接触表在 `docs/reference/`。`examples/reference_recut/` 的示范文案由本次开发编写，不能作为自动写稿质量证明；内置 `starter` 则从图片文字开始完整自动运行。

## 后续扩展顺序

1. 以真实漫画原稿和已配置视觉服务评估人物指代、剧情覆盖、重复内容与解说节奏。
2. 在章节级保留角色、场景与事件记录，再检索小说候选片段；低置信度匹配显式标注，不冒充确定章节。
3. 对番外和资讯保存来源、发布时间与证据位置，按用户的剧透策略选择可引用的世界观补充。
4. 把下载来源做成独立可更新适配器，并处理同名作品、不同译名、更新状态和下载完整性。

这些属于规划，不是当前已实现功能。
