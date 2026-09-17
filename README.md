# 样本链路与隔离处置台

本地 Web 应用：样本批次/条码登记、交接扫描（持有人·地点·时间）、温度区间导入与超限自动冻结、授权处置、可验证保管时间线与审计导出。

- 后端：Python + FastAPI + SQLite（WAL 模式，卷持久化）
- 前端：原生 JavaScript 单页应用（由后端静态托管）
- 启动：Docker Compose 单容器

## 快速开始

```bash
docker compose up --build
```

访问 **http://localhost:8000**

内置账号（首次启动自动创建）：

| 账号 | 密码 | 角色 | 权限 |
|---|---|---|---|
| `staff1` / `staff2` | `staff123` | 员工 | 登记批次/样本、交接扫描、温度导入 |
| `lead1` | `lead123` | 授权人员 | 员工权限 + 隔离解除/纠正处置、冲突确认 |
| `admin` | `admin123` | 管理员 | 全部权限 |

首次启动默认写入演示数据（批次 `DEMO-2026-001`，样本 `DEMO-0001~0003`，其中 `DEMO-0002` 有一条超限待处置），可在 `docker-compose.yml` 中设 `SEED_DEMO_DATA: "false"` 关闭。

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DB_PATH` | `/data/app.db`（容器） | SQLite 数据库文件路径，挂载卷 `custody_data` 持久化 |
| `SESSION_TTL_HOURS` | `12` | 登录会话有效期 |
| `SEED_DEMO_DATA` | `true`（compose 中） | 首次启动且无数据时写入演示批次 |
| `STATIC_DIR` | 自动探测 | 前端静态文件目录 |

端口映射在 `docker-compose.yml` 中修改（默认 `8000:8000`）。

本地开发（不用 Docker）：

```bash
pip install -r backend/requirements.txt
cd backend && uvicorn app.main:app --reload --port 8000
```

## 功能与页面

1. **批次与样本**：创建批次（含温度阈值），按批次批量登记条码（每行一个），条码为天然幂等键，重复登记自动跳过；登记即生成保管链创世事件（seq=1）。
2. **交接扫描**：扫码/输入条码 → 自动带出当前持有人 → 填写新持有人与地点提交。每次交接强制校验前一持有人；表单为每次交接生成唯一**幂等键**，网络重试/双击/刷新重提均返回首次结果，不产生双重链路。
3. **温度导入**：选择批次后粘贴或上传 CSV（格式：`记录时间,温度[,条码]`，首行可为表头）。导入后按批次阈值自动生成超限事件并**冻结**相关样本（无条码的读数冻结整批）的后续交接；重复导入按 `(批次,样本,时间,温度)` 自动去重。
4. **异常处置**：展示待处置超限事件与交接冲突（持有人不符/冻结期尝试/未登记条码，均持久化留痕）。授权人员填写理由后可**解除隔离**或**纠正**，事件关闭后自动解冻；冲突可确认归档。
5. **保管时间线**：每样本一条 SHA-256 哈希链（创世事件起，前向链接），页面可一键重放验证完整性，篡改/缺环会被检出。
6. **审计**：全部关键操作追加写入全局哈希链审计日志，支持按动作过滤、链验证、导出 CSV/JSON。

刷新页面或重启容器后，保管状态、冲突记录与审计日志均保持一致（SQLite 落盘 + 卷持久化）。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | 登录，返回 Bearer 令牌 |
| GET/POST | `/api/batches` | 批次列表 / 创建批次 |
| POST | `/api/samples` | 批量登记样本条码（幂等） |
| GET | `/api/samples/by-barcode/{barcode}` | 样本当前状态（持有人/冻结/超限） |
| POST | `/api/transfers` | 交接（校验前一持有人 + 幂等键） |
| GET | `/api/samples/{id}/timeline` · `/verify` | 保管时间线 / 链完整性验证 |
| POST | `/api/temperature/import` | 导入温度读数，自动生成超限事件 |
| GET | `/api/exceptions` | 待处置异常（`?all=true` 含已处置） |
| POST | `/api/exceptions/{id}/dispositions` | 处置：`{"action":"release"\|"correct","reason":"..."}`（需授权角色） |
| POST | `/api/conflicts/{id}/acknowledge` | 确认交接冲突（需授权角色） |
| GET | `/api/audit` · `/audit/verify` · `/audit/export?format=csv\|json` | 审计查询 / 验证 / 导出 |

交接请求体示例：

```json
{
  "barcode": "DEMO-0001",
  "expected_from_holder": "张倩",
  "to_holder": "李牧",
  "location": "冷藏库 A-01",
  "idempotency_key": "9f1c...（客户端生成的 UUID）",
  "scanned_at": "2026-09-17T08:30:00Z"
}
```

## 幂等与并发设计

- **幂等**：每次交接携带客户端生成的 `idempotency_key`（数据库唯一约束）。重复/重试提交命中约束时返回首次记录并标记 `replay: true`；样本条码、温度读数 likewise 以自然键去重。
- **并发**：所有写操作在 `BEGIN IMMEDIATE` 事务内完成"检查持有人 → 追加链事件 → 推进当前状态"，`custody_events(sample_id, seq)` 唯一约束兜底——同一时刻只有一个交接能延长链路，其余请求收到 409 冲突并留痕，绝不产生双重链路。
- **可验证**：保管事件与审计记录均为 SHA-256 前向哈希链，提供 `/verify` 端点与页面按钮重放校验。
- **持久化**：SQLite（WAL）落盘于挂载卷，重启后状态、冲突、审计一致。

## 测试

```bash
python3 tests/smoke_test.py   # 服务层 43 项检查：幂等/并发/冻结/处置/链验证/重启一致性
```

## 目录结构

```
├── docker-compose.yml
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   └── app/
│       ├── main.py          # FastAPI 入口，挂载 API 与静态前端
│       ├── config.py        # 环境变量配置
│       ├── db.py            # SQLite 连接/schema/写事务
│       ├── services.py      # 业务逻辑（幂等、并发、冻结、处置、验证）
│       ├── security.py      # 口令哈希与会话
│       ├── audit.py         # 哈希链审计
│       └── routers/         # auth / batches / transfers / temperatures / exceptions / audit
├── frontend/                # 原生 JS 单页应用
└── tests/smoke_test.py
```
