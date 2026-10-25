# Reframe 360° Video

一个用于全景视频取景、相机路径平滑、可选滚动快门映射和成片验证的 Agent Skill。适合把360°素材剪成横屏或竖屏视频，并检查运镜、镜头覆盖、持机者出镜和音画连续性。

入口是 [SKILL.md](SKILL.md)。仓库包含处理方法、可调用的数学与检查工具及回归测试；具体相机的原始素材提取、拼接与图像渲染由已验证的适配器完成。

## 相机与输入支持

| 输入或能力 | 当前范围 |
|---|---|
| 已拼接、已稳定的全景母版 | 确认投影后可直接再构图；品牌不限 |
| 相机路径平滑、水平锁定、前向角约束 | 算法通用；需声明姿态方向、坐标系、外参和时轴 |
| DJI Osmo 360 II / OSV | 已有真实素材验证的方法；详细提取与标定边界见 [DJI OSV参考](references/dji-osv.md) |
| 原生 model8 滚动快门映射 | 可选数学模块，默认关闭；时差、参考行、读出方向与镜头标定需逐素材验证 |
| Insta360 稳定全景导出 | 可使用通用再构图流程 |
| Insta360 原始 `.insv` 与惯性防抖 | 尚无本仓库内置的提取、镜头或拼接适配器，未宣称已支持 |

没有通用OSV提取器、陀螺仪融合器或自动同步CLI。同步估计与采用门槛见 [同步与滚动快门参考](references/sync-and-rolling-shutter.md)。技术检查通过与视觉、隐私及用户创意接受分别记录。

## 使用

```sh
git clone https://github.com/BH-M87/reframe-360-video-skill.git reframe-360-video
```

将这个目录放入所用 Agent 识别的 skills 目录，或让 Agent 直接读取 `reframe-360-video/SKILL.md`。例如：

> 使用 reframe-360-video，把这段全景视频剪成展示建筑空间的横屏视频，避开持机者，保留自然转向，并验证最终成片。

先发现当前素材的投影、轨道和依赖。源媒体只读，导出、轨迹和报告使用新路径；不要把另一段视频的角度、毫秒偏移或遮罩坐标直接搬过来。

## 工具与依赖

Python **3.10+**。当前已在 macOS 验证；作业监督依赖 POSIX 进程组和管道语义，Linux 应运行本机测试，Windows 的 runner 未适配。独占文件交付需文件系统支持硬链接；runner 的工作目录与输出须位于同一文件系统。

| 工具 | 用途 | 额外依赖 |
|---|---|---|
| `scripts/probe_media.py` | 有时限的媒体轨道盘点 | 优先系统 `ffprobe`；缺失时仅有限 ISO BMFF fallback |
| `scripts/validate_imu.py` | IMU、姿态和时间覆盖检查 | Python标准库 |
| `scripts/build_camera_path.py` | CSV姿态生成逐帧相机路径与报告 | NumPy |
| `scripts/rolling_shutter.py` | 原生model8逐行反向映射 | NumPy；启用稀疏网格时另需OpenCV |
| `scripts/check_v360_state.py` | 重复绝对姿态的运行时回归 | 支持所需 `v360` / `reset_rot` 的FFmpeg构建 |
| `scripts/verify_export.py` | 完整解码、输出合同、音频连续性检查 | PyAV、NumPy；联系表另需Pillow |
| `scripts/compare_video_streams.py` | 只换音轨后验证视频流等价 | PyAV，需支持packet side-data API；已验证17.1.0 |
| `scripts/run_render_job.py` | 试运行、本地作业监督、验证后交付 | 对应渲染器及导出验证依赖 |

只安装当前任务需要的依赖。若要运行完整测试，可在独立环境中安装：

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-test.txt
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
```

完整测试使用 `imageio-ffmpeg` 提供的二进制。实际处理也可明确指定系统FFmpeg；滤镜与编码支持以当前构建的运行时检查为准。

各命令用 `--help` 查看参数。RS为Python模块，接口见 [同步参考](references/sync-and-rolling-shutter.md#可选映射模块接口)。

## 已沉淀的验证规则

- 在SO(3)上按实际采样间隔平滑，水平锁定与人为取景分别处理；检查真实触限帧和运动峰值，更紧限幅可能带回顿挫。
- 原始gyro和融合姿态分别保留计数与时轴；旧遥测重放、回跳和缺口不能靠排序、补零或外推伪造恢复。
- 自动同步与RS分别比较基线、时间窗口和实际编码画面；全量拟合过外参后的检查不称全链路盲测。
- 数值配准、中心jerk或网格误差下降不能单独证明整片更稳；曝光模糊、平移和近景视差仍需选段与画面检查。
- 音画端点正常也可能存在内部音频时间戳缺口。只修音轨时可复制视频，但需证明包、时序和显示元数据等价后才能按原范围沿用画面证据。

回归测试包含合成旋转、映射边界、真实小型媒体导出和文件不覆盖约束；不包含个人视频、截图、姿态CSV或机型标定素材。行为验收场景见 [tests/scenarios.json](tests/scenarios.json)。

## 参考与许可边界

参考工作流包括 video-use、gyroflow skill、Remotion 和 ECC；数学研究参考 Gyroflow 与 FFmpeg 的固定源码版本。来源与边界见 [执行参考](references/execution-and-timeline.md#来源与许可) 和 [路径算法参考](references/camera-path-algorithms.md#来源和实现边界)。仓库未打包这些上游项目的源码、二进制或媒体素材。
