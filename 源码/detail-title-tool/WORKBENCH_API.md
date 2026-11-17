# 统一工作台接口

统一页仅使用同源地址。旧 `/api/products`、`/api/jobs`、`/api/drafts` 和排版任务继续兼容。

| 请求 | 用途 |
| --- | --- |
| `GET /api/workbench/products` | 读取素材目录和登记的商品列表 |
| `GET /api/workbench/products/<code>` | 读取资料和该商品的 `edit`、`refresh` 能力及缺项 |
| `POST /api/render-tasks` | 提交改字任务。原有 `draft`、`preview` 继续可用，`refresh-edit` 读取已确认的翻新稿 |
| `GET /api/render-tasks/<id>` | 读取当前员工的改字排队状态 |
| `/api/refresh/*` | 同源转发翻新登记、计划、预览、确认、任务和成稿接口 |

翻新流程使用 `detail-page-refresh/REFRESH_API.md` 约定的候选 ID、登记版本和 `Idempotency-Key`。确认后的 `task.id` 是翻新确认任务 ID，`task.result.revision` 是确认版本。两者作为改字交接参数，浏览器无需接触交接文件或存储路径。

```json
{
  "kind": "refresh-edit",
  "code": "1000000000",
  "job_id": "<翻新确认任务 ID>",
  "revision": "<翻新确认版本>",
  "request_id": "<本次改字提交 UUID>"
}
```

可信后端以当前员工身份取确认记录和交接清单，校验 SKU、任务、版本、文件名、SHA-256、画布与分屏区间，并从 PSD 读取真实文字图层。冻结的场景和瓶图直接来自确认稿。改字结果是独立草稿及成稿，响应的 `source` 记录原确认任务、审批状态和摘要。

接口联调使用部署环境内实际生成并确认的翻新稿。
