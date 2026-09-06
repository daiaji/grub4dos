# M2 结果 — A3(描述符分步读取:8 字节先行 + MPS 自适应)

## 驱动改动 (stage2/asm.S)
- 新增模板 `Get_device8`(wLength=8 的 GET_DESCRIPTOR(device))
- `Fill_device_structure`:地址 0 先读 8 字节 → `Cache[7]`=bMaxPacketSize0 → 合法化(仅接受 8/16/32/64,越界回 64)→ 写入 `ConSize`(Set_QH 的 MPS 来源)→ 再执行现有 18 字节完整读
- 8 字节读失败(Status≠0)→ 直接失败(同旧全读失败路径)
- 块占位 0x13F0 → 0x1410

## 模拟回归 (轨道 A)
| 用例 | 基线 | M2 | 说明 |
|---|---|---|---|
| 1 干净枚举 | PASS | PASS | 无回归(HS MPS=64 设备路径不变) |
| 9 int13 读写 | PASS | PASS | 无回归 |
| 13 FS MPS8 | PASS* | PASS | *基线记录的是 MPS=64 模型;现在设备模型 MPS=8,
  A3 8 字节先行真实生效:GET_DESCRIPTOR 序列 [8,18,32] |

## 验证证据
- case 13 设备 ep0 MPS=8:驱动先发 wLength=8 探针,读到 bMaxPacketSize0=8,
  合法化后 ConSize=8,再完整读 18 字节 → 后续 Set_QH 以 MPS=8 建 qTD → 枚举成功
