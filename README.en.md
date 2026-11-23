# Plants vs. Zombies: Hybrid Remake — Damage Feedback

[简体中文](./README.md) · **English**

See when a plant loses health, its armor takes a hit, or its shield absorbs damage. You can watch these events on the Windows dashboard without a device, or connect a DG-LAB Coyote 3.0 for waveform feedback on channel A, B, or both. Zombie damage is tracked separately and is off by default.

Desktop version **1.0.0.6** runs without DGHub. **Scanning a QR code pairs the app but does not start feedback.** Damage-based adjustments change the waveform amplitude, not the channel intensity you set on your phone.

## Download and launch

1. On this repository's GitHub page, select **Code → Download ZIP**. Extract the **entire** archive to a folder.
2. Double-click **`启动.exe`** in the top-level folder. Keep the `app` and `launcher` folders alongside it; the executable cannot run on its own.
3. The first launch downloads and verifies about **170 MiB** of runtime components, then opens a desktop window. Later launches use the local cache. You do not need to install Python, Node.js, or DGHub yourself.

| Component | Supported configuration |
| --- | --- |
| Computer | 64-bit Windows (x64) |
| Game | Plants vs. Zombies: Hybrid Remake (植物大战僵尸杂交重制版), **0.32.1, C# edition** |
| Device | **DG-LAB Coyote 3.0**, connected to the phone over Bluetooth |
| Phone | **DG-LAB app 3.x or 4.x**, paired with the PC over a local WebSocket connection. Choose the **phone app version**, not the hardware version. |

## Your first session

The desktop interface is in Chinese. The labels below match what you will see on screen.

1. **Set up the game mod.** Close the game. Under **反馈设置 → 游戏 Mod** (Feedback Settings → Game Mod), leave **自动安装游戏端 Mod** (automatically install the game mod) on. The desktop app attempts to install the damage watcher when you enable the plugin. Start the game after installation. If automatic installation fails, import [PVZHybrid-Watcher.pmod](./app/resources/backend/app/core/PVZHybrid-Watcher.pmod) manually, enable it in the game, and restart the game.
2. **Pair the phone** (skip this if you only want to see events). Connect the Coyote in the phone app and put the phone and PC on the same local network. On the desktop, open **设备连接** (Device Connection), select your phone app version, leave the address on **自动选择** (Auto), and click **生成配对码** (Generate Pairing Code). Scan the code with the **Socket** scanner inside the DG-LAB app. With app 4.x, select the connected Coyote if prompted.
3. **Choose what to play.** In **反馈设置** (Feedback Settings), choose a mode, output channel, and waveform. Click **启用插件** (Enable Plugin). If the device is ready, confirming allows game events to trigger feedback. If you enabled the plugin before pairing, wait for the selected channel to be ready, then click **允许本次设备反馈** (Allow Device Feedback for This Session). Click **停止反馈** (Stop Feedback) at any time.
4. **Check a game event.** Enter a level and let a plant take damage. Look under **运行面板 → 最近提示** (Dashboard → Recent Notifications). To include zombies, turn on **僵尸受伤** (Zombie Damage) under **触发事件** (Trigger Events). The **连接状态** (Connection Status) card in the sidebar shows the pairing address, connection diagnostics, and the current level and limit for each channel.

## Feedback settings

- **Timing and channels:** Classic mode leaves at least five seconds between feedback events. Continuous rhythm mode is for repeated hits on the same target. Choose A, B, or A+B. The waveform preview only draws the curve; it does not play it on the device.
- **Damage-based intensity:** Set separate adjustments for plant and zombie damage. A temporary boost falls back after the configured duration. In cumulative mode, boosts add up to the cap you set.

## Troubleshooting

**The phone says it can't connect to the server.** Keep the desktop window open, generate a new QR code, and scan it within two minutes. If it still fails, open the `/check` address shown below the QR code in the **phone's browser**. If you see **已连到电脑的配对端口**, the phone can reach the PC's pairing port; read **连接诊断** (Connection Diagnostics) on the desktop for the next clue. If the page does not open, check that both devices are on the same local network. On a PC with multiple network adapters, manually select the IPv4 address of the Wi-Fi or Ethernet adapter you are using.

**The phone is paired, but there is no feedback.** First look for a new event in **最近提示** (Recent Notifications). If none appears, check that the watcher is enabled in the game, that you restarted the game after installing it, and that the relevant event type is on. If events appear, check the selected channel in the sidebar. For example, `0 / 17` means the current level is **0** and the limit is **17**; it does not mean the device is outputting at 17. You control that level in the phone app. The desktop app will not raise it for you.

## Credits and licensing

Created by **TianXianbaobao**. This project is free of charge. It includes neither the game nor the phone app and is not an official DG-LAB client. See the [project terms](./app/LICENSES/PROJECT.txt) for sharing, modification, and distribution, and the [third-party notices](./app/LICENSES/THIRD-PARTY.txt) and [waveform license](./app/LICENSES/DGLAB-GPL-3.0.txt) for the bundled presets and runtime components.
