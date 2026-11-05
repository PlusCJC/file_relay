# 在 Linux 虚拟机运行，通过主机浏览器访问

服务和文件保存在虚拟机里；主机只需要浏览器。网页仍是统一的上传区和共享文件列表，无需选择传输方向。

## 网络前提

主机必须已能访问虚拟机的某个内网 IPv4 地址。建议使用 **Host-only / 仅主机网络**，让流量留在主机及虚拟机之间。它也可能连接同网段的其他虚拟机，因此软件额外检查主机 IP 白名单。[VirtualBox 官方网络说明](https://docs.oracle.com/en/virtualization/virtualbox/7.2/user/networkingdetails.html)、[VMware 网络模式说明](https://knowledge.broadcom.com/external/article?legacyId=1006480)。

本软件不建立虚拟网卡、不增加路由、不打开防火墙、不注册端口转发。已有虚拟网络不可达时，需要你在虚拟机软件中处理网络连通性。

在虚拟机里用 `ip -br -4 addr` 查看地址；Windows 主机用 `ipconfig` 查看对应虚拟网卡地址。下面用 **虚拟机 `192.168.56.10`、主机 `192.168.56.1`** 演示，必须替换成实际地址；不要把虚拟机的默认网关自动当成主机 IP。

## 启动

在虚拟机中解压发布包，进入程序目录。如果该数据目录已有服务，先运行 `./file-relay stop`，等待退出，然后：

```bash
./file-relay configure-vm \
  --listen 192.168.56.10 \
  --allow-client 192.168.56.1

./file-relay start
./file-relay token
```

配置命令会在自己的 `data/tls/` 下生成自签名证书和 3072 位 RSA 私钥，记录到应用 `config.json`，并打印证书 SHA-256 指纹。发布包已内嵌 OpenSSL，无需在目标机安装 Python 或 OpenSSL。配置不会自动启动服务，服务运行期间拒绝修改网络配置。

主机浏览器打开 **`https://192.168.56.10:8765`**。浏览器会提示自签名证书未受信任；先在证书查看器中核对 SHA-256 指纹与虚拟机终端输出一致，再为该站点建立浏览器例外。软件不自动导入证书或更改系统信任库。也可以提供已受信任且包含虚拟机 IP 的证书：

```bash
./file-relay configure-vm \
  --listen 192.168.56.10 --allow-client 192.168.56.1 \
  --cert /path/to/server-chain.pem --key /path/to/server.key
```

私钥须由当前用户拥有，权限为 `0600`；证书必须包含监听 IP，尚未过期，且与私钥匹配。私有 CA 证书需要包含可供 CLI 信任的完整证书链。

输入终端生成的临时令牌即可登录。令牌 10 分钟有效，登录会话记住 2 小时；HTTPS 会话使用 Secure、HttpOnly、SameSite Cookie。

## 文件传输

在主机网页拖入文件，会直接上传到虚拟机。主机点击列表中的“下载”即可取回任意共享文件。虚拟机中也可使用同一列表：

```bash
./file-relay upload /path/to/file-or-directory
./file-relay list
./file-relay download FILE_ID --to /path/to/received
./file-relay download --all --to /path/to/received --extract
```

CLI 自动连接配置的 HTTPS 地址并验证证书，不需要再次输入令牌。默认下载目录、配额和定时清理保持不变。

## 检查与恢复

```bash
./file-relay doctor
./file-relay status
./file-relay stop

# 可选：停止后恢复回环 HTTP，保留文件、内部密钥及原证书
./file-relay configure-local
./file-relay start
```

若原先使用 `--home /path/to/data`，所有命令都需带相同的 `--home`。网络模式只保存到该应用目录；systemd 模板可以继续运行 `serve`，它会读取保存的配置。

连接超时或拒绝连接通常是 IP、虚拟网络、服务状态或防火墙问题。HTTP 403 则需要检查实际来源 IP 和白名单；软件不信任 `X-Forwarded-For`。使用 `http://` 访问 HTTPS 端口无法工作。虚拟机 IP 变化后应停止服务，重新配置监听地址、证书和主机白名单。

**边界**：只允许绑定具体内网 IP，不接受 `0.0.0.0`、公网 IP 或整段网络白名单。白名单是在建立连接后校验的，因此不能隐藏已可达的 TCP 端口；外部网络隔离取决于虚拟机网络。浏览器和虚拟机控制台的首访指纹核对用于确认自签名证书身份。证书默认有效 365 天，应在到期前停止服务并重新生成或更换，之后重新核对指纹。

如果已经有可用 SSH，也可以保持默认回环服务，在主机执行 `ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 user@VM_IP`，再打开 `http://127.0.0.1:8765`；该方案不增加应用监听范围。
