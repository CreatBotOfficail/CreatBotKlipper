# 操作进度 RESPOND 协议

本文档定义 CreatBot Klipper 长时间操作与屏幕程序之间的进度通信协议。
屏幕程序应以本文档的 V2 协议为准实现 PID 校准、4Z 调平、回零和清理喷嘴等页面。

## 版本和实施状态

| 版本 | 状态 | 内容 |
| --- | --- | --- |
| V1 | 已实现 | PID 的开始、峰值进度、结束/中断；4Z 的开始、探测开始、单轮完成、调整、结束/部分错误。 |
| V2 | 已实现 | 4Z 单点 `1/4` 至 `4/4` 进度、结构化探测结果、结构化调整值和验证阶段。 |
| V2 错误收口 | 后续项 | 回零失败、探测失败、重试异常等路径统一发送 `operation_error`。 |

V2 是屏幕端的正式对接协议。屏幕必须能够忽略未知字段和缺失的可选字段，以便后续扩展。

## 传输方式

不新增 `RESPOND TYPE`。所有消息继续使用已有的 command 响应通道：

```gcode
RESPOND TYPE=command MSG="action:operation_<action> key=value key=value"
```

屏幕实际收到的内容以 `//` 开头：

```text
// action:operation_<action> key=value key=value
```

只处理 `action:operation_` 前缀。现有的 `action:prompt_*`、普通日志和错误日志必须继续按原逻辑处理，不能作为操作进度的依据。

## 通用规则

1. 消息格式为 action 加空格分隔的 `key=value` 字段。
2. 字段名、枚举值和标识符只使用 ASCII，无空格。
3. 数值使用十进制格式，单位为毫米（`mm`），温度字段例外时会在字段说明中注明。
4. 屏幕不得解析 `Gantry-relative probe points`、`Actuator Positions` 等人类日志。
5. 未识别的 action 或字段必须忽略，以便后续扩展。
6. `begin` 后必须最终收到一个 `end` 或 `error`。收到新的 `begin` 时，屏幕必须丢弃同一 `id` 的旧进度。

## 公共字段

| 字段 | 是否必填 | 说明 |
| --- | --- | --- |
| `id` | 是 | 本次校准会话标识。PID 格式为 `pid_<heater>`，4Z 固定为 `quad_gantry_level`，主探针偏移固定为 `probe_z_offset`，双喷头 XY 偏移固定为 `dual_nozzle_xy_offset`，回零固定为 `homing`。 |
| `type` | 是 | 校准种类：`pid`、`quad_gantry_level`、`probe_z_offset`、双喷头偏移类型或 `homing`。 |
| `step` | `begin`/`update` 必填 | 当前逻辑步骤，从 `0` 开始。 |
| `total` | `begin`/`update` 必填 | 当前阶段的总逻辑步骤。 |
| `phase` | `begin`/`update`/`measurements` 必填 | 当前阶段枚举。 |
| `round` | 4Z 必填 | 4Z 收敛轮次，从 `1` 开始。 |
| `result` | `end`/`error` 必填 | `completed` 或 `failed`。 |
| `reason` | `error` 必填 | 机器可识别错误原因。 |

## Action 定义

| Action | 含义 |
| --- | --- |
| `operation_begin` | 校准开始。屏幕创建或重置校准页面。 |
| `operation_update` | 阶段、步骤或轮次发生变化。 |
| `operation_measurements` | 上报结构化探测结果或 Z 调整结果。 |
| `operation_end` | 校准成功完成。 |
| `operation_error` | 校准失败或取消。 |

## PID 校准

PID 自动调参内部需要收集 12 个温度峰值，即 6 次完整的加热/冷却振荡。
屏幕消息只在每两个峰值完成后更新，因此 `total=6`，`step` 表示已完成
的温度震荡轮次。

### 阶段

| `phase` | 屏幕显示含义 |
| --- | --- |
| `heating` | 加热器正在接近目标温度。 |
| `tuning` | 正在采集温度峰值。 |

### 示例

```text
// action:operation_begin id=pid_extruder type=pid step=0 total=6 phase=heating
// action:operation_update id=pid_extruder type=pid step=1 total=6 phase=tuning
// action:operation_update id=pid_extruder type=pid step=6 total=6 phase=tuning
// action:operation_end id=pid_extruder type=pid result=completed
```

中断或加热异常时：

```text
// action:operation_error id=pid_extruder type=pid result=failed reason=interrupted
```

屏幕将 PID 显示为普通进度条：`step / total`。

## 4Z / QUAD_GANTRY_LEVEL

一次 4Z 收敛轮次依次执行：回零、四点探测、计算、四 Z 调整、判断是否需要下一轮。由于重试次数由实时误差决定，屏幕不得把 `round` 转成总百分比。

### 阶段

| `phase` | 屏幕显示含义 |
| --- | --- |
| `homing` | 正在执行 `G28`。 |
| `probing` | 正在进行本轮四点探测。 |
| `measured` | 四点探测完成，结果已可显示。 |
| `adjusting` | 正在计算并执行四 Z 电机调整。 |
| `evaluating` | 调整完成，正在根据本轮误差判断是否需要下一轮。 |
| `verifying` | 已请求 `VERIFY=1`，正在执行最终四点复测。 |
| `verified` | 最终复测误差在容差内，已确认龙门架水平。 |

### 最终复测

默认的 `QUAD_GANTRY_LEVEL` 与上游 Klipper 一致：每轮探测后先调整，再根据调整前的四点误差决定是否重试；当误差已经小于容差时，不会再次探测确认调整后的结果。

使用 `VERIFY=1` 可要求最终复测：

```gcode
QUAD_GANTRY_LEVEL RETRIES=3 RETRY_TOLERANCE=0.100 VERIFY=1
```

当常规调平收敛后，Klipper 会启动额外一轮四点探测。复测合格时不再移动 Z 电机，而是发送 `phase=verified` 后结束；复测不合格时，将使用这轮测量结果继续调整和重试。`round` 统计实际探测轮次，因此最终复测会使用新的轮次编号。

### 步骤进度

`phase=probing` 时固定使用 `total=4`：

```text
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=0 total=4 phase=probing round=1
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=1 total=4 phase=probing round=1
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=2 total=4 phase=probing round=1
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=3 total=4 phase=probing round=1
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=4 total=4 phase=probing round=1
```

屏幕显示“第 `round` 轮，已完成 `step / 4` 点”。开始下一轮时重新发 `step=0 total=4`，并将 `round` 加一。

### 探测结果

四点完成后发出一条结构化结果消息。`probe0` 到 `probe3` 是每个点多次采样后的最终探测 Z 值，不是原始逐次采样日志；`range` 是四个最终点的最大值减最小值。

```text
// action:operation_measurements id=quad_gantry_level type=quad_gantry_level phase=measured round=1 probe0=-0.231833 probe1=-0.230271 probe2=-0.234438 probe3=-0.232354 range=0.004167 tolerance=0.100000
```

屏幕应显示四个点的数值、`range` 和 `tolerance`。不应显示同一点的三条 `probe at` 采样日志。

### Z 调整结果

计算完成后发出每个 Z 电机的调整量。正负号必须原样显示。

```text
// action:operation_measurements id=quad_gantry_level type=quad_gantry_level phase=adjusting round=1 adjust0=0.000304 adjust1=0.003120 adjust2=-0.003452 adjust3=0.000028
```

### 完整成功流程示例

```text
// action:operation_begin id=quad_gantry_level type=quad_gantry_level step=0 total=4 phase=homing round=1
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=0 total=4 phase=probing round=1
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=1 total=4 phase=probing round=1
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=4 total=4 phase=probing round=1
// action:operation_measurements id=quad_gantry_level type=quad_gantry_level phase=measured round=1 probe0=-0.231833 probe1=-0.230271 probe2=-0.234438 probe3=-0.232354 range=0.004167 tolerance=0.100000
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=4 total=4 phase=adjusting round=1
// action:operation_measurements id=quad_gantry_level type=quad_gantry_level phase=adjusting round=1 adjust0=0.000304 adjust1=0.003120 adjust2=-0.003452 adjust3=0.000028
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=4 total=4 phase=evaluating round=1
// action:operation_end id=quad_gantry_level type=quad_gantry_level result=completed
```

使用 `VERIFY=1` 的最终复测流程：

```text
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=4 total=4 phase=evaluating round=1
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=0 total=4 phase=verifying round=2
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=1 total=4 phase=probing round=2 point=1 probe_x=46.500 probe_y=90.000 probe_z=-0.231833
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=4 total=4 phase=probing round=2 point=4 probe_x=619.999 probe_y=83.501 probe_z=-0.232354
// action:operation_measurements id=quad_gantry_level type=quad_gantry_level phase=measured round=2 probe0=-0.231833 probe1=-0.230271 probe2=-0.234438 probe3=-0.232354 range=0.004167 tolerance=0.100000
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=4 total=4 phase=verified round=2
// action:operation_end id=quad_gantry_level type=quad_gantry_level result=completed
```

需要继续调平时，评估阶段后开始下一轮：

```text
// action:operation_update id=quad_gantry_level type=quad_gantry_level step=0 total=4 phase=probing round=2
```

### 错误原因

| `reason` | 屏幕显示含义 |
| --- | --- |
| `homing_failed` | 回零失败。 |
| `probe_failed` | 探测失败。 |
| `max_adjust_exceeded` | 所需 Z 调整量超过配置的 `max_adjust`。 |
| `retry_increasing` | 重试时误差持续变大。 |
| `max_retries_exceeded` | 超过允许的最大重试次数。 |
| `cancelled` | 用户或上层流程取消。 |

目前 `max_adjust_exceeded` 已上报；其余错误原因属于 V2 错误收口后续项，屏幕端应预先支持这些枚举。

示例：

```text
// action:operation_error id=quad_gantry_level type=quad_gantry_level result=failed reason=max_adjust_exceeded
```

## 双喷头 XY 偏移 / KTAMV_CALIB_NOZZLE

`KTAMV_CALIB_NOZZLE` 先完成相机标定，再依次校准 T0、T1 喷头的相机中心位置，最后计算两个喷头的 XY 相对偏移。本流程只报告 `x_offset` 与 `y_offset`；双喷头 Z 偏移属于独立流程，不能使用本会话的结果。

| `phase` | `step` | 屏幕显示含义 |
| --- | --- | --- |
| `preparing` | 0 | 正在回零并清洁喷头。 |
| `camera_calibration` | 1 | 正在建立相机到运动坐标的映射。 |
| `nozzle_t0_calibration` | 2 | 正在识别、修正并验证 T0 喷头位置。 |
| `switching_to_t1` | 3 | 正在记录 T0 原点并切换至 T1。 |
| `nozzle_t1_calibration` | 4 | 正在识别、修正并验证 T1 喷头位置。 |
| `calculating` | 5 | 正在计算最终双喷头 XY 偏移。 |

每次 T0/T1 相机修正会额外携带 `attempt`、`dx`、`dy`：`attempt` 从 1 开始，`dx` 和 `dy` 是本轮计算出的修正量，单位 mm。

```text
// action:operation_begin id=dual_nozzle_xy_offset type=dual_nozzle_xy_offset step=0 total=5 phase=preparing
// action:operation_update id=dual_nozzle_xy_offset type=dual_nozzle_xy_offset step=2 total=5 phase=nozzle_t0_calibration attempt=1 dx=-0.120 dy=0.045
// action:operation_update id=dual_nozzle_xy_offset type=dual_nozzle_xy_offset step=3 total=5 phase=switching_to_t1
// action:operation_update id=dual_nozzle_xy_offset type=dual_nozzle_xy_offset step=4 total=5 phase=nozzle_t1_calibration attempt=2 dx=0.030 dy=-0.010
// action:operation_update id=dual_nozzle_xy_offset type=dual_nozzle_xy_offset step=5 total=5 phase=calculating
// action:operation_end id=dual_nozzle_xy_offset type=dual_nozzle_xy_offset result=completed x_offset=-0.210 y_offset=0.035
```

失败时发送：

```text
// action:operation_error id=dual_nozzle_xy_offset type=dual_nozzle_xy_offset result=failed reason=failed
```

## 双喷头 Z 偏移 / DUAL_Z_PROBE_CALIBRATE

`DUAL_Z_PROBE_CALIBRATE` 使用已保存的 XY 偏移依次测量 T0 和 T1 探针平均值，再计算两喷头的最终 Z 相对偏移。它使用独立的 `id=dual_nozzle_z_offset`，不能与 XY 会话混用。

| `phase` | `step` | 屏幕显示含义 |
| --- | --- | --- |
| `preparing` | 0 | 正在读取 XY 前置偏移和 Z 补偿。 |
| `probe_t0_accuracy` | 1 | 正在执行 T0 探针精度测量。 |
| `switching_to_t1` | 2 | 正在切换并移动 T1。 |
| `probe_t1_accuracy` | 3 | 正在执行 T1 探针精度测量。 |
| `calculating` | 4 | 正在计算并校验最终 Z 偏移。 |

成功结束会携带 `z_offset`、`left_probe_avg` 和 `right_probe_avg`，均为毫米且保留六位小数：

```text
// action:operation_end id=dual_nozzle_z_offset type=dual_nozzle_z_offset result=completed z_offset=0.125000 left_probe_avg=1.000000 right_probe_avg=1.125000
```

失败消息的 `reason` 可以是 `missing_xy_offset`、`gcode_command_failed`、`probe_result_failed`、`offset_out_of_range` 或 `calculation_failed`：

```text
// action:operation_error id=dual_nozzle_z_offset type=dual_nozzle_z_offset result=failed reason=gcode_command_failed
```

## 主探针 Z 偏移 / PROBE_CALIBRATE

`PROBE_CALIBRATE` 使用 `id=probe_z_offset` 和 `type=probe_z_offset`。它只有一个逻辑步骤：开始消息使用 `step=0 total=1 phase=calibrating`；有效 `ACCEPT` 将在写入待保存的 `z_offset` 后结束。流程不发送自动探测、移动或 `TESTZ` 中间状态。

```text
// action:operation_begin id=probe_z_offset type=probe_z_offset step=0 total=1 phase=calibrating
// action:operation_end id=probe_z_offset type=probe_z_offset result=completed z_offset=-0.125
```

手动探针未成功结束时发送 `reason=cancelled`；自动探测、自动移动、创建手动会话或写入偏移失败时发送 `reason=failed`。每个会话只会有一个终态消息。

```text
// action:operation_error id=probe_z_offset type=probe_z_offset result=failed reason=cancelled
```

`PROBE_CALIBRATE_NOZZLE` 不使用该协议。完整字段说明见 `docs/Probe_Offset_Operation_API.md`。

## 回零 / G28

`G28` 使用 `id=homing` 和 `type=homing`。开始消息在确定待回零轴后发送，包含 `step`、`total` 和 `axes`。回零成功后发送 `operation_end`；运动失败或打印机断电时发送 `operation_error reason=failed`。回零过程中不发送中间 `update` 消息。

```text
// action:operation_begin id=homing type=homing step=0 total=3 axes=xyz
// action:operation_end id=homing type=homing result=completed axes=xyz
```

失败时：

```text
// action:operation_error id=homing type=homing result=failed reason=failed
```

完整字段说明见 `docs/Homing_Operation_API.md`。

## 屏幕端处理要求

1. 收到 `operation_begin` 时切换或刷新校准页面。
2. 收到 `operation_update` 时仅更新进度和当前阶段，不清空已保存的 `measurements`。
3. 收到 `operation_measurements` 时按 `phase` 覆盖对应的探测结果或调整结果。
4. 收到 `operation_end` 时显示成功状态，并保留最终数据供用户查看。
5. 收到 `operation_error` 时显示与 `reason` 对应的本地化错误，停止加载动画，并保留最后一次有效结果。
6. 本协议是推送协议。屏幕在校准开始后重新连接时无法恢复先前消息；后续可用 Moonraker 状态对象补充恢复能力，但不改变本协议的 action 和字段。
