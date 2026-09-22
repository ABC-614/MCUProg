# MCUProg
MCU programmer for J-Link, ST-Link, CMSIS-DAP and OpenOCD, using Keil MDK's \*.FLM Flashing Algorithm

To run this software, you need python 3.6+ and pyqt5.

To use DAPLink, you need additional pyusb for CMSIS-DAPv2 and another usb-backend for CMSIS-DAPv1 (hidapi or pywinusb for windows, hidapi for mac, pyusb for linux).

``` shell
pip install PyQt5 pyusb hidapi six pyelftools
```

## debug probes

The **调试器** box lists four kinds of probe:

| probe | how it is selected | targets |
| --- | --- | --- |
| J-Link | point the first entry at `JLink_x64.dll` with the `...` button | ARM + RISC-V |
| OpenOCD | connects to a running OpenOCD's Tcl RPC on port 6666 | ARM + RISC-V |
| CMSIS-DAP / DAPLink | auto-detected via pyocd, one entry per probe | ARM |
| ST-Link V2 / V2-1 / V3 | auto-detected via pyusb, one entry per probe | ARM |

ST-Link and CMSIS-DAP probes are rescanned once a second, so plugging one in makes it appear
without restarting. If enumeration fails the reason is printed to the log panel instead of the
probe silently not showing up. ST-Link and CMSIS-DAP are ARM only — use J-Link or OpenOCD for
RISC-V targets.

`stlink.py` talks to the probe directly over USB, so no ST software needs to be installed, but the
probe's debug interface has to be reachable by libusb. ST-Link V2-1 and V3 normally work with ST's
own driver; if an older ST-Link V2 cannot be opened, switch its interface to WinUSB with
[Zadig](https://zadig.akeo.ie/).

## debug panel

The **调试** button opens a panel that keeps the connection open, so the target can be inspected
without programming anything:

- **连接 / 断开** — attaches to the target without downloading the flash algorithm, i.e. without
  resetting or halting whatever is running. Programming still works while connected and the
  connection is kept afterwards.
- **复位 / 暂停 / 运行 / 单步** — run control. Hold Shift while clicking 复位 to stop on the reset
  vector instead of letting the target run.
- **core registers** — refreshed on every run-control action; shown as `—` while the core is running.
- **memory** — read any address range as a hex dump, or write hex bytes to it.

## while programming

- **停止** appears next to the progress bar during any erase / program / read and stops the
  operation at the next sector or page boundary, instead of leaving the only way out as killing
  the process.
- Pages that are already blank (the erase value from the FLM, normally `0xFF`) are **not
  programmed** — the sector was just erased, so writing them again is wasted time. They are still
  verified, so a sector that failed to erase is still caught. The log reports how many pages were
  skipped.
- Waiting for the flash algorithm to finish uses an **adaptive backoff** (poll immediately, then
  0.2 ms doubling up to 10 ms) instead of a fixed 10 ms sleep, which used to dominate the
  programming time on chips with small pages. An algorithm call that never returns now times out
  (30 s, 120 s for a chip erase) instead of hanging forever.
- Before the flash algorithm is downloaded, the target's RAM is **read/write tested** at the
  algorithm's load address and the downloaded algorithm is read back and compared. A wrong RAM
  address in `devices.txt` (the default `0x20000000` / 4 KB does not fit every chip) used to make
  the algorithm silently run off into the weeds and nothing would erase or program; now it stops
  with a message naming the address.
- On connect, the core type and whatever the chip says about itself are printed to the log, so a
  wrong 型号 is easy to spot. The FLM's own device name is shown next to the chip geometry at the
  top of the window.

![](./%E6%88%AA%E5%9B%BE.jpg)

## pick the debug probe first, then identify the chip

The 调试接口 box now sits **above** 目标芯片: you pick the probe, mode and speed first, and identify
the chip from there. Identification no longer depends on whichever 型号 happens to be selected —
that entry is usually for some other chip, and if it were a RISC-V one the probe would not even
open. A generic core name matching the selected mode is passed to the probe instead.

The **识别** button connects to the target (without downloading the flash algorithm, so nothing on
the board is reset or halted) and reads the chip's own identification registers: the Cortex-M
`CPUID`, the vendor's ID register, the flash size register and the UID.

Identification is rule based and **lists every reading that makes sense** instead of guessing:

- **ST, and the parts that copy its register map** (GD32, AT32): `DBGMCU_IDCODE`, low 12 bits are
  the device ID. Several vendors share the same IDs while their algorithms are *not*
  interchangeable, so when the revision field is not one of ST's own, an ST candidate *and* a
  compatible-vendor candidate are both offered and you pick. `REV 0x1303`, for instance, is
  GigaDevice.
- **Nations N32**: `DBG_ID` is at the same address as ST's `DBGMCU_IDCODE` (`0xE0042000`) but the
  fields are scattered — device number across bits [15:12]/[11:8]/[23:20], flash size in [19:16],
  SRAM size in [31:28]. Read as an STM32 ID, the low 12 bits are really "device number middle
  nibble + revision" and can collide with a genuine STM32 device ID, so the N32 rule is tried
  **first**.
- Anything else falls through to 搜索型号 below.

Only a single unambiguous result is applied without asking. Everything else opens a dialog showing
the raw readout and each candidate with the reason for it. Picking the wrong algorithm erases the
chip with the wrong sector layout, so this is not a decision the program makes on a hunch.

If `devices.txt` already has a usable entry it is selected automatically. Matching needs the flash
base, the size **and** the part number to agree — STM32F103RC and STM32F407VE are both 512 KB at
`0x08000000` but their algorithms are not interchangeable.

## 搜索型号 — find an algorithm by part number

Chips whose ID register is not at a known location — HC32 among them — cannot be identified that
way at all. The **搜索型号** button covers them: it downloads Keil's pack index (~1800 packs, cached
for a week), searches it by part number or vendor, lists the devices in whichever pack you pick,
and pulls just that one `.FLM` out of it.

The domestic vendors are all in that index: `NSING.N32G45x_DFP` (Nations is listed as NSING),
`GigaDevice.GD32F10x_DFP`, `HDSC.HC32F460` and so on.

The `.FLM` is fetched with HTTP range requests — a few kilobytes instead of the whole pack, which
can be 50 MB. keil.com only mirrors the `.pdsc`; the pack itself is served by the vendor
(`nsing.com.sg`, `gd32mcu.com`, GitHub for HDSC), so the vendor's server is tried first and
keil.com is the fallback.

The flash base comes from the device's own memory map instead of being assumed to be `0x08000000`
— HC32 puts its flash at `0x00000000`. The RAM block for the algorithm is the largest one the pack
declares, and where the chip reports its own SRAM size (N32 does) that figure wins, because some
packs under-declare it: NSING's `.pdsc` gives N32G451CC 6 KB while the 128 KB part in the same
family gets 48 KB.

If a vendor's certificate does not verify — GigaDevice's pack server has been serving an expired
one — the download stops and asks. It is not waved through silently: the `.FLM` is a binary that
will be executed on your target.

Downloaded `.pdsc` files and the pack index are cached in `FlashAlgo/.packcache/`. Nothing is sent
to the server beyond the plain file requests, and a download only happens when you confirm it.

## add new chip
### Simple method
add chip's name and FLM file path in `devices.txt` as below:
```
STM32F103C8 FlashAlgo/STM32F10x_128.FLM
```
and then, MCUProg can erase/write STM32F103C8.

In the previous configuration, we assume that chip's RAM locates at 0x20000000, and FLM uses 4KB RAM.

If the default values do not apply to your chip, you can explicitly specify the address and size of RAM used by FLM as below:
```
NUM480      0x20000000  0x2000  FlashAlgo/M481_AP_512.FLM
```

### Powerful method
1. put new_chip.FLM to FlashAlgo folder
2. run FlashAlgo/flash_algo.py to generate new_chip_algo.py
3. add below code in device/new_chip.py file:
``` python
class new_chip(chip.Chip):
    def __init__(self, xlink):
        super(new_chip, self).__init__(xlink, 'new_chip_algo')
```
4. add below code in device/\_\_init__.py file:
``` python
('new_chip',       new_chip.new_chip),
```

In class new_chip, you can add arbitrary python code to do something FLM don't support, so i call it 'Powerful method'.

FlashAlgo/flash_algo.py is used to parse Keil MDK's \*.FLM file and extract code and its runing information into a python dict.

## bootloader + app programming
Click the **Boot+App** button to create a programming list (a `.ini` file). It starts with a `BOOT`
and an `APP` entry; double-click **地址** to change an address, double-click **文件** to pick the
bin/hex, and use **添加** / **删除** for more partitions. Every change is saved back to the `.ini`
right away, so the list can be reopened later, shared, or dropped onto the window.

Pressing **擦写** programs every ticked entry in one go, and before connecting to the target it
checks that each address is inside the flash, is sector aligned, and that no two entries share a
sector (otherwise programming the second one would erase the first).

The `.ini` can also be written by hand. Addresses are **absolute**, i.e. they include the flash base
address of the chip:
``` ini
[BOOT]
addr = 0x08000000
path = D:/work_dir/STM32-Boot-Demo/STM32_UserBoot/out/STM32_stdperiph_lib.hex

[APP]
addr = 0x08010000
path = D:/work_dir/STM32-Boot-Demo/STM32_App/out/STM32_stdperiph_lib.bin
```
