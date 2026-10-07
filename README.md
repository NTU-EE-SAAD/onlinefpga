# FPGA reservation · NTUEE Makerspace

供學生自行註冊、借用與預約 FPGA 的網站，由 NTUEE Makerspace 管理，部署在 Makerspace 的 Linux server。介面採黑白、藍灰配色，不使用 SSO 或學校帳號。

本專案以原 [bol-edu/onlinefpga](https://github.com/bol-edu/onlinefpga) 延伸，開發目標為 [NTU-EE-SAAD/onlinefpga](https://github.com/NTU-EE-SAAD/onlinefpga)。原始設備管理程式仍保留；原安裝文件移至 [docs/legacy-onlinefpga.md](docs/legacy-onlinefpga.md)。

## 現在能做什麼

- 自行註冊、登入／登出、修改密碼；密碼使用 Werkzeug scrypt 雜湊。
- 設備列表、借用詳情、使用時間倒數與歷史紀錄。
- 即時借用、未來 14 天內的時段預約、取消預約、提前歸還。
- 單次借用以 15 分鐘為單位；每台設備可設定 15–480 分鐘上限。
- 每個帳號同時保留一筆借用或預約，設備時段不可重疊。
- 每日維護時段檢查，預設台北時間 06:00–07:00。
- 獨立、可重新啟動的排程：準備設備、啟用預約、到期回收、取消錯過的預約。
- 管理後台：新增設備、維護開關、設備名稱與時限、強制歸還、停用／啟用帳號、操作紀錄、失敗回收重試。
- 預設三台**模擬 PYNQ**，不需要 FPGA、MongoDB、Docker 或 Node.js 即可操作完整借用流程。
- 支援 PYNQ-Z2 與安裝 PYNQ 2.7 的 KV260，透過 SSH 管理 Notebook 租期。
- 網站提供 Jupyter HTTP／WebSocket 代理，只有當次借用者可進入；到期與歸還立即撤銷網站連線。
- 板端 systemd 限制 Notebook 執行時間，停止服務時一併終止 kernel 與 terminal；個人檔案保留。

新安裝預設使用模擬設備，沒有 FPGA 也能測試排程。本台 server 已設定兩台實體設備：PYNQ-Z2 `192.168.50.201` 與 Kria KV260 `192.168.50.202`。部署密鑰、板端帳密與資料庫存於 Git 忽略的本機檔案，不會隨 clone 複製。

2026-10-07 已實測兩台設備：網站借用、Notebook Python 執行、PYNQ 2.7 的 EmbeddedDevice／XrtDevice 辨識、提前歸還／到期以及 WebSocket 撤銷均通過；另通過 42 項後端測試與桌面／手機瀏覽器流程。這些測試沒有載入課程 bitstream，實際硬體設計仍需使用相容檔案驗證。

## 架構

```text
瀏覽器 → Flask 網站／租期代理 → 板上 Jupyter（HTTP + WebSocket）
                  ↕
                SQLite ← 獨立 scheduler → SSH → 板端 Notebook 服務
```

網站使用 Python 3.10+、Flask 與 Gunicorn。SQLite 採 WAL 與 `BEGIN IMMEDIATE` 交易，借用衝突檢查與紀錄新增在同一交易完成；唯一索引另限制每人一筆未結束紀錄、每台設備一筆執行中紀錄。適合單台 server、少量至數十台板子的 Makerspace。資料庫需放在本機磁碟，不能放在 NFS；多台應用 server 的部署需另外改成 PostgreSQL 等共用資料庫。

新網站**不讀寫原 MongoDB**，舊 `boleduuser` 明文密碼也不會自動匯入。原 `monitord.py`、`active_monitord.py`、U50 工具保留給舊部署；兩套管理服務不可同時控制同一塊實體板。

## 快速啟動

Ubuntu／Debian 新環境先安裝 Python 的 venv 支援：

```bash
sudo apt install python3-venv
git clone https://github.com/NTU-EE-SAAD/onlinefpga.git
cd onlinefpga
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

第一次先直接啟動模擬模式：

```bash
./scripts/run-local.sh
```

開啟 `http://localhost:8000`，或 `http://<server內網IP>:8000`。腳本會初始化資料庫、啟動獨立排程與兩個 Gunicorn worker；`Ctrl+C` 同時停止網站與排程。預設監聽 `0.0.0.0:8000`，可用 `PORTAL_BIND=127.0.0.1:8000 ./scripts/run-local.sh` 改為只接受本機連線。

網站提供「建立帳號」，沒有預設帳號或預設管理員密碼。未指定 `SECRET_KEY` 時會在 `instance/secret.key` 產生權限為 `0600` 的隨機密鑰，重啟不會更換。

### 建立管理員

另一個終端機執行：

```bash
.venv/bin/flask --app portal create-admin
```

依提示輸入管理員姓名、信箱與至少 12 字元的密碼。也可以讓自己先在網站註冊，再提升帳號：

```bash
.venv/bin/flask --app portal promote-admin your-email@example.com
```

提升後重新登入，導覽列會出現「管理」。只有主機操作者可執行這些 CLI，註冊表單不接受角色設定。

### 環境設定

可複製 `.env.example` 為 `.env`，**先把 SECRET_KEY 範例值改成隨機密鑰**。`.env`、資料庫、密鑰、log 與 `.venv` 都已加入 `.gitignore`。

```bash
cp .env.example .env
chmod 600 .env
.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))'
# 將產生的值填入 .env 的 SECRET_KEY，再啟動
```

`.env` 不會被 Python 自動載入；啟動腳本會載入，systemd 使用 `EnvironmentFile`。直接使用 CLI 時需先載入：

```bash
set -a
source .env
set +a
```

| 設定 | 預設／用途 |
|---|---|
| `SECRET_KEY` | session 簽章密鑰，至少 32 字元；未設時使用本機持久密鑰 |
| `DATABASE_PATH` | 預設 repo 的 `instance/portal.sqlite3`；建議正式環境指定絕對路徑 |
| `TIMEZONE` | `Asia/Taipei`；表單、畫面、維護時段使用此時區，資料庫儲存 UTC epoch |
| `REGISTRATION_OPEN` | `true`；設為 `false` 暫停自行註冊 |
| `MAINTENANCE_START` / `MAINTENANCE_END` | `06:00` / `07:00`；可跨午夜，兩值相同代表關閉每日維護時段 |
| `SCHEDULER_INTERVAL` | `10` 秒，允許 1–60 秒 |
| `COOKIE_SECURE` | 本機 HTTP 為 `false`；正式 HTTPS 設為 `true` |
| `TRUST_PROXY` | 預設 `false`；僅在一層可信反向代理且禁止直接連 Gunicorn 時設為 `true` |
| `ENABLE_HARDWARE` | 預設 `false`；必須明確開啟才可操作實體板 |
| `FPGA_SSH_KEY` | 可選的實體板 SSH 私鑰路徑；也支援 credentials 檔中的 SSH 密碼 |
| `FPGA_KNOWN_HOSTS` | 由可信管道核對過的板端 SSH 公開金鑰檔案 |
| `FPGA_CREDENTIALS` | 權限 `0600` 的 JSON 檔；每台板子分別設定 SSH 與 sudo 密碼 |

環境變數變更後要同時重啟網站與排程。修改維護時間前應先處理與新時段重疊的既有預約；維護規則在建立借用時驗證，不會追溯改寫現有紀錄。

## 在這台 server 持續執行

提供 systemd **使用者服務**，無須用 root 執行應用程式：

```bash
./scripts/install-user-services.sh
```

腳本會根據目前 checkout 的路徑產生服務設定，啟動網站及單一排程程序。若先前使用 `run-local.sh`，先停止它，避免占用 8000 port 與排程鎖。

```bash
systemctl --user status onlinefpga-web onlinefpga-scheduler
journalctl --user -u onlinefpga-web -u onlinefpga-scheduler -f
systemctl --user restart onlinefpga-web onlinefpga-scheduler
systemctl --user stop onlinefpga-web onlinefpga-scheduler
```

**登出後與開機自動執行**需要主機管理員開啟 linger：

```bash
sudo loginctl enable-linger onlinefpga
```

`onlinefpga` 請換成實際部署帳號。沒有 linger 時，使用者服務的存續取決於登入 session，不保證重新開機前尚未登入就會啟動。

### HTTPS 與校外連線

內網 HTTP 可先測試註冊與排程。對外公開時設定域名／路由器轉送及 HTTPS，參考 [deploy/nginx.conf.example](deploy/nginx.conf.example)。

1. 將 Nginx 範例中的域名與憑證路徑換成實際值。
2. 用 `systemctl --user edit onlinefpga-web` 將 Gunicorn 改綁 `127.0.0.1:8000`：

   ```ini
   [Service]
   ExecStart=
   ExecStart=/absolute/path/to/onlinefpga/.venv/bin/gunicorn --workers 2 --worker-class gthread --threads 32 --bind 127.0.0.1:8000 --timeout 90 portal:create_app()
   ```

3. 設定 `.env`：`COOKIE_SECURE=true`、`TRUST_PROXY=true`，重啟兩個服務。
4. 反向代理必須覆寫客戶端傳入的 forwarded headers；不要讓使用者繞過它直接連 Gunicorn。

網站使用 POST CSRF token、HttpOnly／SameSite cookies、固定安全標頭與登入／註冊速率限制。Flask 正式環境應使用 Gunicorn 等 WSGI server；ProxyFix 只可信任正確設定的代理。[Flask 正式部署](https://flask.palletsprojects.com/en/stable/deploying/)、[代理設定](https://flask.palletsprojects.com/en/stable/deploying/proxy_fix/)

## 排程的行為與恢復

```text
已預約 → 準備中 → 使用中 → 回收中 → 已結束
   └→ 已取消              操作失敗 → 設備異常＋設備維護
```

- 即時借用也先建立一筆已預約紀錄，由排程於下一次檢查啟用。
- 未來預約的開始、結束時間固定；不會因 server 停機而重新計算租期。
- 排程離線後重啟，回收已到期的借用；已完全錯過的預約會取消。
- 每次操作先在資料庫 claim，網路操作期間不持有 DB 寫入鎖；180 秒後可重試中斷的操作。
- Linux `flock` 限制同一資料庫只有一個排程程序，不能在每個 Gunicorn worker 啟動一份 scheduler。
- 回收完成前設備仍被占用；下一個時段會等回收成功後再啟用。
- 實體操作失敗會隔離設備。管理員在後台點「重試回收」，成功後再解除維護；不可只把失敗設備直接改成可借。
- 停用帳號會撤銷網站登入、取消待開始預約、回收使用中的設備；準備中的設備完成後會轉入回收。
- 網頁每 10 秒查詢狀態；倒數使用 server 時間校正。排程離線時登入使用者會看到提示。

健康檢查：

```bash
curl http://127.0.0.1:8000/healthz
# 網站與排程正常：HTTP 200，{"web":"ok","scheduler":"ok"}
# 排程沒有近期 heartbeat：HTTP 503
```

手動執行一次排程（需先停止持續執行的 scheduler）：

```bash
.venv/bin/flask --app portal scheduler --once
```

網站代理每次 HTTP 請求都驗證帳號、租期與擁有者，WebSocket 約每 0.2 秒重新檢查。歸還、到期、停用帳號或修改密碼後會拒絕後續請求並斷開 WebSocket。板端 `RuntimeMaxSec` 另以租期剩餘秒數停止 Notebook，即使 server 排程離線也會回收 kernel；SSH 傳輸與停止程序有數秒延遲。排程最終更新租借紀錄，板子回收成功才重新開放。

## 從自己的電腦使用

在 Windows PowerShell／macOS／Linux 本機終端機執行：

```bash
ssh -N -L 18000:127.0.0.1:8000 -p 50 onlinefpga@140.112.194.42
```

輸入 **server 帳號**的密碼後，視窗保持開啟是正常的：`-N` 只轉送連線，不顯示 shell。瀏覽器開啟 `http://localhost:18000`。註冊網站帳號、選擇設備與借用時間，待狀態變成「使用中」後點選「開啟 Jupyter」，不需板端密碼或第二條 tunnel。結束 tunnel 用 `Ctrl+C`。

在 Jupyter 建立 Python 3 Notebook，先執行：

```python
import socket, pynq
print(socket.gethostname())
print(pynq.__version__)
print([type(device).__name__ for device in pynq.Device.devices])
```

程式在租借的板子上執行。PYNQ-Z2 與 KV260 需使用各自相容的 `.bit`／`.hwh`／`.xclbin` 與課程套件，不能互換 FPGA 設計檔；本網站不提供雲端 Vivado 合成。可上傳 Notebook、Python 與設計檔，單次 HTTP 上傳上限 16 MiB，較大檔案需分批或由管理員調整代理限制。

## 設定實體 PYNQ-Z2／KV260

支援本 repo 的 PYNQ 2.7、Notebook 6.4 與 Linux systemd 環境，KV260 必須已安裝 PYNQ。U50、USB-JTAG 與其他系統須另寫 driver。新 driver 使用 `scripts/board_session.py`，**不執行會刪除檔案的舊 `reset_pynq.py`**。

1. 將板子設為維護中，結束其借用與預約。確認 server 可透過內網連接 SSH 與 TCP 9090。
2. 從 USB 終端機等可信管道核對 SSH host key，建立 `FPGA_KNOWN_HOSTS`。未知或變更的金鑰會被拒絕，不會自動信任。
3. 建立私有帳密檔，key 使用設備 slug，例如 `instance/hardware.json`：

   ```json
   {
     "pynq-01": {"ssh_password": "BOARD_SSH_PASSWORD", "sudo_password": "BOARD_SUDO_PASSWORD"},
     "kv260-01": {"ssh_password": "BOARD_SSH_PASSWORD", "sudo_password": "BOARD_SUDO_PASSWORD"}
   }
   ```

   ```bash
   chmod 600 instance/hardware.json
   ```

   使用 SSH key 時可省略 `ssh_password`，並設定 `FPGA_SSH_KEY`。管理帳號必須能用密碼執行 sudo。敏感資料只經加密 SSH stdin 傳送，不放在 argv、頁面或租借紀錄。

4. 透過管理介面建立設備，再用 CLI 登記連線。例如：

   ```bash
   .venv/bin/flask --app portal configure-device pynq-01 \
     --host 192.168.50.201 --ssh-user xilinx \
     --jupyter-url http://192.168.50.201:9090
   .venv/bin/flask --app portal configure-device kv260-01 \
     --host 192.168.50.202 --ssh-user ubuntu --model KV260 \
     --jupyter-url http://192.168.50.202:9090
   ```

   KV260 的設備 `model` 必須設為 `KV260`，以設定板端 PYNQ／XRT 環境。兩種板型共用資料庫的 `pynq` driver。

5. 設定 `.env` 的 `ENABLE_HARDWARE=true`、`FPGA_KNOWN_HOSTS`、`FPGA_CREDENTIALS` 絕對路徑，同時重啟網站與排程。維護狀態下先做實測，再在後台解除維護。

每次準備會寫入板端 `/etc/onlinefpga/session.py`、`/usr/local/bin/onlinefpga-jupyter` 及 `jupyter.service.d/onlinefpga.conf`，使用當次租期 token 與 `/lab/<租借編號>/` base URL。原啟動腳本與 Jupyter 設定保留。租借使用獨立的 `/etc/onlinefpga/jupyter` 設定目錄，並以 `/etc/onlinefpga/templates/tree.html` 覆寫 PYNQ 2.7 首頁中錯誤的 tooltip 呼叫；原套件檔案不變。Notebook 不再於開機自動啟動，避免重開機恢復舊租期權限；重開機後請先歸還舊借用，再借用。每個網站帳號在各板子有 `/home/root/jupyter_notebooks/onlinefpga/users/<帳號ID>/` 目錄，回收會停止服務但保留檔案。

這是信任學生的 Makerspace 共用板環境：PYNQ Notebook 以 root 操作 FPGA，**個人目錄與 token 不是容器或系統權限隔離**。具有板端執行權的使用者能存取系統，也可能存取其他目錄或更改服務；網站代理限制租借入口，不能阻止惡意 root 程式。既有內網管理入口也不由網站控制。若要提供給不可信的公開使用者，需要額外網路隔離、受限帳號／板端 agent、檔案隔離與板端恢復機制。回收目前停止 Notebook／kernel，不會重刷 SD 卡或載入指定的預設 FPGA bitstream；新實驗應主動載入自己的相容 overlay。

回復原 Notebook 啟動方式：先維護設備、結束租借，再由板端管理員刪除 `/etc/systemd/system/jupyter.service.d/onlinefpga.conf`，執行 `sudo systemctl daemon-reload`、`sudo systemctl enable --now jupyter`。這不會刪除學生檔案，但會恢復原先的登入設定與服務行為。

## 維運與資料備份

### 密碼重設

不透過 email 寄送明文密碼。忘記密碼由 server 管理員執行：

```bash
.venv/bin/flask --app portal reset-password student@example.com
```

依提示輸入新密碼；所有舊 session 立即失效。目前沒有 SMTP 郵件驗證、通知信或自助忘記密碼功能。註冊信箱僅作為帳號識別，非經驗證的學生身分。

### 備份

```bash
.venv/bin/flask --app portal backup-db /safe/backup/portal-2026-10-08.sqlite3
```

此命令使用 SQLite backup API，支援服務仍在執行時取得一致性備份；不覆寫既有檔案。另保存 `instance/secret.key`（若使用自動產生密鑰）或部署 `.env`。不要把這些檔案 commit 到 Git。

還原時停止兩個服務，使用備份替換 `DATABASE_PATH`，移除舊 WAL／SHM 檔，再重啟。實體模式還原舊備份前，需先核對板上實際使用狀態，避免舊紀錄導致重複分配。SQLite WAL／備份機制參考 [SQLite 交易](https://www.sqlite.org/lang_transaction.html)、[Backup API](https://www.sqlite.org/backup.html)。

## 測試

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q portal wsgi.py reset_pynq.py
```

預設單元測試與 `smoke-browser.py` 使用隔離的暫存資料庫及模擬 driver，不會連線 FPGA。涵蓋同時搶借、同帳號併發借用、預約衝突、相鄰時段、維護時間、錯過預約、到期回收、中斷恢復、設備隔離與重試、CSRF、角色權限、資料隱私、停用帳號、密碼 session 撤銷及健康檢查。

GitHub Actions 設定於 `.github/workflows/portal-tests.yml`。真實瀏覽器測試會啟動隔離的暫存網站與排程，驗證桌面／手機排版、註冊、登入、借用、倒數、歸還、預約、取消、管理頁與密碼更新，不會寫入部署資料庫：

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/python scripts/smoke-browser.py
```

截圖與測試服務 log 存在已忽略的 `test-results/`。


實體驗收需要先將所有實體設備設為維護中，且沒有正在使用的借用。它會重啟板端服務，使用隔離資料庫與測試帳號，驗證借用、真實瀏覽器 Notebook 執行、PYNQ 裝置辨識、歸還／到期以及 WebSocket 撤銷，最後停止測試工作空間；不刪除既有筆記本。

```bash
set -a; source .env; set +a
.venv/bin/python scripts/smoke-hardware.py --allow-board-control
```

## 目錄

```text
portal/
  __init__.py        Flask app、設定、安全標頭
  db.py              SQLite schema、交易
  service.py         借用規則、狀態機、排程
  hardware.py        模擬／PYNQ-Z2、KV260 SSH driver
  workspace.py       租期權限檢查、Jupyter HTTP／WebSocket 代理
  views.py           註冊、學生頁面、管理頁面、狀態 API
  cli.py             管理員、密碼、備份、排程 CLI
  templates/         Jinja 頁面
  static/            CSS、JavaScript、favicon
scripts/             啟動、安裝、板端 session 管理、瀏覽器與實體驗收
deploy/              systemd 與 Nginx 範例
tests/               後端、併發與權限測試
docs/legacy-onlinefpga.md  原 repo 安裝與設備管理文件
monitord.py / onlinefpga.py / config.py / ... 原管理工具
```

新網站的設定來源是 `.env`，不使用舊 `config.py` 的 IP／密碼。原程式預設 IP 是範例，不代表目前 NTUEE Makerspace 的實際設備清單。
