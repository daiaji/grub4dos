# Phase 2(UHCI/OHCI)工作分解纲要

> 性质:立项前的 briefing,非正式方案(方案 §6 规定 M5 届时"另立方案")
> 前提:**G1 数据门**——M4 真机矩阵的"FS 设备/EHCI 失败兜底"报障占比达到
> 明显阈值才立项。以下按立项后的实施顺序排列。

## 0. 决策与范围

- **UHCI 先行**(Intel ICH7–ICH10 伴随控制器全是 UHCI,是目标硬件主力)
- **OHCI 按数据再定**(AMD SB700/750 伴随是 OHCI;USBDDOS ohci.c 已有参考)
- xHCI 维持不立项(方案既有政策,A6 提示覆盖)
- 核心价值:LS/FS 设备从"EHCI 根端口物理性不可用"(case 4)变为原生可用

## 1. 内存/架构结构调整【第一道坎,所有后续工作的前置】

现状:单块自搬移驱动,常驻区 2908/3072B **只剩 164B**,装不下第二套传输引擎。

| 选项 | 内容 | 代价 |
|---|---|---|
| A | 扩 0x413 收缩量 3KB→4/5KB,双控制器共存一个常驻区 | 修改常驻预算常量;多占 1–2KB 常规内存 |
| B | 学 Plop 拆多 blob 模块('PoLP' 式按需加载) | 架构重构量大,但扩展性最好 |

需评审定夺;此项本身是独立工程,应先于任何控制器代码动工。

## 2. EHCI 侧配套改动【小而逻辑关键】

1. **Port Owner 移交补完**:A1 重试块已写 0x3002(PORTSC bit13),需核对语义
   是否完整(参考 USBDDOS `HCD/ehci.c:177–191`:等自清后**逐端口**移交)
2. **CONFIGFLAG=0 回退**:EHCI 初始化失败后释放端口给伴随控制器
   (方案 §1.2 在 Phase 1 明确不做,Phase 2 的前置条件)
3. **控制器发现扩展**:Check_bus 目前只取第一块 EHCI(B103 SI=0);需要
   枚举全部 PCI 功能,并把 EHCI 与其伴随控制器关联(Intel 平台伴随关系
   看 EHCI xECP/芯片组已知布局;这是"LS/FS 设备交给谁"的依据)

## 3. UHCI 模块【主体工程】

- **寄存器空间**:PCI BAR 是 **I/O 端口**而非 MMIO——寄存器访问层要新增
  in/out 路径(现有 Write_Register/Read_Register 只走 MMIO 窗口)
- **LEGSUP 握手**:Intel 系在 PCI 0xC0(与 EHCI 的 xECP 握手并存,含 PS/2
  直通报语义,不能一刀切清零)
- **初始化**:HC 复位 → 帧表分配与填充(1024 × 4B,1ms/帧)→ 骨架 QH 调度
  → RUN
- **传输**:QH/TD 构建(control/bulk/interrupt)、TD 深度/广度链接、短包处理、
  完成检测(TD active 位轮询,配合 A4 同款真实时间超时)
- **枚举/端口**:PORTSC 位与 EHCI 不同(reset 是置位 bit6;**bit9 直接报告
  LS 设备**——case 4 场景在 UHCI 上是正路);复位语义、重试沿用 A1/A2 模式
- **参考底座**:USBDDOS `HCD/uhci.c`(GPLv2,语义可合法借)+ Plop 反汇编
  UHCI 模块(plpbt-decompile out/disasm_full.asm)
- **验证**:usb2test 新增虚拟 UHCI(I/O 端口桩 + 帧表遍历器);设备模型/SCSI
  层/int13 桩/14 用例全部复用平移,case 4 断言翻转为"枚举成功"

## 4. OHCI 模块【按 AMD 数据决定】

MMIO + 内存 HCCA 通信区(中断表 32 项/done head)、ED/TD 链、状态机须按
SUSPEND→RESUME→OPERATIONAL 时序走。结构比 UHCI 干净但启动时序坑多。
参考 USBDDOS `HCD/ohci.c`。验证同 §3 模式。

## 5. 芯片怪癖清单【贯穿,逐个核对】

USBDDOS README 已列:NEC µPD720101、ALi M5237、SiS 630、OPTi 82C861。
怪癖处理借 USBDDOS 现成逻辑,真机矩阵对应加行。

## 6. A6 状态码与提示扩展

- `usb_fail_stage` 编码扩展或按控制器复用(0x80/0x81/0x82 旧编码仍不动)
- 失败提示文本适配新覆盖面("换 USB2.0 口"类提示在 UHCI 上线后要更新)

## 7. 验证与回归

- usb2test 双控制器轨(虚拟 EHCI + 虚拟 UHCI 并存,模拟 CONFIGFLAG=0 回退
  与 Port Owner 移交路径)
- 真机矩阵扩展:UHCI 平台行(Intel 板全适用)、OHCI 行(AMD 板)
- A7 预算按 §1 选定的内存策略重新核算

## 工作量估计

Phase 1 的 2–3 倍。大头:§1 内存结构调整、§3 UHCI 骨架调度与 I/O 寄存器层。
验证基建(usb2test)是现成资产,是最大的既有优势。
