"""Simulation by default. Optional SSH adapter reuses reset_pynq.py for PYNQ 2.7."""
import secrets
import shlex
import json
from pathlib import Path

from flask import current_app


class SimulatedDriver:
    def prepare(self, device, rental):
        pass

    def release(self, device, rental):
        pass


class PynqDriver:
    def prepare(self, device, rental):
        self.reset(device, rental["access_secret"])

    def release(self, device, rental):
        self.reset(device, secrets.token_urlsafe(24))

    def reset(self, device, password):
        import paramiko
        config = current_app.config
        if not config["ENABLE_HARDWARE"]:
            raise RuntimeError("Physical hardware is disabled")
        if not device["host"] or not config["FPGA_SSH_KEY"] or not config["FPGA_KNOWN_HOSTS"] or not config["FPGA_SUDO_PASSWORD"]:
            raise RuntimeError("SSH host, key, known_hosts and board sudo credentials must be configured")
        client = paramiko.SSHClient()
        client.load_host_keys(config["FPGA_KNOWN_HOSTS"])
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        try:
            client.connect(device["host"], port=device["ssh_port"], username=device["ssh_user"],
                           key_filename=config["FPGA_SSH_KEY"], timeout=8, banner_timeout=8,
                           auth_timeout=8, allow_agent=False, look_for_keys=False)
            # Legacy script cleans Notebook files and restarts Jupyter: PYNQ 2.7 only.
            # Root-owned staging is avoided. Each operation uses a private random directory.
            script = Path(__file__).resolve().parent.parent / "reset_pynq.py"
            with client.open_sftp() as sftp:
                home = sftp.normalize(".")
                folder = home + "/.onlinefpga-" + secrets.token_hex(8)
                sftp.mkdir(folder, mode=0o700)
                sftp.put(str(script), folder + "/reset_pynq.py")
            command = (f"cd {shlex.quote(folder)} && "
                       "/usr/local/share/pynq-venv/bin/python3 reset_pynq.py "
                       "--stdin pynq")
            stdin, stdout, _ = client.exec_command(command, timeout=90)
            stdin.write(json.dumps({"password": password, "sudo_password": config["FPGA_SUDO_PASSWORD"]}) + "\n")
            stdin.flush()
            stdin.channel.shutdown_write()
            output = stdout.read(65536).decode(errors="replace")
            code = stdout.channel.recv_exit_status()
            if code != 0 or "Notebook reset completed" not in output:
                raise RuntimeError("PYNQ reset failed")
            # Verify new credentials against the local Notebook service over SSH.
            _, check, _ = client.exec_command(
                "curl --fail --silent --max-time 8 http://127.0.0.1:9090/login -o /dev/null", timeout=10)
            if check.channel.recv_exit_status() != 0:
                raise RuntimeError("Jupyter health check failed")
            with client.open_sftp() as sftp:
                for name in sftp.listdir(folder):
                    sftp.remove(folder + "/" + name)
                sftp.rmdir(folder)
        finally:
            client.close()


def driver_for(device):
    if device["driver"] == "simulated":
        return SimulatedDriver()
    if device["driver"] == "pynq":
        return PynqDriver()
    raise RuntimeError("Unsupported device driver")
