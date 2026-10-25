---
name: reframe-360-video
description: Use when a user needs to crop or reframe 360°/全景视频 into a flat view, choose a camera direction, exclude the camera operator, or investigate gyro stabilization, horizon tilt, jelly wobble, fisheye projection, and continuous camera motion. Covers OSV, dual-fisheye, and equirectangular media; ordinary flat-video cutting does not require this skill.
---

# 360° 视频裁剪、取景与防抖

把全景素材做成连续、可观看、可验证的平面视频。先稳相机的三维姿态，再设计虚拟镜头；取景参数固定在机身上，并不能抵消机身晃动。

## 先确定交付和输入

- 沿用用户已给出的主题、画幅、时长、原速/加速、原声、字幕及不出镜范围；只有关键缺口才询问。
- 已拼接且稳定的全景只需再构图，不重复提取陀螺或强行再防抖。
- 原片只读，另建本次工作目录。保留源区间、流索引、PTS、帧率、色彩和每次几何处理的来源。
- 用 **scripts/probe_media.py** 盘点所有轨道。封面图、低分辨率预览、双镜头和数据轨分开；CAM meta 名称不证明 gyro 可用。探测不完整时保留 unknown。
- **scripts/check_v360_state.py** 可直接运行绝对姿态重复发送回归；只生成自己的合成素材，不接收用户视频。
- DJI OSV 的探测、提取与标定细节按需读 [DJI OSV](references/dji-osv.md)；不要直接对未知多轨源做无期限 PyAV probe。
- 涉及剪切、变速、叠层或长渲染时读 [执行与时间轴](references/execution-and-timeline.md)：共享输出时钟，按边界检查实际成片，并分开记录生成、技术通过、视觉验收。

## 选择处理路线

| 当前输入/证据 | 路线 |
|---|---|
| 稳定全景母版 | 连续再构图，验证输出 |
| 原厂工具能解释原片标定和运动数据 | 优先验证其稳定/拼接导出，再做取景 |
| 原片 IMU、镜头、轴向、时间均可验证 | 原始鱼眼/球面上三维补偿，一次几何重投影 |
| 只有 metadata 名称或提取失败 | gyro 未验证；走可说明局限的视觉方案，不伪称惯性防抖 |

供应商/软件能力以实际版本和样片验证为准。解析器支持某数据，不等于当前安装软件支持播放、拼接或稳定该视频。

## 设计镜头和稳定

详细数学、覆盖与对照实验见 [运动与取景](references/motion-and-framing.md)。

1. 把重力水平、手持角运动和人为转向分开。四元数按声明的顺序/坐标使用，验证单位、时轴、覆盖和图像对应。
   已有可信姿态 CSV 时，按 [可复用路径算法](references/camera-path-algorithms.md) 调用真实 Δt 双向 SLERP、水平基重建和可选前向限幅；仅凭文件存在不能走这条路线。
2. 保留慢速行进和有意转向，压制高频手摆；转弯连续加减速。只在意图明确的区间约束同向，不强迫整片单向而漏掉内容。
3. 固定合理 FOV，验证全部时刻的射线覆盖、主体可读性和前向安全范围。FOV、平滑窗口、读出时长都从本素材确定。
4. 使用 FFmpeg v360 运行时角度命令时，启用 **reset_rot=1**，先用重复相同姿态的短测试确认不累计旋转。变化按每帧更新，避免低频阶梯；短弧 SLERP/SO(3) 或有连续速度的曲线优于角度硬跳。
5. 尽量只对原始图像做一次几何重采样。通用鱼眼 FOV 展开是近似，不能冒充厂商真实镜头模型。
6. 先做代表性短片，同帧、同速、同 FOV 比较；再查实际触限帧与运动峰值，防止紧限幅带回顿挫。中心 jerk 下降不证明 roll、局部形变或全片都改善。
7. 需要自动同步或滚动快门时读 [同步与逐行校正](references/sync-and-rolling-shutter.md)，比较各自时差、读出方向和留出片段。数学映射正确仍须有实际画面收益；收益不明确则保持关闭。

脚步平移、近景视差和曝光运动模糊不能仅靠 gyro 消除。记录哪些运动仍在，不承诺“全部果冻消失”。

## 隐私、字幕和声音

按 [验证与隐私](references/verification-and-privacy.md) 做最终检查。

- 每次取景、防抖、裁切或镜头模型改变后，重新定位本人、手臂和反射。旧遮罩坐标失效；坐标系及输入 master SHA256 要与实际遮罩阶段匹配。
- 全程看构图，密查镜面/玻璃、快速转身、遮罩切换和首尾。联系表标注放在图外，避免挡住待检查像素。边缘人物归属靠相邻帧连续来源判断，不猜身份；无法排除则保守处理。
- 用户确认的名称/主题优先。只据清晰标识或可靠口述标注；转写幻觉、模糊标签和未证实的空间名称保留不确定。
- 保留原声时避免反复降噪/压缩。已处理合格的 AAC 可直接复制，并核验压缩包载荷一致性；重新编码后复查峰值与音画端点。

## 可复用工具

Python 环境和源/输出路径由当前机器发现，不沿用旧任务的绝对路径。原片只读，输出和报告使用新路径，运行参数看 --help。

    python3 "$SKILL_DIR/scripts/probe_media.py" "$VIDEO_INPUT" --output "$VIDEO_WORK/probe.json"
    python3 "$SKILL_DIR/scripts/check_v360_state.py" --ffmpeg "$FFMPEG"
    python3 "$SKILL_DIR/scripts/validate_imu.py" "$VIDEO_WORK/imu.csv" --gyro-units rad/s --report "$VIDEO_WORK/imu-check.json"
    python3 "$SKILL_DIR/scripts/verify_export.py" --source "$VIDEO_INPUT" --output "$VIDEO_EXPORT" --expected-frames "$EXPECTED_FRAMES" --fps "$OUTPUT_FPS" --width "$OUTPUT_WIDTH" --height "$OUTPUT_HEIGHT" --report "$VIDEO_WORK/export-check.json" --contacts-dir "$VIDEO_WORK/contacts"

- **scripts/build_camera_path.py**：已验证姿态 CSV → 逐帧 NPZ 矩阵与数值报告，接口见算法参考。它不提取/融合原始 gyro、不自动标定或证明画面已稳定。
- **scripts/run_render_job.py**：按已审阅的 argv 计划执行本地渲染，默认 dry-run；记录进度、超时、输入哈希并完整验证后交付新文件。计划 schema、依赖绑定和使用示例见执行参考。渲染器/镜头适配器由当前输入决定。
- **scripts/rolling_shutter.py**：可选的原生 model8 逐行映射模块；显式传入已验证的姿态、外参、时差和读出模型。接口与门槛见同步参考，不自动同步或替代渲染验收。
- **scripts/compare_video_streams.py**：更换音轨后比较视频包、时序及显示元数据，按 [验证与隐私](references/verification-and-privacy.md) 决定哪些旧画面证据可重绑定；声音和新容器仍单独验证。

这里 SKILL_DIR 是本 skill 的实际目录；其余变量来自本次明确的路径和交付计划。探测和 IMU 检查只需标准库；路径生成需 NumPy，RS映射另需 OpenCV；姿态回归需 FFmpeg；视频包比较需 PyAV，完整解码另需 NumPy，联系表另需 Pillow。可在任务独立环境安装依赖，不复制私有运行时路径。

## 交付门槛

- 最后一次修改后，完整解码交付文件；核对容器/编码、实际帧数/PTS、画幅、色彩、音画端点、黑框和文件哈希。
- 检查工具的 technical passed 与人工视觉/隐私结论分开。画面黑暗启发式和抽帧都不能代替全部语义检查。
- 在最终输出时钟上定位首尾、转向、字幕和遮罩切换窗口，查看连续短片、图外标时联系表及音频；原生工程已保存不等于已渲染，技术通过不等于用户创意接受。
- 回看代表性运动和全片构图，复核字幕、本人/倒影和遮罩边界；报告实测改善及残留问题。
- 给出实际成片与必要验证产物。编辑和导出不自动授权上传、发布、覆盖已发布笔记或创建定时任务。
