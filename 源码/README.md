# 商品详情页工作台

本机统一入口为 `http://127.0.0.1:8905/`。输入商品编码后可选择改字或翻新。

- [改字](detail-title-tool/README.md) 读取已登记的 PSD 原生文字层。校验画布、可用区域及保护对象后生成预览，确认后下载同版 PNG 和可编辑 PSD。
- [翻新](detail-page-refresh/README.md) 登记旧长图、瓶图和运营资料，再按屏生成 PNG 和对照。可以复用已确认的素材。审稿图确认后才进入可编辑 PSD 制作。
- [统一工作台接口](detail-title-tool/WORKBENCH_API.md) · [服务接入](detail-title-tool/WORKBENCH_REFRESH.md) · [翻新 API](detail-page-refresh/REFRESH_API.md)

## Windows 本机启动

准备 Python 3.12。渲染预览和 PSD 需要 PhotoCraft CLI 0.22.0，以及与模板匹配的授权字体。源代码和商品素材分开存储。请在本机准备有权使用的 PSD、瓶图、旧页、运营表和正式标志。

在仓库根目录首次配置，以下两个目标文件仅保存在本机。

```powershell
Copy-Item -LiteralPath 'detail-page-storage.example.json' -Destination 'detail-page-storage.local.json'
Copy-Item -LiteralPath 'detail-title-tool/catalog.example.json' -Destination 'detail-title-tool/catalog.json'
```

在 `detail-page-storage.local.json` 中确认 `edit` 和 `refresh` 指向两个已存在、可写的 E 盘成果根。配置缺失时启动器采用原有兼容默认值。要使用 8905 和 E 盘归档，请先完成此步骤。若 E 盘或配置的成果根不可用，先恢复该路径。

配置素材与渲染器，或用本机已有的受控部署环境传入这些变量。

```powershell
$env:DETAIL_ASSET_ROOT = 'E:\你的授权素材目录'
$env:DETAIL_RENDER_CLI = 'C:\Tools\photocraft-cli.exe'
$env:REFRESH_PHOTOCRAFT = $env:DETAIL_RENDER_CLI
$env:REFRESH_V2_PILOT_LIMIT = '0'
$env:REFRESH_V2_REFERENCE_EDIT_LIMIT = '0'
.\detail-title-tool\Start-Workbench.cmd
```

统一工作台在两个配置根的 `运行数据/统一工作台` 中分别保存改字和翻新任务。初始商品登记为空。可访问首页以及 `/api/health`、`/api/refresh/health` 验证服务，添加自己的登记和素材后再测试实际渲染。该流程不使用公司模型额度。

## 翻新的生图和审稿

本地预览和冻结稿复用由工作台完成。授权的首屏光影候选由后端以已登记提示词和冻结参考图调用公司网关的 `gpt-image-2.5-sunburst`，请求 `POST /v1/images/edits`。后端仅合入保护区外的光影变化并保存对照。界面在超时、没有可用回图等情况下保留冻结稿，同时显示网关状态。

网关接入由部署人员使用公司批准的端点、凭据、素材授权和额度单独配置。收到未决状态时先依据原请求号查单。审稿模式提供 PNG 与前后对照，分层成稿走确认后的流程。详见 [翻新 API](detail-page-refresh/REFRESH_API.md)。

## 发布范围

这个仓库包含统一工作台、改字和翻新的应用源代码、运行模板与配置示例。生产目录、历史任务、私有网关配置和商品文件由部署方管理。服务端部署打包工具会复制已登记素材，仅用于有权处理这些文件的内部部署。
