# case 4（LS 设备）问题存档

> 时间:2026-09-06　状态:**未解决,挂起**（用户指示先存档,转聊 Phase 2）
> 关联:USB2DRI_PLAN.md §5.1 用例 4、A1/A2 改动、M3 补跑

## 1. 时间线（同一用例三次结果）

| 阶段 | 驱动 | 设备模型 | 结果 |
|---|---|---|---|
| M0 基线 | 未改动 | 初版模型 | **PASS** err=0x81（端口被跳过,与"现状"一致） |
| M3 补跑 | A1–A6 全部合入 | 初版模型（未动） | **FAIL** err=0x01（LS 设备被成功枚举!） |
| 模型修复后 | A1–A6 全部合入 | `ped_grant()`: kind∈{ls,fs} 永不授予 PED | **FAIL** stop=max_insns:60M insns,ticks=64456,doorbells=17711,qtds=17711,**err=0x80**（初始化开头写入后再未更新） |

## 2. 模型修复内容（已提交到 runner.py,保留）

`port_read` 的 PED 授予点改为谓词:

```python
def ped_grant(p):
    return p is None or (p.kind != "ls" and p.kind != "fs"
                         and p.reset_count >= p.ped_attempts)
```

物理依据:真实 EHCI 根端口只在设备完成高速 chirp 握手后才置 PED;
LS 设备无握手,PED 永不置位（真机上端口由 companion 控制器接管）。
case 13 不受影响（其 kind="fs_mps8" 是 MPS=8 但做 HS 握手的设备）。
注:目前无用例使用 kind="fs",该排除是防御性的。

## 3. 修复后挂死的分析（当前认知）

驱动侧 PED=0 的路径（asm.S:10309 Device_enumerate）:

- 端口扫描 `testb $01,%al`（CCS）命中 → `testb $04,%ah`（PED）=0 → 进 A1 重试块
- 重试块:槽预清 → Port_Reset（A2 版:PR 100ms→清→轮询自清→200ms 恢复→写 0x1007→读）
  → `testb $04,%al` 仍 0 → 写 0x3002（Port Owner 移交位!）+ Delay(3)
  → `decb enum_retry; jnz retry`（有界 3 次）→ fail_stage=2 → 下一端口
- 该循环**不含任何门铃**（无传输）,与 doorbells=17711 矛盾 → 挂起点不在这里
- 之后 cl_jxq 循环:8 个 Dev 槽中 MaxLUN≠0xff 的槽逐个 `call usb_probe`

**矛盾点与嫌疑:**

1. **门铃 17711 次来自某个传输循环**。最可疑:cl_jxq 对"MaxLUN≠0xff"槽调用
   usb_probe（hub 枚举）。BSS 初始为 0（≠0xff）→ 槽 1–7 都会调 usb_probe;
   而 case 3（无设备）证明 usb_probe×8 能正常终止——差别在于 case 4 的端口上
   **挂着 CCS=1 的 LS 设备**。
2. **模型缺陷候选**:`exec_qtd` 不检查端口 PED——只要门铃响就照常执行传输。
   真实硬件上,对未使能端口的地址 0 传输永不完成（超时）。usb_probe 对"连接但
   未使能"的设备发 GET_DESCRIPTOR(hub),模型让设备正常应答（exec_qtd 无
   kind="ls" 特判,LS 设备像普通海量存储设备一样应答）→ 不是 hub → 失败 →
   驱动某处重试 → 疑似死循环。
   修复方向 A（模型侧,物理正确）:exec_qtd 前检查是否存在 PED=1 的端口,
   否则 qTD 不完成、走 A4 的 100ms 超时。
3. **未解之谜**:M0 基线的 0x81 是怎么来的?两种假说:
   (a) M0 时期的模型对强制 0x1007 写做了"授予 PED"应答,随后 Fill 传输失败跳过;
   (b) M0 时期模型/驱动的某处行-status 检查跳过了端口。
   usb2test/ 全程未入 git,**M0 代码已不可回溯**,只能重新推导。
   （注:现版 port_write 忽略 bit2 写,现版驱动 Device_enumerate 无行-status 检查。）

## 4. 恢复工作时的步骤

1. runner.py 加终态转储:预算耗尽时打印 CS:IP（挂起点定位）
2. 门铃打点（PC+tick）定位 17711 次门铃的来源循环
3. 验证假说 2:exec_qtd 增加 PED 门控后重跑 case 4（预期:驱动在 usb_probe 处
   100ms 超时而非死循环,err 收敛到 0x81,stage=2）
4. 重跑全 14 用例;若 case 4 恢复 0x81 且其余无回归 → M3 闭环,更新 M3_result.md
5. 注意:修复方向 A 会改变"PED=0 时传输可达"这一模型假设,需确认 case 3/5/6/12
   不受影响（它们设备均 PED=1 或无设备,理论无影响）

## 5. 相关文件

- 模型修复:`usb2test/runner.py` port_read 内 ped_grant（本存档 §2）
- 驱动路径:`stage2/asm.S:10309`(Device_enumerate) `:8778`(Port_Reset) `:9593-9611`(A6 写入点)
- 用例定义:`usb2test/cases.py` case 4 = `{"kind": "ls", "ped_after_reset": False}`,断言 err=0x81
- 里程碑记录:M1_result.md / M2_result.md / M3_result.md（M3 主表 9 用例,case 4 属补跑发现）
