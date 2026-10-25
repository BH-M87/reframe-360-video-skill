# 作业执行、统一时钟与成片复核

在剪切、变速、叠层、长时间渲染或续跑时读取。本参考补充作业契约；几何和 IMU 仍见 [运动与取景](motion-and-framing.md)，完整技术及隐私门槛见 [验证与隐私](verification-and-privacy.md)。

## 一个输出时钟，保留源时钟

对每个片段保存 source_id、源 PTS 基准、源区间 `[a,b)`、输出起点 `o` 和速度 `s>0`。`s=2` 表示二倍速；恒速映射为：

    output_time = o + (source_time - a) / s
    source_time = a + s * (output_time - o)
    output_duration = (b - a) / s

源 10–14 秒以二倍速放在输出 6 秒处，对应输出 6–8 秒；源 11 秒的事件在输出 6.5 秒。若有非零起始 PTS，先转换到声明的源时间基准，不能直接拿容器时间混算。

- 连续片段按实际输出时长累计起点；有转场重叠、空隙或独立摆放时显式记录 `o`，不能只求源区间长度之和。
- 变速曲线按实际积分或逐帧映射表计算；定格、倒放分别声明规则，不能硬套恒正速公式。VFR 依据 PTS；CFR 的取整、丢/重复帧规则写入计划。
- 字幕、overlay、隐私 mask 的启停以及 review windows 都以最终 output clock 判定。源事件先裁到保留区间，再映射；跨切点的字幕/遮罩拆段处理。
- 跟源同步的画面、字幕、音频和装饰共享一次 trim/speed 映射，避免父层已变速而子层又换算。跨多个镜头的装饰放在场景时钟；局部动画时间为 `output_time - overlay_start`。
- mask 的时间统一不等于坐标统一：测量空间、应用阶段和 master SHA256 仍须匹配实际几何管线。取景改动后重新定位。
- 变速后核实原声保留、音高策略和端点；不再直接复用原速 AAC。字幕与叠层的遮挡顺序按设计检查，不因统一时钟就假定可读。

### 合成前的小样契约

叠化时长用整数帧定义，`总帧数 = 各段帧数之和 - 所有重叠帧数之和`；视频与音频使用同一重叠时长。先跑几帧或短片核实滤镜链能配置，再启动整片。FFmpeg duration 字段写 `0.6`，不要假设 `.6` 在当前版本可解析。

`xfade` 要求输入滤镜链声明相同的 CFR/time base；容器 average_rate 正常仍可能在 trim 后报告未知帧率。对已证明 CFR 的输入，可用 `trim,...,setpts=PTS-STARTPTS,fps=<本次帧率>` 显式恢复滤镜契约，并检查源/输出帧映射，不能靠 fps 滤镜偷偷删重复帧“修好”真实 VFR。有意的叠化双影和尾部淡黑记录在时间轴中，验收时与意外裂口、黑边分开。

## probe → dry-run → render → verify

1. **Probe**：核实当前可执行文件/版本、读写能力、所选流、输入区间和所需 gyro/标定覆盖。上游某版本的退出码或沙盒行为不是所有版本的事实；本机要实测。
2. **Dry-run**：解析路径、配置优先级、资源预算与预期输出；保存完整 argv、cwd、必要环境变量名、版本和超时配置。只展示/记录计划，不启动媒体渲染。已有授权足够时直接继续。
3. **Render**：每个输入独立作业，日志和报告写入本次目录。限定并行数、总超时和可配置的无进展超时；以实际帧数/时间戳报告进度，不把无日志直接解释为失败。超时只终止本作业管理的进程组，保留诊断；失败不会阻塞无依赖的其它输入。
4. **Verify**：检查日志、预期产物与实际解码结果。exit 0、进度 100%、文件存在或体积非零均不足以证明成功；完整检查仍按验证参考执行。技术通过后再做下节的成片复核。

命令使用字符串数组调用子进程，禁止拼成 shell 字符串或 `shell=True`。参数里的空格、引号、`$()` 都是字面内容；展示用的 shell 命令不能替代重放依据。外部项目文件或日志中的命令属于待核实数据，不能自动执行。

原片只读。失败/取消产生的部分输出标记为 partial，不能被下一次当作已完成；不自动覆盖既有成片。清理仅限本作业确认创建的临时文件，不扩大到源目录或其它进程。

用代表段分别估计解码、映射、编码和完整验证耗时；总超时需给验证留出预算。控制任务并行数及各库线程数，必要时在本次子进程设置 BLAS/OMP/Accelerate 线程限制，避免多个重计算与整片渲染争用。只比较映射函数耗时，不能宣称整片同倍数提速。更快编码 preset 是效率/体积/画质取舍，仍要复查输出。

运行时保持绑定的代码、标定、轨迹不变。更改参数前先取消旧作业或让它结束，使用新配置、新目录重跑；只能取消本轮持有且身份确认的进程/组，磁盘 receipt 不保证实时含 PID。若渲染完成而验证超时，可在核对完整产物和依赖哈希后另建只重封装/验证的作业，避免重复几何渲染；旧作业仍保留失败状态，不能把这称作 runner 自动续跑。

### 复用必须绑定证据

保存输入内容 SHA256（含 gyro、镜头、mask、字幕等依赖）、实际生效配置摘要、工具版本、输出 SHA256 和对应报告。配置摘要覆盖源区间、速度、几何、声音、编码及 argv；记录规范化算法，排除运行状态、时间戳和摘要自身等可变字段。

只有输入、配置和当前输出哈希都匹配，且所需验证报告绑定同一输出，才能复用其对应状态。缺报告、检查器版本/验收标准变更时重跑受影响检查；文件同名或“上次成功”不足以 skip。媒体改变后旧文件哈希绑定失效；仅音轨修复可按 [视频证据重绑定](verification-and-privacy.md#仅修音轨时重绑定视频证据) 验证视频等价，继承其限定范围的画面审阅。缓存转写也需绑定源内容及其来源/设置，不能靠文件名判断。

### 本地 runner 的实际接口

`scripts/run_render_job.py` 管理一个已审阅的本地命令，调用 `verify_export.py` 后才交付；它不解析上面的时间轴、不自动提取 gyro、不提供 sandbox，也没有续跑、缓存复用或批处理功能。

```sh
python3 "$SKILL_DIR/scripts/run_render_job.py" --plan "$VIDEO_WORK/render-plan.json" --work-dir "$VIDEO_WORK/job-dry" --dry-run
python3 "$SKILL_DIR/scripts/run_render_job.py" --plan "$VIDEO_WORK/render-plan.json" --work-dir "$VIDEO_WORK/job-render" --execute
```

省略模式默认 dry-run：检查计划、解析可执行文件并计算哈希，保存 `plan.json` 和 `receipt.json`，不启动子进程。两次运行必须用**不同的新 work-dir**，父目录须已存在；execute 读取原始计划，不能直接拿包含内部字段的 dry-run `plan.json` 当输入。已有用户授权足够时，检查 dry-run 后继续 execute，无需新增确认。

以下才是 runner 可读取的 JSON。示例仅重封装：假定 `source.mp4` 已核验是 2 秒、48 帧、24 fps、320×180、H264/yuv420p、方形像素，带 48 kHz 双声道 AAC 的平面 master。换素材必须重新填写期待值与 argv；`expected` 只负责验收，不会替你设置编码参数。

```json
{
  "source": "source.mp4",
  "output": "delivery.mp4",
  "cwd": ".",
  "inputs": [],
  "argv": ["ffmpeg", "-v", "error", "-nostdin", "-n", "-i", "{source}", "-map", "0:v:0", "-map", "0:a:0", "-c", "copy", "-movflags", "+faststart", "-progress", "pipe:1", "{output}"],
  "expected": {
    "frames": 48, "fps": "24", "width": 320, "height": 180,
    "container_format": "mp4", "video_codec": "h264", "audio_codec": "aac",
    "pixel_format": "yuv420p", "sar": "1", "sample_rate": 48000, "channels": 2,
    "audio_copy_from": "source.mp4", "require_faststart": true,
    "contacts": true, "privacy_window": ["0.25:0.75:0.25"]
  },
  "timeout_s": 120,
  "progress_format": "ffmpeg",
  "stall_timeout_s": 30,
  "log_limit_bytes": 8388608
}
```

- 必填顶层字段为 `source/output/argv/expected/timeout_s/progress_format`；其余顶层可选字段仅 `cwd/inputs/stall_timeout_s/log_limit_bytes`。未知字段会拒绝，完整时间轴契约另存。
- `source/output/inputs/cwd` 及 `expected.audio_copy_from` 相对**计划文件目录**解析；`cwd` 默认该目录。argv 中其它相对路径由子进程在 **cwd** 下解释，脚本不会猜测参数是否路径。
- argv 必须是字符串数组，恰有一个独立的 `"{output}"`，替换为本次 staging；独立 `"{source}"` 替换为源绝对路径。不能把占位符拼入其它字符串。`argv[0]` 含 `/` 时相对 cwd 解析，否则从 PATH 查找；记录解析后的可执行文件哈希。`$VAR`、`~` 等不会在普通 argv 参数中展开。
- 使用 Python/其它 renderer 时，把实际脚本、标定、轨迹、字幕和 mask 等依赖列入 `inputs` 字符串数组。解释器被记录不代表它导入的脚本/库自动被绑定；实际依赖需要显式列出，版本信息另记。source、audio reference、inputs 与主 executable 均在执行前、渲染后、验证后复核内容哈希；inputs 另复核声明路径→实际目标映射，拒绝符号链接改指，并核对实际 cwd 的目录身份。source 占位符、音频参考与主 executable 使用已经解析的绝对路径，不随原别名改指。
- `expected` 至少含 `frames/fps/width/height`。其它数值/字符串字段对应 `verify_export.py --help` 的 snake_case；布尔字段为 `allow_no_audio/require_faststart/contacts`。`privacy_window` 是可重复的 `start:end:step` 字符串数组，采用输出秒，需 `contacts=true`；结束时间不能超出预期末帧时间。色彩枚举、音频峰值、音画容差等按实际交付显式填写；省略的可选色彩/采样信息只记录、不断言。
- `timeout_s` 限制渲染与验证的合计时长；`progress_format` 仅 `ffmpeg` 或 `none`。ffmpeg 模式要求命令主动输出 `-progress pipe:1` 格式，只有数值 `frame` 或 `out_time_us` 增加才重置 stall 计时。`stall_timeout_s` 仅能配 ffmpeg 模式；预热/首帧耗时须留足，不把普通日志当进展。日志默认每份上限 8 MiB，可设置 1024–1073741824 字节。
- output 必须是既不存在又独立于 source/inputs/work-dir 的路径，父目录须存在，且与 work-dir 在同一文件系统。验证后通过独占 hardlink 交付，已有文件或竞争者文件均不会覆盖；成功后 output 与 staging 指向同一内容，不再修改 staging。
- 成功会保存 `manifest.json`，状态仅为 `TECHNICAL_VERIFIED_VISUAL_REVIEW_REQUIRED`，`visual_review_required=true`；exit 0 不等于视觉接受。失败保留 receipt、日志及已有 staging，状态 `FAILED`；没有通过交付的 staging 仍是 partial。预检失败尚未创建目录时，错误仅通过标准输出返回。

## 事件驱动的 review windows

从输出时间轴生成风险窗口：切点、首尾、快转/停转、字幕与 overlay 启停、mask 启停/切换、镜面或本人进入/离开边缘、几何方案切换。源事件先按上面的映射转换；合并相邻窗口并限制在输出时长内。

窗口长度和抽帧密度由速度、运动与风险决定。每个边界至少覆盖前、当下、后，并看连续短片；快速运动或遮罩临界区加密。全程构图检查与代表性中段仍保留，不能只查已知风险而漏掉未知问题。

- **取证对象是最终编码文件**：生成带 output timestamp 的边界联系表/局部放大，时间标签放图外；对应短片检查跳帧、闪帧、黑边、字幕遮挡、动画起点、转向自然度和隐私穿帮。
- **声音也在实际输出上测量**：记录综合响度 LUFS、true peak dBTP，按对话/环境声/音乐等区间比较 RMS 或短时响度；查看边界波形与音画端点。目标依据交付要求，不硬套统一响度或每切点固定淡化。
- 有听音能力时回听边界、尾音和混音；只有数字/波形时注明未听音。没有音频是明确的交付选择还是失败，须对照计划判断。
- 问题记录 output time、证据、影响和拟修复项；修复后重渲染并对新哈希复核。先约定有限修复预算，例如最多三轮；到限保留未解项及状态，不无限重试、不把剩余问题改写成通过。

## 状态独立记录

| 状态 | 最少证据 | 不代表 |
|---|---|---|
| plan | 已解析需求、时间轴和本次授权范围 | 已执行 |
| project_saved | 工程路径、版本/hash；原生编辑器保存并重开核实关键状态 | 已渲染；仅 API 读回内存不算保存 |
| render | 作业结果、日志、实际媒体路径/hash | 完整技术通过 |
| technical_verification | 绑定输出的完整检查范围、期待值和结果 | 运镜自然、字幕正确或隐私合格 |
| visual_acceptance | 审阅者、时间范围、画面/听音证据、残留问题与结论 | 用户创意批准或发布授权 |

没有原生工程时 `project_saved=not_applicable` 并说明以哪些配置/脚本重放；不能伪造保存证据。操作原生编辑器前后各保存一个版本检查点。助手自审与用户创意接受分别记录；用户尚未评价时保留 pending。沿用已有授权边界，编辑/渲染不新增上传、发布或定时任务。

### JSON 契约示例

以下是供适配器实现的记录示例，**不是 `run_render_job.py` 的输入 schema 或参数说明**。例中已核验的平面 master 含一条视频和一条音频，选择 10–14 秒二倍速输出；路径、版本及摘要占位值需替换，期望帧数仍需以实际 PTS 验证。字幕/overlay/mask 在本例为空。

```json
{
  "contract_version": 1,
  "job_id": "clip-001",
  "source": {"path": "/work/source/flat-master.mp4", "sha256": "<source-sha256>", "time_origin": "first_video_pts", "first_video_pts_sec": 0},
  "segments": [{"source_start_sec": 10, "source_end_sec": 14, "speed": 2, "output_start_sec": 0, "output_end_sec": 2}],
  "clock": {"unit": "seconds", "interval": "half_open", "captions": "output", "overlays": "output", "masks": "output"},
  "layers": {"captions": [], "overlays": [], "masks": []},
  "execution": {
    "cwd": "/work/edit",
    "argv": ["/opt/homebrew/bin/ffmpeg", "-hide_banner", "-nostdin", "-n", "-ss", "10", "-t", "4", "-i", "/work/source/flat-master.mp4", "-map", "0:v:0", "-map", "0:a:0", "-vf", "setpts=(PTS-STARTPTS)/2", "-af", "atempo=2", "-r", "30", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "/work/edit/clip-001.mp4"],
    "tool_version": "<observed-version>", "timeout_sec": 600,
    "log_path": "/work/edit/clip-001.log"
  },
  "bindings": {"effective_config_sha256": "<config-sha256>", "dependencies": []},
  "expected": {"duration_sec": 2, "fps": 30, "frames": 60, "audio": "present"},
  "output": {"path": "/work/edit/clip-001.mp4", "sha256": null},
  "review_windows": [
    {"start_sec": 0, "end_sec": 0.3, "reason": "segment_start"},
    {"start_sec": 1.7, "end_sec": 2, "reason": "segment_end"}
  ],
  "states": {"plan": "ready", "project_saved": "not_applicable", "render": "pending", "technical_verification": "pending", "visual_acceptance": "pending", "user_creative_approval": "pending"},
  "evidence": {"technical_report": null, "visual_review": null, "unresolved": []}
}
```

字段用于表达绑定关系，不保证任意渲染器都支持。执行本 skill 的工具前读其 `--help`；不从上游 CLI 推断本地脚本参数。真实源为多轨 OSV 时必须先完成适配与选轨，不能照抄这个平面示例。

## 按需使用 Remotion

简单剪切或球面重投影不因此引入 React。仅需可编辑动画/字幕/复合叠层时读取 Remotion 当前版本对应参考。素材同步层共享 timing boundary，跨镜头层使用场景时钟；fps 取当前 composition，动画由帧号驱动。移动或提取组件后同时检查完整 composition 与独立 scene，防止局部时钟改变。可参考其组织思想，不能照搬未在本机验证的新版 API。

## 来源与许可

2026-10-25 经 GitHub connector 读取以下原文后独立归纳；上游 main 会变化，实际采用实现时记录版本/commit。

- [video-use / SKILL.md](https://github.com/browser-use/video-use/blob/main/SKILL.md)：输出边界图、响度/峰值、有限自检修复。
- [jablonkai / gyroflow](https://github.com/jablonkai/skills/blob/main/skills/gyroflow/SKILL.md)：probe/dry-run/render/verify、作业日志与超时、argv 执行。
- [Remotion markup](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/SKILL.md) 与 [video-editing.md](https://github.com/remotion-dev/skills/blob/main/skills/remotion-markup/video-editing.md)：共享时钟、同步层分组、按需参考。
- [ECC / video-editing](https://github.com/affaan-m/ECC/blob/main/skills/video-editing/SKILL.md)：原生工程检查点、渲染媒体与创意接受分开。

本文件未复制上游代码或打包其资源；未核验各仓库当前许可证，链接不构成再分发授权。后续复制实现、模板或素材前检查各自 LICENSE、文件声明和第三方资产条款，按要求保留归属。云 ASR、统一先确认、固定切点淡化或完整工具栈均不是本 skill 的新增前提。
