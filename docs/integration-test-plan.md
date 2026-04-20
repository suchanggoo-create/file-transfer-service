# 上传/下载（文件与目录）集成测试计划

## 目标

在**不依赖大文件**的前提下，用自动化测试覆盖：

- **单文件**：创建上传任务 → 分片 `PUT`（可一次写完）→ `complete` → `GET /api/download/file` 内容与原始一致。
- **多文件目录**：按相对路径上传至少 3 个文件（含一层子目录）→ `GET /api/browse` 验证列表 → `GET /api/download/dir` 拉取 **tar** 流并在内存中解析，校验成员名与文件内容。

## 实现要点（对齐现有 API）

现有行为见 `app/main.py`：

- 上传：`POST /api/uploads`（JSON：`path`, `total_size`）→ `PUT /api/uploads/{upload_id}?offset=`（body 为原始字节，**offset 必须等于当前已接收长度**）→ `POST /api/uploads/{upload_id}/complete`。
- 下载文件：`GET /api/download/file?path=...`。
- 下载目录：`GET /api/download/dir?path=...` 返回 **流式 tar**（`application/x-tar`）。
- 浏览：`GET /api/browse?path=`（空为根）。

存储根目录由 `app/storage.py` 的 `storage_root()` 决定：

- 若设置环境变量 `STORAGE_ROOT`，则写入该目录
- 否则默认写入 `Path.cwd() / "data"`

## 测试文件与依赖

| 项目 | 说明 |
|------|------|
| `tests/conftest.py` | 提供 `TestClient(app)` fixture；（当前实现）使用默认存储目录 `./data` 并保留测试产物 |
| `tests/test_transfer.py` | 两个测试函数：单文件上传/下载；目录上传/浏览/目录 tar 下载 |
| `requirements-dev.txt` | `-r requirements.txt` + `pytest` + `httpx` |

## 单文件测试步骤（逻辑）

1. 准备 `content = b"hello..."`（几十字节即可）。
2. `POST /api/uploads`，`path="single/hello.bin"`，`total_size=len(content)`。
3. `PUT /api/uploads/{upload_id}?offset=0`，body=`content`。
4. `POST /api/uploads/{upload_id}/complete`，期望 200。
5. `GET /api/download/file?path=single/hello.bin`，断言下载内容与 `content` 一致。

可选：`GET /api/browse?path=single` 断言目录下包含目标文件。

## 目录测试步骤（逻辑）

相对路径示例（保持目录结构）：

- `mydir/readme.txt`
- `mydir/a.txt`
- `mydir/sub/b.txt`

对每个文件重复「创建任务 → `PUT` offset=0 → `complete`」。

然后：

1. `GET /api/browse?path=mydir`：应出现 `readme.txt`、`a.txt`、子目录 `sub`（`type=="dir"`）。
2. `GET /api/browse?path=mydir/sub`：应出现 `b.txt`。
3. `GET /api/download/dir?path=mydir`：将响应体作为 tar 解析；对每个成员文件读取内容并与上传内容比对。

注意：tar 内路径会以服务端 `arcname`（目录名）为根，例如 `mydir/readme.txt`。

## 模拟大文件/包含大文件目录测试（建议新增）

目标：在不引入真实超大文件（100G）的情况下，用 **2MB** 级别的文件模拟更接近真实场景的传输与落盘行为，并覆盖“目录中包含大文件”的情况。

### 用例 A：2MB 单文件分片上传/下载

- **文件大小**：2MB（\(2 * 1024 * 1024\) bytes）
- **内容生成**：建议使用确定性内容，便于断言
  - 例如：`hashlib.sha256` 校验，或用固定 seed 生成伪随机字节
- **分片策略**：建议显式分片（而不是一次性 PUT）
  - 例如：chunk_size=256KB，总共 8 片

步骤（逻辑）：

1. 准备 `content`（2MB bytes），`path="large/sim_2m.bin"`，`total_size=len(content)`。
2. `POST /api/uploads` 创建任务。
3. 循环分片上传：
   - 第 1 片：`PUT ...?offset=0` body=`content[0:chunk_size]`
   - 第 2 片：`PUT ...?offset=chunk_size` body=下一片
   - ……
   - 每次上传后可断言返回 JSON 的 `received` 递增，且等于 `offset + len(chunk)`。
4. `POST /api/uploads/{upload_id}/complete` 完成上传。
5. `GET /api/download/file?path=large/sim_2m.bin` 下载并断言内容一致（或断言 sha256 一致）。

可选增强：

- 增加一个 **offset 冲突** 的负例（例如故意传错 offset，期望 409 并返回 expected_offset）。

### 用例 B：目录包含 2MB 文件 + 多小文件，打包下载校验

目录结构示例：

- `bigdir/readme.txt`（小）
- `bigdir/small/a.txt`（小）
- `bigdir/large/sim_2m.bin`（2MB）

步骤（逻辑）：

1. 逐文件上传（其中 2MB 文件使用“用例 A”的分片策略上传）。
2. `GET /api/browse?path=bigdir` 与 `GET /api/browse?path=bigdir/large` 验证目录项存在（至少能看到 `sim_2m.bin`）。
3. `GET /api/download/dir?path=bigdir` 拉取 tar：
   - 解析 tar 成员名应包含 `bigdir/large/sim_2m.bin`
   - 从 tar 中读取 `sim_2m.bin` 的内容或 sha256，与原始一致

## 运行方式

```bash
source .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q
```

## 风险与范围

- **不测**：100G 级别文件、并发、网络中断恢复（可后续扩展）。
- 目录下载测试会把 tar **完整读入内存**；对小目录可接受。

