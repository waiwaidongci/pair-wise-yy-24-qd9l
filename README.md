# 电台播出与版权窗口排程

一个不依赖第三方包、使用 SQLite 和标准库 HTTP 服务的电台排程项目。系统把"计划排期"和"实际播出"分开保存，支持地区授权、日期窗口、禁播时段、节目冷却、赞助商间隔、实播对账与版权越界检查。

排期调整不再改完立即生效：先建**编排草案**（仅内部可见，不影响正式计划），校验通过后**发布为新版本**。已发布版本不可修改；出问题时可以从任一历史版本重建草案再发布来完成回滚，原版本、被取代的排期和已登记的实播记录全部保留，对账时能追溯到当时的计划依据。

## 运行

需要 Python 3.11+。

```bash
python app.py
```

默认端口为 `8111`，页面地址是 <http://127.0.0.1:8111>。第一次启动会创建 `radio.db` 并写入三条演示排期（同时发布为初始版本 v1）。也可以设置端口和数据库位置：

```bash
PORT=9000 RADIO_DB=/tmp/radio.db python app.py
```

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖：草案校验与发布、版本不可变、从历史版本回滚、回滚后实播记录仍参与对账、旧库结构迁移，以及时间重叠、未授权地区、实播错节目等失败场景。

## 草案与版本工作流

1. `POST /api/drafts` 建草案：`title` 必填；`copy_current: true` 从当前计划起步，`base_version_id` 从任一历史版本起步（回滚入口），都不给则是空草案。
2. `POST /api/drafts/{id}/slots` 往草案里加排期；`POST /api/drafts/{id}/slots/{slot_id}/delete` 删除。草案只给内部查看，不影响正式计划。
3. `POST /api/drafts/{id}/validate` 随时校验，返回问题列表，不产生任何变更。
4. `POST /api/drafts/{id}/publish` 校验通过才发布：整个变更在一个事务里完成，任一校验失败正式计划原样不动、不产生版本。发布只替换草案涉及的"日期+地区"范围——草案里删掉的排期被标记为"被新版本取代"（行保留，实播记录关联不断），新增排期逐条校验后入库，未变化的排期保留原 ID。
5. 发布后草案关闭，不能重复发布；`POST /api/drafts/{id}/abandon` 可废弃未发布的草案。

版本是发布时刻完整计划的不可变快照，只增不改。页面上能看到当前版本和最近几次变更（每版新增/移除条数、来源草案、备注、回滚来源）。

## 主要 API

- `GET /api/state`：当前已发布计划、当前版本、最近版本变更、内部草案和最近对账异常
- `POST /api/programs`：创建节目并授权地区
- `POST /api/programs/{id}/regions`：追加地区授权
- `POST /api/drafts` / `GET /api/drafts/{id}`：建草案 / 看草案
- `POST /api/drafts/{id}/slots` / `POST /api/drafts/{id}/slots/{slot_id}/delete`：编辑草案
- `POST /api/drafts/{id}/validate` / `POST /api/drafts/{id}/publish` / `POST /api/drafts/{id}/abandon`
- `GET /api/versions/{id}`：查看历史版本快照（不可修改）
- `POST /api/playout`：登记实播记录
- `POST /api/reconcile`：按日期生成漏播、错播、时长偏差和超授权异常；被取代排期只要有实播记录就仍参与对账，异常详情会标注"排期源自哪个版本、被哪个版本取代"

原来的 `POST /api/schedule` 和 `POST /api/slots/{id}/replace` 已移除，排期变更一律走草案发布，避免编辑和审核互相覆盖。一个已知限制：草案按"日期+地区"整体替换，因此无法通过草案把某天某地区清空成完全无排期（可以换成垫片节目代替）。
