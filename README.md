<h1 align="center">
  <br>
  <a href="https://github.com/ipycc/cloudrave" alt="logo"><img src="https://raw.githubusercontent.com/iPycc/frontend/00aca676ea29ef3186bd330e670df7e7aafaf7b5/public/cloudrave-logo.svg" width="100"/></a>
  <br>
  Cloudrave
  <br>
</h1>

<div align="center"><img src="./Cloudrave.png" width="1200"></img></div>
<h4 align="center">Cloudrave — 支持多存储策略的公有云文件系统 API。</h4>

<p align="center">
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-0.116+-009688?logo=fastapi" alt="FastAPI"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12+-3776AB?logo=python" alt="Python"></a>
  <a href="https://www.sqlalchemy.org/"><img src="https://img.shields.io/badge/SQLAlchemy-2.0+-D71F00?logo=sqlalchemy" alt="SQLAlchemy"></a>
</p>

<p align="center">
  <a href="#-简介">简介</a> •
  <a href="#-特性">特性</a> •
  <a href="#-快速开始">快速开始</a> •
  <a href="#-配置">配置</a> •
  <a href="#-技术栈">技术栈</a> •
  <a href="#-许可证">许可证</a>
</p>

## 📖 简介

Cloudrave，基于 Python + FastAPI 构建，提供文件存储、用户认证、分享、回收站、在线预览等核心能力的 RESTful API。

后端采用异步 SQLAlchemy ORM，支持 SQLite、PostgreSQL 和 MySQL 多种数据库；存储层支持本地文件系统与腾讯云 COS，并可通过适配器模式扩展更多云存储后端。

## ✨ 特性

- 🔐 支持 JWT 访问令牌 + Refresh Token、TOTP 双因素认证与 Passkey/WebAuthn 登录
- 👤 多用户、用户组与基于角色的权限控制（RBAC）
- ☁️ 支持本地文件系统、腾讯云 COS 等多种存储策略
- 📁 文件浏览、目录管理、分片上传、断点续传、并行上传
- 🔗 文件与目录分享链接，支持密码与过期时间
- 🗑️ 回收站机制，支持恢复与自动清理
- 🖼️ 视频、音频、图片、PDF、Office、文本等在线预览（视频海报生成依赖 FFmpeg）
- 📦 压缩包在线预览、多文件打包下载
- 🧩 管理后台 API：用户管理、存储策略、挂载管理、系统设置
- 📝 结构化日志、请求追踪、速率限制与安全响应头

## 🚀 快速开始

### 环境要求

- Python >= 3.12
- 数据库：SQLite / PostgreSQL / MySQL（任选其一，默认 SQLite）
- 如需生成视频海报，请安装 FFmpeg 并确保在系统 PATH 中

### 安装依赖

```bash
cd Cloudrave
pip install -e ".[dev]"
```

### 启动服务

```bash
python main.py
```

默认监听 `0.0.0.0:1309`，API 文档可在开发环境访问：

- Swagger UI: http://127.0.0.1:1309/api/docs
- ReDoc: http://127.0.0.1:1309/api/redoc

生产环境下 `/api/docs` 会自动禁用。

## ⚙️ 配置

首次启动时会自动在项目目录下创建 `config.ini`，主要配置项如下：

```ini
[app]
name = Cloudrave
env = development
host = 0.0.0.0
port = 1309
cors_origins = http://localhost:9130,http://127.0.0.1:9130
local_upload_root = upload
preview_cache_root = data/preview-cache

[database]
backend = sqlite
sqlite_path = data/cloudrave.db
pool_size = 10
max_overflow = 20
pool_pre_ping = true

[auth]
jwt_secret = change-me-cloudrave-jwt-secret-at-least-32-bytes
access_token_ttl_minutes = 15
refresh_token_ttl_days = 30

[logging]
level = INFO
use_color = true
```

生产部署前请务必修改 `jwt_secret`、`totp_secret_key`、`backup_codes_key`、`share_signing_key` 等敏感密钥。

## 📦 构建与部署

### 全栈模式（前后端同一端口）

Cloudrave 后端可以嵌入前端编译产物，作为统一服务运行：

1. 构建前端并自动复制到后端 `assets/` 目录：

```bash
# 在项目根目录执行
python build-fullstack.py
```

该脚本会依次执行：
- `pnpm install --frozen-lockfile`
- `pnpm build`
- 将 `Frontend/dist/` 复制到 `Cloudrave/assets/`

2. 启动后端：

```bash
cd Cloudrave
python main.py
```

访问 `http://localhost:1309/` 即可同时访问前端与 API。

如果 `Cloudrave/assets/` 为空（只有 `.gitkeep`），后端将仅作为 API 服务运行。

### 构建独立可执行文件

#### 本地打包（当前平台）

```bash
cd Cloudrave
pip install -e ".[build]"
python build_binary.py
```

输出为单文件可执行程序：

- Windows：`dist/cloudrave.exe`
- Linux / macOS：`dist/cloudrave`

首次运行后，可执行文件同级目录会自动生成 `config.ini`、数据库与上传目录，这些文件会持久化保留。

#### 一步完成前端嵌入 + 二进制打包

```bash
python build-fullstack.py --binary
```

#### 多平台 CI 打包

仓库包含 [`.github/workflows/build-binaries.yml`](.github/workflows/build-binaries.yml)，推送 `v*` 标签或手动触发后，会在 Ubuntu、Windows、macOS 三个环境分别编译并发布到 GitHub Release。

```bash
git tag v1.0.0
git push origin v1.0.0
```

## 🛠️ 技术栈

- [Python 3.12+](https://www.python.org/) + [FastAPI](https://fastapi.tiangolo.com/) + [Uvicorn](https://www.uvicorn.org/)
- [SQLAlchemy 2.0](https://www.sqlalchemy.org/) + [Alembic](https://alembic.sqlalchemy.org/) 异步数据库访问与迁移
- [Pydantic](https://docs.pydantic.dev/) 数据校验与配置管理
- [Argon2](https://argon2-cffi.readthedocs.io/) 密码哈希 + [PyJWT](https://pyjwt.readthedocs.io/) 令牌
- [PyOTP](https://pyotp.readthedocs.io/) TOTP 双因素认证 + [webauthn](https://github.com/duo-labs/py_webauthn) Passkey
- 前端：[React 19](https://react.dev/) + [Vite](https://vitejs.dev/) + [Tailwind CSS](https://tailwindcss.com/)

## 📜 许可证

MIT
