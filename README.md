# omd

Windows 上的 `omp` 图形前端。单窗口多会话标签，每个标签一个独立的 `omp --mode rpc` 后端进程，可各选各的模型。

## 功能

- **多会话标签**：`Ctrl+N` 新建、`Ctrl+W` 关闭、`Ctrl+Tab` 切换；双击标签可重命名（名字写进 omp 会话）。
- **模型跟随标签**：每个标签独立选择模型和思考强度，切换标签不会串。
- **会话恢复**：启动时用 `--continue` 接上上次对话，并重建历史消息。
- **实时状态**：输入框上方显示 `⠹ 思考中 · 8s` / `执行 bash` / `生成回答`，思考内容默认可见。
- **凭据桥接**：只读扫描本机已装的 Codex CLI、Claude Code、OpenCode、`~/.env` 等，勾选同意后把 API Key / OAuth token 以环境变量注入 omp。
- **API 密钥设置**：14 家主流厂商，写入 `%APPDATA%\omd\keys.json`（仅当前用户可读），不改 omp 配置。
- **安装包自带 omp CLI**：安装时若本机没有 omp，会自动安装并加入用户 PATH；卸载只清理自己装的那份。

## 协议

只通过 `omp --mode rpc` 的换行 JSON 通信（stdin 发命令，stdout 收帧），不复制任何 agent 逻辑。

## 运行要求

- Windows 10/11 x64
- Python 3.11+（仅源码运行；安装包无需 Python）
- `omp` 命令行（安装包已内置）

源码运行：

```powershell
python omd.pyw
```

## 构建

```powershell
.\build.cmd
```

产物：`dist\omd.exe`（绿色版）、`dist\omd-setup.exe`（安装包，默认嵌入 `%LOCALAPPDATA%\omp\omp.exe`）。

## 签名

```powershell
.\sign.ps1 -Files dist\omd.exe,dist\omd-setup.exe -Pfx certs\signing.pfx -Password $env:OMD_PFX_PASSWORD
```

`build.cmd` 检测到 `certs\signing.pfx` 时会自动按「先签 exe、再打包、最后签安装包」的顺序签名。SHA-256 + RFC 3161 时间戳。

## 目录

```
omd.pyw        主程序（tkinter，无第三方依赖）
installer.py   安装/卸载器
build.cmd      一键打包
sign.ps1       Authenticode 签名
assets/        图标
```

## 说明

- 安装包未签名（或使用自签名证书）时，Windows SmartScreen 会提示一次。
- 凭据桥接只读取本机配置文件，不修改源应用，也不上传任何内容。

## 许可

MIT
