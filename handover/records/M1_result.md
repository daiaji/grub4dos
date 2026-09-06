# M1 结果 — A1(端口重试)+ A2(复位自清/恢复期)

## 驱动改动 (stage2/asm.S)
- **A2** `Port_Reset`: 清 PR 后轮询 PORTSC bit8 自清(50×Delay(10) ≤ 500ms),再 200ms 复位恢复期,写 0x1007
- **A1** `Device_enumerate`: 每端口最多 3 次尝试;失败(复位 PED 未置 / Fill 失败)时重置设备槽(DevID=0、ConSize=0x40、MaxLUN=0xff、端点清零)后重试
- 新增 `enum_retry` 字节(初始化区,不占常驻预算)
- 块占位 0x13C0 → 0x13E0(§2.1 规则)
- **A7: Ending-USB2DRI = 2892 ≤ 3072 OK**

## 模拟回归 (轨道 A)
| 用例 | 基线 | M1 | 说明 |
|---|---|---|---|
| 1 干净枚举 | PASS | PASS | 无回归 |
| 2 慢设备 PR 300ms | PASS* | PASS | *A2 轮询真实等待,PR 自清 220ms 观察到 |
| 5 首次复位失败 | PASS* | PASS | *A1 重试真实发生:PR 断言 2 次 |
| 6 枚举 STALL | PASS* | PASS | A1 槽重置后重枚举 |
| 9 int13 读写 | PASS | PASS | 无回归 |
| 11 hub | PASS | PASS | 无回归 |

## 模拟器修正(测试基建,非驱动)
- PR 位可见性:PORTSC 读须含 PR 位(此前缺失致 A2 poll 首读即误判自清)
- 0x1007 不再强制 PED(PED 只读,尊重设备 ped_attempts 门)
- Delay 时间模型:0xCF8 读计数按 Count_1ms 推进虚拟 tick;tick_ms=55ms
