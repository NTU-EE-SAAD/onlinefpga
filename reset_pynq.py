"""Reset a PYNQ 2.7 Notebook workspace; deletes non-retained user files.

Legacy usage: reset_pynq.py PASSWORD pynq|kv260
Portal usage: reset_pynq.py --stdin pynq (JSON credentials on stdin)
"""
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from notebook.auth import passwd

RemoveExcept = {
    'Welcome to Pynq.ipynb', '.pynq-notebooks', 'base', 'common', 'kv260',
    'pynq-dpu', 'pynq-helloworld', 'getting_started', 'logictools',
    'pynq_composable', 'pynq_peripherals',
}


def get_random_passwd(length=24):
    return secrets.token_urlsafe(length)


def gen_hashedpasswd(password):
    config = {'NotebookApp': {'password': passwd(password)}}
    Path('jupyter_notebook_config.json').write_text(json.dumps(config, indent=2))
    print('Notebook password configuration generated')


def sudo_run(arguments, sudo_password, allow_no_process=False):
    result = subprocess.run(
        ['sudo', '-S', '-p', ''] + arguments,
        input=sudo_password + '\n', text=True, timeout=60,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    if result.returncode != 0 and not (allow_no_process and result.returncode == 1):
        raise RuntimeError('Board reset command failed')


def main():
    if len(sys.argv) != 3 or sys.argv[2] not in ('pynq', 'kv260'):
        raise ValueError('Usage: reset_pynq.py PASSWORD|--stdin pynq|kv260')
    board = sys.argv[2]
    if sys.argv[1] == '--stdin':
        credentials = json.loads(sys.stdin.readline())
        password = credentials['password']
        sudo_password = credentials['sudo_password']
    else:
        password = get_random_passwd() if sys.argv[1] == 'random' else sys.argv[1]
        # Preserve old CLI defaults; the portal supplies explicit credentials.
        sudo_password = os.environ.get('FPGA_SUDO_PASSWORD', 'xilinx' if board == 'pynq' else 'boledukv260')
    version = subprocess.run(
        ['/usr/local/share/pynq-venv/bin/pynq', '-v'],
        capture_output=True, text=True, check=True, timeout=10,
    )
    if '2.7.0' not in version.stdout.split():
        raise RuntimeError('Only PYNQ 2.7.0 is supported by this reset script')
    notebooks = Path('/home/xilinx/jupyter_notebooks' if board == 'pynq' else '/home/root/jupyter_notebooks')
    gen_hashedpasswd(password)
    for path in notebooks.iterdir():
        if path.name not in RemoveExcept or 'Untitled Folder' in path.name:
            sudo_run(['rm', '-rf', '--', str(path)], sudo_password)
    # pkill returns 1 when there is no old Notebook process.
    sudo_run(['pkill', '-f', '[j]upyter-notebook'], sudo_password, allow_no_process=True)
    sudo_run(['mv', './jupyter_notebook_config.json', '/root/.jupyter/'], sudo_password)
    sudo_run(['/bin/bash', '/usr/local/bin/start_jupyter.sh'], sudo_password)
    time.sleep(2)
    print('Notebook reset completed')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never echo credentials, hashes or command arguments.
        print('Board reset failed: ' + type(error).__name__, file=sys.stderr)
        sys.exit(1)
