# zen-relay — Relay + Harness stack

مرحّل متوافق مع OpenAI API يوزّع الطلبات على عناوين خروج متغيرة (rotating egress
IPs) لتفادي حدود المعدل المفروضة لكل IP، مع حزمة تشغيل كاملة تربطه بـDeepSeek
Harness وتنشره على نفق HTTPS.

**النسخة الحالية: 1.1.7** · المصدر: `zen-router/rust/` · الملف التنفيذي:
`zen-router/bin/zen-relay`.

---

## ما يحتويه المستودع

| المسار | الوصف |
|---|---|
| `zen-router/rust/` | مصدر Rust الكامل للمرحّل |
| `zen-router/bin/zen-relay` | ملف تنفيذي جاهز لـLinux x86_64 |
| `run.sh` | تشغيل بأمر واحد (تنفيذي جاهز أو بناء من المصدر) |
| `install.sh` | تثبيت الحزمة كاملة: مرحّل + نفق + Harness |
| `stack/` | مشرف `dsh-stack` + plugin استئناف الجلسات `session-keeper` |
| `config/` | إعدادات نموذجية: `settings.example.json`، وحدة systemd، وربط dsh، وCaddyfile |
| `scripts/expose-relay.sh` | نشر المرحّل على نفق Cloudflare وطباعة العنوان والمفتاح |
| `docs/` | الدليل الكامل، المعمارية، التشغيل على أجهزة أخرى، التثبيت، تجمّع البروكسي، استكشاف الأخطاء |

> **الدليل الكامل خطوة بخطوة** — من جهاز فارغ إلى OpenCode وdsh يعملان عبر
> المرحّل، بكل التفاصيل والأوامر: [`docs/SETUP-GUIDE.md`](docs/SETUP-GUIDE.md).

---

## التشغيل السريع (المرحّل وحده)

```bash
git clone https://github.com/utopianumber39ray-spec/zen-relay.git
cd zen-relay
./run.sh
```

أول تشغيل:
- ينشئ `zen-router/settings.json` ويولّد `relay_api_key` عشوائياً؛
- يستمع على `http://127.0.0.1:8901`؛
- يبدأ ببناء pool من البروكسيات الحرة (يحتاج ثوانٍ ليصبح جاهزاً).

```bash
grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1   # المفتاح
curl -s http://127.0.0.1:8901/healthz                             # الجاهزية
```

استخدمه من أي عميل OpenAI:

- **Base URL:** `http://127.0.0.1:8901/v1`
- **API key:** المفتاح أعلاه
- **Model:** مثال `deepseek-v4.1-flash-free`

### الاستخدام من أجهزة أخرى

هذا ما كان المستودع ينقصه. التفاصيل الكاملة في
[`docs/OTHER-DEVICES.md`](docs/OTHER-DEVICES.md)، والخلاصة:

```bash
scripts/expose-relay.sh
# base URL : https://<name>.trycloudflare.com/v1
# api key  : rly_...
```

ثم على الجهاز الآخر:

```bash
export OPENAI_BASE_URL=https://<name>.trycloudflare.com/v1
export OPENAI_API_KEY=rly_...
```

> `zen-relay` ليس VPN: هو OpenAI-compatible API فقط. أي جهاز يستخدمه يجب أن يكون
> عميل OpenAI API (تطبيق، SDK، أو Harness).

القيود المعروفة: نفق quick يتغيّر اسمه عند كل تشغيل، مفتاح واحد يصلح أيضاً
لإدارة `/api/*`، ولا CORS لواجهات المتصفح. الحلول في نفس المستند.

---

## الحزمة كاملة (مرحّل + نفق + Harness)

```bash
./install.sh --dry-run     # عرض الخطوات فقط
./install.sh               # تثبيت وتشغيل
```

`install.sh` يستدعي `stack/install.sh`، الذي:

1. يكتشف `node`, `dsh`, `zen-relay`, `cloudflared` ويتحقق من `node >= 22`؛
2. يكتب `~/.config/dsh-stack/config.json`؛
3. يعطّل الوحدات القديمة المتعارضة ويسجّل حالتها للاسترجاع؛
4. يثبّت `dsh-stack.service` (`Type=notify`, `WatchdogSec=90`)، ويشغّله؛
5. ينتظر جاهزية المرحّل ثم النفق ثم الـHarness، وينشر رابط الدخول في
   `~/dsh-url.txt`.

بعدها اربط الـHarness بالمرحّل عبر
[`config/dsh-cordis.patch.yml`](config/dsh-cordis.patch.yml)، وضع المفتاح في مخزن
الاعتماد باسم `OMP_ZEN_RELAY_KEY`.

الشكل الكامل:

```
client ──HTTPS──▶ cloudflared ──▶ dsh web :30141 ──▶ zen-relay :8901 ──▶ opencode.ai
                                      ▲
                                 dsh-stack (systemd --user)
```

التفاصيل في [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## الإعدادات الافتراضية

- الوجهة: `https://opencode.ai/inference/openai/v1` (مفتاح `public`، الطبقة المجانية)
- المنفذ: `8901`، الاستماع: `127.0.0.1` فقط (`HOST`/`PORT` للتغيير)
- الـpool: 30–40 lane، فحص حقيقي لكل بروكسي قبل ترقيته
- `upstream_user_agent`: بصمة OpenCode — بدونها ترفض الطبقة المجانية الطلب

كل الحقول في [`config/settings.example.json`](config/settings.example.json).

كل تفاصيل طبقة الخروج (المصادر الـ49، الفحص، الترتيب، `lanes.json` و
`burned.json`، Webshare وTor) في [`docs/PROXY-POOL.md`](docs/PROXY-POOL.md).

---

## المتطلبات

- **Linux x86_64:** الملف الجاهز يتطلب glibc 2.34+ (Ubuntu 22.04+، Debian 12+).
- **macOS / Windows / معماريات أخرى:** ثبّت Rust ثم:

  ```bash
  cd zen-router/rust && cargo build --release
  ```

  أو أعد تشغيل `./run.sh` وسيبني تلقائياً عند وجود `cargo`.
- **الحزمة كاملة:** Node 22+، `dsh`، و`cloudflared` (اختياري للنفق).

---

## المسارات والأسرار

لا تُرفع إلى Git: `zen-router/settings.json` (فيه المفتاح)، `lanes.json`،
`burned.json`، `~/.dsh/.credentials.yaml`، `~/dsh-url.txt`. المستودع يحتوي
`.gitignore` يمنعها.

---

## License

MIT — انظر [LICENSE](LICENSE).

> استخدم الأداة وفق شروط مزوّد الخدمة. الهدف توزيع الحدود المسموحة لكل IP، لا
> تجاوزها.
