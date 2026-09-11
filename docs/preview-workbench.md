# Cloudrave 在线预览工作台

本轮升级把原先按文件类型分散的预览器收敛到同一个预览窗口，并保持桌面端与移动端能力一致。桌面端可针对大屏优化布局，移动端只改变工具栏、缩略图和侧栏的排列方式，不裁剪功能。

## 文件类型与能力

| 类型 | 查看 | 轻编辑 | 保存策略 |
| --- | --- | --- | --- |
| PDF | 缩略图、翻页、搜索、缩放、旋转 | 高亮、画笔、文字、矩形 | 私人 sidecar，自动保存，不修改 PDF |
| Markdown | 渲染预览 | Milkdown 所见即所得、源码编辑 | UTF-8 覆盖原文本，使用版本号防冲突 |
| 代码/文本 | 语法高亮、搜索 | 桌面 Monaco、移动 CodeMirror 6 | UTF-8 覆盖原文本，使用版本号防冲突 |
| 图片 | 缩放、平移、旋转、1:1、EXIF 摘要 | 裁剪、旋转、翻转、画笔、矩形、箭头、文字 | 始终另存为新 PNG，保留原图 |
| HEIC/HEIF | 服务端转 WebP 后预览 | 与普通图片一致 | 另存为新 PNG |
| ZIP/7Z/TAR | 树形浏览、条目预览、分页加载 | ZIP/7Z 全部或按条目解压 | 创建后台解压任务 |

Live Photo 和其他动图联动不在本次范围内。

## 后端契约

- `GET /api/v1/explorer/preview/{node_id}/manifest`：返回渲染器、资源、元数据和能力列表。
- `GET|PUT /api/v1/explorer/preview/{node_id}/annotations`：读取或更新当前用户的 PDF 私人标注。更新必须携带 `If-Match` 标注修订号。
- `POST /api/v1/explorer/preview/{node_id}/image/save-as`：接收编辑后的图片并在原目录创建新文件。
- `GET /api/v1/explorer/preview/{node_id}/archive/entries`：分页读取压缩包目录。
- `GET /api/v1/explorer/preview/{node_id}/archive/entry`：只读取一个安全条目用于在线查看。
- `POST /api/v1/explorer/archive/{node_id}/extract`：`paths` 为空时全部解压，否则只解压选中的文件或目录。

PDF 标注存储在 `preview_annotations` 表中，以 `(owner_id, node_id)` 唯一约束隔离用户。`source_version` 绑定源文件版本，`revision` 提供乐观并发控制。公开分享清单会移除编辑、解压和私人标注能力。

## 安全边界

- PDF 标注和图片编辑都不会覆写原始二进制文件。
- EXIF 只返回相机、镜头和曝光摘要；GPS 不进入预览清单和公开分享。
- 压缩包路径拒绝空段、`.` 和 `..`，并限制条目数、总展开体积、单文件体积和压缩率。
- 7Z 条目预览与解压拒绝符号链接。
- 文本编辑和 PDF 标注都使用版本或修订号避免静默覆盖并发修改。
- 图片另存接口重新验证图片内容、类型、输出名和 64 MB 上限。

## 部署依赖

Python 依赖新增 `pillow-heif` 和 `py7zr`。容器镜像安装 `ffmpeg` 与 Noto CJK 字体，以保证媒体转码和中文衍生图一致。前端编辑器按需拆包：只有打开对应编辑器时才加载 Monaco、CodeMirror、Milkdown 或 Fabric.js。
