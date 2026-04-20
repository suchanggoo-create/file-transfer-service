# `transfer.sh` 使用说明

这个脚本用于在 **客户端设备（Android/macOS/Linux）** 上通过 HTTP 直接与本项目的服务端通信，完成：

- **上传单个文件**（分片上传，带百分比进度）
- **下载整个目录**（目录/文件覆盖写入本地，带进度：已完成/剩余数量 + 当前文件百分比）
- **可选路径替换**（仅替换目录路径部分，不替换文件名）
- **可选自动创建目录**（`--mkdir`）

> 传输路径与数据流量都发生在 “设备 ↔ 服务端” 之间；PC 只负责触发脚本（例如 `adb shell`）。

---

## 依赖

- 必需：`sh`、`curl`
- 下载额外需要：`tar`、`dd`、`tee`、`wc`
- 可选：`pv`（若存在则用于显示更精细的当前文件进度；没有也能工作）

Android 常见 toybox/busybox 环境通常具备上述工具。

---

## 通用参数

脚本入口是单命令形式：

```sh
sh transfer.sh --type <upload|download> [options...]
```

常用参数：

- `--type upload|download`：必填
- `--server <url>`：服务端地址，例如 `http://192.168.1.10:8000`
- `--game <name>`：游戏名（服务端目录的第一层）
- `--account <name>`：账号名（服务端目录的第二层）
- `--chunk-size <bytes>`：上传分片大小（默认 4MB）

---

## 上传（upload）

### 上传单个文件

```sh
sh transfer.sh \
  --type upload \
  --server http://127.0.0.1:8000 \
  --game star_rail \
  --account gamer1 \
  --file /sdcard/Android/data/com.xxx/files/save/data.bin
```

行为说明：

- `--file` **必须是绝对路径**，并且文件必须存在
- 服务端落盘路径为：

```
<game>/<account>/<absolute_path>
```

例如上述示例会写到：

```
star_rail/gamer1//sdcard/Android/data/com.xxx/files/save/data.bin
```

脚本会打印类似：

```
Uploading: 52% (54525952/104857600 bytes)  /path/to/file
```

---

### 批量上传（find + xargs）

macOS/Linux 示例（筛选 `.md` 批量上传）：

```sh
find /Users/sunmeng/file_trans/ -type f -name '*.md' -print0 \
  | xargs -0 -n 1 sh -c \
    'sh transfer.sh --type upload --server http://127.0.0.1:8000 --game star_rail --account gamer1 --file "$1"' _
```

> Android 上如果 `xargs -0` 不可用，可换成不含空格路径的方案，或先验证 `xargs` 支持情况。

---

## 下载（download）

下载会从服务端目录：

```
<game>/<account>
```

拉取 `tar`，然后把成员写到本地的绝对路径：

```
/<rel_path>
```

### 严格模式（不自动建目录）

```sh
sh transfer.sh --type download --server http://127.0.0.1:8000 --game star_rail --account gamer1
```

如果本地目标目录不存在（例如需要写 `/Users/...` 但目录不存在），会 **FAILED** 并提示使用 `--mkdir`。

### 自动创建目录（推荐）

```sh
sh transfer.sh --type download --server http://127.0.0.1:8000 --game star_rail --account gamer1 --mkdir
```

### 路径替换再覆盖写入

只替换 **目录路径部分**（不会替换文件名中出现的 `from` 字符串）。

```sh
sh transfer.sh \
  --type download \
  --server http://127.0.0.1:8000 \
  --game star_rail \
  --account gamer1 \
  --mkdir \
  --from 'Users/sunmeng' \
  --to 'sdcard/restore'
```

替换命中规则：

- 允许 0 次或 1 次命中
- 若某个条目的目录部分命中次数 **> 1**，会报错退出并打印详细信息

---

## 进度输出说明

- **上传**：分片上传后计算百分比并刷新进度行
- **下载**：
  - 会显示 `done/total` 和 `remaining`
  - 目录项也会计入进度
  - 当前文件会显示百分比（无 `pv` 也能实时显示）

进度输出写到 **stderr**，方便你把 stdout 另作处理（例如仅收集最终列表）。

---

## 故障排查

- `Unknown flag: ...`：检查参数位置/拼写，或先运行 `sh transfer.sh --help`
- `Permission denied` / `Read-only file system`：本地目标路径可能不可写（尤其是写入 `/<...>` 这种绝对路径时）
- 中文文件名显示为 `\351...`：脚本已做解码处理；若仍异常，请贴出 `tar -tf` 的输出与错误日志

