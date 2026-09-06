# grub4dos USB2DRI Phase 1 交接文档

> 交接日期:2026-09-06　分支:0.4.6a（工作区改动**未提交**,以补丁形式归档）
> 状态一句话:Phase 1 代码(A1–A6)全部完成,模拟验证 14 用例中 13 个 PASS,
> case 4 挂起待定位;M4 真机矩阵与最终交付待办。

---

## 1. 项目是什么

以 [`USB2DRI_PLAN.md`](USB2DRI_PLAN.md) 为唯一规格,对 grub4dos 内置 USB 2.0 驱动
（USB2DRI,`stage2/asm.S:8076–10297`）做**行为对齐 Plop Boot Manager** 的改造:
不做结构移植,只对齐 EHCI 初始化/枚举/传输的行为语义(改动项 A1–A7)。
验证双轨:轨道 A = Unicorn 模拟回归(`usb2test/`),轨道 B = 真机矩阵(未执行)。

规格要点:仅 EHCI 第一控制器、CONFIGFLAG=1、保留 hub/多 LUN/3 次批量重试、
0x80/0x81/0x82 旧编码兼容、`--delay` 语义不变、int13 hook/自搬移机制不动。

## 2. 当前状态快照

| 里程碑 | 状态 | 依据 |
|---|---|---|
| M0 模拟基线 | ✅ 完成 | 未改驱动 14/14 PASS([records/M0_baseline_all14.txt](records/M0_baseline_all14.txt)) |
| M1 A1+A2 | ✅ 完成 | case 2/5/6 验证,1/9/11 无回归([records/M1_result.md](records/M1_result.md)) |
| M2 A3 | ✅ 完成 | case 13 MPS=8 设备经 [8,18,32] 序列枚举成功([records/M2_result.md](records/M2_result.md)) |
| M3 A4+A6 | 🔶 ~90% | 主表 9 用例 PASS([records/M3_result.md](records/M3_result.md));**case 4 挂起**(见 §6) |
| M4 真机矩阵 | ⬜ 待办 | 方案 §5.2 七行表;当前环境无硬件,接手人执行 |
| G1 决策门 | ⬜ 待办 | 依 M4 数据决定 Phase 2(UHCI/OHCI)立项与否 |

A7 常驻预算:**Ending-USB2DRI = 2908 字节 ≤ 3072 上限**(实测,余量 164B)。
A6 stage 语义已验证:成功=0、无设备=1、复位失败=2、描述符失败=3、LUN 失败=4。

## 3. 代码改动清单(全部未提交,补丁在 patches/)

**驱动改动** — [`patches/usb2dri_driver.patch`](patches/usb2dri_driver.patch)(302 行,149 insertions):

- `stage2/asm.S`(+117):
  - **A1** `Device_enumerate`(~10309):每端口 3 次尝试循环(`enum_retry` 计数器,
    位于 Ending 之后零常驻成本),重试前槽预清(DevID=0、ConSize=0x40、
    MaxLUN=0xff、端点清零),失败写 stage=2
  - **A2** `Port_Reset`(~8778):清 PR 后轮询自清(50×10ms=500ms 上限)+ 200ms
    恢复期,替代旧的"清完就强制 PED=1"
  - **A3** `Fill_device_structure`(~10135)+ 模板区:新增 `Get_device8` 模板
    (8 字节描述符先行),bMaxPacketSize0 合法化(仅 8/16/32/64,越界回退 64)
    → ConSize,8 字节失败回退现行 18 字节路径
  - **A4** `Set_QH`(~8803):两处 0x100000 圈死循环改为 100ms 真实时间上限
    (每圈读 USBSTS + Delay(1),case 12 实测超时从 6.3M 指令降到 36k 指令)
  - **A6** `usb_fail_stage` 写入点(VARIABLE @0x8357 原未用字节):
    Initialization 开头=0、无设备=1、描述符失败=3、LUN 失败=4
  - 块占位 `. = USB2DRI + 0x13c0` → `+ 0x1440`
- `stage2/builtins.c`(+48):`usb` 命令失败分支新增 switch(usb_fail_stage)
  输出 case 1/2/3/4 可操作提示(换 USB2.0 口 / 检查 BIOS hand-off / xHCI-only 说明)
- `stage2/shared.h`(+2):`extern unsigned char usb_fail_stage;`

**构建环境改动** — [`patches/build_env.patch`](patches/build_env.patch)(38 行,仅 ldscript):

- `ldscript`:`__bss_start`/`edata`/`end` 的 PROVIDE 符号移出丢弃段(x64 -m32
  链接的 bss 符号检查修复)
- autogen.sh/bootstrap.sh/build/compile/config.* 等 11 个文件 diff 为 0 行,
  仅权限位变化,无内容改动
- `config.cache`(仓库根,未跟踪):预置 `grub_cv_asm_uscore=no` 与 bss 符号检查
  结果,configure 依赖它

## 4. 构建手册(Windows 环境,坑全在此)

1. **工具链**:`D:\tools\x64-gcc`(mingw64,x86_64 宿主 + `-m32 -mno-sse`)。
   **必须用 x86_64 宿主工具链**:i686 工具链的 gas 不支持 `.code64`
   (asm.S 长模式路径需要),已实测不可用。同目录 `x64-gcc.7z` 是备份包。
2. **构建**:`sh usb2test/build.sh`——仓库无 GNU make,该脚本把 stage2/Makefile.am
   的规则转写为 sh(所有 flag 从 configure 生成的 Makefile 读取,保证与
   autotools 一致),支持增量编译,内建 A7 检查(超 3072 直接报错)。
   产物:`stage2/pre_stage2`、`stage2/grldr`、`usb2test/pre_stage2.map-symbols`。
3. **已知坑**(已解决,换环境复发时查这里):
   - `grldrstart.S` 的 `102:` 多位数字本地标签在 binutils 2.39 PE 下触发
     DISP16 \*ABS\* 断言 → build.sh 内建 Python 补丁改名 LBL102_1/2/3
   - configure 需要 `--build=x86_64-w64-mingw32` 显式指定(旧 config.guess)
   - USCORE 检测:x64 -m32 应为无下划线,靠 config.cache 预置 + CFLAGS 加
     `-fno-leading-underscore`(asm.S 用裸符号)
   - `squeeze.py`:grldr 镜像 squeeze 头部语义修正(保留 3MB 洞而非剪掉)

## 5. 测试手册(usb2test/)

- **依赖**:Python 3 + unicorn(2.1.4)
- **运行**:`cd usb2test && python run.py`(全部 14 用例)或 `python run.py 4`(单用例),
  `--json` 出机器可读结果。退出码:有 FAIL 则 1
- **架构**:
  - `runner.py`(~53KB,核心):Unicorn 16 位 harness。BIOS 桩(PCI BIOS32/INT13/
    BDA/端口桩)、虚拟 EHCI 控制器(寄存器模型+PR 自清定时+PED 门控+QH/qTD
    异步引擎)、虚拟 USB 设备模型(MPS/LUN/慢复位/STALL 等)、以及 Unicorn
    16 位模式全部原生解码缺陷的手动仿真(far ret/iret/CR0/lgdt/ljmp/rep 串
    指令/DATA32 ret/int 0xFE 帧构造)
  - `cases.py`:14 用例定义与断言(矩阵见方案 §5.1)
  - `ext_syms.py`/`a7check.py`:map 符号提取与 A7 预算检查
  - 镜像来源:`stage2/grldr`(按 `"USB2DRI "` 签名提取驱动块)
- **当前用例状态**:case 1/2/3/5/6/7/8/9/10/11/12/13/14 PASS,**case 4 挂起**;
  M0 基线(未改驱动)14/14 PASS,对照记录在 records/
- **模型当前态**:runner.py 已含 LS 设备永不授予 PED 的修复(物理正确,
  见 case4_notes.md §2),修复后暴露 case 4 挂死(见 §6)

## 6. 已知问题:case 4(LS 设备)挂起

完整分析见 [`records/case4_notes.md`](records/case4_notes.md)。摘要:

- 三次结果:M0 基线 PASS(err=0x81)→ M3 补跑 FAIL(err=0x01,LS 被枚举成功)
  → 模型修复后 FAIL(60M 指令预算耗尽、17711 次门铃、err 停在 0x80)
- **头号嫌疑**:模型 `exec_qtd` 不检查端口 PED——usb_probe 对"连接但未使能"的
  LS 设备发 hub 描述符请求,模型让设备照常应答导致重试死循环;真实硬件上
  未使能端口的传输永不完成
- 恢复步骤(五步)与注意事项(改动会动"PED=0 时传输可达"这一模型假设,
  需确认 case 3/5/6/12 不受影响)全在 case4_notes.md §4
- M0 时期的模型代码未入 git 无法回溯,"原基线 0x81 从何而来"作为未解之谜
  一并记录在案

## 7. 接手人路线图(从这里开始)

1. **case 4 定位与修复**(case4_notes.md §4)→ M3 闭环
2. **全量回归**:`python run.py` 确认 14/14,更新 usb2test/M3_result.md
3. **M4 真机矩阵**(方案 §5.2):Intel ICH 板 / AMD SB 板 / 双控制器 / hub 拓扑 /
   FS 老设备 / 写路径 / `--delay`。**无硬件时如实记录为待办,不得以模拟代真机**
4. **最终交付**:代码改动汇总(本档 §3 可直接用)、双轨回归记录、G1 结论
   (模拟侧数据:FS 设备在 EHCI 根端口无传输路径,A6 提示已覆盖用户引导;
   Phase 2 依真机报障占比决策)
5. **建议**:工作区改动 review 后提交到 0.4.6a 分支(或先开 feature 分支);
   usb2test/ 与 handover/ 一并入库,避免再出现"M0 代码无法回溯"的情况
6. 若 G1 立项 Phase 2:工作分解纲要见 [`PHASE2_OUTLINE.md`](PHASE2_OUTLINE.md)
   (内存结构调整是第一道坎——常驻区仅剩 164B;UHCI 先行;参考底座
   USBDDOS GPLv2 可合法借逻辑)

## 8. 参考资料

**plpbt-decompile(本机独有,已打包随交接)** — [`plpbt-decompile_snapshot_2026-09-06.zip`](plpbt-decompile_snapshot_2026-09-06.zip):

| 包内路径 | 内容 |
|---|---|
| `DRIVERS.md` | Plop 三控制器逆向行为规格(**A1–A6 的对齐依据**) |
| `out/disasm_full.asm` | Plop 完整反汇编(含 UHCI/OHCI 模块,Phase 2 对齐要用) |
| `tools/bootlab.py` | 模拟 harness 方法论来源(usb2test 的前身) |
| `DYNAMIC_FINDINGS.md` 等 | 动态实测结论、移植评估 |
| `plpbt.bin` | Plop 原始二进制(重跑 bootlab 动态实验需要) |

> ⚠️ 法律注意:该目录是对专有软件 Plop Boot Manager 的**逆向产物**(含其
> 二进制),仅限内部交接与开发参考,**不得公开发布或再分发**。

**USBDDOS(公开仓库,不随附,可重新获取)**:

- 下载:https://github.com/crazii/USBDDOS (GPLv2,可合法借逻辑)
- 分析时所用版本:commit `0d2d6b6`(Merge PR #42 v86-irq-reentrancy)
- 关键文件:`HCD/ehci.c:92–110`(LEGSUP 握手)、`ehci.c:177–191`(等自清 +
  逐端口 PortOwner 移交)、`HCD/uhci.c` `ohci.c`(Phase 2 参考)、
  `README`(芯片怪癖清单:NEC µPD720101/ALi M5237/SiS 630/OPTi 82C861)

**其他**:Linux `drivers/usb/host/ehci.h` — PORTSC 位定义参照。

## 9. 文件地图

```
D:\repo\grub4dos\
├── USB2DRI_PLAN.md            ← 规格(权威版本,本目录同名文件为交接快照)
├── handover\                  ← 本交接目录
│   ├── HANDOVER.md            ← 本文档
│   ├── USB2DRI_PLAN.md        ← 规格快照
│   ├── plpbt-decompile_snapshot_2026-09-06.zip ← Plop 逆向工作区完整快照(本机独有,勿公开)
│   ├── patches\
│   │   ├── usb2dri_driver.patch   ← 驱动改动(stage2 三文件)
│   │   └── build_env.patch        ← 构建环境改动(仅 ldscript)
│   └── records\
│       ├── M0_baseline_all14.txt  ← M0 基线 14/14 PASS 原始记录
│       ├── M1_result.md / M2_result.md / M3_result.md
│       ├── M2_extra_run.txt / M3_full_run.txt ← 补跑原始输出
│       └── case4_notes.md     ← case 4 完整分析(接手必读)
├── usb2test\                  ← 可运行的测试套件(见 §5)
│   ├── runner.py / cases.py / run.py / build.sh / a7check.py / ext_syms.py / squeeze.py
│   └── M*_result.md / case4_notes.md / baseline_all.txt(与 records/ 内容对应)
└── stage2\
    ├── asm.S / builtins.c / shared.h  ← 已改动的驱动源(工作区)
    └── grldr                          ← 最新构建产物(含全部 Phase 1 改动)
```
