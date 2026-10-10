# VPS Dashboard Deploy Prompt

把下面整段直接交给 VPS 上的 Agent 执行。

---

你在一台 Linux VPS 上操作，目标是把 `EATLQ` 仓库里的量化驾驶舱安全部署上线，并保持最小公网暴露面。

## 你的任务

请在 VPS 上完成以下事情：

1. 拉取 `EATLQ` 仓库最新 `main` 分支代码。
2. 只部署量化驾驶舱相关服务，不要改动无关业务。
3. 确保 dashboard 只通过反向代理对外暴露，不要把 Redis / Postgres / ClickHouse / strategy / executor 直接暴露到公网。
4. 使用 HTTPS。
5. 给 dashboard 加上基础访问认证。
6. 默认把 dashboard 设置为只读模式；不要开放任何高风险交易写接口。
7. 启动完成后做验收，确认页面、API、容器、反向代理都正常。
8. 最后给我返回：
   - 实际访问 URL
   - Basic Auth 用户名
   - 涉及到的配置文件路径
   - 验收结果

## 强约束

- 不允许使用任何内网穿透工具。
- 不允许把 Redis、Postgres、ClickHouse、strategy、executor 的端口暴露到公网。
- 不允许关闭服务器防火墙。
- 不允许把 dashboard 的受控动作默认开放到公网。
- 不允许修改 git 历史，不允许强推。
- 不允许破坏现有 compose 结构。

## 部署目标

仓库中与 dashboard 相关的文件包括：

- `services/dashboard/server.py`
- `services/dashboard/static/index.html`
- `services/dashboard/static/app.js`
- `services/dashboard/static/styles.css`
- `services/dashboard/requirements.txt`
- `infra/compose/compose.yaml`
- `infra/compose/.env.example`
- `docs/dashboard-v1-cockpit.md`

另有一份部署说明：

- `docs/vps-dashboard-agent-prompt.md`

## 环境变量要求

请在 VPS 上的实际 `.env` 中为 dashboard 至少配置这些变量：

```env
DASHBOARD_BIND=0.0.0.0
DASHBOARD_HOST_BIND=127.0.0.1
DASHBOARD_PORT=8090

DASHBOARD_BASIC_USER=<generate_secure_username>
DASHBOARD_BASIC_PASS=<generate_strong_password>

DASHBOARD_ACTION_TOKEN=<generate_long_random_token>
DASHBOARD_READONLY=1
DASHBOARD_ALLOW_PANIC_ACTIONS=0

DASHBOARD_MAX_SIGNALS=60
DASHBOARD_MAX_SHADOW=120
DASHBOARD_MAX_MARKET_TICKS=1200
DASHBOARD_MAX_MARKET_CANDLES=180
```

要求：

- `DASHBOARD_HOST_BIND=127.0.0.1`
- dashboard 只能被本机反向代理访问
- `DASHBOARD_READONLY=1`
- `DASHBOARD_ALLOW_PANIC_ACTIONS=0`

## 反向代理要求

优先使用 `Caddy`，如果服务器上已有 `Nginx` 且在使用中，则使用 `Nginx`。

要求：

- 对外只开放 `80/443`
- 反向代理到 `127.0.0.1:8090`
- 开启 HTTPS
- dashboard 路径直接挂到主域名或子域名根路径
- 添加 Basic Auth
- 不要把其他服务一起暴露

如果使用 `Caddy`，请生成完整 `Caddyfile`。
如果使用 `Nginx`，请生成完整站点配置并启用。

## Docker / Compose 启动要求

先检查现有容器状态，再按最小变更方式启动。

建议顺序：

1. 进入仓库根目录
2. `git pull`
3. 检查 `infra/compose/.env`
4. 确保 `redis` 已正常运行
5. 启动 `dashboard`
6. 如当前量化运行链路本就需要显示完整内容，则仅在已有安全前提下检查这些服务是否在运行：
   - `pool-discovery`
   - `swap-poller`
   - `strategy`
   - `executor`
7. 不要因为 dashboard 部署而擅自重构其他服务

dashboard 启动后，要验证：

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml ps dashboard
curl -I http://127.0.0.1:8090/
curl http://127.0.0.1:8090/api/health
```

## 数据填充说明

如果页面结构能打开但数据为空，请不要误判为 dashboard 故障。

dashboard 的内容依赖这些运行时数据：

- `signals`
- `shadow:signals`
- `market_ticks`
- `pool:discovered:*`
- `exec:open_pools`
- `exec:pos:*`
- `exec:trades`
- `exec:daily_eval:*`
- `SYSTEM_PANIC`

因此请检查：

1. Redis 是否在线
2. `strategy` 是否在产出 `signals`
3. `swap-poller` / `ingestor` 是否在产出 `market_ticks`
4. `pool-discovery` 是否在产出 `pool:discovered:*`
5. `executor` 是否在产出 `exec:*`

只做检查和启动，不要擅自更改交易逻辑。

## 验收步骤

请至少完成下面这些验收：

### 1. Git 版本

确认 VPS 上代码已是远端最新 `main`。

### 2. 容器状态

输出：

```bash
docker compose --env-file infra/compose/.env -f infra/compose/compose.yaml ps
```

### 3. Dashboard 本机可达

输出：

```bash
curl http://127.0.0.1:8090/api/health
curl http://127.0.0.1:8090/api/data
```

### 4. 反向代理可达

输出：

```bash
curl -I https://<your-domain>
```

### 5. Basic Auth 生效

未带认证请求应失败，带认证请求应成功。

### 6. 公网端口安全

确认公网只开放：

- `22`
- `80`
- `443`

并确认下面这些端口未对公网开放：

- `6379`
- `5432`
- `8123`
- `9000`
- `8080`
- `8090`
- 任何 strategy / executor 内部端口

### 7. Dashboard 内容检查

检查页面是否为新的 `EATLQ Quant Cockpit` 驾驶舱。

如果内容为空，请说明是“运行时数据未产出”还是“dashboard API 故障”。

## 最终输出格式

最后请按下面格式返回：

```md
## Deploy Result

- Git revision: <commit>
- Public URL: <url>
- Basic Auth username: <username>
- Dashboard local upstream: 127.0.0.1:8090
- Reverse proxy: <caddy-or-nginx>

## Runtime Status

- redis: <status>
- dashboard: <status>
- pool-discovery: <status>
- swap-poller: <status>
- strategy: <status>
- executor: <status>

## Verification

- local api health: PASS/FAIL
- public https access: PASS/FAIL
- basic auth: PASS/FAIL
- public port exposure check: PASS/FAIL
- dashboard content source check: PASS/FAIL

## Notes

- <important findings>
- <if data is empty, explain why>
```

如果遇到阻塞，不要瞎猜，直接说明卡在哪一步、缺什么信息、推荐下一步是什么。

---
