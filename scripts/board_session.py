#!/usr/bin/env python3
"""Root session manager on PYNQ 2.7 boards. JSON stdin; preserve all notebooks."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request


def write_private(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)


def main():
    data = json.load(sys.stdin)
    if os.geteuid() != 0:
        raise RuntimeError("Board session manager requires root")
    action = data["action"]
    if action not in ("prepare", "release"):
        raise ValueError("Invalid action")
    if action == "release":
        subprocess.run(["systemctl", "stop", "jupyter.service"], check=True, timeout=25)
        write_private(Path("/etc/onlinefpga/session.py"), "# No active rental.\n")
        print("Session released")
        return
    rental_id, user_id = int(data["rental_id"]), int(data["user_id"])
    if rental_id <= 0 or user_id <= 0 or len(data["token"]) < 24:
        raise ValueError("Invalid rental credentials")
    base = "/lab/{}/".format(rental_id)
    lease_seconds = int(data["lease_seconds"])
    if lease_seconds < 1:
        raise ValueError("Rental has expired")
    workspace = Path("/home/root/jupyter_notebooks/onlinefpga/users/{}".format(user_id))
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    cfg = "\n".join([
        "c = get_config()",
        "c.NotebookApp.ip = '0.0.0.0'",
        "c.NotebookApp.port = 9090",
        "c.NotebookApp.port_retries = 0",
        "c.NotebookApp.allow_root = True",
        "c.NotebookApp.open_browser = False",
        "c.NotebookApp.allow_remote_access = True",
        "c.NotebookApp.allow_password_change = False",
        "c.NotebookApp.password = ''",
        "c.NotebookApp.token = " + repr(data["token"]),
        "c.NotebookApp.base_url = " + repr(base),
        "c.NotebookApp.notebook_dir = " + repr(str(workspace)),
        "c.NotebookApp.extra_template_paths = ['/etc/onlinefpga/templates']",
        "c.NotebookApp.iopub_data_rate_limit = 100000000",
        "c.NotebookApp.log_level = 'WARN'", "",
    ])
    write_private(Path("/etc/onlinefpga/session.py"), cfg)
    write_private(Path("/etc/onlinefpga/jupyter/nbconfig/notebook.json"), "{}\n")
    # PYNQ 2.7's tree template calls tooltip() on a literal string. A managed
    # override removes that placeholder; installed package files remain intact.
    for template in Path('/usr/local/share/pynq-venv/lib').glob('python*/site-packages/notebook/templates/tree.html'):
        text = template.read_text()
        text = text.replace("('#element').tooltip('enable')", "// Removed broken placeholder tooltip call.")
        write_private(Path('/etc/onlinefpga/templates/tree.html'), text)
    launcher = Path("/usr/local/bin/onlinefpga-jupyter")
    write_private(launcher, "#!/bin/bash\nset -a\n. /etc/environment\nset +a\n"
                  ". /usr/local/share/pynq-venv/bin/activate\nexport SHELL=/bin/bash\n"
                  "export PYNQ_JUPYTER_NOTEBOOKS=/home/root/jupyter_notebooks\n"
                  "export JUPYTER_CONFIG_DIR=/etc/onlinefpga/jupyter\n"
                  "export XILINX_XRT=/usr\n"
                  + ("export BOARD=KV260\n" if data.get("model") == "KV260" else "export BOARD=Pynq-Z2\n")
                  + "export PATH=$PATH:/usr/local/share/pynq-venv/bin/microblazeel-xilinx-elf/bin\n"
                  + "exec /usr/local/share/pynq-venv/bin/jupyter-notebook --config=/etc/onlinefpga/session.py\n")
    launcher.chmod(0o700)
    # Keep the original launcher/config. Removing this drop-in restores that service.
    dropin = Path("/etc/systemd/system/jupyter.service.d/onlinefpga.conf")
    write_private(dropin, "[Service]\nType=simple\nExecStart=\n"
                      "ExecStart=/usr/local/bin/onlinefpga-jupyter\nKillMode=control-group\n"
                      "TimeoutStopSec=15\nRestart=no\nRuntimeMaxSec={}\n".format(lease_seconds))
    subprocess.run(["systemctl", "daemon-reload"], check=True, timeout=15)
    # A reboot must not restore credentials from an old rental.
    subprocess.run(["systemctl", "disable", "jupyter.service"], check=True, timeout=15)
    subprocess.run(["systemctl", "restart", "jupyter.service"], check=True, timeout=30)
    req = urllib.request.Request("http://127.0.0.1:9090" + base + "api",
                                 headers={"Authorization": "token " + data["token"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 65
    while time.monotonic() < deadline:
        try:
            with opener.open(req, timeout=3) as response:
                if response.status == 200:
                    print("Session ready")
                    return
        except (OSError, ValueError):
            time.sleep(1)
    raise RuntimeError("Notebook failed to become ready; inspect board journalctl -u jupyter")


if __name__ == "__main__":
    main()
