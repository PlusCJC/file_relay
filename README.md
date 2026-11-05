# 私有文件中转站 / File Relay

面向 WSL 与 Windows 主机、以及其他 Linux 机器的自包含文件中转服务。使用 **Python 3 + Starlette + Uvicorn**，网页使用原生 HTML/CSS/JavaScript。发布包内含解释器、依赖和网页，目标机器无需安装 Python、Node.js，也无需联网安装依赖。

## 快速使用

本仓库开发入口：

```bash
./file-relay init
./file-relay start
./file-relay token
```

Windows 浏览器打开 **http://127.0.0.1:8765**，粘贴最后一条命令生成的临时令牌。拖入文件即自动上传，网页和 CLI 共用一个文件列表，所有文件都可以直接下载，无需选择传输方向。

如果服务运行在**独立 Linux 虚拟机**，请按 [虚拟机部署说明](docs/virtual-machine.md) 配置内网 HTTPS，然后在主机浏览器打开虚拟机地址。无需改变共享文件的操作方式。

```bash
# 上传文件或目录到共享中转站
./file-relay upload ./report.pdf ./my-folder

# 查看共享文件及 ID
./file-relay list

# 下载任意共享文件到指定目录
./file-relay download FILE_ID --to /path/to/received

# 下载全部共享文件，目录传输自动安全解压
./file-relay download --all --to /path/to/received --extract

./file-relay status
./file-relay doctor           # 不监听端口，检查运行库、网页资源和配置
./file-relay cleanup          # 预览已过期的中转副本
./file-relay cleanup --apply  # 实际删除已过期副本
./file-relay delete FILE_ID   # 删除指定中转副本
./file-relay stop
```

`download` 不指定 `--to` 时保存至默认下载目录。下载不会自动删除服务器副本；重复批量下载会产生新副本。同名文件追加 ID 和序号，拒绝覆盖现有文件。目录上传为 ZIP，网页下载会得到 ZIP；CLI 的 `--extract` 在目标目录下创建 `目录名-ID前8位/`，保留相对路径，不恢复可执行权限。CLI 支持空目录，网页文件夹选择器无法保留空目录。

## 自包含发布包与安装

构建命令（只操作当前项目）：

```bash
.venv/bin/python -m pytest
.venv/bin/python scripts/build.py
.venv/bin/python scripts/smoke.py --offline
```

输出：`dist/file-relay` 单文件程序、`dist/file-relay-0.3.1-linux-x86_64.tar.gz` 发布包、SHA-256 校验文件和构建信息。将发布包复制到目标 Linux 后：

```bash
tar -xzf file-relay-0.3.1-linux-x86_64.tar.gz
cd file-relay-0.3.1-linux-x86_64
sha256sum -c SHA256SUMS

# 以下两种方式任选其一
# 方式一：直接运行，不必安装
# ./file-relay start

# 方式二：安装到全新目录，默认注册 ~/.local/bin/file-relay
./install.sh --prefix "$HOME/Applications/file-relay"

# 若安装程序提示 ~/.local/bin 不在 PATH，本终端执行：
export PATH="$HOME/.local/bin:$PATH"
file-relay start
file-relay token
```

安装默认创建指向已安装程序的用户级命令链接；不需要 sudo，不修改 shell 启动文件，不注册系统服务。安装程序会检查当前 PATH，并在需要时打印可直接执行的 `export` 命令。若需永久生效，可自行将该命令加入当前 shell 的启动文件。WSL 中注册的是 Linux 命令，可在 WSL 终端运行；Windows PowerShell 可通过 `wsl file-relay …` 调用，前提是对应 WSL 环境的 PATH 已配置。

可以指定已在 PATH 内的命令目录，或仅复制程序：

```bash
./install.sh --prefix "$HOME/Applications/file-relay" --bin-dir "$HOME/bin"
# 或跳过命令注册：
./install.sh --prefix "$HOME/Applications/file-relay" --no-command
```

安装路径必须尚不存在，命令目录必须位于安装目录之外；已有同名文件或链接（含失效链接）一律拒绝覆盖。安装后不要移动安装目录，以免命令链接及安装记录失效。命令链接与程序绝对路径共用安装目录内的 `data/`。直接解压运行不注册命令；源码入口不支持安装/卸载子命令。

### 一键卸载

新安装会在安装目录提供 `uninstall.sh`，也可直接调用已注册命令：

```bash
# 两种入口任选其一；默认保留 data/，包括配置、中转内容和已下载文件
file-relay uninstall
# 或：
"$HOME/Applications/file-relay/uninstall.sh"

# 若明确不再需要安装目录内的数据，选择以下命令替代上面的卸载命令
"$HOME/Applications/file-relay/uninstall.sh" --purge
```

卸载会先通过数据目录的进程锁检查默认服务，必要时发送停服请求并等待退出；停服失败则取消卸载。随后移除已安装程序、随附文档/许可证、安装与卸载脚本及本次注册的命令链接。如果命令已被其他程序替换，则保留它。默认保留 `data/`，`--purge` 会删除整个安装目录内的 `data/`（包括其中的 CLI 下载副本），请先备份。安装根目录中额外添加的文件会保留。

自行指定的 `--home`、安装目录外的下载文件、原始上传文件、PATH 配置和自行配置的 systemd/Windows 启动项均不由卸载程序删除。使用自定义 `--home` 启动的实例须先用相同 `--home` 手动停止，受外部服务管理的实例须先停止并禁用其自动重启。解压直接运行及旧版复制安装没有安装记录，不能使用新版卸载器。默认保留数据卸载后，程序和卸载器已删除；若之后要删除保留数据，请自行确认后删除该数据目录。

**兼容范围**：本机构建基线是 Linux x86_64、glibc 2.39（Ubuntu 24.04）。同架构且兼容的较新 glibc 系统通常可用，需要在目标系统验证；不声称已验证所有发行版。Alpine/musl、ARM64、旧 glibc 需要各自在对应平台重新构建。PyInstaller 不打包 glibc，[官方兼容说明](https://pyinstaller.org/en/stable/usage.html#making-gnu-linux-apps-forward-compatible)。单文件程序运行时会将内嵌库临时解包到系统临时目录；若临时目录禁止执行，可在应用自己的可执行临时目录下设置 `TMPDIR`。这不会写入系统配置。

## 默认目录与配置

默认使用**程序所在目录的 `data/`**。源码入口则使用仓库下的 `data/`。建议长期使用时显式指定稳定路径，升级或移动程序时继续使用相同 `--home`：

```bash
./file-relay --home /path/to/private-relay-data init
./file-relay --home /path/to/private-relay-data start
./file-relay --home /path/to/private-relay-data token
./file-relay --home /path/to/private-relay-data download --all
```

`--home` 也可以放在子命令之后。该目录必须由当前用户拥有，权限为 `0700`，不允许作为符号链接。不要指定已有的共享目录。配置和令牌权限要求为 `0600`。

| 路径 | 用途 |
| --- | --- |
| `data/config.json` | 应用配置，不含令牌 |
| `data/token` | 内部随机密钥，供 CLI 自动认证和签发临时凭证，不用作网页登录令牌 |
| `data/blobs/` | 中转内容，按随机 ID 存储 |
| `data/relay.sqlite3` | 名称、大小、SHA-256、类型和创建时间 |
| `data/partial/` | 上传临时文件，中断后删除，异常退出后启动时恢复清理 |
| `data/client-tmp/` | CLI 目录打包临时空间 |
| `data/downloads/` | 默认 CLI 下载目录 |
| `data/logs/` | 轮转日志，服务日志最多约 8 MiB |
| `data/tls/` | 虚拟机模式自动生成的证书和私钥，不加入 Git |

停止服务后编辑 `config.json`，再启动生效。默认端口 `8765`，保留 `168` 小时，每 `3600` 秒清理一次，单文件上限 `2 GiB`，中转总配额 `10 GiB`，CLI 解压上限 `8 GiB`。`download_dir` 可设为绝对路径或相对于 `--home` 的路径。配额不足时拒绝新上传，不会删除尚未过期的文件。目录压缩还需要额外临时空间；网页目录归档采用无压缩 ZIP，CLI 使用压缩 ZIP。

**过期时间按上传完成时计算**。下载不会延长保留时间。自动清理只删除 `blobs/` 中有记录的过期中转副本，跳过正在下载的文件，绝不扫描或清理源目录、CLI 下载目录或主机浏览器下载目录。服务启动时也会检查过期文件。

## 登录有效期

`file-relay token` 每次生成一个 **10 分钟有效**的网页登录令牌，不输出内部密钥。过期后重新运行该命令即可。登录成功后使用 HttpOnly、SameSite=Strict 的 Cookie 记住会话 **2 小时**；在同一浏览器、同一网址刷新、关闭再打开页面，无需重复输入。临时令牌过期不会使已有会话立即失效，生成新令牌也不会使其他正常会话退出。会话到期或主动退出后需要重新登录。

`config.json` 的 `login_token_minutes`、`session_minutes` 可分别调整这两个时长，修改后重启生效。CLI 使用本地私有密钥自动认证，不需要手工输入网页登录令牌。不要复制 `data/token` 的内容到网页。

从旧版升级时，继续使用原来的 `--home`；首次启动会在数据库事务中将原有两个方向的文件合并到共享列表，保留文件 ID 和内容。旧版无时效令牌和旧 Cookie 不再用于网页登录，升级后首次登录需运行新版 `token` 命令。

## 网络与安全边界

默认监听 **`127.0.0.1`**，用于 WSL 和本机访问。Windows 访问 WSL 服务通常借助已有的 localhost 转发；这与 WSL 访问 Windows 服务所用的网关是两个方向。[微软 WSL 网络说明](https://learn.microsoft.com/en-us/windows/wsl/networking)。如果主机关闭了本地转发，本软件不会擅自修改 `.wslconfig`、防火墙或网络设置。

虚拟机模式必须显式运行 `configure-vm`，仅允许具体的 RFC1918 内网 IPv4 地址、具体主机 IP 白名单和 HTTPS。不接受 `0.0.0.0`、公网 IP、网段白名单或明文内网监听。CLI 在虚拟机内使用配置的服务地址，验证证书和 IP，自动连接同一个存储。远端浏览器不能使用内部 CLI 密钥或停止服务。主机隔离建议由现有 Host-only 网络提供；应用层白名单不能让可达端口对 TCP 扫描不可见。

还包括带签名的短期登录令牌、独立过期的浏览器会话、登录失败限速、Host/Origin/跨站校验、写操作验证头、CSP、禁止嵌入、附件下载，以及目录解压的路径穿越/符号链接/重复路径/解压大小检查。传输不采用内存整包读取。代理环境变量不会影响 CLI 本地连接，服务不信任代理头，不加载外部网页资源。

本机模式使用回环 HTTP，虚拟机模式使用 HTTPS。发布包内嵌 OpenSSL，生成证书无需在目标机额外安装工具；源码构建环境需要有 `openssl` 命令。默认自动生成的自签名证书有效期 365 天，首次访问须在主机浏览器核对虚拟机输出的 SHA-256 指纹；也可使用已有证书。软件不导入系统信任库，不自动关闭证书验证。

它无法隔离拥有同一用户权限或管理员权限的程序，也无法防止用户自行创建公网反向代理。浏览器下载位置由浏览器决定，虚拟机/WSL 目标路径由 CLI 的 `--to` 决定。

## 长期后台运行与自启动

`start/stop/status` 是软件自身的后台进程管理，不注册系统服务。`start` 启动的进程可脱离终端，但不保证机器重启、WSL 关闭或系统用户会话被清理后仍然运行。需要进程监督时使用 `serve` 前台模式。

`docs/file-relay.service.example` 提供可选的 systemd **用户服务模板**，支持失败后重启；`docs/windows-start.cmd.example` 提供保持 WSL 前台调用的示例。它们仅是文件，本次未安装、启用或执行。用户服务一般随用户会话启动；登录前运行需要额外的系统设置。Windows 重启后自动唤起 WSL，还需要用户自行配置 Windows 启动项或计划任务。**完全不注册任何启动机制，无法实现真正的开机自启。** 本项目不修改 `/etc`、systemd 配置、Windows 注册表、计划任务或 shell 启动文件。[微软说明：systemd 服务本身不会保持 WSL 实例存活](https://learn.microsoft.com/en-us/windows/wsl/systemd)。

## 开发结构

`src/file_relay/` 包含 CLI、配置、存储、HTTP 服务和 `web/` 静态资源；`tests/` 验证安全边界及真实传输；`scripts/` 包含构建和目录安装脚本；`docs/` 保存架构与可选服务模板。开发环境需要 Python 3.12 或更高版本；依赖安装和 Git 提交由使用者执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest
# 本项目未配置独立 lint 工具；可检查补丁空白错误
git diff --check
git status --short
```

虚拟环境、运行数据、令牌、证书私钥、日志、数据库和测试缓存均被 `.gitignore` 排除。当前版本的程序、发布包、构建信息和校验文件作为明确例外允许提交。`dist/` 和 `.build/` 中早于 v0.3.0 的发布产物已移除；Git 历史中的旧内容是否清除须单独决定。发布包不包含 Git 元数据或运行数据。备份时停止服务并整体复制 `--home` 目录；迁移后检查其权限。

测试分为不使用 socket 的进程内测试和发布包冒烟测试。执行沙箱可能禁止监听端口，进程内测试会内联线程调度来验证 ASGI 行为，不能替代真实网络测试。在正常 WSL/Linux 终端运行 `.venv/bin/python scripts/smoke.py` 可验证移动后的单文件、后台启动、真实 HTTP、浏览器会话、文件/目录 CLI 传输、SHA-256 与正常停止；该脚本只在 `.build/` 中建立临时测试数据，不注册服务。主机浏览器的 localhost 转发仍需从 Windows 实际访问验证。

`.venv/bin/python scripts/smoke.py --tls` 增加真实 HTTPS 传输测试；`--offline` 不使用网络，也会验证发布包在没有系统 Python/OpenSSL 的 PATH 下能自行生成证书、配置 HTTPS、检查指纹和恢复回环模式，并验证发布包的命令注册、保留数据卸载与 `--purge` 卸载。测试还通过 SSL MemoryBIO 验证真实 TLS 握手、加密数据和错误证书拒绝，不需要监听 socket。
