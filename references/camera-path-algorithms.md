# 可复用相机路径算法

已有可信姿态 CSV、相机/IMU 外参和视频时间映射时读取。实现为 [camera_path.py](../scripts/camera_path.py)，命令入口为 [build_camera_path.py](../scripts/build_camera_path.py)。它们生成旋转矩阵；镜头投影、拼接、图像重采样和画面验收仍由适配器完成。

## 输入证据和坐标约定

- 输入为有限且严格递增的秒级时间、Hamilton `w,x,y,z` 单位四元数。调用方明确选择 body-to-world 或 world-to-body；后者先求逆。不能直接把角速度 CSV 当姿态输入。
- 每个四元数范数误差须在 `1e-3` 内；只在内存中归一化并连续化 q/-q 符号，原 CSV 不变。相邻近 180° 姿态差的最短弧不唯一，拒绝而不猜转向；跨 ±180° 欧拉角但相对转动很小不属于此错误。
- `E=camera_from_imu` 把 IMU 向量变换到相机。它必须是已核验的 SO(3) 矩阵；只有坐标确实一致时才能显式用 `--identity-camera-frame`。
- 使用列向量、相机 x 向右/y 向下/z 向前。设 `R_imu` 为 IMU→世界、`C` 为相机→世界、`T` 为输出视角→世界：

      C = R_imu @ E.T
      ray_source_camera = C.T @ T @ ray_output

  禁止把该矩阵直接当成另一后端的 Euler 参数。先用已知轴向转动和射线回映验证，再检查实际图像。
- `world_up` 必须与姿态使用同一个世界坐标。行走中的瞬时加速度不能直接当重力；这个工具不会估计重力、轴向或时差。数值满足水平只证明相对于调用方声明的 up 水平。

## 采用的三个算法

### 1. 实际 Δt 的双向指数 SLERP

对于 `τ>0`，使用每个相邻采样的时间间隔，而非假定均匀采样：

    Δ[i] = t[i] - t[i-1]
    α[i] = -expm1(-Δ[i] / τ)
    F[0] = q[0]
    F[i] = slerp(F[i-1], q[i], α[i])
    B[n-1] = F[n-1]
    B[i] = slerp(B[i+1], F[i], α[i+1])

反向步使用下一段间隔；`τ=0` 旁路平滑。SLERP 使用最短弧和 sinc 形式处理小角度。先处理完整连续日志，再插值到请求帧时间，保留源区间之外可用的分析缓冲。

`--tau-s .35` 是试验起点，需按意图转向及同源帧对照确定；强过滤会增加取景偏离。双向处理降低单向滞后，但有限端点和 SO(3) 非线性不支持“严格零相位”的承诺。逐帧插值也不保证连续角加速度或无抖动。

时间断档超过 `--max-input-gap-s`（默认 `.05` 秒）就失败；把日志按真实连续区间拆分后分别处理，不跨断档补造姿态。即使长断档位于选取区间外，当前实现仍拒绝整份日志，需先选择连续日志。

### 2. 保持前向的水平基重建

平滑后保留前向 `f`，用明确的世界向上 `u` 重建右/下轴：

    f = unit(forward); u = unit(world_up)
    right = unit((-u) × f)
    down = f × right
    T = [right, down, f]

去掉 roll，同时保留前向的 yaw/pitch。当 `||(-u) × f|| ≤ 1e-6` 时接近天顶/地底，水平朝向不确定，直接拒绝。检查 `T.T @ T≈I`、`det(T)≈1` 及世界重力在输出相机中的 roll。

### 3. 不带回 roll 的前向角约束

设原相机光轴为 `f_source`、平滑后目标为 `f_target`、可配置角限为 `L`：

    θ = atan2(||f_source × f_target||, f_source · f_target)
    β = min(1, L / θ)
    f_limited = spherical_interpolate(f_source, f_target, β)
    T = horizon_basis(f_limited, world_up)

只限制前向并再次重建水平基；把完整目标姿态 SLERP 回原始姿态会重新引入原始 roll。`0 ≤ L < 180°`，近反向的方向弧拒绝；当前工具要求同时启用水平锁。

角限不是镜头覆盖、本人不出镜或无黑边的证明。接近限制时目标会更贴近原始抖动，可能造成速度折点；报告触限帧数/比例、前向角和角速度，再检查每个触限区间及运动峰值，不能只挑普通窗。更紧限幅可能比适当放宽更抖；放宽须重新证明投影覆盖与隐私余量，而非无条件改成另一固定值。不可恢复的源曝光模糊可在任务允许剪辑时换段/裁除，不能用旋转平滑冒充清晰度恢复。

## 命令和输出契约

仅支持一个连续源区间、原速 CFR 帧时间。剪切/变速/VFR 需由适配器提供明确的源帧时间映射，不能用这个 CLI 的输出时钟冒充实际 PTS。

以下变量由本次验证结果设置；输出与报告必须不存在，父目录先建好：

```sh
python3 "$SKILL_DIR/scripts/build_camera_path.py" \
  --input "$QUATERNION_CSV" \
  --output "$VIDEO_WORK/camera-path.npz" \
  --report "$VIDEO_WORK/camera-path.json" \
  --time-column t_video_s --quat-columns qw,qx,qy,qz \
  --frames "$EXPECTED_FRAMES" --fps "$OUTPUT_FPS" \
  --source-start-s "$SOURCE_START_S" --time-offset-s "$TIME_OFFSET_S" \
  --direction body-to-world --camera-from-imu "$CAMERA_FROM_IMU_JSON" \
  --tau-s "$SMOOTHING_TAU_S" --max-input-gap-s "$MAX_IMU_GAP_S" \
  --horizon-lock --world-up="$WORLD_UP_XYZ" \
  --max-forward-angle-deg "$MAX_FORWARD_ANGLE_DEG"
```

`--world-up` 为逗号分隔向量，`--camera-from-imu` 为 3×3 JSON；方向和列名以真实输入为准。无需水平锁时同时去掉 horizon/up/cap 三组参数。不传 cap 就不限制前向偏离。可先运行 `--help`，帮助解析不依赖 NumPy。

三个时钟分别保存：

    output_timestamps_s = frame_index / fps
    source_timestamps_s = source_start_s + output_timestamps_s
    imu_sample_timestamps_s = source_timestamps_s + time_offset_s

CSV 的时间轴必须与第三项一致；即使列名叫 `t_video_s`，仍需核实已包含哪些偏移，避免重复加 offset。禁止外推；仅接受覆盖端点最多 4 个 float64 ULP 的舍入差，使用准确端点采样并在报告记录调整，其他时间保持原值。

NPZ 字段：

| 字段 | 含义 |
|---|---|
| `camera_to_world`, `target_to_world` | 每帧的 `C`、`T`，形状 `(N,3,3)` |
| `output_ray_to_source_camera` | 每帧的 `C.T @ T`，渲染器消费的反向映射 |
| `output_timestamps_s`, `source_timestamps_s`, `imu_sample_timestamps_s` | 三个时钟；IMU 时钟包含已报告的微量端点舍入 |
| `timestamps_s` | **源视频时间**的兼容别名，不是输出相对时间 |
| `world_up` | 启用水平锁时写入的归一化世界向上向量 |

不包含镜头标定、焦距或图像尺寸。下游需明确加载已验证的镜头模型和输出内参，逐帧验证有效射线范围。

JSON 保存输入/参数/输出 SHA256、符号处理数量、覆盖和间隔、SO(3) 误差、水平几何、触限比例及运动统计。`angular_speed_change_deg_s2` 是角速度**大小**的变化率，不是完整三维角加速度或 jerk；不要拿它单独作画面结论。

新文件在各自目录暂存后独占交付，不覆盖已有路径或输入别名。NPZ 和报告分别发布，不是支持断电恢复的双文件事务；只有两者齐全且哈希吻合才算一次完整输出。成功状态为 `camera_path_built_unverified_visual_effect`；`projection_coverage_verified`、`stabilization_proven` 和 `visual_effect_verified` 仍是 false，交给后续渲染与验收。

## 没有自动采用的算法

原始 gyro 积分/融合、自动视频-IMU 同步、滚动快门、动态 zoom、每轴经验增益均没有加入本路径模块。需要同步/RS时按 [同步与逐行校正](sync-and-rolling-shutter.md) 另外验证，可用独立 `rolling_shutter.py` 消费固定目标姿态；路径生成不会自动启用它。当前实现也没有复现 Gyroflow DefaultAlgo 的速度/FOV/距离自适应多阶段策略。

## 来源和实现边界

2026-10-25 阅读并固定以下版本；本模块按标准旋转数学独立编写，没有移植 Rust/C 源码。这里是设计来源，不表示与上游的完整算法或输出等价。

- Gyroflow `684c6a561ae4529c28841a78a631bdc249dda453`：[plain.rs](https://github.com/gyroflow/gyroflow/blob/684c6a561ae4529c28841a78a631bdc249dda453/src/core/smoothing/plain.rs)、[horizon.rs](https://github.com/gyroflow/gyroflow/blob/684c6a561ae4529c28841a78a631bdc249dda453/src/core/smoothing/horizon.rs)；参考双向平滑和水平分离思路。本实现使用逐间隔 Δt 和显式 world_up 基，与上游实现细节不同。
- 同版本 [default_algo.rs](https://github.com/gyroflow/gyroflow/blob/684c6a561ae4529c28841a78a631bdc249dda453/src/core/smoothing/default_algo.rs) 与 [synchronization/mod.rs](https://github.com/gyroflow/gyroflow/blob/684c6a561ae4529c28841a78a631bdc249dda453/src/core/synchronization/mod.rs)：复杂自适应/同步路线仅研究，没有声称实现或验证。
- FFmpeg `464fc8c2ed53648b9ede81555921e3995d32bbe5`：[vf_v360.c](https://github.com/FFmpeg/FFmpeg/blob/464fc8c2ed53648b9ede81555921e3995d32bbe5/libavfilter/vf_v360.c)：参考输出射线→旋转→输入投影的重采样契约；运行时状态仍需本机回归。

上述 Gyroflow 源文件为 GPL-3.0-or-later，v360 源文件为 LGPL-2.1-or-later；FFmpeg 整体构建许可还取决于配置。后续若复制或改编上游代码，应另行核对仓库许可兼容性、声明和归属，不能把本参考当作源码再分发授权。
