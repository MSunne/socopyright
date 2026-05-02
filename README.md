# 软著申请材料自动生成系统

给定「公司名 + 统一社会信用代码 + 成立日期 + 软著数量」，批量生成符合中国版权保护中心 2025 标准的软著申请 zip 包。

> **架构/约定权威文档**：`PROJECT_SPEC.md`
> **当前阶段**：Phase 1 完成（骨架+认证+账号 API+前端）

## 本地运行（macOS / Linux）

```bash
chmod +x run.sh
./run.sh
```

首次运行会创建 `.venv`、安装依赖、生成 `.env`。修改 `.env` 里的 `LLM_*`、`JWT_SECRET`、`ADMIN_TOKEN` 后重启。

访问：<http://localhost:8000>

## 账号管理（纯 API，无后台 UI）

```bash
# 创建用户
curl -X POST http://localhost:8000/admin/users \
  -H "X-Admin-Token: <你的 ADMIN_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"secret123"}'

# 列表
curl http://localhost:8000/admin/users -H "X-Admin-Token: <...>"

# 改密码或禁用
curl -X PATCH http://localhost:8000/admin/users/1 \
  -H "X-Admin-Token: <...>" -H "Content-Type: application/json" \
  -d '{"password":"newpass"}'

# 删除
curl -X DELETE http://localhost:8000/admin/users/1 -H "X-Admin-Token: <...>"
```

## 当前可用功能

- 用户登录 / 登出
- 查看自己的任务列表
- 提交任务（目前只入库，生成流水线在 Phase 5 接入）

## 后续阶段

见 `PROJECT_SPEC.md` 第 10 节。
