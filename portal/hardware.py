"""Non-destructive session control for PYNQ-Z2 and PYNQ-enabled Kria KV260."""
import json
import secrets
import shlex
import stat
from pathlib import Path

from flask import current_app
from . import service


class SimulatedDriver:
    def prepare(self, device, rental):
        pass

    def release(self, device, rental):
        pass


class PynqDriver:
    def prepare(self, device, rental):
        self.control(device, rental, "prepare")

    def release(self, device, rental):
        self.control(device, rental, "release")

    def control(self, device, rental, action):
        import paramiko
        config = current_app.config
        if not config["ENABLE_HARDWARE"]:
            raise RuntimeError("Physical hardware is disabled")
        if not device["host"] or not config["FPGA_KNOWN_HOSTS"] or not config["FPGA_CREDENTIALS"]:
            raise RuntimeError("Board host, known_hosts and credentials must be configured")
        credentials_path = Path(config["FPGA_CREDENTIALS"])
        if stat.S_IMODE(credentials_path.stat().st_mode) & 0o077:
            raise RuntimeError("Board credentials file must have mode 600")
        creds = json.loads(credentials_path.read_text()).get(device["slug"])
        if not creds or not creds.get("sudo_password"):
            raise RuntimeError("Device credentials must be configured")
        client = paramiko.SSHClient()
        client.load_host_keys(config["FPGA_KNOWN_HOSTS"])
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        folder = None
        try:
            client.connect(device["host"], port=device["ssh_port"], username=device["ssh_user"],
                           key_filename=config["FPGA_SSH_KEY"] or None,
                           password=creds.get("ssh_password"), timeout=8, banner_timeout=8,
                           auth_timeout=8, allow_agent=False, look_for_keys=False)
            with client.open_sftp() as sftp:
                folder = sftp.normalize(".") + "/.onlinefpga-" + secrets.token_hex(8)
                sftp.mkdir(folder, mode=0o700)
                sftp.put(str(Path(__file__).resolve().parent.parent / "scripts/board_session.py"),
                         folder + "/board_session.py")
            command = "sudo -S -p '' /usr/bin/python3 " + shlex.quote(folder + "/board_session.py")
            stdin, stdout, stderr = client.exec_command(command, timeout=115)
            stdin.write(creds["sudo_password"] + "\n")
            stdin.write(json.dumps({"action": action, "rental_id": rental["id"],
                                    "user_id": rental["user_id"], "token": rental["access_secret"],
                                    "model": device["model"], "lease_seconds": max(0, rental["ends_at"] - service.now())}) + "\n")
            stdin.flush()
            stdin.channel.shutdown_write()
            output = stdout.read(65536).decode(errors="replace")
            if stdout.channel.recv_exit_status() != 0 or ("Session ready" if action == "prepare" else "Session released") not in output:
                raise RuntimeError("Board session operation failed; inspect the board service journal")
        finally:
            if folder:
                try:
                    with client.open_sftp() as sftp:
                        sftp.remove(folder + "/board_session.py")
                        sftp.rmdir(folder)
                except (OSError, paramiko.SSHException):
                    pass
            client.close()


def driver_for(device):
    if device["driver"] == "simulated":
        return SimulatedDriver()
    if device["driver"] == "pynq":
        return PynqDriver()
    raise RuntimeError("Unsupported device driver")
