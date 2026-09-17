# 样本链路与隔离处置台

面向实验室/冷链场景的本地 Web 应用：登记样本批次与条码，按交接扫描构建**可验证的保管链路**；导入温度记录后按批次阈值自动生成超限事件并**冻结后续交接**；授权人员可提交带理由的**解除隔离 / 纠正事件**；全程哈希链审计，支持导出。

- 后端：Python 3.12 · FastAPI · SQLite（WAL）
- 前端：原生 JavaScript 单页应用（无构建步骤，由后端直接托管）
- 部署：Docker Compose 一键启动

---

## 快速开始（Docker Compose）

```bash
docker compose up -d --build
```

访问 <http://localhost:8000>

默认账号（可通过环境变量覆盖，见下文）：

| 账号  | 密码      | 角色     | 权限说明 |
|-------|-----------|----------|----------|
| alice | alice123  | staff    | 登记批次/条码、交接扫描、导入温度 |
| bob   | bob123    | staff    | 同上（用于交接对端演示） |
| carol | carol123  | supervisor | 上述全部 + 解除隔离、登记纠正事件 |

停止并保留数据：`docker compose down`；数据保存在命名卷 `app_data` 中，重启容器后状态、链路、冲突记录与审计日志完全一致。

## 配置项（环境变量）

均可在 `docker-compose.yml` 的 `environment` 中修改，或通过 shell 环境注入：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `APP_SECRET` | `dev-only-secret-change-me` | 登录令牌 HMAC 签名密钥，**生产必改** |
| `APP_USERS` | 内置三个账号 | JSON：`{"账号":{"password":"...","role":"staff|supervisor"}}` |
| `APP_TOKEN_TTL_MINUTES` | `720` | 登录令牌有效期（分钟） |
| `APP_DB_PATH` | 容器内 `/data/app.db` | SQLite 数据库文件路径 |

示例：

```bash
APP_SECRET='请换成随机长字符串' \
APP_USERS='{"op1":{"password":"s3cret","role":"staff"},"qa":{"password":"s3cret2","role":"supervisor"}}' \
docker compose up -d --build
```

## 本地开发（不用 Docker）

```bash
pip install -r backend/requirements.txt
uvicorn backend.app.main:app --reload --port 8000
```

数据库默认写入 `./data/app.db`（可用 `APP_DB_PATH` 覆盖）。

## 使用流程

1. **批次与样本**：创建批次（设定温度阈值）→ 登记条码。登记人自动成为样本第一任持有人（链路第 0 环）。
2. **交接扫描**：输入条码 → 系统查出当前持有人并自动填入"前一持有人" → 填写接收人、地点、时间提交。
   - 服务端强制校验前一持有人，不匹配返回 409 并提示当前持有人；
   - 每次表单生成唯一幂等键，网络重试/双击/刷新重提均返回原记录，**不会产生双重链路**；
   - 并发提交由 `BEGIN IMMEDIATE` 事务 + `UNIQUE(sample_id, seq)` + 唯一幂等键三重保障串行化。
3. **温度导入**：选择批次，粘贴或上传 CSV（`条码,温度,记录时间`，条码与时间可空）。同一记录重复导入幂等跳过；超出批次阈值的记录自动生成超限事件并**冻结该批次全部后续交接**。
4. **异常处置**：supervisor 在"异常处置"页对待处置异常提交**解除隔离**（批次无其他未决异常时自动解冻）或**纠正事件**，均必须填写理由并写入审计。
5. **保管时间线**：输入条码查看完整链路（每环含 SHA-256 哈希与前环引用），一键验证链路完整性。
6. **审计日志**：所有写操作追加哈希链审计记录，可在线验证、导出 CSV/JSON。

## 验证与测试

端到端冒烟测试（覆盖幂等重放、10 路并发交接、超限冻结、越权拒绝、链校验等 25 项）：

```bash
# 服务运行中执行
pip install requests
python3 backend/tests/smoke_test.py            # 默认 http://127.0.0.1:8000
```

## API 概览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/login` | 登录，返回 Bearer 令牌 |
| POST | `/api/batches` | 创建批次（含温度阈值） |
| POST | `/api/batches/{code}/samples` | 登记条码（幂等） |
| GET  | `/api/samples/{barcode}` | 当前持有人/冻结状态 |
| POST | `/api/handovers` | 交接（校验前一持有人 + 幂等键） |
| GET  | `/api/samples/{barcode}/timeline` | 保管时间线 |
| GET  | `/api/samples/{barcode}/verify` | 链路哈希校验 |
| POST | `/api/temperature/import` | 导入温度（幂等，超限自动冻结） |
| GET  | `/api/events?status=OPEN` | 待处置异常 |
| POST | `/api/events/{id}/release` | 解除隔离（supervisor，需理由） |
| POST | `/api/events/{id}/corrections` | 纠正事件（supervisor，需理由） |
| GET  | `/api/audit` · `/api/audit/verify` · `/api/audit/export?format=csv\|json` | 审计查询/验证/导出 |

交互式接口文档：<http://localhost:8000/docs>

## 数据一致性设计

- **幂等**：交接以 `idempotency_key` 唯一约束去重；温度记录以内容哈希 `reading_uid` 去重，超限事件 ID 由记录派生（`OOR:<uid>`），重复导入不产生重复事件；解除隔离以部分唯一索引保证每个异常只解除一次。
- **并发**：所有写操作在进程写锁 + `BEGIN IMMEDIATE` 事务中串行执行，`UNIQUE(sample_id, seq)` 兜底，杜绝双重链路。
- **可验证**：保管链与审计链均为 SHA-256 哈希链（每条记录含前序哈希），任何篡改都会在验证接口中暴露。
- **持久化**：SQLite WAL 模式，数据落盘于 Docker 卷，刷新页面或重启容器后状态、冲突记录与审计日志保持一致。
