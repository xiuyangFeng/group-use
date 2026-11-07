# 组内工具（group-use）

组内自用工具仓库。每个子项目独立开发、独立使用，互不依赖；密码、密钥、患者数据**都不进仓库**。

| 子项目 | 目录 | 用途 |
|---|---|---|
| 文件保险箱 | [`文件保险箱/`](文件保险箱/README.md) | 把整个文件夹加密上锁，跨 macOS / Windows，组内共用一个 6 位数字密码，带密保问题找回（答对 3 个里任意一个）、审计日志、自动上锁 |
| CT 数据脱敏工具 | [`脱敏小软件/`](脱敏小软件/README.md) | 把 CT DICOM 脱敏（批量：每个子文件夹一个患者；也可以直接选一个患者的文件夹单独处理），输出可外发的数据，本地留映射表 |

## 下载安装包

安装包在 [Releases v0.3.0](https://github.com/xiuyangFeng/group-use/releases/tag/v0.3.0)：

| 文件 | 平台 |
|---|---|
| `fdvault-0.3.0-Windows-x64.zip` | Windows 10/11 x64（解压后双击 `文件保险箱/文件保险箱.exe`） |
| `fdvault-0.3.0-macOS-arm64.dmg` | macOS Apple 芯片（拖进 Applications） |
| `fdvault-0.3.0-macOS-arm64.zip` | macOS Apple 芯片（解压出 `.app`） |
| `CT_Deid_Windows.zip` | Windows x64（CT 脱敏 v13） |
| `fdvault-0.3.0-source.zip` | 源码包 |
| `SHA256SUMS.txt` | 校验文件 |

未签名，首次打开按提示放行（macOS：右键打开；Windows：更多信息 → 仍要运行）。文件名用 ASCII `fdvault`，避免中文文件名在部分浏览器里乱码。

## 文件保险箱

- 使用说明：[文件保险箱/使用说明](文件保险箱/docs/使用说明.md)
- 需求与边界：[需求说明书 v0.2](文件保险箱/docs/需求说明书.md)
- 验收情况：[验收清单](文件保险箱/docs/验收清单.md)
- 常见问题：[常见问题与报错](文件保险箱/docs/常见问题与报错.md)
- 数据安全：先写新副本、fsync、再删旧副本；任意时刻断电都可恢复；0 字节丢失

## 脱敏工具

- 使用说明：[脱敏小软件/README.md](脱敏小软件/README.md)
- 原仓库 `xiuyangFeng/CT_Deid` 已迁移到这里并已删除（历史提交保留在 `脱敏小软件/` 的提交记录中，见 [迁移说明](文件保险箱/docs/仓库迁移说明.md)）

## 云端构建（GitHub Actions）

仓库根目录 `.github/workflows/` 下有两个独立流程：

- `build-vault.yml`：文件保险箱的 macOS/Windows 测试 + 打包 + 产物上传
- `build-deid-windows.yml`：脱敏工具的 Windows exe 云端构建 + exe 自检 + 产物上传

两个流程只在对应子目录有改动时触发，也可以手动 `workflow_dispatch`。

## 安全约定

- 仓库为**私有**；不要把患者数据、真实编号、映射表、密码、密保答案、恢复密钥提交上来
- 各子项目自带 `.gitignore`；`.venv/`、`dist/`、`build/`、`release/`、测试数据都已忽略
