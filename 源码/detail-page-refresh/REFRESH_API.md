# 商品详情页翻新接口

翻新服务提供同源 `/api/refresh/*`。统一工作台由可信后端在同一进程内转发该前缀，保留原路径。统一代理先完成身份认证，再覆盖请求头 `X-Detail-User` 和 `X-Refresh-Proxy-Token`。前端只使用同源地址，不读取内部端口、文件路径或代理令牌。现有改字 API 及 8774 店铺评审服务独立运行。

## 启动

需要 Python 3.12、`requirements.txt`、PhotoCraft CLI 0.22.0 和可用的 Noto Sans SC。Photoshop 用于验收。默认资料根为 `E:\工作\规划落地对话专用素材\产品编码测验`，可用 `REFRESH_V2_ASSETS` 指定同结构的资料根。资料由有权限的人员放入相应商品编码目录，浏览器提交候选 ID 完成登记。`REFRESH_V2_DATA` 指向私有数据卷，`REFRESH_V2_PORT` 默认为 `8775`。

```powershell
python -m pip install -r requirements.txt
python -B refresh_service.py
```

企业模式注入 `REFRESH_V2_PROXY_TOKEN` 和 `REFRESH_V2_PUBLIC_ORIGIN`。令牌通过可信代理注入，不能下发浏览器。独立联调可用本机模式，仅供一个本机操作者。多员工在企业模式测试。服务为单实例 CPU 工作者，使用 SQLite 保存不可变登记版本与异步任务。文件夹 `artifacts/<任务 ID>/` 位于私有数据根。草图版本和成稿文件只由 API 返回。禁止把私有目录作为静态站点公开。

可选的 `REFRESH_V2_LOCAL_LAYOUTS` 指向服务端私有 JSON，以商品编码区分旧页核对版式。每份版式绑定旧图 SHA-256，登记时冻结版式摘要。其 `screens` 使用旧页原坐标的半开区间、逐屏文案与槽位、产品和品牌位置，以及私有素材根下的独立图层和摘要。只有登记中的指定素材与摘要一致，才能套用配置中的本轮业务决定。核对版式只接受本地预览和复用，登记返回的旧图链接会带 `scope=review`，按屏段合并为 790 px 对照图。无该配置时使用现有通用分屏。

统一工作台沿用登记、分屏和异步预览入口。启用核对配置的商品在 `whole_review` 模式按逐屏来源拼入已确认 PNG、本地透明素材或上线旧图裁段。完整旧长图仍可单独读取。试点的预览、分屏核对及前后对照共享同一任务版本，确认接口对该阶段返回 `review_only`。

需要验证的新场景可在服务端配置一屏 `scene_edit` 和确定的无页面文字参考、局部遮罩及文字保护区。该屏须在允许发送的编码范围内，服务端设置 `REFRESH_V2_REFERENCE_EDIT_LIMIT` 作为独立数据目录的提交额度，再以 `gpt-image-2.5-sunburst` 调用公司 `/v1/images/edits`。返回的蒙版合成候选与原屏一起冻结在本次任务。主预览保留原屏，待人工核查构图和过渡。网关未就绪时使用上线原屏，任务记录注明原因。

品牌首屏光影使用单独的 `protected_sunburst` 预览模式。分屏计划返回 `protected_scene_screen` 时，客户端可提交该屏。服务端使用 `REFRESH_V2_HERO_LIGHTING_CODES` 控制授权，参考图必须与已确认首屏的 SHA-256 和尺寸一致。仍通过 `/v1/images/edits` 生成一张回图，本地只将低频明暗变化应用于保护区之外。成功时 `preview` 返回保护合成图，`comparison` 对照上线裁段，`baseline_comparison` 对照已确认首屏。保留已确认首屏时，`background_runs` 会明示原因。相同幂等键返回原任务，同商品、同员工有未决公司请求时，新的试稿保留已确认首屏。该模式只供审稿。

## 数据、任务与成稿

| 请求 | 用途 |
|---|---|
| `GET /api/refresh/health` | 查看本机或代理身份模式、模型配置状态与试点额度 |
| `GET /api/refresh/products/{code}/candidates` | 扫描已登记的素材根，列出实际旧页、瓶图、运营资料、标志候选及缺项 |
| `GET /api/refresh/products/{code}/candidates/{kind}/{id}/image` | 在登记前查看共享资料库中的图像候选，返回旧图或独立素材 |
| `POST /api/refresh/products/{code}/registrations` | 提交 `old_page`、`bottle`、`operations`、`logo` 候选 ID，和可选 `name`、`packaging`、`channel`、`brand_rule`、`approved_copy` |
| `GET /api/refresh/products/{code}` | 取该员工最近的登记、缺项和旧页 URL |
| `GET /api/refresh/products/{code}/old-page?version={version}` | 读取该登记的实际旧页。素材摘要变化时要求重新登记 |
| `POST /api/refresh/products/{code}/plans` | 以 `registration` 版本启动分屏任务，`mode` 为 `local_review` 或 `company` |
| `POST /api/refresh/previews` | 以 `plan_job_id` 和 `screens` 启动 PNG 草图，支持 `local_review`、`company`、`reuse` |
| `POST /api/refresh/jobs/{id}/confirm` | 对指定草图提交 `revision`，后台生成 PSD 与同版 PNG |
| `GET /api/refresh/products/{code}/jobs` | 恢复当前员工及编码的最近任务 |
| `GET /api/refresh/jobs/{id}` | 查询异步状态与同源预览或下载 URL |
| `POST /api/refresh/jobs/{id}/query` | 用已记录的网关任务 ID 查询原任务 |
| `GET /api/refresh/jobs/{id}/files/{name}` | 读该员工对应任务允许下载的预览或成稿 |
| `GET /api/refresh/jobs/{id}/handoff` | 可信后端读取已确认交接清单，`id` 为确认任务 ID |

登记中的 `packaging`、`channel`、`brand_rule` 接收 `{value,status,approval_ref}`，已核准状态需有依据。`approved_copy` 以稳定的文案字段键映射 `{text,approval_ref}`。运营表转录和模型识别结果均为待审。`internal_layout_confirmed` 表示该员工锁定版式，运营与发布审批单独处理。

任务提交使用 `Idempotency-Key`（16–128 个允许字符）。同员工、同操作、同键及同输入返回原任务。同键绑定其他输入返回 `409 idempotency_conflict`。任务状态为 `queued`、`running`、`succeeded`、`failed`、`unknown`。服务中断后运行中的任务进入 `unknown`。有可查网关任务 ID 时查询原任务，否则由接入方依据请求 ID 人工核查。重放提交不会再次调用模型。新试点请求由配额控制。

```json
{"plan_job_id":"<分屏任务 ID>","screens":["<屏 ID>"],"base_job_id":"<已有草图 ID>","mode":"reuse","edits":{"<屏 ID>":{"<文字键>":"内部改字"}},"offsets":{"<屏 ID>":{"text_y":4}}}
```

已有草图下只改字或位置时使用 `reuse`。逐屏更新使用 `base_job_id`、`redo_screen` 和目标背景模式，其他屏读取原冻结背景。确认前只写轻量 PNG 及冻结素材。确认时重开 PSD 导 PNG、逐屏比对预览并写交接清单。

## 交接契约

可信后端以原员工身份读取 `handoff.json`，并提供独立的 `X-Refresh-Handoff-Token`，值由 `REFRESH_V2_HANDOFF_TOKEN` 配置。统一代理丢弃浏览器的这一标头。交接含 `schema_version:1`、`code`、`job_id`、`revision`、`approval_status`、`width`、`height`、`screens[{id,name,y,height}]`、`assets.{psd,png}.{file,sha256}` 以及 `source.{template,layout,copy,bottle}`。`file` 只使用 `detail.psd` 和 `detail.png`。改字服务可通过同机接口取得该员工的文件，再校验摘要并创建自己的新草稿。保留原 `job_id`、`revision` 及源 PSD 摘要。

## 模型与授权

公司网关由部署环境注入。默认试点额度 `REFRESH_V2_PILOT_LIMIT=0`，授权后按批准额度设置。用于实际旧页的编码白名单 `REFRESH_V2_COMPANY_OLD_PAGE_CODES` 与场景提示编码白名单 `REFRESH_V2_COMPANY_SCENE_CODES` 分开配置。普通场景生成请求发送场景文字，由本地合成真瓶、标签和标志。参考图编辑上传已登记的单张参考图。授权由公司实际端点能力、素材范围和预算决定。`local_review` 产物仅用于校验导入、版式、文件与交接链路。
