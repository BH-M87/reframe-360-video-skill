# 视频—姿态同步与滚动快门

仅在原始姿态、镜头模型、轴向和视频时钟都有可核验证据，且需要时差或逐行校正时读取。原片提取异常先按 [DJI OSV](dji-osv.md) 处理；轨迹平滑与取景见 [运动与取景](motion-and-framing.md)。同步估计和滚动快门（RS）是两项决策，不能以其中一项通过替代另一项。

## 先声明模型，再估计时差

记录视频PTS基准、传感器时标如何映射到该基准、Hamilton四元数方向、IMU→相机外参E、镜头/rig及读出参考行。明确 `t_sensor = t_video + offset` 的符号和已应用过的偏移；曝光起点、中心或帧封装时刻不能混用。既有标定不能在后端再次置换轴。

自动同步应检验当前素材，而不是沿用旧片数毫秒的经验值：

1. 在分散时间窗口采集可信特征对应，保留真实源PTS、帧身份、源像素行和拒绝条件。用本片镜头模型反投影到球面；反向光流检查和稳健拟合可减少错配，不能自动消除动态人物、遮挡或模糊。
2. 单纯rotation-only误差会受行走平移污染。需要时用球面epipolar约束，并将每对帧的平移方向作为干扰参数；近退化、特征少或行覆盖窄时降低结论强度，不加自由形变硬压残差。
3. 以**时间窗口**分训练/留出集并互换，而不是将相邻特征随机拆分。比较零偏移基线、搜索曲线、最优偏移、边界命中及各折一致性。搜索范围依据时钟证据和完整覆盖设定；损失曲线平坦、最优值卡边界或折间冲突，均不能称同步已确定。
4. 外参微调需要独立依据，避免用E吸收镜头畸变、时差或RS误差。若E先在全量数据上拟合，再固定E检查时差/RS，后面的留出是**条件于该E**的验证，不能称整条标定链路严格盲测。声明最终拟合使用的数据范围。
5. 当前短片若固定offset已足够，不自动增加漂移模型；长素材或时钟残差提示漂移时才分段检验，并再次验证新模型。

报告测量残差、可辨识性和采用/保留/未确定状态。时钟拟合微秒级误差不等于图像曝光已同步，某个数值优化器成功也不等于offset可信。

## 区分统一时差与逐行时差

先取得当前模式的传感器高度、读出时间来源、方向及参考行定义。将0、正、反读出作为受控候选，各自估计或验证统一offset，避免把全局曝光时差误判为读出方向。GS（零读出）与RS的最优offset可能不同，不能强行共用，也不能给每个像素任意拟合时差。

比较中固定目标视角T、源帧、速度、输出尺寸、FOV与编码设置。分别保存各模型offset、读出值/符号及覆盖；没有可靠读出量时保留未知，不从他片抄常量。仅有上半幅特征时须指出行覆盖与offset/readout耦合。

## 按原传感器行计算反向映射

列向量下，E将IMU向量变换到相机，R是IMU→世界姿态；每个输出帧固定一个虚拟目标T：

    world_ray = T @ output_ray
    source_ray = E @ R(t_sensor).T @ world_ray
    native_uv = lens_project(source_ray)
    t_sensor = t_source_frame + offset
               + signed_readout * (native_uv.y / native_sensor_height - row_reference)

源投影决定采样行，采样行又决定姿态，因此从帧中心的映射初始化并迭代求解。使用未缩放的native sensor row；不能用输出画面行或下采样后的行号直接代替。参考行、像素中心及读出时间定义必须一致，公式里的 `row_reference` 不是默认取0.5的授权。

- 逐帧显式传入frame index，不能靠矩阵字节相同与否猜索引；重复目标姿态是合法输入。
- 所有迭代时刻必须位于连续姿态覆盖内；不跨断档、不延长坏尾，不用外推消除边界报错。
- 校验“读出=0、offset相同”重现GS反向映射；改变RS offset时，该比较需显式恢复GS offset。
- 比较迭代次数、投影有效区、背向光轴/无定义射线以及数值误差。未收敛或越界应失败或缩小明确的处理区间，不能把无效像素填黑后当成功。
- 网格插值可按 [重采样约定](motion-and-framing.md#视场和重采样) 加速；误差超预算减小步长或退回稠密，稀疏抽查不称全像素证明。

可复用数学实现见 [rolling_shutter.py](../scripts/rolling_shutter.py)。它消费已经核验的姿态、镜头和时序，不负责从OSV提取数据、自动寻找offset/方向或证明画面收益。具体API以模块说明为准，不套用原厂或其它工具的CLI参数。

### 可选映射模块接口

在调用者的Python环境中将本skill的 `scripts/` 加入模块路径，再导入 `RSMapper`。它不读写媒体；数组和参数来自当前任务的已验证适配器：

```python
from rolling_shutter import RSMapper

mapper = RSMapper(
    imu_times_s, quaternions_wxyz, source_times_s, target_to_world,
    direction=pose_direction, camera_from_imu=E,
    calibration=native_model8, output_intrinsics=K,
    width=output_width, height=output_height,
    offset_s=verified_offset, row_reference=verified_reference_row,
    readout_s=verified_signed_readout,
    max_input_gap_s=allowed_imu_gap,
)
map_x, map_y, diagnostics = mapper.maps(frame_index, decoded_width, decoded_height)
if not diagnostics['rectangular_coverage_passed']:
    raise ValueError('当前取景超出源图像，先调整/验证几何')
# 另查原生镜头有效mask、隐私及当前误差预算，再用map_x/map_y重采样一次。
```

- 时间数组单位为秒；四元数形状 `(M,4)`、Hamilton wxyz；源帧时刻 `(N,)` 和目标姿态 `(N,3,3)` 一一对应。输入方向必须明确。`target_to_world` 可直接来自路径模块，`source_times_s` 应取其源时钟而非输出相对时钟。
- 标定字段为 `width,height,fx,fy,cx,cy,k1..k5`，以及 `p1,p2` 或二元 `p`；只实现已确认的model8。K和输出尺寸由调用者对应设置，改变输出尺寸不能沿用未缩放的K。源图像缩放使用 `(native_uv+0.5)*scale-0.5` 像素中心映射。
- `offset_s` 和 `row_reference` 必填，`readout_s=0` 默认关闭逐行处理；正值表示native y增加时采样更晚。默认 `grid_step=1` 稠密计算只需NumPy；启用2的幂网格步长（至32）才需要OpenCV。`max_grid_error_px`、`convergence_px` 均以原生源像素计；默认值只是数值预算起点。
- 网格路径按随机点与全部边界像素复核，超预算自动减小步长；仅外延支撑点导致的时标越界也可在确认真实网格节点有效后有限回退，实际输出时标越界仍拒绝。`grid_retries` 记录回退原因。`exact(frame_index, output_rays, readout_s=0)` 返回原生UV与迭代记录，可用于同offset零读出控制实验。`project_model8` 提供相同模型的显式投影。
- 报告中的 `rectangular_coverage_passed` 只证明矩形像素范围；圆形有效镜头区、拼接缝、遮挡与隐私仍须外部检查。迭代末次更新量不是真实解误差的数学上界；抽样grid误差不称全像素误差。`visual_effect_verified` 和 `privacy_verified` 始终为false。

本模块没有自动同步CLI、视频解码器或OSV提取器。使用者若尚未验证E、时差、读出方向或原生标定，应先停在上述分析阶段，不能用“默认关闭RS”掩盖错误的全局姿态映射。

## 数值通过后仍要比较实际编码画面

1. 对代表性运动、运动峰/触限和端点生成同源帧RS off/on短片，源图像各只作一次几何重采样；低清诊断与最终分辨率检查分开标记。
2. 对**编码后重新解码的画面**计算局部/分行配准、近景及直线几何，并检查首中尾、快速运动和边缘。全局roll或中心jerk改善不能证明RS残差已消失；配准代价下降也不是主观稳定性同幅度提升。
3. 记录有效帧/配对数量、拒绝率、指标定义、窗口范围及每折结果；例如90帧只有89个连续帧对。曝光模糊、平移/视差和压缩仍会影响指标。
4. 实际收益可重复且无明显几何回退时才采用RS；收益不明确时保持关闭，并保留对照。静帧检查、连续播放、听音和完整解码是不同证据，按实际完成范围记录。
5. RS改变了几何，重新检查本人/反射、遮罩与边缘覆盖。最终媒体变化后绑定新的哈希和验收状态。

## 已验证的决策经验

2026-10-25本地实测中，三段不同场景均支持按各自时差与当前读出模型启用RS，且实际编码对照有局部配准收益；另一素材虽然读出方向在数值上受支持，实际局部几何/直线检查没有稳定收益，最终保持关闭。原始证据留在各任务交付中，本参考只保留匿名化方法结论。

这些实测不规定默认offset、读出时间、方向或改善百分比，也未验证通用自动同步器。更复杂的联合E/读出/焦距/温度模型、逐行平移、软构图约束和高位深中间处理仍需各自的独立证据；不要因存在优化空间就默认开启更多自由度。
