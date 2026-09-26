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

## 2. Chuẩn bị

Cần có sẵn trên máy (host):

1. **`~/.oci/config`** + private API key (`.pem`) — tạo ở OCI Console:
   *Profile → User settings → API keys → Add API key*.
2. **SSH public key** (`~/.ssh/id_ed25519.pub`) để SSH vào instance sau khi tạo.
3. Ba OCID:
   - `COMPARTMENT_ID` — OCID tenancy hoặc compartment.
   - `SUBNET_ID` — subnet của VCN (nên là public subnet nếu muốn có public IP).
   - `IMAGE_ID` — **bắt buộc là image aarch64** (bản ARM của Ubuntu / Oracle Linux).
     Lấy nhanh bằng OCI CLI:
     ```bash
     oci compute image list --compartment-id <OCID> --shape VM.Standard.A1.Flex --output table
     ```

---

## 3. Cấu hình

Copy file mẫu rồi sửa:

```bash
cp .env.example .env          # hoặc: cp config.yaml.example config.yaml
```

Thứ tự ưu tiên (cao xuống thấp): **biến môi trường thật → `.env` → `config.yaml` → mặc định**.
Dùng file nào cũng được, hoặc kết hợp cả hai.

| Biến | Mặc định | Ghi chú |
|---|---|---|
| `COMPARTMENT_ID` / `SUBNET_ID` / `IMAGE_ID` | — | bắt buộc |
| `SSH_PUBLIC_KEY_PATH` | `/keys/id_ed25519.pub` | **đường dẫn bên trong container** |
| `OCI_CONFIG_FILE` | `~/.oci/config` | **đường dẫn bên trong container** |
| `OCI_PROFILE` | `DEFAULT` | profile trong file config |
| `OCPUS` / `MEMORY_GB` | `2` / `12` | nằm trong hạn mức free |
| `BOOT_VOLUME_GB` | `50` | tối thiểu 50 |
| `MIN_DELAY_SECONDS` / `MAX_DELAY_SECONDS` | `60` / `180` | nghỉ ngẫu nhiên giữa 2 vòng |
| `AD_DELAY_SECONDS` | `10` | nghỉ giữa 2 AD trong cùng một vòng |
| `RATE_LIMIT_SLEEP_SECONDS` | `900` | nghỉ thêm khi dính HTTP 429 |
| `MAX_ROUNDS` | `0` | `0` = lặp vô hạn |
| `AVAILABILITY_DOMAINS` | rỗng | rỗng = tự lấy và thử hết mọi AD |
| `LOG_FILE` | `/var/log/a1launcher/a1launcher.log` | log xoay vòng 5 MB × 3 |
| `TELEGRAM_ENABLED` | `false` | xem mục 8 |

---

## 4. Lấy image

### 4.1. Cách khuyên dùng: pull từ GHCR (CI build sẵn)

Push code lên GitHub là xong — workflow `.github/workflows/build-image.yml` tự chạy
3 job nối tiếp:

| Job | Làm gì |
|---|---|
| `test` | chạy `tests/smoke_test.py` + preflight trên config tốt (phải PASS) và config hỏng (phải FAIL) |
| `build` | build multi-arch `linux/amd64` + `linux/arm64`, push lên GHCR |
| `smoke-test` | pull lại image vừa push, chạy thử, kiểm tra manifest có đủ 2 kiến trúc |

Tag được tạo tự động:

| Tag | Khi nào |
|---|---|
| `latest` | push lên nhánh mặc định |
| `v1.2.3`, `v1.2` | push git tag `v*` |
| `sha-abc1234` | mọi commit |

Trên Raspberry Pi chỉ cần:

```bash
docker pull ghcr.io/<github-user>/<repo>:latest
```

Docker tự chọn layer arm64 — không cần làm gì thêm.

### 4.2. Mở public package (làm 1 lần, để Pi khỏi phải `docker login`)

⚠️ **Package trên GHCR mặc định là PRIVATE**, kể cả khi repo là public. Chưa mở thì
`docker pull` trên Pi sẽ báo `denied`.

Mở public — **chỉ làm đúng một lần**, từ đó mọi lần push sau đều public:

1. Push code lên GitHub, đợi workflow chạy xong lần đầu (package phải tồn tại rồi
   mới mở được).
2. Mở `https://github.com/<github-user>/<repo>/pkgs/container/<repo>`
   (hoặc: trang repo → mục **Packages** bên phải → chọn package).
3. Bấm **Package settings** ở cột phải.
4. Kéo xuống **Danger Zone** → **Change visibility** → chọn **Public** → xác nhận
   bằng cách gõ tên package.

Xong. Trên Pi pull thẳng, không cần đăng nhập, không cần token gì hết.

> **Tại sao CI không tự làm hộ?** `GITHUB_TOKEN` không có quyền đổi visibility của
> package — muốn tự động thì phải nhét thêm một PAT quyền admin vào secrets, phiền
> và rủi ro hơn hẳn so với một lần bấm chuột.
>
> Bù lại, job `smoke-test` có bước **"Check the package is pullable without logging in"**:
> nó thử gọi GHCR **không kèm credential**. Nếu vẫn private thì workflow không fail,
> chỉ in cảnh báo vàng kèm đúng đường dẫn cần bấm trong phần *Summary* của lần chạy đó.
> Sau khi mở public, chạy lại workflow sẽ thấy `✅ Image is public`.

> Nếu vẫn muốn giữ private: trên Pi đăng nhập bằng PAT classic scope `read:packages`:
> ```bash
> echo <PERSONAL_ACCESS_TOKEN> | docker login ghcr.io -u <github-user> --password-stdin
> ```

> Nếu job `build` báo lỗi `403 denied` lúc push: vào *Settings → Actions → General →
> Workflow permissions* và chọn **Read and write permissions**.

Cập nhật về sau:

```bash
docker pull ghcr.io/<github-user>/<repo>:latest     # kéo bản mới
# hoặc thêm --pull always vào lệnh docker run
```

### 4.3. Hoặc build tại chỗ

Build ngay trên con Pi:

```bash
docker build -t a1launcher:latest .
```

Build trên máy khác (Mac/PC) rồi mới đẩy sang Pi:

```bash
docker buildx build --platform linux/arm64 -t a1launcher:latest --load .
```

Phần dưới dùng tên `a1launcher:latest`; nếu pull từ GHCR thì thay bằng
`ghcr.io/<github-user>/<repo>:latest`.

---

## 5. Preflight check — **chạy trước, luôn luôn**

`--check` chỉ kiểm tra cấu hình rồi thoát, **không launch gì cả**:

```bash
docker run --rm \
  -v ~/.oci:/home/app/.oci:ro \
  -v "$PWD/.env:/app/.env:ro" \
  -v ~/.ssh/id_ed25519.pub:/keys/id_ed25519.pub:ro \
  a1launcher:latest --check
```

Nó kiểm tra và in PASS/FAIL từng mục:

- đủ biến bắt buộc, không rỗng;
- định dạng OCID (`ocid1.tenancy`/`ocid1.compartment`, `ocid1.subnet`, `ocid1.image`);
- `ocpus`/`memory_gb` trong hạn mức Always Free (≤ 4 OCPU, ≤ 24 GB, ≤ 6 GB mỗi OCPU)
  và còn dư bao nhiêu cho instance A1 thứ hai;
- `~/.oci/config` tồn tại, đọc được, profile hợp lệ, `key_file` trỏ tới file có thật;
- SSH public key: tồn tại, đúng 1 dòng, bắt đầu bằng `ssh-ed25519`/`ssh-rsa`/`ecdsa-`,
  **FAIL nếu lỡ trỏ vào private key**, và **in nội dung key ra để mắt kiểm tra**;
- gọi thật 1 API read-only (`ListAvailabilityDomains`) và in danh sách AD tìm được.

Có bất kỳ `FAIL` nào → exit code `1`. Chỉ khi tất cả PASS mới nên chạy launch thật.

> Khi chạy launch thật, preflight vẫn tự chạy lại trước và **từ chối launch nếu có FAIL**.
> Bỏ qua bằng `--skip-preflight` (không khuyến khích).
> Muốn kiểm tra offline, không gọi API: thêm `--no-api-check`.

---

## 6. Chạy thật trên Raspberry Pi

```bash
docker run --rm --name a1launcher \
  -v ~/.oci:/home/app/.oci:ro \
  -v "$PWD/.env:/app/.env:ro" \
  -v ~/.ssh/id_ed25519.pub:/keys/id_ed25519.pub:ro \
  -v "$PWD/logs:/var/log/a1launcher" \
  a1launcher:latest
```

Container là **one-shot**: chạy retry tới khi tạo được instance thì exit `0`,
`--rm` sẽ tự xoá container.

Muốn chạy nền lâu dài (bỏ `--rm`, vì `--rm` xung đột với `--restart`):

```bash
docker run -d --name a1launcher --restart on-failure:3 \
  -v ~/.oci:/home/app/.oci:ro \
  -v "$PWD/.env:/app/.env:ro" \
  -v ~/.ssh/id_ed25519.pub:/keys/id_ed25519.pub:ro \
  -v "$PWD/logs:/var/log/a1launcher" \
  a1launcher:latest

docker logs -f a1launcher
```

> ⚠️ **Đừng dùng `--restart unless-stopped` hay `--restart always`.** Hai policy này
> chạy lại container **kể cả khi nó exit `0`** — tức là ngay sau khi tạo instance thành
> công, nó sẽ khởi động lại và **tạo tiếp instance thứ hai**, ăn nốt phần quota A1 còn lại.
> `on-failure` chỉ chạy lại khi exit code khác 0, nên launch xong là dừng hẳn.

### Những điểm cần nhớ khi mount

- **Image không chứa credential.** `.env`, `~/.oci`, `.pem`, SSH key đều chỉ được mount
  lúc chạy và `.dockerignore` chặn chúng khỏi build context — xoá image là không còn key.
- ⚠️ **`key_file` trong `~/.oci/config` phải là đường dẫn BÊN TRONG container, không phải
  đường dẫn trên host.** Đây là lỗi hay gặp nhất. Nếu mount `~/.oci` vào
  `/home/app/.oci` thì trong file config phải ghi:

  ```ini
  [DEFAULT]
  key_file=/home/app/.oci/oci_api_key.pem
  ```

  chứ không phải `/home/pi/.oci/oci_api_key.pem`. `--check` sẽ báo FAIL rõ ràng nếu sai.
- Container chạy bằng user `app` **uid/gid 1000** — trùng user mặc định của Raspberry Pi OS,
  nên file mount từ host đọc được mà không phải nới quyền. Nếu user trên Pi của bạn không
  phải uid 1000, thêm `--user "$(id -u):$(id -g)"` vào lệnh `docker run`.
- Mount `logs/` ra ngoài nếu muốn giữ log sau khi container biến mất.

### Dọn sạch sau khi xong

```bash
docker rmi ghcr.io/<github-user>/<repo>:latest   # hoặc a1launcher:latest
docker image prune -f                            # dọn layer thừa
docker builder prune -f                          # dọn cache build (nếu có build tại chỗ)
```

Credential không bao giờ nằm trong image, nên xoá image là sạch.

---

## 7. Chạy trực tiếp (không Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# nhớ sửa lại SSH_PUBLIC_KEY_PATH / OCI_CONFIG_FILE / LOG_FILE về path trên host
python -m a1launcher --check
python -m a1launcher
```

Chạy test offline (không cần tài khoản OCI, không cần mạng):

```bash
python tests/smoke_test.py
```

---

## 8. Tuỳ chọn

### `--dry-run`

In ra payload `LaunchInstanceDetails` cho từng AD rồi thoát, **không gọi `LaunchInstance`**:

```bash
docker run --rm ... a1launcher:latest --dry-run
```

### Thông báo Telegram

Bật trong `.env`:

```ini
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=987654321
```

Khi tạo thành công, tool gửi tin nhắn kèm OCID, public IP, số lần thử và tổng thời gian.
Gửi lỗi chỉ ghi log, không làm hỏng kết quả launch.

### Các flag khác

| Flag | Ý nghĩa |
|---|---|
| `--check` | chỉ preflight rồi thoát |
| `--dry-run` | in payload, không launch |
| `--no-api-check` | preflight không gọi API |
| `--skip-preflight` | launch thẳng, bỏ preflight |
| `--env-file` / `--config` | đổi đường dẫn `.env` / `config.yaml` |
| `--profile` | ghi đè `OCI_PROFILE` |
| `--log-file` / `--log-level` | ghi đè cấu hình log |

---

## 9. Cách tool xử lý lỗi

Phân loại dựa trên **`status` và `code`** có cấu trúc của `oci.exceptions.ServiceError`,
không dò chuỗi text:

| Lỗi | Hành động |
|---|---|
| `OutOfCapacity` / `OutOfHostCapacity` | log 1 dòng, thử AD kế tiếp |
| `LimitExceeded` / `QuotaExceeded` | **dừng hẳn, exit `1`** — đã hết quota A1 |
| HTTP 429 / `TooManyRequests` | nghỉ thêm `RATE_LIMIT_SLEEP_SECONDS` (mặc định 15 phút) |
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

## 10. Ghi chú về hạn mức Always Free

- Tổng cho **tất cả** instance A1: **4 OCPU + 24 GB RAM**.
- Cấu hình mặc định `2 OCPU / 12 GB` nằm gọn trong free tier và **vẫn dư 2 OCPU + 12 GB**
  cho một instance A1 thứ hai sau này. Preflight in rõ phần còn dư này.
- Tỉ lệ A1.Flex: tối đa **6 GB RAM mỗi OCPU**.
- Block storage Always Free tổng cộng 200 GB; boot volume 50 GB là an toàn. Đặt trên
  200 GB sẽ bị `--check` cảnh báo WARN vì có thể phát sinh phí.
