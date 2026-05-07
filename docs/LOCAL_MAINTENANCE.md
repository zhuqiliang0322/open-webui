# 本地维护说明

本文档记录本机 `open-webui` 的维护方式。当前目标是单用户、本地使用、默认登录管理员账号，并通过 OpenClaw 处理模型请求。

## 当前状态

- 仓库路径：`/Users/panda/open-webui`
- 本地补丁分支：`1agent-dev`
- Web 入口：`http://127.0.0.1:3000/`
- 数据目录：`/Users/panda/open-webui/.data`
- 默认管理员：`zhuqiliang0322@gmail.com`
- 管理员数量要求：只保留 1 个管理员

`main` 分支只保留上游基线。本机修改统一放在补丁分支维护，不直接提交到 `main`。

## 本地登录模式

本机启用免输入密码的管理员登录。

必需环境变量：

```bash
LOCAL_AUTO_ADMIN=true
LOCAL_AUTO_ADMIN_EMAIL=zhuqiliang0322@gmail.com
```

行为说明：

- 浏览器打开 `http://127.0.0.1:3000/` 时，会自动尝试登录默认管理员。
- 只允许本机访问触发自动登录。
- 请求来源必须是 `localhost`、`127.0.0.1` 或 `::1`。
- 如果默认账号不存在，或不是管理员，自动登录会失败。
- 该模式只适合本机使用，不适合公网、局域网共享或多人使用。

## 单管理员要求

当前只保留以下管理员：

```text
zhuqiliang0322@gmail.com
```

检查管理员数量：

```bash
sqlite3 .data/webui.db "select email,name,role from user order by email; select 'admin_count', count(*) from user where role='admin';"
```

期望结果：

```text
zhuqiliang0322@gmail.com|panda|admin
admin_count|1
```

如果后续误创建了其他管理员，先备份数据库，再降级或删除多余管理员。

备份示例：

```bash
mkdir -p .data/backups
cp .data/webui.db ".data/backups/webui-before-admin-cleanup-$(date +%Y%m%d-%H%M%S).db"
```

## OpenClaw 模型链路

当前模型请求链路：

```text
Open WebUI -> Open WebUI backend -> OpenClaw Worker API -> OpenClaw -> 本地模型服务
```

核心要求：

- Open WebUI 不直接连 LM Studio。
- Open WebUI 通过 Responses API 方式把请求交给 OpenClaw。
- Worker API 默认地址为 `http://127.0.0.1:8090`。

关键环境变量：

```bash
OPENAI_API_CONFIGS='{"0":{"api_type":"responses","connection_type":"local"}}'
OPENCLAW_WORKER_API_BASE_URL=http://127.0.0.1:8090
```

如需设置 Worker API token，使用：

```bash
OPENCLAW_WORKER_API_TOKEN=...
```

不要把真实 token 写入文档或提交到 Git。

## 启动前检查

确认本地配置文件存在：

```bash
test -f .env
test -d .data
```

确认 `.env` 至少包含：

```bash
LOCAL_AUTO_ADMIN=true
LOCAL_AUTO_ADMIN_EMAIL=zhuqiliang0322@gmail.com
OPENAI_API_CONFIGS='{"0":{"api_type":"responses","connection_type":"local"}}'
DATA_DIR=/Users/panda/open-webui/.data
```

`.env` 属于本机私有配置，不提交。

## 启动方式

优先使用仓库本地 Python 环境：

```bash
source .venv/bin/activate
npm run dev
```

如果服务已经在运行，只需检查健康状态：

```bash
curl -sS http://127.0.0.1:3000/health
curl -sS http://127.0.0.1:3000/api/version
```

期望 `/health` 返回：

```json
{"status":true}
```

## 验证方式

### 1. 自动管理员登录

```bash
curl -sS -X POST http://127.0.0.1:3000/api/v1/auths/local-admin/signin \
  -H 'Content-Type: application/json'
```

期望返回用户信息，且 email 为：

```text
zhuqiliang0322@gmail.com
```

role 应为：

```text
admin
```

### 2. 浏览器验证

打开：

```text
http://127.0.0.1:3000/
```

期望结果：

- 不停留在登录页。
- 自动进入 Open WebUI 主界面。
- 当前用户是 `zhuqiliang0322@gmail.com`。
- 可以看到 OpenClaw 模型入口。

### 3. 数据库验证

```bash
sqlite3 .data/webui.db "select email,name,role from user order by email; select 'admin_count', count(*) from user where role='admin';"
```

期望只有一个管理员。

### 4. 定向测试

后端：

```bash
.venv/bin/python -m pytest \
  backend/open_webui/test/util/test_middleware.py \
  backend/open_webui/test/util/test_openai_worker.py
```

前端：

```bash
npx vitest run \
  src/lib/apis/openai/direct.test.ts \
  src/lib/apis/streaming/index.test.ts \
  src/lib/utils/openai-errors.test.ts \
  src/lib/utils/openclaw-worker.test.ts
```

格式检查：

```bash
git diff --check
```

### 5. 敏感文件检查

提交前确认没有把本机数据加入 Git：

```bash
git status --short
git ls-files | rg '(^|/)(\\.env|webui\\.db|webui\\.db-|chroma\\.sqlite3|session|secret|token)'
git ls-files --others --exclude-standard | rg '(^|/)(\\.env|\\.data|webui\\.db|webui\\.db-|chroma\\.sqlite3|session|secret|token)'
```

正常情况下，`.env`、`.data/`、数据库、session、真实 token 都不应出现在待提交文件里。

## 全量检查说明

当前仓库的 `npm run check` 会报告大量既有类型检查问题。因此提交前主要依赖：

- 定向后端测试
- 定向前端测试
- `git diff --check`
- 本地服务健康检查
- 浏览器实际登录检查
- 敏感文件扫描

如果后续要把 `npm run check` 作为提交门槛，需要先单独清理仓库已有类型检查问题。

## 回退方式

代码回退：

```bash
git switch main
```

数据库回退：

```bash
cp .data/backups/<backup-file>.db .data/webui.db
```

回退数据库前需要停止正在运行的 Open WebUI，避免写入冲突。

## 禁止事项

- 不把 `.env` 提交到 Git。
- 不把 `.data/`、`webui.db`、上传文件、session、token 提交到 Git。
- 不把本地补丁直接提交到 `main`。
- 不在公网或局域网开放免登录管理员模式。
- 不绕过 OpenClaw 直接连接 LM Studio，除非明确要做临时排障。
