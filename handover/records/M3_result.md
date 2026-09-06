# M3 结果 — A4(真实时间超时)+ A6(失败状态码)

## 驱动改动
- **A4** `Set_QH` 两处 `movl $0x100000; loop` CPU 循环 → `100 × Delay(1)` 真实 100ms 超时(USBSTS 未完成则 Delay(1) 再查,超时置 Status bit2/bit8 同旧)
- **A6** 新变量 `usb_fail_stage`(asm.S, 0x8357 原未用字节):
  - stage 0=无 EHCI(Check_bus 前)、1=端口无设备(Device_enumerate 后 usb_count==0)、3=描述符失败(Fill_device_structure 出口)、4=驱动枚举失败(max_dri==0)、复位失败在重试耗尽处=2
  - `shared.h` extern + `builtins.c` `usb` 命令失败分支按 stage 输出可操作提示
- `usb_count_error` 0x80/0x81/0x82 旧编码与语义未动(§4.4 兼容)
- 块占位 → 0x1440;A7=2908 ≤ 3072

## 模拟回归 (轨道 A) — 9 用例全 PASS
| 用例 | 结果 | 说明 |
|---|---|---|
| 1/2/5/6/9/11/13 | PASS | A1/A2/A3 无回归 |
| 3 无设备 | PASS | err=0x81 stage=1 |
| 12 qTD 永不完成 | PASS | err=0x81 stage=1;超时 6.3M insns → 36k insns(真实 100ms) |

## A4 关键验证
- case 12 超时从 CPU 循环(6.3M insns,随主频漂移)→ 36k insns 恒定
  (Delay 由 0xCF8 计数驱动,与虚拟 CPU 速度解耦)
