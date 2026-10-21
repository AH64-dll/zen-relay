# التثبيت — Install

## المتطلبات

| المكوّن | الإصدار | ملاحظات |
|---|---|---|
| Linux x86_64 | glibc 2.34+ | الملف التنفيذي الجاهز. غير ذلك: ابنِ من المصدر |
| Node.js | 22+ | لتشغيل `dsh-stack` والـHarness |
| `dsh` | 0.2.x | `npm i -g @deepseek-ai/dsh` |
| `cloudflared` | أي إصدار حديث | اختياري: للنفق العام فقط |
| Rust | 1.80+ | اختياري: للبناء من المصدر |

## 1. المرحّل وحده (أسرع طريق)

```bash
git clone https://github.com/utopianumber39ray-spec/zen-relay.git
cd zen-relay
./run.sh
```

أول تشغيل ينشئ `zen-router/settings.json` بمفتاح عشوائي. اقرأه:

```bash
grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1
```

فحص:

```bash
curl -s http://127.0.0.1:8901/healthz
# {"ok":true,"warm":39,"pool_target":40,...}
```

الاستخدام المحلي:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8901/v1
export OPENAI_API_KEY=rly_...
```

## 2. الحزمة كاملة (مرحّل + نفق + Harness)

> شغّل المثبّت من طرفية، لا من داخل واجهة الـHarness: هو يعيد تشغيل الواجهة
> التي قد تكون تتحدث عبرها.

```bash
git clone https://github.com/utopianumber39ray-spec/zen-relay.git
cd zen-relay
./install.sh --dry-run     # يعرض كل خطوة بلا تعديل
./install.sh               # يثبّت ويشغّل
```

ما يفعله:

1. يتحقق من `node >= 22` و`dsh` و`cloudflared`.
2. ينشئ `zen-router/settings.json` إن لم يوجد ويطبع المفتاح.
3. يستدعي `stack/install.sh` الذي:
   - يكتشف المسارات ويكتب `~/.config/dsh-stack/config.json`;
   - يعطّل الوحدات القديمة المتعارضة (`dsh-harness`, `zen-rotator`,
     `cloudflared-tunnel`) ويسجل حالتها للاسترجاع;
   - يثبّت `dsh-stack.service` (‏`Type=notify`, `WatchdogSec=90`)؛
   - ينتظر جاهزية المرحّل والنفق والـHarness.

إن كان `Linger=no`:

```bash
sudo loginctl enable-linger $(id -un)
```

## 3. ربط الـHarness بالمرحّل

انسخ `config/dsh-cordis.patch.yml` إلى `~/.dsh/cordis.patch.yml` ثم ضع المفتاح في
مخزن الاعتماد (لا في الملف):

```bash
# الطريقة الأضمن: متغيّر بيئة في وحدة systemd
mkdir -p ~/.config/systemd/user/dsh-stack.service.d
printf '[Service]\nEnvironment=OMP_ZEN_RELAY_KEY=%s\n' "$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)" \
  > ~/.config/systemd/user/dsh-stack.service.d/relay-key.conf
chmod 600 ~/.config/systemd/user/dsh-stack.service.d/relay-key.conf
systemctl --user daemon-reload
```

أو اكتبه في `~/.dsh/.credentials.yaml` تحت `refs:` ثم `chmod 600` عليه. الطرق
الثلاث وأسبقيتها في [`SETUP-GUIDE.md`](SETUP-GUIDE.md).

أعد التشغيل:

```bash
systemctl --user restart dsh-stack
```

## 4. التحقق

```bash
node stack/bin/dsh-stack status          # جدول المكوّنات والجاهزية
node stack/bin/dsh-stack doctor          # المسارات والوحدات والصلاحيات
curl -s http://127.0.0.1:8901/healthz
cat ~/dsh-url.txt                         # رابط الدخول للـHarness
```

## 5. التحديث

```bash
git pull
cargo build --release --manifest-path zen-router/rust/Cargo.toml   # إن بنيت من المصدر
systemctl --user restart dsh-stack
```

## 6. إلغاء التثبيت

```bash
./stack/install.sh --uninstall          # يوقف ويعيد الوحدات القديمة
./stack/install.sh --uninstall --purge  # يحذف أيضاً حالة dsh-stack
```

لا يحذف المثبّت `zen-router/settings.json` ولا جلسات الـHarness.

## منصّات أخرى

لا يوجد ملف تنفيذي لويندوز/ماك. على تلك الأجهزة:

```bash
cd zen-router/rust && cargo build --release
```

ثم شغّل `target/release/zen-relay` (أو `.exe`). راجع `docs/OTHER-DEVICES.md`.
