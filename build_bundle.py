import io
import os
import tarfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {"__pycache__", ".git"}


def _skip(name):
    return name.endswith(".pyc") or any(part in SKIP_DIRS for part in name.split("/"))


def inner_tar_gz(dir_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for base, dirs, files in os.walk(dir_path):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            for fname in sorted(files):
                full = os.path.join(base, fname)
                arc = os.path.relpath(full, dir_path).replace(os.sep, "/")
                if _skip(arc):
                    continue
                tar.add(full, arcname=arc)
    return buf.getvalue()


def add_bytes(tar, name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o644
    tar.addfile(info, io.BytesIO(data))


def main():
    out_path = os.path.join(ROOT, "nws.tar.gz")
    with open(os.path.join(ROOT, "nws_common.py"), "rb") as src:
        common_bytes = src.read()
    with open(os.path.join(ROOT, "payload", "nws_common_embedded.py"), "wb") as dst:
        dst.write(common_bytes)
    payload_bytes = inner_tar_gz(os.path.join(ROOT, "payload"))
    web_bytes = inner_tar_gz(os.path.join(ROOT, "web"))
    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(os.path.join(ROOT, "manifest.json"), arcname="manifest.json")
        tar.add(os.path.join(ROOT, "nws_common.py"), arcname="nws_common.py")
        add_bytes(tar, "payload", payload_bytes)
        add_bytes(tar, "web", web_bytes)
    size = os.path.getsize(out_path)
    print(f"Wrote {out_path} ({size} bytes)")


if __name__ == "__main__":
    main()
