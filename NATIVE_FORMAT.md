# 原生转换的依据与验收

v1.6.0 的目标是把源工程的参数和对象关系写成 Resolve 21 数据，而不是继续积累针对某几个样本的补丁。原生 DRP 仍处于实验阶段；离线包检查不等于完成 Resolve 导入和视听验收。

## 本次确认和修正

| 问题 | 当前实现 | 证据与边界 |
|---|---|---|
| 多序列共用模板的 24 fps、1920×1080 | 每个池内 `Sm2Sequence` 写入自己的 `FrameRate`、`Resolution`、`MediaExtents` | 对照 Resolve 导出和 Relink 产物，离线回读测试；尚未实机验收 |
| 新增时间线未注册到工程 | `TimelineHandleVec` 从池内各序列的 `Parent` 建立 | 三类产物中的引用关系相符；切换卡顿是否完全解决仍需实机确认 |
| 两份 Motion 二进制夹带样本位移及其他效果 | 独立生成参数曲线，静态值不依赖样本白名单 | 原生样本证实现代 transform section 4 的参数 42/43、40/41、47；第三方旧 rotation=1 解码器不能覆盖现代格式 |
| 变速片段仍写成 1× identity map | 正向恒速视频写入 `Sm2TimeMap`，源入点换算到 retimed record domain | 独立第三方解码器确认 50%/200% 的斜率；播放与渲染仍需验收 |
| 第一视频轨为空就漏选序列 | 累计所有视频、音频轨内容 | 合成后轨与纯音频序列回归；字幕轨另行记录未转换状态 |
| 中文 Motion 参数名被漏读，动画被压成标量 | 按 ParameterID 解析，XML 写关键帧并保留原始记录 | 与 Premiere 直出 XML 静态参数对照；Bezier 仅作线性近似 |
| 同名素材、同名序列被移动到错误媒体夹 | 素材路径、源序列 UID、bin 父 UID 决定归属 | 合成重名及嵌套 bin 回归；仅导出所选序列使用的媒体 |
| `--no-xml` 同时屏蔽报告 | 报告独立于 XML，原生导出拒绝时保留报告 | CLI 回归，测试禁止调用 Resolve 生命周期接口 |
| native 包遗漏时间线 marker | 生成 `Sm2SequenceLockableBlob`，以序列 DbId 关联；多序列合并时重写 owner | Resolve 21 实际导出使用 `0x80` 原始 protobuf；第三方解码器交叉检查位置、名称、注释和时长；尚未实机验收 |

## 颜色、标记与编辑状态

片段标签颜色已有 16 档 Premiere 预设到 Resolve 的映射；独立轨道颜色与旗标尚未实现，不能把片段标签当作旗标。自定义标签 RGB 也未建立映射。

时间线 marker 写入位置、名称、注释和时长。源 marker 颜色索引仍保留在 XML 注释中，目前输出 Blue；有颜色索引时会明确告警。Adobe 的 [Marker API](https://developer.adobe.com/premiere-pro/uxp/ppro-reference/classes/marker/) 将颜色、正文、时长分开处理，不能套用片段标签的颜色表。第三方格式资料中的 Resolve 19 marker 压缩封装与本地 Resolve 21 导出的封装不同；本项目采用后者，交叉解码时只转换封装以核对 protobuf 内容，不把此检查当作 Resolve 导入验证。

序列 In/Out、工作区和轨道静音/锁定尚未写入 native DRP；存在明确源状态时会告警。API 路径能设置成对 In/Out、清除明确未设置的 marks，单边状态会提示未应用；它会报告 marker 冲突及 API 拒绝，片段标签匹配也计入时间线起始帧偏移。轨道 marker 仅保留源记录，源片段 marker 尚未完整提取；两者都没有目标写入实现。上述 API 行为仅完成模拟测试，出点端点语义和可见状态仍需实机对照。

## 行业实现中值得采用的做法

- [Blackmagic Resolve scripting API](https://www.blackmagicdesign.com/developer/) 提供导入后读取与设置时间线、轨道和片段属性的正式接口。它适合作为验收通道；用户正在工作的 Resolve 会话不应成为自动测试环境。
- [Apple XMEML 时间规则](https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/FinalCutPro_XML/Topics/Topics.html) 区分时间线区间与源区间。源帧率、序列帧率、速度和端点约定必须一起处理，不能从片段时长推断完整源时长。
- [OpenTimelineIO FCP7 adapter](https://github.com/OpenTimelineIO/otio-fcp-adapter) 提供编辑时间结构与互通参考；其支持范围不能代替私有效果和 Resolve 原生序列化的实现。当前仍保留零运行时外部依赖。
- [samuelgursky/davinci-resolve-mcp 的 drp-format](https://github.com/samuelgursky/davinci-resolve-mcp/tree/main/resolve-advanced/vendor/drp-format) 提供 keyed-dict、time map、marker 等独立格式证据。尤其应采用其“生成→导入→API 回读→渲染检查”的验收方法，关注[已公开的失败案例](https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/docs/reference/api-limitations.md)。第三方 README 中的验证声明仍需对照实际 harness 与结果，不能只按宣传判断。

本项目的新时间映射代码是独立的 Python 标准库实现，格式字段参考上述公开资料并用独立解码器比对，没有引入第三方代码或依赖。

## 尚未建立的等价关系

- **图像缩放基准**：native zoom 补偿假设模板采用 fit 输入缩放。[Blackmagic 官方手册](https://documents.blackmagicdesign.com/UserManuals/DaVinci_Resolve_12_Reference_Manual.pdf)说明还有 center crop、fill、stretch 等模式；尚未从 Resolve 21 私有 blob 确认模板的具体枚举或逐序列覆盖。位置、旋转和缩放必须再用可见帧验证。
- **动画与复杂效果**：XML 的线性插值不等于 Premiere Bezier；native 不会把动画静默变成静态值。反向、速度曲线、音频变速、非默认锚点、转场、嵌套与生成器仍需要专门实现与样本。
- **工程状态**：marker 颜色、轨道/片段 marker、轨道 mute/lock、In/Out、旗标、Fairlight strip 与音轨路由尚未完整写入。保留源记录只表示能够追查，不表示已应用。源起始时码写入媒体缓存，仍需确认 Resolve 的可见表现。
- **Relink 服务端**：公开前端与输出包只能提供行为和格式约束，没有恢复其服务端源码。本次不把浏览器中的选项、厂商陈述或压缩包大小当作保真度证明。

## 如何继续验收

离线运行（不会调用 Resolve，也不需要真实媒体）：

```bash
python .github/scripts/ci_smoke_tests.py
python -m unittest discover -s .github/scripts -p '*_tests.py' -v
python tests/run_local_tests.py --mode full
```

下一步应在独立、闲置的 Resolve 21 环境中，以合成彩条、坐标网格、帧号和已知频率音频逐项验证。每次只改变一个参数：导入后读取帧率、分辨率、源入点、速度、轨道和媒体夹，再检查导出的画面及音频。多序列切换和关闭重开也应纳入验收。不能仅以 ZIP 完整、文件较小、结构读回或没有报错作为通过标准。

优先补齐的实现顺序是：确认输入缩放枚举及像素坐标变换；为线性/Bezier 动画建立可见帧对照；独立实现音频路由与 retime；再处理 marker 颜色与轨道/片段范围、字幕、转场与嵌套。每类能力都需要自己的样本与回归，避免再用一个工程的二进制状态替代算法。
