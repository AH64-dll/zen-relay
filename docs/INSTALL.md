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
git clone https://github.com/AH64-dll/zen-relay.git
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
git clone https://github.com/AH64-dll/zen-relay.git
cd zen-relay
./install.sh --dry-run     # يعرض كل خطوة بلا تعديل
./install.sh               # يثبّت ويشغّل
```

ما يفعله:

1. يتحقق من `node >= 22` و`dsh` و`cloudflared` و`opencode`.
2. ينشئ `zen-router/settings.json` إن لم يوجد ويطبع المفتاح.
3. يستدعي `stack/install.sh` الذي:
   - يكتشف المسارات ويكتب `~/.config/dsh-stack/config.json`;
   - يفعّل الجسر (`opencode-bridge`) إن وُجد `opencode` على الجهاز — وهو
     ما يجعل نماذج OpenCode المجانية تصل إلى الـHarness، لأن الطلب يصدر من
     عميل OpenCode نفسه. عطّله بـ`--no-bridge`;
   - يعطّل الوحدات القديمة المتعارضة (`dsh-harness`, `zen-rotator`,
     `cloudflared-tunnel`) ويسجل حالتها للاسترجاع;
   - يثبّت `dsh-stack.service` (‏`Type=notify`, `WatchdogSec=90`)؛
   - ينتظر جاهزية المرحّل والنفق والـHarness.

إن كان `Linger=no`:

```bash
sudo loginctl enable-linger $(id -un)
```

## 3. ربط الـHarness بالمرحّل

انسخ `config/dsh-cordis.patch.yml` إلى `~/.dsh/cordis.patch.yml` ثم وفّر المفتاح
باسم `OMP_ZEN_RELAY_KEY` (لا تضعه في الملف). `dsh 0.2.x` لا يوفّر أمر
`dsh config credentials`؛ استخدم إحدى الطريقتين:

**أ) من الواجهة:** صفحة Models في الـHarness ← خزّن `OMP_ZEN_RELAY_KEY`.

**ب) من الطرفية (مناسب للتثبيت بلا واجهة):**

```bash
install -m 600 /dev/null ~/.config/dsh-stack/relay.env
printf 'OMP_ZEN_RELAY_KEY=rly_...\n' > ~/.config/dsh-stack/relay.env
mkdir -p ~/.config/systemd/user/dsh-stack.service.d
cat > ~/.config/systemd/user/dsh-stack.service.d/10-relay-key.conf <<'EOF'
[Service]
EnvironmentFile=%h/.config/dsh-stack/relay.env
EOF
systemctl --user daemon-reload
```

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
