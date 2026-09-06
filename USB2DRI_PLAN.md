# grub4dos 内置 USB 2.0 驱动(USB2DRI)改进实施方案

> 版本:v1.0(2026-09-05)
> 依据:USB2DRI(`stage2/asm.S:8076–10297`)静态分析、Plop Boot Manager 逆向行为验证
> (`D:\repo\plpbt-decompile` 的 DRIVERS.md + 反汇编 + bootlab 动态实测)、USBDDOS
> (`D:\repo\USBDDOS`,GPLv2)参考评估。三份分析结论已交叉核对,本文只收口为可执行项。

---

## 0. 摘要

- **路线:行为对齐 Plop。** Plop 与 grub4dos 架构差异大(三控制器模块化 vs 单块汇编),
  不做结构移植,只对齐 EHCI 初始化/枚举/传输的**行为语义**。
- **Phase 1 只动 EHCI 路径**,七项改动(A1–A7),其中五项直接对应 Plop 的成功率来源。
- **验证双轨**:轨道 A 为 Unicorn 模拟回归(把 plpbt-decompile 的 bootlab 方法论平移到
  USB2DRI 驱动块,先建基线再改代码);轨道 B 为真机矩阵。模拟与真机均通过才算验收。
- **Phase 2(UHCI/OHCI)设数据决策门**,默认不做;xHCI 不立项,改为探测 + 用户提示。

---

## 1. 背景与路线决定

### 1.1 为什么 Plop 成功率更高(已实证的三类根因)

| 维度 | grub4dos 现状 | Plop 行为 | 差距影响 |
|---|---|---|---|
| 端口枚举 | 单遍,失败即跳过 | 每端口有界重试循环 | 时序边界型偶发失败无第二次机会 |
| 端口复位 | PR 100ms→清→200ms→**强制 PED=1**,不等自清、无恢复期 | 禁用全部端口→PR ~330ms→**等 PR 自清**→PED 门 | 慢设备枚举失败 |
| 描述符读取 | 首请求直接 18 字节 | **8 字节先行**,bMaxPacketSize0 自适应 MPS | 小 ep0 设备兼容性 |
| 传输超时 | 固定 0x100000 圈 CPU 循环 | 真实时钟(依赖 qTD 错误完成) | 超时随主频漂移(#139 的 --delay 即为救此) |
| 控制器覆盖 | 仅 EHCI、仅第一块(B103 SI=0) | EHCI/UHCI/OHCI × 全部控制器 × 全部端口 | 硬覆盖差距,Phase 2 范围 |

### 1.2 明确不采纳的部分

- Plop 的 **CONFIGFLAG=0 回退**(EHCI 失败后释放端口给 1.1 伴随控制器):grub4dos 没有
  UHCI/OHCI 模块可接收,Phase 1 引入只会"把端口让出去收不回来"。Phase 1 保持
  grub4dos 现有的 **CONFIGFLAG=1**(对齐 Plop 的 EHCI-only 行为)。
- Plop 的五 blob 常驻架构('PoLP' 协议、驱动器号重定向):grub4dos 的 INT13 hook 框架
  已自成体系,不动。

---

## 2. 现状结构:改造前必须知道的两个工程事实

### 2.1 USB2DRI 块的两段式布局(以 `Ending` 为界,asm.S:9342)

```
USB2DRI (8076)
  ├─ 数据区:qh(0x90)、cbw_qq/csw_qq、GDT、Dev 表(8×DevLen)、Dri 表、
  │          请求模板(Get_device 等 13 组)、控制变量
  ├─ 常驻代码:Entry_int13(8326) 及 int13 服务、Delay(8676)、Con_mon(8761)、
  │            Port_Reset(8775)、Set_QH(8803)、Bulk_transfer(8906)、
  │            SCSI_Command(9006)、Read_write_usb(9079)、寄存器访问(9165+)
  Ending (9342)  ←—— 初始化时 rep movsb 复制到常规内存顶(0x413 收缩 3KB)的截止点
  ├─ 一次性初始化代码(不常驻,留在 stage2 原位运行):
  │   Check_bus(9319)、host_initialization(9403)、Initialization(9550)、
  │   usb_probe(10081)、Fill_device_structure(10135)、Device_enumerate(10248)
  └─ 块结束占位:. = USB2DRI + 0x13c0(约 10300)
```

**推论:**
- A1(枚举重试)、A3(描述符分步)、A5(初始化时序)落在 Ending 之后,**不占常驻预算**;
- A2(Port_Reset)、A4(Set_QH 超时)在常驻区,**改动需控制字节数**,并确认
  `Ending - USB2DRI ≤ 3072` 的既有余量(M0 中实测确认,见 §5.1)。
- 若块整体要加东西,需同步调整 `. = USB2DRI + 0x13c0` 占位及后续符号偏移。

### 2.2 已核实、无需重做的部分

- **LEGSUP 握手 grub4dos 已有**:host_initialization 读 HCCPARAMS.xECP,走 cap ID=1,
  写 OS-owned(+3 字节位 0,经 `orw $0x100` 于 +2 字)、轮询 BIOS-owned 清零
  (500×Delay(10)≈5s 有界)、最后写 0 到 +4(关 Intel USBLEGCTLSTS SMI)。与
  Plop/USBDDOS 的握手语义一致,**仅纳入回归测试,不改代码**。
- 数据通路已有 3 次重试 + Bulk Reset + 清端点 halt(`Read_write_repeat_Number`,
  asm.S:9102)——保留。
- hub 枚举(usb_probe,10081)Plop 反而没有——保留,属 grub4dos 优势。

---

## 3. Phase 1:行为对齐改动清单

### A1. 端口级枚举重试循环 【初始化区,无预算约束】

- **位置**:`Device_enumerate`(asm.S:10248)。
- **现状**:每端口一次"连接判断→Port_Reset→Fill_device_structure",失败直接下一端口。
- **目标(Plop 语义)**:每端口最多重试 2 次(共 3 次尝试),重试前重置该设备槽状态。
- **改法**:在 `3:` 分支(Fill_device_structure 调用处)外包一层重试计数;失败路径
  (Status≠0 → `5:`/`jscl`,MaxLUN=0xf0)时:恢复 Dev 槽初值(DevID=0、ConSize=0x40、
  MaxLUN=0xff、In/Out 端点清零)→ 重新 `call Port_Reset` → 重走 Fill_device_structure。
- **验收**:模拟中让虚拟设备在前 N 次复位后 PED 才置位(慢设备模型),驱动能在
  重试内枚举成功;重试耗尽后状态与现状一致。

### A2. Port_Reset 对齐:等 PR 自清 + 复位恢复期 【常驻区,注意字节数】

- **位置**:`Port_Reset`(asm.S:8775)。
- **现状**:PR|PP 写入 → 100ms → 清 PR → 200ms → 写 0x1007(强制 PED=1)。
- **目标(Plop 语义)**:清 PR 后**轮询 PR 位(bit8,即 AH 的 bit0)自清**,上限约
  500ms(50 次 × Delay(10));随后加 200ms 复位恢复期,再写 0x1007。
- **示意**(替换现有清 PR 后的 200ms Delay):
  ```asm
  	movl	$0x00001000, %eax
  	call	Write_Register
  	movw	$50, %bp                 /* 50 × 10ms = 500ms 上限 */
  1:	call	Read_Register
  	testb	$01, %ah                 /* PORTSC bit8 = Port Reset */
  	je	2f
  	movw	$10, %ax
  	call	Delay
  	decw	%bp
  	jnz	1b
  2:	movw	$200, %ax                /* 复位恢复期 */
  	call	Delay
  	movl	$0x00001007, %eax
  	call	Write_Register
  	call	Read_Register
  	ret
  ```
- **行为变化(需记录)**:PED 门从"必真"(强制写 1)变为"写 1 但以自清为前置"。
  高速设备行为不变;纯 FS 设备在 EHCI 根端口本就无可用传输路径(EHCI 无 split
  transaction),枚举结果不变,但失败更快、状态更真实。
- **验收**:模拟中 PR 自清延迟 300ms 的设备能通过;PR 永不自清的故障设备在 500ms
  上限后失败且不影响其他端口。

### A3. 描述符分步读取:8 字节先行 + MPS 自适应 【初始化区】

- **位置**:`Fill_device_structure`(asm.S:10135)+ 模板区(约 8280)。
- **现状**:首笔控制传输直接 `Get_device`(wLength=0x12,18 字节),地址 0。
- **目标(Plop 语义)**:
  1. 新增模板 `Get_device8: .byte 0x80,06,00,01,00,00,0x08,00`;
  2. 地址 0 先读 8 字节 → `Cache[7]` 即 bMaxPacketSize0;
  3. 合法化(仅接受 8/16/32/64,越界回退 64)后写入 `ConSize(%bx)`;
  4. 再执行现有 `Get_device`(18 字节)→ 后续流程不变(Set_QH 每次从 Dev 表取 MPS,
     自动生效)。
- **验收**:虚拟设备 ep0 MPS=8 时枚举成功(现状应失败或异常);MPS=64 设备行为不变;
  8 字节读失败时回退到现行完整读路径。

### A4. 传输完成等待改真实时间 【常驻区,尺寸近似中性】

- **位置**:`Set_QH`(asm.S:8803),两处 `movl $0x00100000, %ecx` 循环(8843、8860)。
- **现状**:0x100000 圈迭代(每圈一次完整的实模式↔保护模式 MMIO 往返),超时时长
  随 CPU 主频漂移。
- **目标**:两处均改为"每圈读 USBSTS → 未完成则 `Delay(1)`"的 **100ms 真实时间上限**
  (Delay 的毫秒定标已存在,Determine_delay_units asm.S:9210):
  ```asm
  	movw	$100, %cx                /* 100 × Delay(1ms) */
  1:	movw	$04, %si
  	call	Read_Register
  	testb	$03, %al                 /* USBINT|USBERRINT */
  	jne	2f
  	pushw	%cx
  	movw	$1, %ax
  	call	Delay
  	popw	%cx
  	loop	1b
  	orb	$2, (Status - USB2DRI)
  	jmp	1f
  ```
  (8860 处等 qTD inactive 的循环同样处理。)
- **兼容性**:保留 `usb --delay`(#139)语义不变;Delay(1) 的实际粒度在 M0 模拟中
  校验(定标依赖 0x46C tick + 0xCF8 端口循环,模拟环境两者均已具备)。
- **验收**:同一测试用例在"快/慢 CPU 模拟"(不同指令预算下 tick 仍按真实时间推进)
  中超时行为一致。

### A5. 初始化时序微调 【初始化区】

- **位置**:`host_initialization`(asm.S:9403)尾部 + `Initialization`(9550)。
- **现状**:全端口上电 → 100ms → CONFIGFLAG=1 → 500ms → 枚举。
- **目标(Plop 语义)**:上电后总等待 ≥ 300ms 即可(现 600ms 已满足,不动);唯一
  增量:在 `Device_enumerate` 逐端口循环里,对"上电后 CCS 置位但首次复位失败"的端口
  由 A1 的重试覆盖,无需额外全局延时。**本项实为核对项,无代码改动**(M0 模拟确认
  上电→CCS 采样间隔足够即可)。

### A6. 枚举阶段状态码细化 【C 侧,可选但推荐】

- **位置**:`usb_func`(stage2/builtins.c:15009)+ `usb_count_error` 约定。
- **现状**:0x80(开始)/0x81(枚举中)/0x82(错误)/<0x80(设备数)。失败时用户只能
  看到"未找到设备"。
- **目标**:新增 `usb_fail_stage` 字节(shared.h):0=无 EHCI 控制器、1=端口无设备、
  2=复位失败、3=描述符失败、4=LUN 失败;`usb` 命令失败分支按 stage 输出可操作的
  提示(换 USB2.0 口 / 检查 BIOS EHCI hand-off / xHCI-only 平台说明)。编码沿用
  0x80–0x82 旧值不变,新字段只增不改,外部脚本兼容。
- **验收**:模拟各失败分支,输出文本正确;`usb --init` 现有输出格式不回归。

### A7. 块尺寸与常驻预算核对 【工程护栏,贯穿全程】

- 每次改动后核对:`Ending - USB2DRI ≤ 3072`(0x413 收缩 3 字节 = 3KB);块总长
  0x13C0 占位是否需要同步扩大。A2/A4 的净增量预计 < 40 字节,余量充足,但必须在
  构建输出里固化这个检查(见 §5.3 构建脚本项)。

---

## 4. 明确不变项(防范围蔓延)

1. 仅 EHCI、仅第一块控制器(Check_bus B103 SI=0)——Phase 1 不变。
2. CONFIGFLAG=1 不变;不引入 CONFIGFLAG=0 回退。
3. hub 枚举、多 LUN(Get_lun,最多 4 LUN)、3 次批量重试 + Reset_Recovery——全部保留。
4. `usb_count_error` 0x80/0x81/0x82 旧编码语义兼容。
5. `--delay` 参数语义不变(#139)。
6. int13 hook 框架、驱动自搬移机制(0x413 trick)不动。

---

## 5. 验证体系

### 5.1 轨道 A:模拟回归(Unicorn,方法平移自 plpbt-decompile/bootlab)

**位置**:`D:\repo\grub4dos\usb2test\`(新建):`runner.py`(harness)+ `cases/`(用例)+ `README.md`。

**搭建步骤:**
1. **驱动块提取**:构建 grub4dos 后,在 grldr/stage2 镜像中按 `"USB2DRI "` 签名定位
   块起始,提取 0x13C0 字节;从链接 map 取 `usb_count_error / usb_drive_num /
   usb_md_address / max_dri` 四个 EXT_C 绝对符号的线性地址(harness 在该地址放置
   可写变量,使块内 `%fs:ABS(...)` 引用命中)。备选:把块加载到链接原址。
2. **BIOS 桩**(直接复用 bootlab.py 的通用件):INT 1Ah PCI BIOS32(B101/B103/B108–
   B10C,`PCI_DEVS` 配 class 0x0C0320 的 EHCI)、INT 13h(磁盘镜像,充当局内"老 BIOS"
   供 old_int13 链回)、BDA(0x413=640、0x410、0x475、0x46C 由 IRQ0 tick 递增)、
   端口桩(0x60/0x64/0x92 A20、0xCF8 延时读返回 0、INT 15h AH=24h)。
3. **虚拟 EHCI**:MMIO 页 + 读物化/写捕获(USBCMD/USBSTS/PORTSC/ASYNCLISTADDR),
   触发条件沿用 bootlab 的"USBCMD 写入且 value&0x24 → usb_process_async"——已核实
   grub4dos 每笔传输写 USBCMD=0x80021(asm.S:8840)恰好命中;QH/qTD 按标准 EHCI
   语义执行(grub4dos 的 QH 在 qTD1+0x40、ASYNCLISTADDR 指向它,结构已比对一致);
   overlay 回写镜像最后一支 qTD(grub4dos 靠读 overlay token bit31 续 toggle,
   asm.S:8994 附近,天然闭环)。
4. **测试壳**:16 位 stub,DS=CS,far call `Initialization`,回收 usb_count_error /
   usb_drive_num,随后发 INT 13h AH=42h 读扇区与镜像逐字节比对。
5. **M0 基线(改代码前先跑)**:用例 1–12 全部在**未修改**驱动上跑通/记录现状,
   作为回归对照;同时实测 `Ending - USB2DRI` 实际字节数(§A7)。

**用例矩阵(cases/):**

| # | 用例 | 断言 |
|---|---|---|
| 1 | 干净枚举(HS 设备) | 设备数=1、地址分配、LUN 读取、AH=42h 读与镜像一致 |
| 2 | 慢设备(PED 延迟 300ms) | A2 等自清生效,枚举成功 |
| 3 | 无设备(CCS=0) | usb_count_error=错误码,A6 stage=0/1 |
| 4 | LS 设备(line status=K) | 端口被跳过(行为与现状一致) |
| 5 | 首次复位失败、重试内恢复 | A1 重试生效,枚举成功 |
| 6 | 枚举中 STALL(halted qTD) | A1 重试;耗尽后失败状态干净 |
| 7 | LEGSUP:BIOS-owned=1 | 握手完成,BIOS-owned 清零,SMI 区写 0 |
| 8 | HCCPARAMS=0(无扩展能力) | 跳过握手分支,正常初始化 |
| 9 | int13 AH=02/AH=03/AH=42/48 | 读写与镜像一致(#137 写回归) |
| 10 | 多 LUN 设备(2 LUN) | 两个盘号均注册 |
| 11 | hub 二级枚举 | hub 后设备可见 |
| 12 | qTD 永不完成 | A4:100ms 真实超时,Status 置错,不挂死 |
| 13 | FS 设备(ep0 MPS=8,HS 握手失败) | A3 前后行为对比记录;A3 后 MPS=8 设备可枚举 |
| 14 | 双 EHCI 控制器 | 现状确认:只用第一块(Phase 1 预期行为) |

### 5.2 轨道 B:真机矩阵

| 平台 | 关注点 |
|---|---|
| Intel ICH(ICH7–ICH10)主板 | UHCI 伴随环境下的 EHCI 主路径、BIOS hand-off 后接管 |
| AMD SB700/SB750 主板(2020 更新的目标硬件) | 回归确认不破坏 |
| 双控制器主板 | 首块选中策略的行为记录 |
| 带 hub 的拓扑 | hub 枚举保留验证 |
| FS 老 U 盘 / 读卡器 | 记录现状与 A3 后变化(预期:EHCI 根端口仍不支持 FS,失败信息更明确) |
| 写路径 | AH=03/43 写盘 + #137 复现场景 |
| `--delay` 参数 | #139 语义不变 |

真机为最终验收;模拟通过 ≠ 真机通过,两轨结论分别记录。

### 5.3 构建与 CI

- 构建走仓库现有流程(`./build` / autotools;当前工作区 autotools 文件有本地改动,
  动工前先确认可出干净基线构建)。
- 在构建脚本(或 usb2test/extract)中固化 §A7 检查:输出 `Ending-USB2DRI` 字节数,
  超 3072 直接报错。
- 可选:把 usb2test 用例 1/9/12 设为 CI 冒烟集(参照 USBDDOS 的 7-test QEMU 套件形态)。

---

## 6. 里程碑与决策门

| 里程碑 | 内容 | 通过标准 |
|---|---|---|
| M0 | usb2test harness 建成 + 未改驱动基线(14 用例) | 用例 1/9 现状通过并记录;块尺寸实测 |
| M1 | A1 + A2(重试循环、复位自清/恢复期) | 用例 2/5/6 通过,1/9/11 无回归 |
| M2 | A3(8 字节先行) | 用例 13 通过,1/9 无回归 |
| M3 | A4(真实时间超时)+ A6(状态码) | 用例 12 通过;3/6 的输出含 stage 信息 |
| M4 | 真机第一轮(Intel 板 + AMD 板) | 主路径无回归,记录 FS/慢设备实测 |
| G1 | **Phase 2 决策门** | 依据 M4 数据:FS 设备或 EHCI 失败兜底的报障占比 ≥ 明显阈值 → 立项 UHCI(先)→ OHCI(后);否则关闭 Phase 2 |
| M5(可选) | UHCI 模块(参考 USBDDOS uhci.c 语义 + Plop 阶段化流程),随后按数据决定 OHCI | 另立方案 |

**xHCI 政策(不立项,长期有效):** 不写实模式 xHCI 模块。A6 的提示覆盖用户引导;
若未来第三方 xHCI 控制器(ASMedia/Etron/Renesas 等)报障积累,参考底座为 Linux
xhci-pci(GPL)+ QEMU xHCI 模型,届时另立方案。

---

## 7. 风险与缓解

| 风险 | 缓解 |
|---|---|
| A2/A4 在常驻区超预算 | §A7 构建期检查;A4 尺寸近似中性,A2 净增 <25 字节 |
| 强 PED 语义变化影响个别现存"能用"的 FS 设备 | 先在 M0 模拟与 M4 真机记录现状行为再合入;此类设备在 EHCI 上本无传输路径 |
| Delay(1) 粒度问题(定标依赖 55Hz tick + 0xCF8 循环) | M0 用例 12 专项校验;必要时内部按 Count_1ms 细分 |
| 模拟器与真实 EHCI 同时错的盲区 | 真机矩阵为金标准;关键寄存器语义以 Linux ehci-hcd.h 位定义为参照 |
| 外部脚本依赖 usb_count_error 旧编码 | §4.4 兼容约定 + 用例覆盖 |

---

## 8. 参考材料索引

- 本仓库:`stage2/asm.S:8076–10297`(USB2DRI)、`stage2/builtins.c:15009`(usb 命令)、
  `stage2/shared.h:1121–1125`(驱动对外符号)。
- Plop 行为规格:`D:\repo\plpbt-decompile\DRIVERS.md`(§2 blob 框架、§6 EHCI/UHCI/OHCI
  传输层实测)、`out/disasm_full.asm`(EHCI 模块 file 0x469D–0x9307:初始化 sub_501B、
  端口 sub_525E–52A3、复位 0x49C3、枚举 sub_4B9C、门铃 sub_54A9、等待 sub_54F6)、
  `tools/bootlab.py`(harness 通用件来源)、`DYNAMIC_FINDINGS.md`。
- USBDDOS(GPLv2,可合法借逻辑):`USBDDOS/HCD/ehci.c:92–110`(LEGSUP)、
  `ehci.c:177–191`(等自清 + 逐端口 PortOwner 移交,Phase 2 的 FS/LS 参考解法)、
  `uhci.c`/`ohci.c`(Phase 2 参考)、README(芯片怪癖清单:NEC µPD720101、ALi M5237、
  SiS 630、OPTi 82C861)。
- 位定义参照:Linux `drivers/usb/host/ehci.h`(PORTSC 位布局,含 line-status 11:10)。
