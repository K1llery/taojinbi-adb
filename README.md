# 淘金币 ADB 浏览助手

Windows 本地工具，Python 3.14 + Tkinter，无第三方 Python 依赖。手机通过 USB 连接电脑，手动打开淘金币的“做任务赚能量”列表后启动。

## 使用

1. 双击 `start.cmd`。如果已经开着开发过程中的测试窗口，先关闭旧窗口再打开。
2. 点击“检查连接”，确认设备，然后点击“扫描任务”。
3. 勾选可执行任务，设置本次上限，点击“开始执行”。上限 `0` 表示持续处理可执行的选定入口，直到耗尽或遇到异常。
4. 可以暂停、继续或停止。停止后不再发送新操作；手机已经接收的手势可能完成。
5. “打开本地记录”可查看这次会话的日志、XML 和截图。

保持手机亮屏、淘宝处于前台，并避免同时手动操作。无需再给每个任务演示流程。

本机已放置 ADB 和所需 DLL 到 `tools/platform-tools/`。换电脑时，从 [Android 官方 Platform Tools](https://developer.android.com/tools/releases/platform-tools) 取得工具，并在窗口选择 `adb.exe`。小米设备需启用“USB 调试（安全设置）”，首次开启后重启手机。

## 当前支持范围

- **已启用：当前名称为“好物沉浸看”的纯浏览入口。** 工具进一步检查目标页面是否包含已经实测的任务组件，才开始浏览；该页自动轮播商品图片，不需要额外点击商品或滚动。
- 其他任务显示“待适配 / 跳过”，包括“好物随心逛”、`看看#…`、搜索、攻略、跨应用、答题、小游戏、邀请、兑换和交易。累计任务奖励的“立即领取”目前也由用户手动处理。
- 活动会轮换任务名称、位置和总次数。工具每次重新定位；核验成功时比较同组次数分布和全局进度，要求变化来源与结果都唯一。后续继续筛选当前选定名称，不自动追踪到其他名称的页面。
- 验证、风险或网络提示、意外前台变化、未知页面、断连、超时、进度不明确都会停止输入。不会自动解决验证码或自动重做未确认成功的任务。
- 两个同组任务的起始次数相同时，当前无法可靠归因，会停止并保留记录，不记作成功。

等待依据页面加载和任务要求；任务间采用5—10秒阅读间隔，避免连续快速点击。此工具无法保证不被平台识别。无障碍权限检测处理未实现。

## 命令行和离线测试

```powershell
python -m taojinbi --scan
python -m taojinbi --task 好物沉浸看 --limit 1
python -m taojinbi --task 好物沉浸看 --limit 0
python -m unittest discover -v
```

多台设备连接时使用 `--serial` 指定设备。命令行默认上限1次；窗口默认0。相同设备在本工具中只允许一个扫描或运行会话。

## 页面读取器与本地数据

`tools/ui-dump.jar` 是由本项目 Java 源码构建的约3KB页面读取器，经 ADB 临时放入 `/data/local/tmp/` 执行。它复用 Android 自带 UiAutomationShellWrapper 连接生命周期，直接读取控件树，避开系统 `uiautomator dump` 在动画页等待静止失败的问题。无需安装 APK、常驻服务或 Android SDK；未修改系统无障碍开关。仅在当前 Xiaomi / Android 16 上验证过，其他系统可能不兼容。

重新构建需要 JDK 和网络访问 Google Maven：

```powershell
powershell -ExecutionPolicy Bypass -File tools/build-reader.ps1
```

构建脚本使用固定版本 R8/D8 `9.5.26`，首次下载到被 Git 忽略的工具目录。日常运行无需下载。

`artifacts/run-*` 包含本机页面截图、控件树、带本地时区的 `events.jsonl` 和完成结果 `results.json`，可能包含购物信息。它们不上传、不提交 Git；ADB 二进制、本机配置和构建缓存同样被 Git 忽略。

## 开发与验收

源码位于功能分支 `feat/taojinbi-adb`。`main` 保持初始基线，用户验收并明确授权后才合并。

真机验证和限制见 [VALIDATION.md](VALIDATION.md)。
