# TI DCA1000EVM CLI 的 ARM64 移植补丁

这些补丁修复 TI CLI 源码在 aarch64 上的两个致命问题。
**不含 TI 原始源码** —— 需自行从 TI mmWave Studio 获取：

    mmWaveStudio/ReferenceCode/DCA1000/SourceCode

## 应用

```bash
cd <SourceCode 根目录>
dos2unix makefile                      # makefile 是 CRLF
patch -p1 < Common_Osal_Utils_osal.h.patch
patch -p1 < Common_rf_api_internal.h.patch
patch -p1 < RF_API_recorddatarecv.h.patch
make                                   # 必须在 SourceCode 根目录
```

产出 `libRF_API.so` / `DCA1000EVM_CLI_Control` / `DCA1000EVM_CLI_Record`。
运行前 `export LD_LIBRARY_PATH=$PWD:$LD_LIBRARY_PATH`
（TI 用户指南 3.2.2 写的 `$pwd` 是笔误，小写取不到值）。

## 改了什么

`#pragma pack(1)` 在 `Common/globals.h:63` 和
`Common/rf_api_internal.h:95` 开了不关，经 include 链污染到
`cUdpDataReceiver`（实测 `alignof == 1`）。该类内嵌
`pthread_cond_t`/`pthread_mutex_t`，被压到未对齐地址后，
aarch64 glibc 的 `__aarch64_ldadd8_acq` 直接 SIGBUS。
x86 容忍未对齐原子操作，所以 TI 在 Ubuntu 16.04 x86_64 上测不出来。

| 文件 | 改动 |
|---|---|
| `Common/Osal_Utils/osal.h` | 信号量 typedef 外包 `pragma pack(push,8)`/`pop` + `aligned(8)` |
| `Common/rf_api_internal.h` | 文件末尾 `#endif` 前补 `#pragma pack()` |
| `RF_API/recorddatarecv.h` | `cUdpDataReceiver` 类前后 `pack(push,8)`/`pop`，类名前 `aligned(8)` |

**协议结构体一律不动**：`rf_api.h` 里走网线的结构必须逐字节紧凑
（0xA55A 头 / 命令码 / 0xEEAA 尾），加对齐会插填充使 FPGA 解析错乱**且不报错**。

判据：`alignof(cUdpDataReceiver)` 从 1 变 8；gdb 下五线程全起不崩；
`ss -ulnp | grep 4098` 有监听。

## 另一个不用改源码的坑

`start_record` **必须加 `-q`**。非 quiet 模式下 `CLI_Control` 用
`gnome-terminal -x` 起 `CLI_Record`（`cli_control_main.cpp:1660`），
Pi OS 没有 gnome-terminal，`system()` 失败但**退出码仍是 0** ——
表现为"命令全部成功但没有数据"。
