# 原生转换的依据与验收

v1.6.0 的目标是把源工程的参数和对象关系写成 Resolve 21 数据，而不是继续积累针对某几个样本的补丁。原生 DRP 仍处于实验阶段；离线包检查不等于完成 Resolve 导入和视听验收。

## 本次确认和修正

| 问题 | 当前实现 | 证据与边界 |
|---|---|---|
| 多序列共用模板的 24 fps、1920×1080 | 每个池内 `Sm2Sequence` 写入自己的 `FrameRate`、`Resolution`、`MediaExtents` | 对照 Resolve 导出和 Relink 产物，离线回读测试；尚未实机验收 |
| 新增时间线未注册到工程 | `TimelineHandleVec` 从池内各序列的 `Parent` 建立 | 三类产物中的引用关系相符；切换卡顿是否完全解决仍需实机确认 |
| 两份 Motion 二进制夹带样本位移及其他效果 | 独立生成参数曲线，静态值不依赖样本白名单 | 原生样本证实现代 transform section 4 的参数 42/43、40/41、47；第三方旧 rotation=1 解码器不能覆盖现代格式 |
| 变速片段仍写成 1× identity map | 正向恒速视频写入 `Sm2TimeMap`，源入点换算到 retimed record domain | 独立第三方解码器确认 50%/200% 的斜率；播放与渲染仍需验收 |
| 混合帧率的 In/Out 使用了错误时钟 | `.prproj` 从原 ticks 直接换算到 XML clipitem.rate；native 也按 clipitem.rate 解读 trim | 原 ticks、量化/裁切误差保留在源状态；素材自身帧率仍决定媒体范围 |
| 变速图的入点与源位置混用 | 以 `speedkfin.when` 为 record In、`speedkfin.value` 为 source In；保留全图和时间线起止 | 实际 XML 中 260 与 169 的非等值样本回归，按 Apple 出点公式核对最后显示帧；原生分段线性近似不等于已证明渲染一致 |
| API XML 导入可能忽略变速 | 导出后统一修正包内映射，所有时间线校验完成后一次替换 | native/API 包解码一致、整包失败不变；图片共用模板定义的 source-zero hold。实际 API 导出包导入及渲染未验证 |
| 第一视频轨为空就漏选序列 | 累计所有视频、音频轨内容 | 合成后轨与纯音频序列回归；字幕轨另行记录未转换状态 |
| 中文 Motion 参数名被漏读，动画被压成标量 | 按 ParameterID 解析，XML 写关键帧并保留原始记录 | 与 Premiere 直出 XML 静态参数对照；Bezier 仅作线性近似 |
| 同名素材、同名序列被移动到错误媒体夹 | 素材路径、源序列 UID、bin 父 UID 决定归属 | 合成重名及嵌套 bin 回归；仅导出所选序列使用的媒体 |
| `--no-xml` 同时屏蔽报告 | 报告独立于 XML，原生导出拒绝时保留报告 | CLI 回归，测试禁止调用 Resolve 生命周期接口 |
| native 时间线 marker 位置减半且正文为空 | 按 Resolve 实录写入逐条 map entry、嵌套 sint 帧键与 marker 正文，以序列 DbId 关联 | Resolve Studio 21.0.2.4 双时间线导入、保存和重开回读通过，覆盖帧 0/10/37、范围、中文及 emoji；固定实录字节回归 |

## 颜色、标记与编辑状态

片段标签颜色已有 16 档 Premiere 预设到 Resolve 的映射；独立轨道颜色与旗标尚未实现，不能把片段标签当作旗标。自定义标签 RGB 也未建立映射。

时间线 marker 写入位置、名称、注释和时长。源 marker 颜色索引仍保留在 XML 注释中，目前输出 Blue；有颜色索引时会明确告警。Adobe 的 [Marker API](https://developer.adobe.com/premiere-pro/uxp/ppro-reference/classes/marker/) 将颜色、正文、时长分开处理，不能套用片段标签的颜色表。Resolve Studio 21.0.2.4 实录确认每个 map entry 含嵌套的 sint 帧键和独立正文；旧编码把帧键直接放入正文容器，导致实机读到半数位置及空白标签，已修正。编码器使用标准库生成 `0x80` 原始封装；实录也有 `0x81` 压缩封装，验收先解压后比较完整 protobuf 字节，再导入原生包并保存重开核对 API 返回的全部 marker 字段。

轨道名称写入 `Sm2TiTrack.UserDefinedName`，锁定使用 `Flags` 的 bit 0，禁用/静音使用 bit 1。位值由 Resolve 21 的同一 `DC` 时间线 DRP 与 XML 配对确认：0=启用且未锁定、1=锁定、2=禁用、3=两者都有。native 和 API 共用 FCP7 track 的名称、enabled、locked 状态；测试分别检查输出包与 API 调用。轨道 solo 尚未映射。

序列 In/Out、工作区尚未写入 native DRP；存在明确源状态时会告警。API 路径能设置成对 In/Out、清除明确未设置的 marks，单边状态会提示未应用；它会报告 marker 冲突及 API 拒绝，片段标签匹配也计入时间线起始帧偏移。轨道 marker 仅保留源记录，源片段 marker 尚未完整提取；两者都没有目标写入实现。上述 API 行为仅完成模拟测试，出点端点语义和可见状态仍需实机对照。

## 行业实现中值得采用的做法

Lumetri 现在按源组件顺序、blob 内关键帧与 shader 顺序读取；每个效果保留全部参数和原 XML。目标以每个 Lumetri 一个串行节点生成 DRX；native 的 clip version Body 与 API `GetNodeGraph().ApplyGradeFromDRX` 使用相同字节。空视频轨不再压缩调色身份索引，API 匹配计入时间线起始帧；应用失败不会继续把 DRT/DRP 报为成功。原生版本表的 `pActive`、`HasCorrection` 与双冒号类名按实际导出结构写入。

DRX 节点字段依据[第三方 DRX codec](https://github.com/samuelgursky/davinci-resolve-mcp/tree/main/resolve-advanced/vendor/drx-codec)及实录 fixture 独立实现，参考仓库为 MIT 许可，未复制其代码。独立解码器已核对多节点及连接，测试覆盖 20 个节点时的标识冲突。跨软件公式仅为近似：曝光 → gain `2^stops`，对比度 → `1 + PR/100`，饱和度 → Resolve UI `PR/2`。这些公式未经跨软件画面对照；LUT、其他 shader、曲线和动画未映射。含动画的源效果不会被悄悄压成静态值。相同 Body 只证明两条路径共用调色数据，不证明 Resolve 已加载或渲染相同。

字幕源解析按实际 `FormattedTextData` 的 FlatBuffer 表、offset 和字符串长度读取文本 runs，并按 BlockVector 顺序拼接；不扫描“看起来像文字”的字节。无法识别的结构保留原负载并标记解码失败。28 条实际工程字幕均能读取，字体列表、原 ticks、BinaryHash 和 block 引用保存在 XML 状态中。最小原生 Subtitle 编码器已有独立测试，但尚未接入工程输出；完整样式及 native/API 字幕转换仍未完成。

- [Blackmagic Resolve scripting API](https://www.blackmagicdesign.com/developer/) 提供导入后读取与设置时间线、轨道和片段属性的正式接口。它适合作为验收通道；用户正在工作的 Resolve 会话不应成为自动测试环境。
- [Apple XMEML 时间规则](https://developer.apple.com/library/archive/documentation/AppleApplications/Reference/FinalCutPro_XML/Topics/Topics.html) 区分时间线区间与源区间。源帧率、序列帧率、速度和端点约定必须一起处理，不能从片段时长推断完整源时长。
- [OpenTimelineIO FCP7 adapter](https://github.com/OpenTimelineIO/otio-fcp-adapter) 提供编辑时间结构与互通参考；其支持范围不能代替私有效果和 Resolve 原生序列化的实现。当前仍保留零运行时外部依赖。
- [samuelgursky/davinci-resolve-mcp 的 drp-format](https://github.com/samuelgursky/davinci-resolve-mcp/tree/main/resolve-advanced/vendor/drp-format) 提供 keyed-dict、time map、marker 等独立格式证据。尤其应采用其“生成→导入→API 回读→渲染检查”的验收方法，关注[已公开的失败案例](https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/docs/reference/api-limitations.md)。第三方 README 中的验证声明仍需对照实际 harness 与结果，不能只按宣传判断。

本项目的新时间映射代码是独立的 Python 标准库实现，格式字段参考上述公开资料并用独立解码器比对，没有引入第三方代码或依赖。

## 尚未建立的等价关系

- **图像缩放基准**：native zoom 补偿假设模板采用 fit 输入缩放。[Blackmagic 官方手册](https://documents.blackmagicdesign.com/UserManuals/DaVinci_Resolve_12_Reference_Manual.pdf)说明还有 center crop、fill、stretch 等模式；尚未从 Resolve 21 私有 blob 确认模板的具体枚举或逐序列覆盖。位置、旋转和缩放必须再用可见帧验证。
- **动画与复杂效果**：XML 的线性插值不等于 Premiere Bezier；native 不会把 Motion 动画静默变成静态值。单调 graphdict 视频曲线采用分段线性映射，反向、定格、Bezier/anchoroffset、音频变速、非默认锚点、转场、嵌套与生成器仍需要专门实现与样本。API 导出包使用同一编码器修正 `In` 和 `MediaTimemapBA`，包内其他字段与成员保持原样；无法唯一匹配时拒绝报告成功。API 内存时间线不随之改变，两路实际导入及渲染等价性尚未建立。
- **工程状态**：marker 颜色、轨道/片段 marker、In/Out、旗标、Fairlight strip 与音轨路由尚未完整写入。轨道名称、mute/lock 已写入，solo 未映射。保留源记录只表示能够追查，不表示已应用。源起始时码写入媒体缓存，仍需确认 Resolve 的可见表现。
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
