# DJI OSV：探测、运动数据和标定

本参考保留已验证的方法及 Osmo 360 II 经验，不是所有 DJI/360 机型的统一适配器。精确 schema、轨道和字段以当前原片/解析器为准。

## 路线与能力边界

优先核实 DJI Studio 对该型号、固件和录制模式的稳定/拼接支持。全景增稳往往在原厂导出时选择；稳定全景母版仍保留可重新构图的球面信息，平面导出则锁死视场。

- OSV 可能有双鱼眼 HEVC、音频、多个 metadata/debug/calibration 轨和合成封面。先盘点，不能按扩展名或首个 video 盲选。
- gyro 读取、360 播放、镜头标定、双镜头拼接、稳定渲染是不同能力。
- 当前安装版本、稳定发布版、main/master 文档可能不同。曾出现文档提前描述 motion-only 支持，而稳定版尚无对应功能；不要把搜索索引或开发文档当本机可用性证明。
- 不把 Action 系列“关闭 EIS/特定 FOV 才有 gyro”等规则直接套给 360。

## OSV probe 卡住时

普通探测有明确超时。多数据轨源在某些 PyAV/FFmpeg 版本中可能长期卡在 stream-info，不代表 USB 文件损坏。

先用 bundled probe 的 ISO BMFF fallback 只读 moov/track/sample 描述；它不读取整块 mdat，不解码 IMU，且可能看不到 covr 等元数据封面。fragmented MP4、stz2 或损坏时轴保留 unsupported/partial，换合适的解析器。

若只需要解码一条已选镜头，可无损 remux 到本次目录，避免反复 probe 所有私有轨道：

    "$FFMPEG" -hide_banner -nostdin -analyzeduration 0 -probesize 1048576 -i "$VIDEO_INPUT" -map "0:$LENS_INDEX" -c:v copy -an -movflags +faststart "$VIDEO_WORK/lens-only.mp4"

输出必须是新文件，选正确容器/codec tag，检查流、帧数、PTS和色彩。此文件仅供解码，原 OSV 仍是 IMU/标定依据；不要把删掉 metadata 的 remux 当新的完整源。

## 提取不是看到 CAM meta 就结束

选择可追溯、支持当前产品的解析器。记录版本/commit、schema来源及许可证；不把个人视频上传给不必要的外部服务，不把本次用户的原片/CSV写入 skill。

没有通用内置提取器是有意边界：需先确认产品与 schema，再选相应适配器。仅凭声明的 proto 文件名不够；某些产品复用了不同声明名下的 wire schema。

提取时保留：

- 原始包/未知字段和解析错误、数据轨索引。
- 样点时间、单位、原轴 gyro/accel；独立声明四元数顺序/方向。
- 样点数、有效采样率、时间覆盖、时钟断点和曝光。
- 每帧时钟到视频 PTS 的映射、拟合/留出误差；不把标称 1000 Hz 强行写成精确采样间隔。
- 原始标定与选择的校准族、传感器尺寸、内外参、读出字段；缺失值明确 unknown/null。

raw IMU 与融合 quaternion 是独立数组：分别保留样点数、原始时间戳、单位、时间轴拟合和覆盖范围。只有对应关系得到证实时才能按索引配对；计数相同本身不证明时间一致，计数不同也不能直接 zip、截尾或补零。比较角速度时先在共同覆盖区间按各自时标对齐，并记录插值方法。parse_errors 来自真实解析结果，不能写成常量 0。

标准化 CSV 时间用秒；源表时间稍早于首帧或晚于尾帧可能是合法读出/曝光缓冲，不应直接删掉。用 validate_imu.py 先检查数据，再用图像验证安装轴与曝光中心时序。工具的数据通过状态始终不代表画面配准通过。

### 回跳、重复旧包与可用区间

视频仍在前进而传感器时标回跳时，先对比包序号、源字节位置、raw/q各自时标和早先payload。相邻静止姿态相同未必异常；时标回跳且整段旧姿态重放则不能当作新测量。区分有schema依据的计数器回绕与陈旧payload，不能用排序、去重、平移时间轴或跨包插值“修好”后宣称恢复。

- 保留完整原始提取、坏包索引、重复比较及失败报告，另存已验证的连续前缀或分段。声明包含/排除的包区间、视频区间、采样覆盖及失败原因；局部通过不能改写成全片通过。
- 编辑区间必须同时满足视频帧与所有所需传感器时刻的覆盖，包含同步偏移、逐行读出和插值邻点。安全末端由当前数据计算，不套用旧任务的时间余量。
- 另一metadata轨只有在当前文件上独立验证了必要姿态、有效时标、轴向与图像关系，才能作为恢复来源；同名轨、重复raw或缺融合姿态都不足以恢复。
- 选段明确允许舍弃坏尾时可采用可验证区间；若用户要求保留该段，则说明惯性方案未覆盖并评估原厂/视觉路线，不静默删掉内容或编造姿态。

## 已核验的 Osmo 360 II 细节（2026-10-25）

这些是该产品适配的参考，不是其它素材的默认值：

- 所检查的文件声明 dvtm_OQ102.proto；公开实现按 product_name 选择兼容的 WA530/Eagle4 schema，不能仅按声明文件名找不存在的 proto。
- 同一产品实现把 video0 绑定到 master、video1 到 slave；其它产品可能相反。按当前产品绑定查证，不能把流0当通用前向镜头。
- master/slave 的 raw gyro 可能重复；不要拼接成双倍采样率。使用包含原始时标和融合姿态的完整轨。
- raw Hamilton wxyz 的体角速度可与声明的 raw XYZ gyro 做一致性核验。q与-q表示相同旋转，符号连续化只写另存副本或由最短弧插值处理。
- 曾用的 Gyroflow姿态变换是 q_gf=(0,0,1,0)*q_raw*(0.5,-0.5,-0.5,0.5)。其体角速度在该适配中是 (gyroY,-gyroZ,-gyroX)；上游字符串 zyx 不是可再次照字面套到 q_gf 的轴。
- 单位与变换要从当前适配核实。拿不到它们时标记未配准，不靠画面“看似更稳”猜轴。
- 实际曝光中心偏置必须用数据和留出图像验证；某片约5ms的经验偏置不是所有片的常量。
- 某片逐行读出约18.48ms。这个数只能从当前 metadata 读取并验证，不能硬写入所有模式。

## model8 与一次重投影

在该适配中，原生 model8 为 equidistant theta polynomial 到十一阶，再加切向畸变：

    theta_d = theta * (1 + k1*theta^2 + k2*theta^4 + k3*theta^6 + k4*theta^8 + k5*theta^10)
    x0 = x/rho * theta_d
    y0 = y/rho * theta_d
    xd = x0 + 2*p1*x0*y0 + p2*(theta_d^2 + 2*x0^2)
    yd = y0 + p1*(theta_d^2 + 2*y0^2) + 2*p2*x0*y0
    u = fx*xd + cx
    v = fy*yd + cy

正确处理 rho=0、背向光轴和无效像素。坐标缩放要遵守像素中心约定。

该公开适配的 lens rig 旋转为 inverse(Ry(yaw)*Rz(roll)*Rx(pitch-90°))，并有校准族的产品相关回退顺序。所核 II 文件使用 native_refine，不能只见 cam_imu_extri identity 就跳过 rig、坐标基和型号绑定。温度/保护罩/水下模式也要按原片选择。

行走近景会使单纯 rotation-only/homography 拟合偏向平移。需要时用真实标定的球面对应和 epipolar 平移零空间校验安装关系；不同时间区间留出。微调若未改善留出集，不宣称更精确。同步和逐行补偿的选择、条件留出边界见 [同步与滚动快门](sync-and-rolling-shutter.md)。

## 原声和最终输出

若先前版本的 AAC 已经处理并验证合格、源区间/速度相同，可直接复制该音轨。完整核验包载荷一致性和端点；变速、删头、不同起点不能盲 copy。

不要用裸 stream-copy 的尾裁结果假定精确帧数：B帧/DTS依赖可能使末尾帧和时长不同。由 PTS 明确选择区间，并最终完整解码验证实际帧数、首尾和音画同步。

## 一手来源

使用时复核当前版本，链接不是自动执行或下载授权：

- DJI Osmo 360 II 规格：https://www.dji.com/360-2/specs
- DJI Studio 下载及文档：https://www.dji.com/360/downloads
- 产品相关遥测实现：https://github.com/AdrianEddy/telemetry-parser
- DJI schema/时序/rig：该仓库 src/dji/mod.rs、timing.rs、rig.rs。
- Gyroflow 支持说明：https://github.com/gyroflow/docs.gyroflow.xyz/blob/main/getting-started/supported-cameras/dji.md

本 skill 没有 vendor 上游 schema、库源码或许可证文件；如后续要复制源码，先按其当前许可保留所需声明。
