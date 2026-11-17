# 商品详情页工作台 · Mac 本地部署

独立于套图模块的本地详情页工作台，包含 PSD 改字、按屏翻新与 Mac 启停入口。

## 来源与范围

基于 https://github.com/ruer857/product-detail-workbench ，部署基线提交 `af8dc9f40fb01414530c72752daed0cf108d2e95`。上游说明保留在 `源码/README.md`。
本副本增加 Mac 进程管理、本地素材和字体配置，以及网页试点结果对照页。上游代码快照未发现 LICENSE 文件，本仓库不另行授予上游代码开源许可；对外分发前需确认授权。

## 已验证环境

macOS 14.7.4 / Intel x86_64、Python 3.12.14、PhotoCraft 0.22.0。此 Mac 启动器未在 Windows 验证；源码内 Windows 入口沿用上游。

## 首次配置

1. 自行安装 Python 3.12、PhotoCraft 和有权使用的中文字体。PhotoCraft CLI 预期位置为 `/Applications/PhotoCraft.app/Contents/MacOS/photocraft-cli`。
2. 在本仓库根目录执行：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r 已安装依赖.txt
mkdir -p 运行数据 日志 素材/产品编码测验
cp 配置/catalog.example.json 配置/catalog.json
cp 配置/本机路径.example.json 配置/本机路径.json
chmod +x 启动详情页工作台.command 停止详情页工作台.command
```

3. 编辑 `配置/本机路径.json`：填写自己的商品目录、已安装字体文件及字体族名称。商品目录当前要求名称为“产品编码测验”，内部按商品编码分目录；素材结构见上游工作台说明。示例绝对路径不可直接使用。
4. 双击 `启动详情页工作台.command`，访问 http://127.0.0.1:8905/ 。关闭浏览器不会停止服务，双击停止入口停止本工作台。

状态检查：`.venv/bin/python -B 启动管理.py status`。
服务日志位于 `日志/服务.log`；健康检查 `/api/health` 和 `/api/refresh/health`。

## 能力边界

- 本地素材读取、文字排版和 PhotoCraft 渲染不等于 AI 重新设计。默认未配置公司 AI 网关，相关试点额度为零。
- 上游文档中的模型名属于其配置约定，本副本未验证这些模型在任何服务中可用。
- `/web-trial/<商品编码>` 仅展示本地已有网页试点结果，不负责登录或自动调用 ChatGPT。未上传真实试点成果，也未保留指向私人商品试点的首页入口。
- 商品登记、素材授权、正式标志、文案和包装信息仍需使用者审核。完整无人值守生图与正式交付流程尚未验证。

## 不包含的内容

不包含本机真实配置、商品素材、产品信息表、字体文件、PhotoCraft 二进制、运行环境、历史任务、聊天链接或账号凭据。首次克隆后必须按上述步骤配置自己的环境。
