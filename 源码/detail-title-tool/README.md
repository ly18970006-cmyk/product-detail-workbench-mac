# 详情页改字

在统一工作台输入商品编码，点选 PSD 的可见文字层修改。可以在同一草稿累计编辑多个图层。确认对应的预览版本后即可下载可编辑 PSD 和整页 PNG。可用区域、画布和保护对象由登记和渲染校验共同判断。Photoshop 用于成稿验收及后续编辑。

## 启动和素材登记

本机统一入口与 E 盘配置见 [仓库启动说明](../README.md)。Windows 独立改字入口使用 `Start.cmd`，默认端口 8768。改字和翻新联调优先用统一工作台。按环境配置 `DETAIL_RENDER_CLI` 和实际素材根。

`catalog.example.json` 初始为空，可复制为本机的 `catalog.json`，也可用 `DETAIL_CATALOG` 指向私有登记。按商品编码在 `products` 中添加记录。例如下面是虚拟商品的登记结构。

```json
{
  "1000000000": {
    "name": "示例商品",
    "template": {"root": "assets", "path": "demo/template.psd"},
    "bottle": {"root": "assets", "path": "demo/bottle.png"},
    "render": {
      "expected_width": 790,
      "first_screen_height": 1000,
      "title_path": ["首屏", "标题"]
    }
  }
}
```

路径相对于配置的素材根。尺寸和图层路径必须与该 PSD 一致。将该登记用在生产前，应先用实际模板检查图层命名、必要字体、字框和保护范围。

## 服务与部署

可在本机使用 `GET /api/health` 和 `GET /api/workbench/products` 验证统一入口。排版队列、翻新稿接续改字和文件交接见 [统一工作台 API](WORKBENCH_API.md) 与 [翻新接入](WORKBENCH_REFRESH.md)。

服务器部署可使用 `python -B pack_for_server.py <空目录> --workbench`，该命令会把已登记 PSD、瓶图及引用的场景文件复制到部署包。通过 `DETAIL_MODE=proxy` 进行企业访问时，可信代理须完成身份校验。部署环境配置 `DETAIL_PUBLIC_ORIGIN`、`DETAIL_PROXY_TOKEN`、`DETAIL_DATA_DIR` 及渲染器路径。服务仅在环回地址监听，任务和文件按员工身份隔离。启动配置、代理令牌和素材留在部署环境。
