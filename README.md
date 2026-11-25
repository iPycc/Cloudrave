<h1 align="center">
  <br>
  <a href="https://github.com/ipycc/cloudrave" alt="logo"><img src="https://raw.githubusercontent.com/iPycc/frontend/00aca676ea29ef3186bd330e670df7e7aafaf7b5/public/cloudrave-logo.svg" width="100"/></a>
  <br>
  Cloudrave
  <br>
</h1>

<div align="center"><img src="./Cloudrave.png" width="1200"></img></div>
<h4 align="center">能接公有云和主流网盘的自托管个人与团队云盘。</h4>

<p align="center">
  <a href="https://fastapi.tiangolo.com/"><img src="https://img.shields.io/badge/FastAPI-0.116+-009688?logo=fastapi" alt="FastAPI"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.12+-3776AB?logo=python" alt="Python"></a>
  <a href="https://www.sqlalchemy.org/"><img src="https://img.shields.io/badge/SQLAlchemy-2.0+-D71F00?logo=sqlalchemy" alt="SQLAlchemy"></a>
  <a href="https://github.com/iPycc/Cloudrave/releases"><img src="https://img.shields.io/badge/release-v1.0.0--beta.5-blue" alt="v1.0.0-beta.5"></a>
</p>

<p align="center">
  <a href="#-cloudrave-是什么">简介</a> •
  <a href="#-特性">特性</a> •
  <a href="#-快速上手">快速上手</a> •
  <a href="#-构建与打包">构建</a> •
  <a href="#️-技术栈后端">技术栈</a> •
  <a href="#-许可证">许可证</a>
</p>

## 📖 Cloudrave 是什么？

Cloudrave 是基于 Python 3.12+ 与 FastAPI 构建的异步多存储云盘与虚拟文件系统服务。

在底层设计上，Cloudrave 采用**元数据与物理存储解耦**的架构：通过 SQLAlchemy 2.0 异步 ORM 维护统一的虚拟文件系统（VFS）目录树、权限上下文与事务状态（支持 SQLite、MySQL 与 PostgreSQL 跨库在线迁移），同时通过标准化存储驱动层将本地磁盘、对象存储（腾讯云 COS、阿里云 OSS）及运营商网盘（139 移动、189 天翼、联通云盘）抽象为统一的异步读写与分片传输接口。数据链路默认优先协商客户端直传，并在跨域或网络受限时自动降级为后端流式分片中转，兼顾高吞吐与多存储后端的一致体验。


## ✨ 特性

- **存储挂载**：本地文件系统、腾讯云 COS、阿里云 OSS、139 移动云盘、189 天翼云盘、联通云盘；支持多路径挂载与容量显示。
- **文件管理**：目录浏览、搜索、创建、重命名、移动、复制、多文件打包下载、回收站恢复与彻底删除。
- **上传与下载**：分片并行上传、断点续传、秒传（重复文件检测）；公有云与运营商网盘优先浏览器直传，受限时自动回退后端中转。
- **预览与编辑**：图片（含 HEIC）、音视频、PDF、Office、文本和压缩包（ZIP/7z）在线预览；支持文本/Markdown 编辑保存、PDF 注释与图片另存为。
- **外链分享**：单文件或目录分享链接，支持提取密码、有效期限制、访客预览与下载。
- **账号与权限**：JWT 认证、Passkey 通行密钥、TOTP 双因素认证、GitHub/Google/QQ 第三方登录；支持管理员、用户与访客权限控制。
- **管理与运维**：站点设置、存储策略分配、数据库在线测试与热迁移、安全审计日志与请求速率限制。

## 🚀 快速上手

### ⚡ 方式一：Releases 发行版

从 [GitHub Releases](https://github.com/iPycc/Cloudrave/releases) 下载对应系统的单文件可执行程序，直接运行即可（已内置前端界面，默认使用 SQLite）：

```bash
./cloudrave        # Linux / macOS
.\cloudrave.exe    # Windows
```

启动后浏览器访问 `http://localhost:1309`。首次运行会在同级目录自动生成 `config.ini`、`data/` 和 `upload/` 目录。

### 🐳 方式二：Docker Compose（推荐服务端部署）

在同时包含 `Cloudrave/` 和 `Frontend/` 的工作区根目录下运行：

```bash
docker compose up -d --build
```

启动后访问 `http://localhost:1309`（默认包含 Cloudrave 与 MySQL 容器，并内置 `ffmpeg` 用于生成视频缩略图）。

> 生产环境对外开放前，请记得修改 `docker-compose.yml` 中的数据库密码及 `config.ini` 中的安全密钥。

### 💻 方式三：从源码运行（本地开发）

**1. 启动后端**（需 Python >= 3.12，可选安装 FFmpeg）：

```bash
cd Cloudrave
pip install -e ".[dev]"
python main.py
```

后端默认运行在 `http://127.0.0.1:1309`（开发环境 API 文档：`/api/docs`）。

**2. 启动前端**（前后端分离调试时使用，需 Node.js 与 pnpm）：

```bash
cd Frontend
pnpm install
pnpm dev
```

前端开发服务运行在 `http://localhost:9130`，会自动将 `/api` 请求代理至后端。

## 📦 构建与打包

### 🧩 1. 构建全栈资源（前后端合一，单端口运行）

在工作区根目录执行脚本，会自动编译前端并将产物放入 `Cloudrave/assets/`：

```bash
python build-fullstack.py
```

完成后直接运行 `python Cloudrave/main.py`，访问 `http://localhost:1309` 即可同时使用前端页面与后端 API。

### 🔨 2. 打包为单文件可执行程序

在工作区根目录一键完成「前端编译 + 嵌入后端 + PyInstaller 二进制打包」：

```bash
pip install -e "./Cloudrave[build]"
python build-fullstack.py --binary
```

打包产物位于 `Cloudrave/dist/`（Windows 下为 `cloudrave.exe`，Linux / macOS 下为 `cloudrave`）。

## 🛠️ 技术栈（后端）

- **核心框架**：Python 3.12+ / FastAPI / Uvicorn / Pydantic v2
## 📜 许可证

MIT
