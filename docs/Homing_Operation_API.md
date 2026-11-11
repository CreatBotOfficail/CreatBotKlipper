# 回零操作进度接口

本文档定义 `G28` 的 `operation_*` 状态消息，用于屏幕展示回零（homing）操作进度。

本接口不适用于 `PROBE_CALIBRATE`、`BED_MESH_CALIBRATE` 或其它校准流程。这些流程具有各自独立的会话标识和状态协议。

## 适用命令

```gcode
G28
G28 X
G28 Y
G28 Z
G28 X Y
G28 X Y Z
```

不带参数的 `G28` 默认回零所有三个轴，等价于 `G28 X Y Z`。

## 固定标识

| 字段 | 值 |
| --- | --- |
| `id` | `homing` |
| `type` | `homing` |

## 消息流程

```text
operation_begin    step=0 total=N axes=<axes>
operation_end      completed axes=<axes>
```

回零失败时：

```text
operation_begin    step=0 total=N axes=<axes>
operation_error    failed reason=failed
```

每个会话只会发送一个终态消息（`operation_end` 或 `operation_error`）。

## 开始消息

确定待回零轴列表后、实际执行运动前发送：

```text
// action:operation_begin id=homing type=homing step=0 total=3 axes=xyz
```

| 字段 | 说明 |
| --- | --- |
| `step` | 固定为 `0`，表示回零开始。 |
| `total` | 待回零的轴数量，例如 `1`、`2` 或 `3`。 |
| `axes` | 待回零轴的小写拼接，例如 `x`、`yz`、`xyz`。 |

屏幕可将 `total` 作为步骤上限显示进度，但回零过程中不再发送中间 `update` 消息，进度保持等待状态直到终态。

## 成功消息

所有轴回零完成、可靠轴记录更新后发送：

```text
// action:operation_end id=homing type=homing result=completed axes=xyz
```

| 字段 | 说明 |
| --- | --- |
| `result` | 固定为 `completed`。 |
| `axes` | 已完成回零的轴小写拼接。 |

## 失败消息

回零过程中发生错误（如限位开关未触发、运动失败等）时发送：

```text
// action:operation_error id=homing type=homing result=failed reason=failed
```

| `reason` | 含义 |
| --- | --- |
| `failed` | 回零运动失败，例如限位开关未触发、运动超时或打印机断电。 |

发生失败时固件会自动关闭电机（`motor_off`），屏幕应提示用户检查硬件后重试。

## 屏幕端处理

1. 收到 `operation_begin` 时创建或重置回零页面，显示回零进行中。
2. 收到 `operation_end` 时停止加载状态并显示回零完成。
3. 收到 `operation_error` 时停止加载状态，提示回零失败并建议检查硬件。
4. 收到新的同 `id` 开始消息时丢弃上一个未结束会话的状态。
