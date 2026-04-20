import io
import hashlib
import tarfile


def _upload_one_file(client, rel_path: str, content: bytes) -> None:
    r = client.post("/api/uploads", json={"path": rel_path, "total_size": len(content), "overwrite": True})
    assert r.status_code == 200, r.text
    upload_id = r.json()["upload_id"]

    r = client.put(
        f"/api/uploads/{upload_id}",
        params={"offset": 0},
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["received"] == len(content)

    r = client.post(f"/api/uploads/{upload_id}/complete")
    assert r.status_code == 200, r.text


def _upload_one_file_chunked(client, rel_path: str, content: bytes, chunk_size: int) -> None:
    r = client.post("/api/uploads", json={"path": rel_path, "total_size": len(content), "overwrite": True})
    assert r.status_code == 200, r.text
    upload_id = r.json()["upload_id"]

    offset = 0
    while offset < len(content):
        chunk = content[offset : offset + chunk_size]
        r = client.put(
            f"/api/uploads/{upload_id}",
            params={"offset": offset},
            content=chunk,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["received"] == offset + len(chunk)
        offset += len(chunk)

    r = client.post(f"/api/uploads/{upload_id}/complete")
    assert r.status_code == 200, r.text


def test_upload_download_single_file(client):
    content = b"hello-from-test\n" * 5
    rel_path = "single/hello.bin"

    _upload_one_file(client, rel_path, content)

    r = client.get("/api/download/file", params={"path": rel_path})
    assert r.status_code == 200, r.text
    assert r.content == content

    r = client.get("/api/browse", params={"path": "single"})
    assert r.status_code == 200, r.text
    entries = r.json()["entries"]
    assert any(e["type"] == "file" and e["name"] == "hello.bin" for e in entries)


def test_upload_directory_browse_and_tar_download(client):
    files = {
        "mydir/readme.txt": b"readme\n",
        "mydir/a.txt": b"A\n",
        "mydir/sub/b.txt": b"B\n",
    }
    for p, content in files.items():
        _upload_one_file(client, p, content)

    r = client.get("/api/browse", params={"path": "mydir"})
    assert r.status_code == 200, r.text
    names = {e["name"]: e["type"] for e in r.json()["entries"]}
    assert names.get("readme.txt") == "file"
    assert names.get("a.txt") == "file"
    assert names.get("sub") == "dir"

    r = client.get("/api/browse", params={"path": "mydir/sub"})
    assert r.status_code == 200, r.text
    names = {e["name"]: e["type"] for e in r.json()["entries"]}
    assert names.get("b.txt") == "file"

    r = client.get("/api/download/dir", params={"path": "mydir"})
    assert r.status_code == 200, r.text

    buf = io.BytesIO(r.content)
    with tarfile.open(fileobj=buf, mode="r:") as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        member_names = {m.name for m in members}

        expected_names = set(files.keys())
        assert expected_names.issubset(member_names)

        for rel_path, expected in files.items():
            m = tf.getmember(rel_path)
            f = tf.extractfile(m)
            assert f is not None
            actual = f.read()
            assert actual == expected


def test_upload_download_simulated_large_file_chunked(client):
    # 2MB simulated "large" file (deterministic content)
    content = (b"0123456789ABCDEF" * (2 * 1024 * 1024 // 16))[: 2 * 1024 * 1024]
    assert len(content) == 2 * 1024 * 1024
    rel_path = "large/sim_2m.bin"

    _upload_one_file_chunked(client, rel_path, content, chunk_size=256 * 1024)

    r = client.get("/api/download/file", params={"path": rel_path})
    assert r.status_code == 200, r.text
    assert hashlib.sha256(r.content).digest() == hashlib.sha256(content).digest()


def test_upload_directory_with_large_file_tar_download(client):
    big_content = (b"Z" * (2 * 1024 * 1024))
    files = {
        "bigdir/readme.txt": b"readme\n",
        "bigdir/small/a.txt": b"A\n",
        "bigdir/large/sim_2m.bin": big_content,
    }

    _upload_one_file(client, "bigdir/readme.txt", files["bigdir/readme.txt"])
    _upload_one_file(client, "bigdir/small/a.txt", files["bigdir/small/a.txt"])
    _upload_one_file_chunked(client, "bigdir/large/sim_2m.bin", big_content, chunk_size=256 * 1024)

    r = client.get("/api/browse", params={"path": "bigdir"})
    assert r.status_code == 200, r.text
    names = {e["name"]: e["type"] for e in r.json()["entries"]}
    assert names.get("readme.txt") == "file"
    assert names.get("small") == "dir"
    assert names.get("large") == "dir"

    r = client.get("/api/browse", params={"path": "bigdir/large"})
    assert r.status_code == 200, r.text
    names = {e["name"]: e["type"] for e in r.json()["entries"]}
    assert names.get("sim_2m.bin") == "file"

    r = client.get("/api/download/dir", params={"path": "bigdir"})
    assert r.status_code == 200, r.text

    buf = io.BytesIO(r.content)
    with tarfile.open(fileobj=buf, mode="r:") as tf:
        m = tf.getmember("bigdir/large/sim_2m.bin")
        f = tf.extractfile(m)
        assert f is not None
        actual = f.read()
        assert hashlib.sha256(actual).digest() == hashlib.sha256(big_content).digest()

