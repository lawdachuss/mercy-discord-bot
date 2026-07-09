import sys
import os
import subprocess
import threading

def _writer(stream, path):
    with open(path, "wb", buffering=0) as f:
        while True:
            data = stream.read(4096)
            if not data:
                break
            f.write(data)

def main():
    wd = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    stdout_path = os.path.join(wd, "bot_stdout.log")
    stderr_path = os.path.join(wd, "bot_stderr.log")

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"

    proc = subprocess.Popen(
        [sys.executable, "-u", "main.py"],
        cwd=wd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    out_thread = threading.Thread(target=_writer, args=(proc.stdout, stdout_path), daemon=True)
    err_thread = threading.Thread(target=_writer, args=(proc.stderr, stderr_path), daemon=True)
    out_thread.start()
    err_thread.start()

    proc.wait()
    out_thread.join(timeout=5)
    err_thread.join(timeout=5)
    sys.exit(proc.returncode)

if __name__ == "__main__":
    main()
