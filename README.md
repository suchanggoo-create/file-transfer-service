# Large File Transfer Service (Android-friendly)

一个用于 **上传/下载超大文件或目录** 的服务端，支持：

- 断点续传式分片上传（单文件可达 100G+）
- 目录结构保持不变（客户端按相对路径逐文件上传即可）
- 文件 Range 下载（便于断点续传/下载器）
- 目录打包下载（`tar` 流式输出，不落整包到磁盘）
- 浏览已上传文件/目录树

## Quickstart

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 可选：指定存储根目录（默认 ./data）
export STORAGE_ROOT="$(pwd)/data"

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

打开 `http://localhost:8000/docs` 查看 Swagger。

## API 概览

- `GET /api/browse?path=` 浏览目录（path 为空表示根）
- `POST /api/uploads` 创建上传任务（返回 upload_id）
- `PUT /api/uploads/{upload_id}?offset=` 上传分片（二进制 body）
- `GET /api/uploads/{upload_id}` 查看上传进度
- `POST /api/uploads/{upload_id}/complete` 完成并落盘为最终文件
- `GET /api/download/file?path=` 下载单文件（支持 `Range`）
- `GET /api/download/dir?path=` 下载目录（`tar` 流式）

## 目录上传怎么做

服务端不需要“整目录”作为一个请求来上传。客户端遍历目录树，把每个文件用其相对路径上传即可，例如：

- `photos/2026/01/a.jpg`
- `photos/2026/01/b.jpg`

上传完成后服务器会在存储根目录下生成同样的目录结构。

## 重要约束/建议

- 客户端分片大小建议：8MB~64MB（根据网络与内存调优）
- 分片上传接口要求 **顺序追加**（offset 必须等于服务器当前已接收字节数）
- `path` 参数会做安全校验，禁止 `..` 路径穿越

## Tests

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

测试设计说明见 [`docs/integration-test-plan.md`](docs/integration-test-plan.md)。

