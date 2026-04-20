#!/system/bin/sh
set -eu

SERVER_DEFAULT="${SERVER:-http://127.0.0.1:8000}"
GAME_DEFAULT="${GAME:-yuanshen}"
ACCOUNT_DEFAULT="${ACCOUNT:-gamer_name}"
CHUNK_SIZE_DEFAULT="${CHUNK_SIZE:-4194304}"

SERVER="$SERVER_DEFAULT"
GAME="$GAME_DEFAULT"
ACCOUNT="$ACCOUNT_DEFAULT"
CHUNK_SIZE="$CHUNK_SIZE_DEFAULT"

json_escape() {
  # Very small JSON string escaper (paths expected to be single-line).
  # Keep it portable across BSD/macOS sed, toybox/busybox sed.
  printf "%s" "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

file_size() {
  # prefer stat, fallback wc
  if stat -c %s "$1" >/dev/null 2>&1; then
    stat -c %s "$1"
  else
    wc -c < "$1" | tr -d ' '
  fi
}

escape_sed() {
  # Escape string for use in sed s#FROM#TO#g where delimiter is '#'
  # Escapes: backslash, ampersand, delimiter '#'
  printf "%s" "$1" | sed -e 's/[\\&#]/\\&/g'
}

count_substr() {
  # Count literal occurrences of NEEDLE in HAYSTACK (no regex).
  # Usage: count_substr "hay" "needle"
  hay="$1"
  needle="$2"
  if [ -z "$needle" ]; then
    echo 0
    return 0
  fi
  count=0
  rest="$hay"
  while :; do
    case "$rest" in
      *"$needle"*)
        count=$((count + 1))
        rest="${rest#*"$needle"}"
        ;;
      *)
        break
        ;;
    esac
  done
  echo "$count"
}

print_progress_line() {
  # Print progress to stderr as a single updating line.
  # Usage: print_progress_line "text"
  printf "\r%s" "$1" >&2
}

finish_progress_line() {
  # Finish the progress line with newline.
  printf "\n" >&2
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

tar_list() {
  # List tar members, preferring raw (no escape) if supported.
  tarfile="$1"
  if tar --raw -tf "$tarfile" >/dev/null 2>&1; then
    tar --raw -tf "$tarfile"
  else
    tar -tf "$tarfile"
  fi
}

tar_list_verbose() {
  # Verbose list for size mapping; prefers raw if supported.
  tarfile="$1"
  if tar --raw -tvf "$tarfile" >/dev/null 2>&1; then
    tar --raw -tvf "$tarfile"
  else
    tar -tvf "$tarfile"
  fi
}

tar_extract_stdout() {
  # Extract one member to stdout; prefers raw if supported.
  tarfile="$1"
  member="$2"
  if tar --raw -xOf "$tarfile" "$member" >/dev/null 2>&1; then
    tar --raw -xOf "$tarfile" "$member"
  else
    tar -xOf "$tarfile" "$member"
  fi
}

create_upload() {
  rel_path="$1"
  total_size="$2"

  jpath="$(json_escape "$rel_path")"
  body="{\"path\":\"$jpath\",\"total_size\":$total_size,\"overwrite\":true}"

  resp="$(curl -sS -X POST "$SERVER/api/uploads" \
    -H "Content-Type: application/json" \
    -d "$body")"

  upload_id="$(echo "$resp" | sed -n 's/.*"upload_id"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  if [ -z "$upload_id" ]; then
    echo "Failed to parse upload_id. Response: $resp" >&2
    exit 1
  fi
  echo "$upload_id"
}

print_help() {
  cat <<EOF
Usage:
  transfer.sh [global_options] <command> [command_options]

Global options (apply to all commands):
  --server <url>         Default: $SERVER_DEFAULT (or env SERVER)
  --game <name>          Default: $GAME_DEFAULT (or env GAME)
  --account <name>       Default: $ACCOUNT_DEFAULT (or env ACCOUNT)
  --chunk-size <bytes>   Default: $CHUNK_SIZE_DEFAULT (or env CHUNK_SIZE)
  -h, --help             Show this help

Commands:
  upload_file <absolute_local_path>
    Upload a single file (absolute path required).
    Server target path will be: <game>/<account>/<absolute_path>

  # Download a whole server directory and overwrite local absolute paths.
  # Mapping rule:
  #   server: <server_dir_rel>/<rel_path>
  #   local : /<rel_path>
  #
  # Default: directory must already exist; otherwise FAIL (prints hint for --mkdir).
  download_dir <server_dir_rel> [--mkdir] [--from <str> --to <str>]

Examples:
  # Upload
  transfer.sh --server http://192.168.1.10:8000 --game yuanshen --account gamer_name \
    upload_file /sdcard/Android/data/com.xxx/files/save/data.bin

  # Upload multiple files (batch upload with find + xargs)
  find /Users/sunmeng/file_trans/ -type f -name '*.md' -print0 \
    | xargs -0 -n 1 sh transfer.sh --account game_name1 --game star_rail upload_file

  # Download (strict: do not create dirs)
  transfer.sh --server http://192.168.1.10:8000 download_dir yuanshen/gamer_name

  # Download and auto-create missing dirs
  transfer.sh --server http://192.168.1.10:8000 download_dir yuanshen/gamer_name --mkdir

  # Download with path replacement before writing locally (overwrite)
  # Example: server rel_path 'Users/sunmeng/...' becomes local '/sdcard/restore/...'
  transfer.sh --server http://192.168.1.10:8000 download_dir yuanshen/gamer_name --mkdir \
    --from 'Users/sunmeng' --to 'sdcard/restore'

Notes:
  - Download prints 'Downloaded local paths:' with paths actually written/created this run.
  - On failure, it also prints 'Undownloaded server paths:'.
EOF
}

upload_file() {
  # usage:
  #   upload_file /sdcard/path/to/file
  local_path="$1"

  case "$local_path" in
    /*) ;;
    *)
      echo "Path must be absolute: $local_path" >&2
      exit 1
      ;;
  esac

  if [ ! -f "$local_path" ]; then
    echo "File not found: $local_path" >&2
    exit 1
  fi

  total="$(file_size "$local_path")"
  # Store under: yuanshen/gamer_name/<absolute_path>
  # Example: yuanshen/gamer_name//sdcard/Android/data/...
  server_rel="${GAME}/${ACCOUNT}${local_path}"

  upload_id="$(create_upload "$server_rel" "$total")"

  offset=0
  while [ "$offset" -lt "$total" ]; do
    remain=$((total - offset))
    if [ "$remain" -lt "$CHUNK_SIZE" ]; then
      bs="$remain"
    else
      bs="$CHUNK_SIZE"
    fi

    # 读一段 bytes 并直接 PUT 给服务端（offset 必须严格匹配）
    dd if="$local_path" bs=1 skip="$offset" count="$bs" 2>/dev/null \
      | curl -sS -X PUT "$SERVER/api/uploads/$upload_id?offset=$offset" \
          -H "Content-Type: application/octet-stream" \
          --data-binary @- >/dev/null

    offset=$((offset + bs))
    pct=$((offset * 100 / total))
    print_progress_line "Uploading: ${pct}% (${offset}/${total} bytes)  $local_path"
  done
  finish_progress_line

  curl -sS -X POST "$SERVER/api/uploads/$upload_id/complete" >/dev/null
  echo "OK upload: $local_path -> $server_rel"
}

download_dir_overwrite() {
  # usage:
  #   download_dir_overwrite server_dir_rel [--mkdir] [--from STR --to STR]
  #
  # 会把 tar 内的：
  #   yuanshen/gamer_name/xxx/yyy.txt
  # 覆盖写入到本地：
  #   /xxx/yyy.txt
  #
  # 若目标目录（如 /xxx）不存在，则默认失败退出；传入 --mkdir 时自动创建目录。
  # 无论成功或失败，都会输出已写入的本地路径列表；
  # 若失败，会额外输出未下载的服务端路径列表。
  server_dir_rel="$1"
  shift || true
  auto_mkdir=0
  from_str=""
  to_str=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --mkdir)
        auto_mkdir=1
        shift
        ;;
      --from)
        from_str="${2:-}"
        shift 2
        ;;
      --to)
        to_str="${2:-}"
        shift 2
        ;;
      -h|--help)
        print_help
        exit 0
        ;;
      *)
        echo "Unknown flag: $1" >&2
        echo "HINT: use --help" >&2
        status=1
        return 1
        ;;
    esac
  done

  if [ -n "$from_str" ] || [ -n "$to_str" ]; then
    if [ -z "$from_str" ] || [ -z "$to_str" ]; then
      echo "ERROR: --from and --to must be provided together." >&2
      status=1
      return 1
    fi
  fi

  # Pick a writable temp directory across Android/macOS/Linux.
  # - Android (adb shell): /data/local/tmp is usually writable.
  # - macOS/Linux: use $TMPDIR or /tmp.
  tmp_base="/data/local/tmp"
  if [ ! -d "$tmp_base" ] || [ ! -w "$tmp_base" ]; then
    tmp_base="${TMPDIR:-/tmp}"
  fi

  tar_path="$tmp_base/download_$$.tar"
  list_path="$tmp_base/download_list_$$.txt"
  vlist_path="$tmp_base/download_vlist_$$.txt"
  size_map_path="$tmp_base/download_size_map_$$.txt"
  downloaded_path="$tmp_base/downloaded_local_paths_$$.txt"
  pending_path="$tmp_base/pending_server_paths_$$.txt"
  status=0
  downloaded_count=0
  tar_root="${server_dir_rel##*/}"
  total_items=0
  done_items=0

  fail_with_pending_from_current() {
    msg="$1"
    echo "ERROR: $msg" >&2
    if [ "$auto_mkdir" -eq 0 ]; then
      echo "HINT: re-run with --mkdir to auto-create directories." >&2
    fi
    status=1
    echo "$normalized" > "$pending_path"
    while IFS= read -r rest; do
      n2=""
      case "$rest" in
        "$server_dir_rel"/*) n2="$rest" ;;
        "$tar_root"/*) n2="$server_dir_rel/${rest#${tar_root}/}" ;;
        *) n2="" ;;
      esac
      if [ -n "$n2" ]; then echo "$n2" >> "$pending_path"; fi
    done
    return 1
  }

  cleanup_and_report() {
    echo "Downloaded local paths:"
    if [ -f "$downloaded_path" ]; then
      cat "$downloaded_path"
    fi
    if [ "$status" -ne 0 ]; then
      echo "Undownloaded server paths:"
      if [ -f "$pending_path" ]; then
        cat "$pending_path"
      fi
      echo "FAILED"
    else
      echo "SUCCESS"
    fi
    rm -f "$tar_path" "$list_path" "$vlist_path" "$size_map_path" "$downloaded_path" "$pending_path" 2>/dev/null || true
  }
  trap cleanup_and_report EXIT

  # 用 curl 的 URL 编码避免 path 里有特殊字符
  if ! curl -sS --get "$SERVER/api/download/dir" --data-urlencode "path=$server_dir_rel" -o "$tar_path"; then
    status=1
    return 1
  fi

  if ! tar_list "$tar_path" > "$list_path"; then
    status=1
    return 1
  fi

  # Build a best-effort size map for per-file progress (if supported by tar output).
  # Assumes filenames do not contain spaces.
  if tar_list_verbose "$tar_path" > "$vlist_path" 2>/dev/null; then
    awk '
      function is_month(s) { return (s ~ /^[A-Z][a-z][a-z]$/) }
      {
        name=$NF
        size=""
        for (i=1; i<NF; i++) {
          if ($i ~ /^[0-9]+$/ && is_month($(i+1))) { size=$i; break }
        }
        if (size == "") size=0
        print name "\t" size
      }
    ' "$vlist_path" > "$size_map_path" 2>/dev/null || true
  fi

  # Compute total items (files + dirs) to report remaining count.
  while IFS= read -r n; do
    case "$n" in
      "$server_dir_rel"/*|"$tar_root"/*) total_items=$((total_items + 1)) ;;
    esac
  done < "$list_path"

  # Iterate entries in order; on first failure, record remaining as pending.
  while IFS= read -r name; do
    # Tar root is usually the last path segment (e.g. gamer_name/...)
    # Accept either:
    # - yuanshen/gamer_name/...
    # - gamer_name/...
    normalized=""
    case "$name" in
      "$server_dir_rel"/*) normalized="$name" ;;
      "$tar_root"/*) normalized="$server_dir_rel/${name#${tar_root}/}" ;;
      *) continue ;;
    esac

    rel="${normalized#${server_dir_rel}/}"
    # Path replacement applies to directory part only (do not touch filename).
    if [ -n "$from_str" ]; then
      from_esc="$(escape_sed "$from_str")"
      to_esc="$(escape_sed "$to_str")"

      case "$name" in
        */)
          # Directory entry: replace within the directory path itself.
          rel_no_slash="${rel%/}"
          hits="$(count_substr "$rel_no_slash" "$from_str")"
          if [ "$hits" -gt 1 ]; then
            fail_with_pending_from_current "path replace allows 0 or 1 hit in directory path, got $hits. server='$normalized' dir='$rel_no_slash' from='$from_str'"
          fi
          rel_no_slash="$(printf "%s" "$rel_no_slash" | sed -e "s#${from_esc}#${to_esc}#g")"
          rel="${rel_no_slash}/"
          ;;
        *)
          dir_part="$(dirname "$rel")"
          base_part="$(basename "$rel")"
          if [ "$dir_part" = "." ]; then
            dir_part=""
          else
            hits="$(count_substr "$dir_part" "$from_str")"
            if [ "$hits" -gt 1 ]; then
              fail_with_pending_from_current "path replace allows 0 or 1 hit in directory path, got $hits. server='$normalized' dir='$dir_part' file='$base_part' from='$from_str'"
            fi
            dir_part="$(printf "%s" "$dir_part" | sed -e "s#${from_esc}#${to_esc}#g")"
          fi
          if [ -n "$dir_part" ]; then
            rel="${dir_part}/${base_part}"
          else
            rel="${base_part}"
          fi
          ;;
      esac
    fi

    # bsdtar may escape non-ASCII names as octal sequences like \351.
    # Decode such sequences for actual local paths and tar member matching.
    rel_decoded="$(printf '%b' "$rel")"
    member_name="$(printf '%b' "$name")"

    # Directory entry (including empty dirs): ensure it exists (or fail if not allowed).
    case "$name" in
      */)
        out_path="/$rel_decoded"
        existed_before=0
        if [ -d "$out_path" ]; then existed_before=1; fi

        if [ "$auto_mkdir" -eq 1 ] && [ "$existed_before" -eq 0 ]; then
          if ! mkdir -p "$out_path"; then
            fail_with_pending_from_current "cannot create directory: $out_path"
          fi
        fi

        if [ ! -d "$out_path" ]; then
          fail_with_pending_from_current "directory does not exist: $out_path"
        fi

        done_items=$((done_items + 1))
        remaining=$((total_items - done_items))
        print_progress_line "Downloading: done=${done_items}/${total_items} remaining=${remaining} dir $out_path/"
        finish_progress_line

        # Only list newly created directories (not pre-existing ones)
        if [ "$existed_before" -eq 0 ]; then
          echo "$out_path/" >> "$downloaded_path"
          downloaded_count=$((downloaded_count + 1))
        fi
        continue
        ;;
    esac

    out_path="/$rel_decoded"
    out_dir="$(dirname "$out_path")"

    if [ ! -d "$out_dir" ]; then
      if [ "$auto_mkdir" -eq 1 ]; then
        if ! mkdir -p "$out_dir"; then
          fail_with_pending_from_current "cannot create directory: $out_dir"
        fi
      fi
    fi

    if [ ! -d "$out_dir" ]; then
      fail_with_pending_from_current "target directory does not exist: $out_dir"
    fi

    done_items=$((done_items + 1))
    remaining=$((total_items - done_items))

    expected_size=""
    if [ -f "$size_map_path" ]; then
      expected_size="$(awk -v n="$name" '$1==n{print $2; exit}' "$size_map_path" 2>/dev/null || true)"
    fi

    if [ -n "$expected_size" ] && [ "$expected_size" -gt 0 ] 2>/dev/null && has_cmd pv; then
      print_progress_line "Downloading: done=${done_items}/${total_items} remaining=${remaining} file $out_path"
      finish_progress_line
      if ! tar_extract_stdout "$tar_path" "$member_name" | pv -s "$expected_size" > "$out_path"; then
        status=1
        echo "$normalized" > "$pending_path"
        while IFS= read -r rest; do
          case "$rest" in
            */) continue ;;
          esac
          n2=""
          case "$rest" in
            "$server_dir_rel"/*) n2="$rest" ;;
            "$tar_root"/*) n2="$server_dir_rel/${rest#${tar_root}/}" ;;
            *) n2="" ;;
          esac
          if [ -n "$n2" ]; then echo "$n2" >> "$pending_path"; fi
        done
        return 1
      fi
    else
      print_progress_line "Downloading: done=${done_items}/${total_items} remaining=${remaining} file 0% $out_path"
      finish_progress_line
      if ! tar_extract_stdout "$tar_path" "$member_name" > "$out_path"; then
        status=1
        echo "$normalized" > "$pending_path"
        while IFS= read -r rest; do
          case "$rest" in
            */) continue ;;
          esac
          n2=""
          case "$rest" in
            "$server_dir_rel"/*) n2="$rest" ;;
            "$tar_root"/*) n2="$server_dir_rel/${rest#${tar_root}/}" ;;
            *) n2="" ;;
          esac
          if [ -n "$n2" ]; then echo "$n2" >> "$pending_path"; fi
        done
        return 1
      fi
      print_progress_line "Downloading: done=${done_items}/${total_items} remaining=${remaining} file 100% $out_path"
      finish_progress_line
    fi

    echo "$out_path" >> "$downloaded_path"
    downloaded_count=$((downloaded_count + 1))
  done < "$list_path"

  # If nothing matched, treat as failure and list all candidate files as pending.
  if [ "$downloaded_count" -eq 0 ]; then
    status=1
    while IFS= read -r rest; do
      case "$rest" in
        */) continue ;;
      esac
      n2=""
      case "$rest" in
        "$server_dir_rel"/*) n2="$rest" ;;
        "$tar_root"/*) n2="$server_dir_rel/${rest#${tar_root}/}" ;;
        *) n2="" ;;
      esac
      if [ -n "$n2" ]; then echo "$n2" >> "$pending_path"; fi
    done < "$list_path"
    return 1
  fi
}

cmd="${1:-}"
if [ -z "$cmd" ]; then
  print_help
  exit 2
fi

# Parse global options before command
while :; do
  case "$cmd" in
    --server)
      SERVER="${2:-}"
      shift 2 || true
      cmd="${1:-}"
      ;;
    --game)
      GAME="${2:-}"
      shift 2 || true
      cmd="${1:-}"
      ;;
    --account)
      ACCOUNT="${2:-}"
      shift 2 || true
      cmd="${1:-}"
      ;;
    --chunk-size)
      CHUNK_SIZE="${2:-}"
      shift 2 || true
      cmd="${1:-}"
      ;;
    -h|--help|help)
      print_help
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

shift || true

case "$cmd" in
  upload_file)
    # upload_file <local_path>
    upload_file "$@"
    ;;
  download_dir)
    # download_dir <server_dir_rel> [--mkdir] [--from STR --to STR]
    download_dir_overwrite "$@"
    ;;
  *)
    print_help
    exit 2
    ;;
esac

