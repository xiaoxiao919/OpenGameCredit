# 腾讯游戏信用分批量查询工具

一个基于 Python 的 Windows 桌面工具，用于通过 QQ 扫码登录腾讯游戏信用分，批量查询多个账号的信用分，并查询已授权账号的 Q 币余额。

项目提供 Tkinter 图形界面，也提供命令行入口，适合个人账号管理和定期查询。

## 下载

不想配置 Python 环境的话，可以直接下载打包好的 Windows 可执行文件：

**[⬇ 下载 OpenGameCredit.exe](https://github.com/xiaoxiao919/OpenGameCredit/releases/latest/download/OpenGameCredit.exe)**

- 单文件绿色版，双击即用，无需安装 Python 或任何依赖
- 首次运行若出现 SmartScreen“未知发布者”提示，点击“更多信息”→“仍要运行”即可
- 所有历史版本见 [Releases](https://github.com/xiaoxiao919/OpenGameCredit/releases)

想自己打包或改代码，请继续看下面的安装步骤。

## 功能

- **一码两查**：扫一次码同时授权信用分、Q 币和黑白，不用再分别扫码
- 批量查询信用分，支持并发数设置、请求限速、重试和连续失败熔断
- 查询和刷新 Q 币余额
- 黑白号检测（依据活动接口应答判定，仅供参考，非官方评级）
- 表格显示序号、昵称、QQ、状态、黑白、上次 Q 币、当前 Q 币、Q 币变化、上次信用分、当前信用分和变化；首次登录时“上次”显示 `-`
- 底部统计条：账号数、Q 币总额、分数分布、黑/白数量
- 支持按昵称 / QQ 实时搜索过滤，点击表头排序（再点一次切换升降序）
- 卡片式浅色界面，窗口按内容定尺寸后锁定，不会误拉伸
- 账号数据导出为 CSV，兼容 Excel 中文显示
- 从 CSV 导入账号，按 QQ号 新增或更新，不覆盖已有登录凭据
- 凭据过期检测、查询失败提示和账号删除

## 运行环境

- Windows 10/11
- Python 3.10 或更高版本
- 可访问 QQ、腾讯游戏信用分和支付相关接口的网络环境

Tkinter 通常随 Windows 版 Python 一起安装。如果使用精简 Python 发行版，请确认已安装 Tk 支持。

## 安装

在项目目录执行：

```bash
python -m pip install requests cryptography pillow
```

其中：

- `requests` 用于网络请求
- `cryptography` 用于加密保存登录凭据
- `pillow` 用于显示扫码二维码

## 图形界面

```bash
python app.py
```

使用流程：

1. 点击右侧「获取二维码」，用手机 QQ 扫码并确认。
2. 扫码成功后账号自动加入列表，并立刻查询它的信用分、Q 币和黑白。
3. 用「查分 / 查币 / 查黑白」批量刷新全部账号。
4. 可用「导出 CSV」保存当前账号数据。

扫描用的是一次性 QQ 互联 H5 授权：同一份 `openid + access_token` 会被三处复用——
信用分走 Cookie（`acctype=qq;appid=101477677;openid=…;access_token=…`）、
Q 币走 `session_type=kp_accesstoken` 的 `openkey`、
黑白走活动接口的同名 Cookie。凭据有效期约 60 天。

## 命令行

项目当前是扁平脚本结构，请在项目目录直接运行 `cli.py`。

### 添加信用分账号

```bash
python cli.py login
```

程序会将二维码保存为当前目录的 `qr.png`。扫码后按回车开始轮询登录状态。

### 添加支付授权并查询 Q 币

```bash
python cli.py login pay
```

支付授权成功后，程序会兑换支付凭据并查询当前 Q 币余额。

### 列出账号

```bash
python cli.py list
```

### 刷新信用分

默认只刷新本地当天尚未查询的账号：

```bash
python cli.py refresh
```

刷新全部账号：

```bash
python cli.py refresh all
```

### 刷新 Q 币余额

```bash
python cli.py qb
```

### 诊断账号

```bash
python cli.py diag QQ号
```

例如：

```bash
python cli.py diag 123456789
```

### 恢复异常中断时保存的凭据

如果数据库写入失败，程序会在数据目录生成救援文件。修复数据库问题后执行：

```bash
python cli.py rescue
```

## 打包为 exe

在 Windows 上可打包成免安装的单文件 OpenGameCredit.exe：

`bash
python -m pip install pyinstaller
build_exe.bat
`

或手动执行：

`bash
python -m PyInstaller --noconfirm --clean --onefile --windowed --name OpenGameCredit --hidden-import cryptography --hidden-import PIL._tkinter_finder app.py
`

生成结果位于 dist\OpenGameCredit.exe，双击即可运行，无需安装 Python。

注意：exe 首次启动需要解压运行时，启动会比直接运行脚本慢几秒；它读写的数据目录与脚本版本完全相同（%USERPROFILE%\\.gamecredit_tool），两边的账号数据是共用的。

## 数据保存

数据默认保存到当前 Windows 用户目录：

```text
%USERPROFILE%\\.gamecredit_tool\\accounts.db
%USERPROFILE%\\.gamecredit_tool\\key
```

账号的 `gs_id`、`gs_code` 以及支付侧令牌会使用 Fernet 加密保存，密钥保存在同一目录的 `key` 文件中。请同时备份数据库和密钥；只备份数据库无法恢复加密凭据。

如果数据库写入失败，程序会生成：

```text
%USERPROFILE%\\.gamecredit_tool\\rescue_last_login.json
```

该文件包含登录凭据，恢复完成后应及时删除并妥善保护。

## 语法检查

检查所有脚本语法：

```bash
python -m py_compile app.py cli.py login.py login_oauth.py bw.py pay.py query.py store.py utils.py worker.py
```

## 项目结构

| 文件 | 作用 |
| --- | --- |
| `app.py` | Tkinter 图形界面（v2 卡片式布局） |
| `cli.py` | 命令行入口 |
| `login_oauth.py` | **单码登录**：一次扫码换取 openid / access_token / QQ 号 |
| `login.py` | 旧版 QQ 扫码登录（双目标），保留供 `cli.py` 使用 |
| `bw.py` | 黑白号检测（活动接口探针 + 三分类判定） |
| `query.py` | 游戏信用分查询接口（含 token 版） |
| `pay.py` | Q 币授权、余额查询、用户信息 |
| `store.py` | SQLite 数据库、凭据加密和数据迁移 |
| `worker.py` | 批量刷新调度、限速、重试和熔断 |
| `utils.py` | 编码、哈希、JWT 和登录协议辅助函数 |

## 更新日志

### v2.0.0

- **一码两查**：移除「信用分 / 支付」二选一扫码，改为一次 QQ 互联授权同时覆盖信用分、Q 币、黑白
- 新增 `login_oauth.py`：扫码 → `ptqrshow` / `ptqrlogin` → 回调取 `openid` / `access_token` → `unipay cmd=1` 换 QQ 号与昵称
- 新增「黑白」列与「查黑白」按钮，判定规则：`仓库券礼包领取参数不满足` → 白；`活动太火爆…` → 失效；其它（如 `账号存在异常`）→ 黑
- 界面重做：卡片式浅色仪表盘、顶栏状态胶囊、圆角按钮、分数分布条、悬停提示
- 表格新增「状态」「黑白」列，支持搜索过滤与表头升降序排序
- 窗口按内容定尺寸并锁定，不允许拉伸
- 数据库新增 `oa_*`（单码凭据）与 `bw` / `bw_msg` / `bw_at`（黑白结果）字段，旧库自动迁移
- 旧账号（`gs_code` / `pay_*`）仍可继续使用，三条查询会按可用凭据自动选择

## 注意事项

- 本工具依赖腾讯相关接口的现有行为，接口、登录页面或授权流程变化可能导致登录或查询失败。
- 请只使用自己的 QQ 账号，并妥善保护 `.gamecredit_tool` 目录及其中的密钥和救援文件。
- 不要将 `accounts.db`、`key`、`rescue_last_login.json` 或包含凭据的日志提交到公开仓库。
- 项目不包含腾讯官方 API 密钥，也不代表腾讯官方产品。
