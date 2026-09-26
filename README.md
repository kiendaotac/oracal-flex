# a1launcher

Tool retry tạo instance **VM.Standard.A1.Flex** (Oracle Cloud Always Free) cho tới khi
Oracle còn chỗ. Viết bằng Python + OCI SDK, chạy one-shot trong Docker trên
Raspberry Pi (arm64). Image được GitHub Actions build sẵn — trên Pi chỉ việc `docker pull`.

Tier Always Free A1 gần như luôn trả về `Out of host capacity`. Tool lặp qua **tất cả
availability domain**, backoff ngẫu nhiên giữa các vòng để không bị rate-limit, và
thoát ngay khi tạo được instance.

---

## 1. Cấu trúc

```
a1launcher/
├── cli.py            # entry point, parse flag, điều phối preflight + launch
├── config.py         # đọc .env / config.yaml / env var, hằng số free tier
├── preflight.py      # toàn bộ check PASS/FAIL của --check
├── launcher.py       # vòng lặp retry, xoay AD, backoff, lấy public IP
├── errors.py         # phân loại oci.exceptions.ServiceError -> hành động
├── oci_clients.py    # dựng client OCI, list availability domain
├── shutdown.py       # SIGINT/SIGTERM + sleep ngắt được
├── notify.py         # thông báo Telegram (tuỳ chọn)
└── logging_setup.py  # log ra stdout + file xoay vòng

tests/smoke_test.py   # test offline, không cần tài khoản OCI
.github/workflows/build-image.yml   # CI: test -> build multi-arch -> push GHCR
```

---

## 2. Bố cục thư mục trên Pi — mọi thứ nằm ở MỘT chỗ

Không dùng `~/.oci`, `~/.ssh` hay bất kỳ thư mục hệ thống nào. Tất cả config và key nằm
chung trong thư mục project, và cả thư mục được mount vào container ở **`/data`**:

```
~/oracal-flex/                 (trên Pi)        →  /data/ (trong container, read-only)
├── .env                       cấu hình         →  /data/.env
├── oci_config                 config OCI       →  /data/oci_config
├── oci_api_key.pem            private API key  →  /data/oci_api_key.pem
├── id_ed25519.pub             SSH public key   →  /data/id_ed25519.pub
└── logs/                      log (ghi được)   →  /var/log/a1launcher/
```

⚠️ **Mọi đường dẫn trong `.env` và `oci_config` là đường dẫn BÊN TRONG container**
(`/data/...`), không phải đường dẫn trên Pi.

Cả bốn file đều đã có trong `.gitignore` / `.dockerignore`, nhưng vẫn **đừng `git add .`**
trong thư mục này.

---

## 3. Chuẩn bị từng file

Làm lần lượt trên Pi, trong thư mục project:

```bash
cd ~/oracal-flex
```

### 3.1. `oci_api_key.pem` + `oci_config` — lấy từ Oracle Console

1. <https://cloud.oracle.com> → **avatar góc phải trên** → **My profile** →
   **Tokens and keys** → **API keys** → **Add API key**.
2. **Generate API key pair** → **Download private key** → bấm **Add**.
3. Oracle hiện khung **Configuration file preview** (bắt đầu bằng `[DEFAULT]`) → copy lại.
   Lỡ đóng thì bấm **⋮** ở dòng key → **View configuration file**.

Đưa file `.pem` lên Pi (chạy trên máy đã tải file về):

```bash
scp ~/Downloads/<ten-file>.pem <user>@<pi>:~/oracal-flex/oci_api_key.pem
```

Trên Pi, tạo `oci_config`, dán đoạn vừa copy, **sửa dòng `key_file`**:

```bash
nano oci_config
```

```ini
[DEFAULT]
user=ocid1.user.oc1..aaaa...
fingerprint=12:34:56:...
tenancy=ocid1.tenancy.oc1..aaaa...
region=ap-singapore-2
key_file=/data/oci_api_key.pem
```

```bash
chmod 600 oci_api_key.pem
```

### 3.2. `id_ed25519.pub` — SSH public key

Key này dùng để SSH vào instance sau khi tạo. Dùng key của **máy mà bạn sẽ SSH từ đó**
(ví dụ Mac) — chỉ copy file `.pub`, private key ở yên trên máy đó:

```bash
scp ~/.ssh/id_ed25519.pub <user>@<pi>:~/oracal-flex/id_ed25519.pub
```

Chưa có key thì tạo trước bằng `ssh-keygen -t ed25519` (Enter hết).

### 3.3. `.env`

```bash
cp .env.example .env && nano .env
```

Hoặc chỉ ghi những dòng cần — mọi biến khác lấy mặc định. `.env` tối thiểu:

```ini
OCI_CONFIG_FILE=/data/oci_config
SSH_PUBLIC_KEY_PATH=/data/id_ed25519.pub
COMPARTMENT_ID=ocid1.tenancy.oc1..aaaa...
SUBNET_ID=ocid1.subnet.oc1.ap-singapore-2.aaaa...
IMAGE_ID=ocid1.image.oc1.ap-singapore-2.aaaa...
```

Lấy ba OCID:

| Biến | Lấy ở đâu |
|---|---|
| `COMPARTMENT_ID` | dùng luôn giá trị `tenancy=` trong `oci_config`: `echo "COMPARTMENT_ID=$(grep '^tenancy' oci_config \| cut -d= -f2 \| tr -d ' ')" >> .env` |
| `SUBNET_ID` | Console → **Networking** → **Virtual cloud networks** → VCN → **Subnets** → public subnet → **Copy** OCID. Chưa có VCN: **Start VCN Wizard** → *Create VCN with Internet Connectivity* |
| `IMAGE_ID` | chạy lệnh ở dưới — hỏi thẳng Oracle, đồng thời kiểm tra luôn `oci_config` + `.pem` có chạy không |

Liệt kê image Ubuntu **aarch64** hợp với A1 trong region của bạn:

```bash
docker run --rm -v "$PWD:/data:ro" --entrypoint python ghcr.io/kiendaotac/oracal-flex:latest -c '
import oci
cfg = oci.config.from_file("/data/oci_config")
c = oci.core.ComputeClient(cfg)
imgs = oci.pagination.list_call_get_all_results(
    c.list_images, cfg["tenancy"],
    operating_system="Canonical Ubuntu",
    shape="VM.Standard.A1.Flex",
    sort_by="TIMECREATED", sort_order="DESC").data
for i in imgs[:5]:
    print(i.display_name, "\n  ", i.id)
'
```

Chọn bản `Canonical-Ubuntu-24.04-aarch64-...` (không phải Minimal) và copy OCID bên dưới.

⚠️ `SUBNET_ID` và `IMAGE_ID` phải **cùng region** với `oci_config`, và region đó phải là
**home region** (Console → avatar → **Tenancy** → *Home region*) thì mới được tính free.

### 3.4. Thư mục log

```bash
mkdir -p logs
```

---

## 4. Lấy image

### 4.1. Pull từ GHCR (CI build sẵn)

```bash
docker pull ghcr.io/kiendaotac/oracal-flex:latest
```

Package đang **public** — không cần `docker login`. Docker tự chọn layer arm64.

Workflow `.github/workflows/build-image.yml` chỉ chạy khi push lên **`master`/`main`** hoặc
push tag `v*`. Push lên `develop` **không** tạo image mới.

| Job | Làm gì |
|---|---|
| `test` | chạy `tests/smoke_test.py` + preflight trên config tốt (phải PASS) và config hỏng (phải FAIL) |
| `build` | build multi-arch `linux/amd64` + `linux/arm64`, push lên GHCR |
| `smoke-test` | pull lại image vừa push, chạy thử, kiểm tra manifest có đủ 2 kiến trúc |

| Tag | Khi nào |
|---|---|
| `latest` | push lên nhánh mặc định (`master`) |
| `v1.2.3`, `v1.2` | push git tag `v*` |
| `sha-abc1234` | mọi commit |

> **Package mới tạo trên GHCR mặc định là PRIVATE.** Mở public (làm một lần): trang repo →
> **Packages** → chọn package → **Package settings** → **Danger Zone** → **Change visibility**
> → **Public**. `GITHUB_TOKEN` không có quyền đổi visibility nên CI không làm hộ được; job
> `smoke-test` chỉ cảnh báo nếu package vẫn private.

> Job `build` báo `403 denied` lúc push: *Settings → Actions → General → Workflow
> permissions* → **Read and write permissions**.

### 4.2. Hoặc build tại chỗ

```bash
docker build -t a1launcher:latest .                                    # build trên Pi
docker buildx build --platform linux/arm64 -t a1launcher:latest --load .  # build trên Mac/PC
```

Khi đó thay `ghcr.io/kiendaotac/oracal-flex:latest` bằng `a1launcher:latest` trong các lệnh dưới.

---

## 5. Preflight check — **chạy trước, luôn luôn**

`--check` chỉ kiểm tra cấu hình rồi thoát, **không launch gì cả**:

```bash
docker run --rm -v "$PWD:/data:ro" ghcr.io/kiendaotac/oracal-flex:latest --env-file /data/.env --check
```

Kết quả mong đợi ở dòng cuối: `N passed, 0 warning(s), 0 failed` → `All checks passed`.

Nó kiểm tra và in PASS/FAIL từng mục:

- đủ biến bắt buộc, không rỗng;
- định dạng OCID (`ocid1.tenancy`/`ocid1.compartment`, `ocid1.subnet`, `ocid1.image`);
- `ocpus`/`memory_gb` trong hạn mức Always Free (≤ 4 OCPU, ≤ 24 GB, ≤ 6 GB mỗi OCPU)
  và còn dư bao nhiêu cho instance A1 thứ hai;
- `oci_config` tồn tại, đọc được, profile hợp lệ, `key_file` trỏ tới file có thật;
- SSH public key: tồn tại, đúng 1 dòng, bắt đầu bằng `ssh-ed25519`/`ssh-rsa`/`ecdsa-`,
  **FAIL nếu lỡ trỏ vào private key**, và **in nội dung key ra để mắt kiểm tra**;
- gọi thật 1 API read-only (`ListAvailabilityDomains`) và in danh sách AD tìm được.

Có bất kỳ `FAIL` nào → exit code `1`.

> Khi chạy launch thật, preflight vẫn tự chạy lại trước và **từ chối launch nếu có FAIL**.
> Bỏ qua bằng `--skip-preflight` (không khuyến khích). Kiểm tra offline: `--no-api-check`.

---

## 6. Chạy thật trên Raspberry Pi

```bash
docker run -d --name a1launcher --restart on-failure \
  -v "$PWD:/data:ro" \
  -v "$PWD/logs:/var/log/a1launcher" \
  ghcr.io/kiendaotac/oracal-flex:latest --env-file /data/.env
```

| Mount | Ý nghĩa |
|---|---|
| `-v "$PWD:/data:ro"` | cả thư mục project → `/data`, **read-only** |
| `-v "$PWD/logs:/var/log/a1launcher"` | log ghi ra `./logs` trên Pi, còn lại sau khi xoá container |
| `--env-file /data/.env` | flag của app (không phải của Docker) — chỉ chỗ đọc `.env` |

### Restart policy — vì sao là `on-failure`

- **Pi mất điện / reboot**: container bị ngắt đột ngột được tính là lỗi → Docker tự chạy lại
  khi Pi khởi động (cần `sudo systemctl enable docker` — kiểm tra bằng
  `systemctl is-enabled docker`).
- **Tạo instance thành công**: app exit `0` → Docker **không** chạy lại → không sinh
  instance thứ hai.

> ⚠️ **Không dùng `--restart unless-stopped` / `always`**: hai policy này chạy lại container
> **kể cả khi exit `0`**, tức là ngay sau khi tạo được instance nó sẽ khởi động lại và
> **tạo tiếp instance thứ hai** (2 OCPU/12 GB vẫn vừa quota). Nếu lỡ dùng thì phải
> `docker rm -f a1launcher` ngay khi nhận thông báo.

### Lệnh hằng ngày

```bash
docker logs -f a1launcher              # xem log trực tiếp (Ctrl+C để thoát, app vẫn chạy)
docker logs --tail 20 a1launcher       # 20 dòng cuối
docker ps                              # chỉ được có MỘT container a1launcher
docker rm -f a1launcher                # dừng + xoá container
```

Sửa `.env` xong phải **tạo lại container** thì mới nhận cấu hình mới:

```bash
docker rm -f a1launcher && docker run -d --name a1launcher --restart on-failure \
  -v "$PWD:/data:ro" -v "$PWD/logs:/var/log/a1launcher" \
  ghcr.io/kiendaotac/oracal-flex:latest --env-file /data/.env
```

Cập nhật image mới: `docker pull ghcr.io/kiendaotac/oracal-flex:latest` rồi tạo lại container
như trên.

### Log bình thường trông như thế nào

```
Attempt #4: launching VM.Standard.A1.Flex in rjjd:AP-SINGAPORE-2-AD-1
Out of host capacity in rjjd:AP-SINGAPORE-2-AD-1 (3 so far) — trying the next AD
Round 4 found no capacity; sleeping 283s
```

`Out of host capacity` là **bình thường** — Oracle hết máy A1 free, app tự thử lại mãi.
Có thể mất vài giờ tới vài tuần. Khi thành công sẽ có dòng `SUCCESS — instance ... created`
kèm public IP (và tin Telegram nếu bật). SSH vào từ máy có private key:

```bash
ssh ubuntu@<IP>
```

### Những điểm cần nhớ

- **Image không chứa credential.** Mọi key chỉ được mount lúc chạy; `.dockerignore` chặn
  chúng khỏi build context — xoá image là không còn key.
- Container chạy bằng user `app` **uid/gid 1000** — trùng user mặc định của Raspberry Pi OS,
  nên `chmod 600 oci_api_key.pem` vẫn đọc được. Nếu user trên Pi không phải uid 1000, thêm
  `--user "$(id -u):$(id -g)"`.

---

## 7. Tuỳ chỉnh thời gian retry

Oracle giới hạn rất chặt số lần gọi `LaunchInstance` với tài khoản free: thực tế cứ khoảng
**4 lần trong 5 phút** là dính `429 TooManyRequests`. Thử dày hơn **không** tăng cơ hội,
chỉ bị phạt nghỉ lâu hơn. Cấu hình đang dùng (đã là mặc định trong `.env.example`):

```ini
MIN_DELAY_SECONDS=240          # nghỉ ngẫu nhiên tối thiểu giữa 2 vòng
MAX_DELAY_SECONDS=360          # ... tối đa  → ~1 lần mỗi 4–6 phút, ~290 lần/ngày
RATE_LIMIT_SLEEP_SECONDS=600   # dính 429 thì nghỉ thêm 10 phút
```

| Biến | Mặc định code | Ghi chú |
|---|---|---|
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | `60` / `180` | nghỉ ngẫu nhiên giữa 2 vòng. `60–180` quá dày cho tài khoản free |
| `AD_DELAY_SECONDS` | `10` | nghỉ giữa 2 AD trong cùng vòng (region 1 AD thì không dùng tới) |
| `RATE_LIMIT_SLEEP_SECONDS` | `900` | nghỉ thêm khi dính HTTP 429 |
| `MAX_ROUNDS` | `0` | `0` = lặp vô hạn |

Log vẫn thấy `429` đều đặn → tăng `MIN_DELAY_SECONDS`/`MAX_DELAY_SECONDS` thêm 60–120s.

### Cấu hình máy

| Biến | Mặc định | Ghi chú |
|---|---|---|
| `OCPUS` / `MEMORY_GB` | `2` / `12` | tổng mọi máy A1 ≤ 4 OCPU / 24 GB, ≤ 6 GB mỗi OCPU |
| `BOOT_VOLUME_GB` | `50` | tối thiểu 50, tổng block storage free 200 GB |
| `DISPLAY_NAME` | `a1-flex` | tên instance |
| `AVAILABILITY_DOMAINS` | rỗng | rỗng = tự lấy và thử hết mọi AD |

Máy nhỏ dễ có chỗ hơn: 2/12 dễ hơn 4/24, 1/6 còn dễ hơn nữa. Tạo xong vẫn **tăng được**
(Console → Instance → **Edit** → **Edit shape**; máy sẽ reboot, và có thể báo hết capacity).

### Toàn bộ biến

| Biến | Mặc định | Ghi chú |
|---|---|---|
| `COMPARTMENT_ID` / `SUBNET_ID` / `IMAGE_ID` | — | bắt buộc |
| `SSH_PUBLIC_KEY_PATH` | `/keys/id_ed25519.pub` | đặt `/data/id_ed25519.pub` |
| `OCI_CONFIG_FILE` | `~/.oci/config` | đặt `/data/oci_config` |
| `OCI_PROFILE` | `DEFAULT` | profile trong `oci_config` |
| `SHAPE` | `VM.Standard.A1.Flex` | |
| `ASSIGN_PUBLIC_IP` | `true` | |
| `FAULT_DOMAIN` / `NSG_IDS` | rỗng | tuỳ chọn |
| `LOG_FILE` | `/var/log/a1launcher/a1launcher.log` | log xoay vòng 5 MB × 3 |
| `LOG_LEVEL` | `INFO` | |
| `TELEGRAM_*` | tắt | xem mục 8 |

Thứ tự ưu tiên (cao xuống thấp): **biến môi trường thật → `.env` → `config.yaml` → mặc định**.

---

## 8. Thông báo Telegram

Khi tạo thành công, app gửi tin nhắn kèm OCID, public IP, số lần thử và tổng thời gian.
Gửi lỗi chỉ ghi log, không ảnh hưởng kết quả launch.

1. **Bot**: dùng bot có sẵn (**@BotFather** → `/mybots` → chọn bot → **API Token**, đừng bấm
   *Revoke*) hoặc tạo mới (`/newbot`). Token dạng `123456789:AAH-xxxx`.
2. **Mở chat với bot** và gửi một tin bất kỳ — bot không thể nhắn trước cho bạn.
3. **Chat ID**: nhắn `/start` cho **@userinfobot**, lấy số ở dòng `Id:`. Đây là ID **tài khoản
   của bạn** — **không phải** dãy số đầu token (đó là ID của bot).
4. Thêm vào `.env`:

   ```ini
   TELEGRAM_ENABLED=true
   TELEGRAM_BOT_TOKEN=123456789:AAH-xxxx
   TELEGRAM_CHAT_ID=987654321
   ```

5. Gửi thử — lệnh tự đọc token từ `.env`, không phải gõ token ra terminal:

   ```bash
   (set -a; . ./.env; curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" -d chat_id="$TELEGRAM_CHAT_ID" -d text="Test a1launcher OK")
   ```

   Phải thấy `{"ok":true,...` và nhận được tin. Xong thì **tạo lại container** (mục 6).

| Lỗi | Nguyên nhân |
|---|---|
| `403 ... bot can't send messages to the bot` | `TELEGRAM_CHAT_ID` đang là ID của bot → lấy lại ở @userinfobot |
| `400 ... chat not found` | chưa nhắn tin gì cho bot → mở bot, bấm **Start** |
| `401 Unauthorized` | token sai hoặc đã bị revoke |

---

## 9. Các flag

| Flag | Ý nghĩa |
|---|---|
| `--env-file /data/.env` | đường dẫn `.env` (bắt buộc với bố cục `/data`) |
| `--check` | chỉ preflight rồi thoát |
| `--dry-run` | in payload `LaunchInstanceDetails`, không gọi `LaunchInstance` |
| `--no-api-check` | preflight không gọi API |
| `--skip-preflight` | launch thẳng, bỏ preflight |
| `--config` | đường dẫn `config.yaml` |
| `--profile` | ghi đè `OCI_PROFILE` |
| `--log-file` / `--log-level` | ghi đè cấu hình log |

---

## 10. Xử lý sự cố

| Triệu chứng | Cách xử lý |
|---|---|
| `docker pull` báo `denied` dù package public | Pi còn lưu token GHCR cũ đã hết hạn → `docker logout ghcr.io` rồi pull lại |
| `429 TooManyRequests` liên tục | tăng `MIN_DELAY_SECONDS`/`MAX_DELAY_SECONDS` (mục 7); kiểm tra `docker ps` không có 2 container cùng chạy, và không máy nào khác dùng chung API key |
| `Out of host capacity` mãi | bình thường — cứ để chạy; lâu quá thì giảm `OCPUS`/`MEMORY_GB` |
| `--check` FAIL ở `OCI key_file` | `key_file` trong `oci_config` phải là `/data/oci_api_key.pem` |
| `--check` FAIL ở SSH key | `SSH_PUBLIC_KEY_PATH=/data/id_ed25519.pub`, file phải là `.pub` |
| `NotAuthorizedOrNotFound` | sai OCID, hoặc subnet/image khác region với `oci_config` |

---

## 11. Chạy trực tiếp (không Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# sửa SSH_PUBLIC_KEY_PATH / OCI_CONFIG_FILE / LOG_FILE và key_file về path trên host
python -m a1launcher --check
python -m a1launcher
```

Chạy test offline (không cần tài khoản OCI, không cần mạng):

```bash
python tests/smoke_test.py
```

---

## 12. Cách tool xử lý lỗi

Phân loại dựa trên **`status` và `code`** có cấu trúc của `oci.exceptions.ServiceError`,
không dò chuỗi text:

| Lỗi | Hành động |
|---|---|
| `OutOfCapacity` / `OutOfHostCapacity` | log 1 dòng, thử AD kế tiếp |
| `LimitExceeded` / `QuotaExceeded` | **dừng hẳn, exit `1`** — đã hết quota A1 |
| HTTP 429 / `TooManyRequests` | nghỉ thêm `RATE_LIMIT_SLEEP_SECONDS` (mặc định code 15 phút, `.env.example` 10 phút) |
| 401/403, `NotAuthorizedOrNotFound` | cảnh báo to (thường là sai OCID/policy), vẫn thử tiếp |
| còn lại | log full traceback, thử tiếp |

> Ngoại lệ duy nhất có so chuỗi: một số region vẫn trả `500 InternalError` với message
> `"Out of host capacity."` thay vì mã lỗi riêng. Trường hợp này được nhận diện riêng
> (xem `errors.py::_is_legacy_capacity_error`), giới hạn chặt trong 500/`InternalError`
> để không che mất lỗi khác — nếu không thì vòng lặp sẽ chết đúng ngay lỗi mà nó sinh ra để chịu đựng.

### Exit code

| Code | Nghĩa |
|---|---|
| `0` | tạo được instance (hoặc `--check` PASS hết / `--dry-run` xong) |
| `1` | preflight FAIL, hết quota A1, hoặc lỗi cấu hình |
| `130` | nhận SIGINT/SIGTERM và thoát sạch |

Nhận `Ctrl-C` hoặc `docker stop`, tool **ngắt luôn giấc ngủ backoff** (không chờ hết 180s),
in số lần đã thử rồi thoát gọn.

---

## 13. Ghi chú về hạn mức Always Free

- Tổng cho **tất cả** instance A1: **4 OCPU + 24 GB RAM**.
- Cấu hình mặc định `2 OCPU / 12 GB` nằm gọn trong free tier và **vẫn dư 2 OCPU + 12 GB**
  cho một instance A1 thứ hai sau này. Preflight in rõ phần còn dư này.
- Tỉ lệ A1.Flex: tối đa **6 GB RAM mỗi OCPU**.
- Block storage Always Free tổng cộng 200 GB; boot volume 50 GB là an toàn. Đặt trên
  200 GB sẽ bị `--check` cảnh báo WARN vì có thể phát sinh phí.
- Chỉ free ở **home region** (Console → avatar → **Tenancy** → *Home region*).
- Tài khoản **Free Tier** không thể bị trừ tiền — vượt hạn mức thì Oracle chặn. Tài khoản
  **Pay As You Go** vượt hạn mức sẽ bị tính tiền → nên đặt *Billing → Budgets* cảnh báo 1 USD.
