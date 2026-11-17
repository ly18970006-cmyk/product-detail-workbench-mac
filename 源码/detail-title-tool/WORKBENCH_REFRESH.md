# 统一工作台翻新接入

同一页面通过 `/api/refresh/*` 使用翻新服务的原生 API，接口和任务状态见[翻新 API](../detail-page-refresh/REFRESH_API.md)。统一应用完成代理身份校验后，使用自己的员工身份和进程内凭据调用独立的翻新 Flask 应用。翻新工作者有自己的队列和私有数据目录。浏览器只访问统一服务的地址。

默认从相邻的 `detail-page-refresh` 加载服务。部署时可用 `DETAIL_REFRESH_APP_ROOT` 指定可信翻新服务代码目录。`REFRESH_V2_ASSETS` 可以覆盖商品资料根，默认沿用统一应用素材目录中的 `产品编码测验`。PhotoCraft CLI 不在 `PATH` 时，分别通过 `DETAIL_RENDER_CLI` 和 `REFRESH_PHOTOCRAFT` 指定两个工作者使用的可执行文件。公司模型和试点额度继续由翻新服务的部署配置决定。资料扫描、版式草图、确认和成稿可以用 `local_review` 联调，界面应标出这一模式。该模式没有发起真实模型调用。

本工作区的 Windows 默认入口从 `detail-page-storage.local.json` 读取两类成果根。统一工作台的改字和翻新运行数据分别存入各自成果根的 `运行数据/统一工作台`。默认改字目录下可以用 `REFRESH_V2_DATA` 单独指定翻新数据根。显式设置 `DETAIL_DATA_DIR` 的统一实例继续使用其 `refresh-v2` 子目录，便于复用该实例的历史草稿。

## 确认稿交接

1. 查询 `GET /api/refresh/jobs/{id}`。确认任务必须为 `action: "confirm"`、`state: "succeeded"`，并且结果包含 `approval_status: "internal_layout_confirmed"` 和 `revision`。
2. 在改字队列提交 `POST /api/render-tasks`，传入 `kind: "refresh-edit"`、`code`、确认任务的 `job_id`、`revision` 和该次操作的 `request_id`。
3. 继续查询 `status_url`。成功结果是新的改字草稿，可以使用现有图层预览、确认和下载接口。

确认任务和文件都按员工查找。可信后端通过独立的进程内令牌读取交接清单，按 SHA-256 核对 PSD 和 PNG 后才建立新草稿。后端只读取确认目录中的 `detail.psd`、`detail.png` 和 `checks.json`。草稿保留翻新任务、修订和素材摘要，原确认稿保留在翻新数据目录中。
