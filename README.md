# Seedream Photo Workflow · 自动照片后期

一个基于 Python 和豆包 Seedream 图像编辑 API 的本地照片后期工作台。把按照片类别选择风格、试处理、批量生成、成片保存与前后对比串成可重复使用的流程。

它面向户外摄影的一组原片：远景强调秋叶与空间层次，近景强调金黄橙红和背景层次，写真强调自然肤色与明亮光线，写实强调人物与现场故事感。其它文件夹采用自然写实策略。

## 使用流程

```text
原片目录 → 扫描与分类 → 方向校正 / 去除 EXIF → Seedream 图像编辑
                                              ↓
保留原片 ← 成片保存至同目录「后期」← 校验尺寸与比例
                         ↓
                前后对比 / 提示词 / 批次记录
```

- 按最近一层的「远景 / 近景 / 写真 / 写实」文件夹选择编辑策略，也可全局选择自然写实、秋日暖色、明亮通透。
- 先做四类抽样或两张近景测试，查看结果后再处理全部；可选择单张照片。
- 1K / 1.5K / 2K 输出，按原片比例请求；1–8 张并发，默认 3 张。
- 内容与配置共同决定缓存键；重复照片复用生成结果，并用文件锁防止并发重复生成。
- 显式 429 限流退避重试；已生成但下载失败时重试下载已有结果。超时和 5xx 不自动重新发起生成。
- 取消停止尚未开始的照片，已发出的请求继续完成并保存成功成片。
- 保留原片，检查现有同名成片的归属，避免覆盖非本工作流创建或已手动改动的文件。
- 浏览器提供前后对比、编辑提示词和错误诊断；每批生成可离线打开的对比页。

## 快速开始

需要 **Python 3.10+**，适用于 **macOS / Linux**（使用 POSIX 文件锁）；macOS 另提供双击启动脚本。Windows 原生环境尚不支持 `fcntl`。

```sh
git clone https://github.com/JaneHuang1833/seedream-photo-workflow.git
cd seedream-photo-workflow
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m photo_workflow.server
```

打开 <http://127.0.0.1:8765/>，在配置区填写原片目录、自己的火山方舟 API Key 和已开通的模型 ID。默认模型为 `doubao-seedream-5-0-pro-260628`；当前实现使用该模型，不能任意切换其它模型。

配置可由工作台保存到本机 `.env`，或复制 `.env.example` 后编辑。也支持环境变量 `ARK_API_KEY`、`DOUBAO_MODEL`、`PHOTO_INPUT_DIR`；本机 `.env` 中的非空值优先。请先运行小测试，再决定是否处理全部。

macOS 可双击 `启动照片后期.command`，启动脚本会优先使用 `.venv`，无可用运行时时自动创建虚拟环境并安装依赖。

### 命令行

```sh
# 只检查目录，不调用生成 API
python -m photo_workflow.cli --inspect --input /path/to/photos
# 四类抽样测试
python -m photo_workflow.cli --input /path/to/photos --style folders --size 2K --concurrency 3
# 近景测试 / 全部处理
python -m photo_workflow.cli --input /path/to/photos --near-test
python -m photo_workflow.cli --input /path/to/photos --all
```

支持 JPEG、PNG、WebP、TIFF、BMP；RAW / HEIC 请先导出 JPEG。扫描跳过「后期」目录。详细操作见 [使用说明](使用说明.md)。

## 输出与配置

| 位置 | 内容 |
| --- | --- |
| 原片目录 / 后期 | `原名_后期.jpg` 交付成片 |
| `outputs/批次ID/` | 对比页、前后图、`recipe.json`、`manifest.json` |
| `outputs/.seedream-cache/` | 缓存与接口响应检查点 |
| `.env` | 本机密钥、模型及照片目录 |
| `logs/` | 本机启动日志 |

上述运行数据和真实 `.env` 已被 Git 忽略；仓库只包含源码、依赖声明、启动脚本和文档，没有个人照片或生成示例。

## 实现

Python 标准库提供本地 HTTP 服务、线程池、缓存和原子写入；Pillow / NumPy 处理图片，certifi 提供证书。原生 HTML / CSS / JavaScript 工作台直接访问本机服务，无前端构建步骤。

`pipeline.py` 负责扫描、分类、批次与缓存，`seedream.py` 负责模型请求、响应恢复及下载，`imaging.py` 提供图片处理，`server.py` / `cli.py` 为交互入口。

## 数据与效果边界

服务仅绑定 `127.0.0.1`。密钥只保存在本机，浏览器不回显、不放入 localStorage；提交给模型的图片已去除 EXIF / GPS，最长边不超过 4096 px。照片内容会上传至火山方舟完成编辑，API 调用会产生费用。

Seedream 使用图生图重新生成成片，不是仅调整原有像素；人物、衣物、文字和背景细节可能改变，需要查看前后对比后人工选择。2K 为约 400 万像素的输出档位，不表示保留原分辨率。此工具为本地工作流，不提供公开在线修图服务。

已有人工测试记录见 [测试记录](测试记录.md)，属于历史运行记录，不是自动化测试套件或跨平台兼容性承诺。

## English

A local Python workbench for batch photo editing with the Doubao Seedream API. Folder-specific prompts, small sample runs, configurable concurrency, content-based caching, result recovery and before/after comparisons connect photo editing into a repeatable workflow. Originals are preserved; generated images require manual review. Run locally on macOS or Linux with your own API key. No personal images or credentials are included in this repository.
