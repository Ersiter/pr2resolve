# .prproj 格式研究

> 基于 PRPROJ-READER (sergeiventurinov) 逆向工程 + Adobe ppro-scripting 文档
> 研究日期: 2026-06-04

---

## 1. 容器格式

`.prproj` = **gzip 压缩的纯 XML 文本**。

```python
import gzip
with gzip.open('project.prproj', 'rb') as f:
    xml_bytes = f.read()
# xml_bytes 是标准的 UTF-8 XML
```

不同于 DRT/DRP 的 ZIP 容器，.prproj 是单层 gzip，解压后直接是 XML 字符串。

---

## 2. XML 结构全景

```
<PremiereData Version="3">
  <Project ObjectRef="1"/>
  <Project ObjectID="1" ClassID="..." Version="30">
    <Node>
      <Properties>
        <MZ.Project.Name>项目名</MZ.Project.Name>
      </Properties>
    </Node>
  </Project>

  <ProjectSettings>
    <VideoSettings ObjectID="..." FrameRate="..." .../>
  </ProjectSettings>

  <BinProjectItem ObjectUID="..." ClassID="...">
    <ProjectItem><Name>素材箱名</Name></ProjectItem>
    <ProjectItemContainer>
      <Items>
        <Item ObjectURef="uuid-of-masterclip"/>
      </Items>
    </ProjectItemContainer>
  </BinProjectItem>

  <MasterClip ObjectUID="..." ClassID="...">
    <LoggingInfo ObjectRef="..."/>
    <Clips>
      <Clip ObjectRef="uuid-of-video-audio-clip"/>
    </Clips>
  </MasterClip>

  <VideoClip ObjectID="..." ClassID="9308dbef-2440-4acb-9ab2-953b9a4e82ec">
    <Clip>
      <InPoint>0</InPoint>
      <OutPoint>1000</OutPoint>
      <PlaybackSpeed>100</PlaybackSpeed>  <!-- 100 = 1.0x -->
      <PlayBackwards>false</PlayBackwards>
      <Source ObjectRef="media-source-uuid"/>
    </Clip>
  </VideoClip>

  <AudioClip ObjectID="..." ClassID="b8830d03-de02-41ee-84ec-fe566dc70cd9">
    <!-- 同 VideoClip 结构，另含 InPoint/OutPoint -->
  </AudioClip>

  <Sequence ObjectUID="..." ClassID="...">
    <Name>序列 01</Name>
    <Node>
      <Properties>
        <MZ.Sequence.PreviewFrameSizeWidth>1920</MZ.Sequence.PreviewFrameSizeWidth>
        <MZ.Sequence.PreviewFrameSizeHeight>1080</MZ.Sequence.PreviewFrameSizeHeight>
      </Properties>
    </Node>
    <TrackGroups>
      <TrackGroup>
        <First/><Second ObjectRef="uuid-of-trackgroup"/>
      </TrackGroup>
    </TrackGroups>
  </Sequence>

  <VideoTrackGroup ObjectID="uuid-of-trackgroup">
    <TrackGroup>
      <FrameRate>...</FrameRate>     <!-- Adobe 内部 timebase -->
      <Tracks>
        <Track ObjectURef="uuid-of-cliptrack"/>
      </Tracks>
    </TrackGroup>
  </VideoTrackGroup>

  <VideoClipTrack ObjectUID="uuid-of-cliptrack">
    <ClipTrack>
      <Track><ID>1</ID></Track>
      <ClipItems>
        <TrackItems>
          <TrackItem ObjectRef="uuid-of-trackitem"/>
        </TrackItems>
      </ClipItems>
    </ClipTrack>
  </VideoClipTrack>

  <VideoClipTrackItem ObjectID="uuid-of-trackitem">
    <ClipTrackItem>
      <IsMuted>false</IsMuted>      <!-- 可选 -->
      <TrackItem>
        <Start>0</Start>             <!-- Adobe timebase 单位 -->
        <End>1000</End>
      </TrackItem>
      <ComponentOwner>
        <Components ObjectRef="uuid-of-componentchain"/>
      </ComponentOwner>
      <SubClip ObjectRef="uuid-of-subclip"/>
    </ClipTrackItem>
  </VideoClipTrackItem>
</PremiereData>
```

---

## 3. UUID 引用模型

Adobe 用四种 UUID 属性构建引用图：

| 属性 | 语义 | 出现在 |
|------|------|--------|
| `ObjectID` | 当前元素被其他元素引用时的标识 | 数据节点（Clip、Filter、Component 等） |
| `ObjectRef` | "请去查这个 ObjectID 的元素" | 子元素引用父级数据 |
| `ObjectURef` | 全局唯一引用（跨文件持久化） | MasterClip→Bin、Track→TrackGroup |
| `ObjectUID` | 全局唯一标识 | Sequence、Bin、MasterClip、Media |

### 遍历示例：从 Sequence 找到一个 Clip 的 Scale 值

```
1. Sequence → TrackGroups/TrackGroup/Second[@ObjectRef]
2. VideoTrackGroup[@ObjectID==ref] → Tracks/Track[@ObjectURef]
3. VideoClipTrack[@ObjectUID==ref] → ClipTrack/ClipItems/TrackItems/TrackItem[@ObjectRef]
4. VideoClipTrackItem[@ObjectID==ref]
   ├─ ClipTrackItem/TrackItem/Start, End  → 时间线位置
   ├─ ClipTrackItem/ComponentOwner/Components[@ObjectRef]
5. VideoComponentChain[@ObjectID==ref] → Components/Component[@ObjectRef]
6. VideoFilterComponent[@ObjectID==ref] → Component/Params/Param[@ObjectRef]
7. VideoComponentParam[@ObjectID==ref] → Name="Scale", StartKeyframe="...,100.0"
   ├─ ClipTrackItem/SubClip[@ObjectRef]
8. SubClip[@ObjectID==ref]
   ├─ MasterClip[@ObjectURef] → 素材本身
   ├─ Clip[@ObjectRef]
9. VideoClip[@ObjectID==ref] → InPoint, OutPoint, PlaybackSpeed
```

---

## 4. Transform 数据结构

### Scale
```
VideoComponentParam
  ├─ Name: "Scale"
  ├─ StartKeyframe: "0,100.0"     → time=0, val=100.0
  └─ Keyframes: "0,100.0;100,50.0;200,100.0"  (可选，分号分隔)
```

PRPROJ-READER 注意：Scale 的 `StartKeyframe` 被解释为 "value * 100"（即 100.0 = 100%），但**未确认是否是 Scale to Frame Size 的残留**。

### Position
```
PointComponentParam
  ├─ Name: "Position"
  ├─ StartKeyframe: "0,0.5:0.5:0"  → x=0.5*width, y=0.5*height, t=0
  └─ Keyframes: (同上格式，分号分隔)
```
Position 的 x 分量 = **序列宽度的比例**（PRPROJ-READER 通过 `seq_width * value` 得到绝对坐标）。

### Rotation
```
VideoComponentParam
  ├─ Name: "Rotation"
  ├─ StartKeyframe: "0,45.0"      → 45 度
```

### Opacity
```
VideoComponentParam
  ├─ Name: "Opacity"
  ├─ StartKeyframe: "0,100.0"     → 100%
```

---

## 5. 帧率 / Timebase

Adobe 不使用标准 fps，而是用内部 timebase：

```python
# 从 PRPROJ-READER 逆推的公式
actual_fps = round((10594584000 * 23.976) / internal_timebase, 3)
```

视频 TrackGroup 有独立的 `FrameRate`（内部 timebase），序列本身不直接存 fps。

音频 TrackGroup 的 timebase 不同：`actual_sample_rate = round((5292000 * 48000) / audio_timebase)`。

---

## 6. 音频参数

```
AudioClipTrackItem
  └─ ClipTrackItem/ComponentOwner/Components → AudioComponentChain
       └─ AudioFilterComponent
            └─ AudioComponentParam
                 ├─ Name: "Level"
                 │    └─ StartKeyframe: "0,0.0"   → dB 值
                 └─ Panner → StereoToStereoPanProcessor
                      └─ AudioComponentParam
                           ├─ Name: "Balance"
                           └─ StartKeyframe: "0,0.0"
```

---

## 7. Lumetri 数据（待研究）

Lumetri 在 .prproj 中以 `SyntheticClip` + `VideoFilterComponent` 存在。PRPROJ-READER 未解析 Lumetri（仅处理了 Position/Scale/Rotation/Opacity）。这是 Phase 2 研究项——如果能从 .prproj 提取 Lumetri 原始参数值，就可以在生成 DRT 时将其映射到达芬奇 Color 节点。

---

## 8. 参考

- [PRPROJ-READER](https://github.com/sergeiventurinov/PRPROJ-READER) — 主要逆向工程来源
- [ppro-scripting.docsforadobe.dev](https://ppro-scripting.docsforadobe.dev) — Adobe 官方对象模型文档
- [prproj_downgrade](https://github.com/snorkem/prproj_downgrade) — 另一个 .prproj 工具（gzip+regex）
