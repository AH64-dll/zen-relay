# zen-relay — Relay + Harness stack (Zen Relay 1.2.0)

مرحّل متوافق مع OpenAI API يوزّع الطلبات على عناوين خروج متغيرة (rotating egress
IPs) لتفادي حدود المعدل المفروضة لكل IP، مع حزمة تشغيل كاملة تربطه بـDeepSeek
Harness وتنشره على نفق HTTPS.

**النسخة الحالية: 1.2.0** · المصدر: `zen-router/rust/` · الملف التنفيذي:
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
| `docs/` | المعمارية، التشغيل على أجهزة أخرى، التثبيت، استكشاف الأخطاء |

---

## التشغيل السريع (المرحّل وحده)

```bash
git clone https://github.com/AH64-dll/zen-relay.git
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

إذا ظهر خطأ `GLIBC_2.34 not found` على توزيعة قديمة، استخدم بناء المصدر أعلاه.

## تخصيص التشغيل

```bash
PORT=8901 HOST=127.0.0.1 ./run.sh
```

لا تفتح المرحّل على الإنترنت (`HOST=0.0.0.0`) دون جدار ناري ومفتاح قوي؛ فهو يمنح وصولاً إلى الوجهة عبر عناوينك.

لتغيير الوجهة أو المفتاح أو إعدادات الـpool: عدّل `zen-router/settings.json` ثم أعد التشغيل.

## ملاحظات مهمة

- هذا المستودع يحتوي نسخة **Rust** فقط.
- `zen-router/rust/` هو المصدر الكامل، و`zen-router/bin/zen-relay` هو الملف التنفيذي الجاهز.
- تم استثناء ملفات البناء والحالة والأسرار: `target/`، `.venv/`، `.git/`، `settings.json`، `lanes.json`، `burned.json`، `.env`، والسجلات. المرحّل يولّدها عند أول تشغيل.
- **التحقق من شهادات TLS مفعّل** منذ 1.1.8: البروكسي يرى العنوان والتوقيت فقط، ولا يستطيع قراءة المحادثة أو تعديلها. أي بروكسي يحاول اعتراض TLS سيُرفض بدل أن يُقبل.
- ملفات الأسرار (`settings.json`، `lanes.json`، `burned.json`) تُكتب بصلاحيات `0600`، ولا يعيد `/api/settings` الأسرار في استجابته.
- `probe_model` يُتحقق منه مقابل قائمة الموديلات الحقيقية عند الإقلاع، ويُستبدل تلقائياً إن لم يعد متوفراً.
- استخدم الأداة وفق شروط مزوّد الخدمة. الهدف منها توزيع الحدود المسموحة لكل IP، لا تجاوزها.
- **قيد خارجي (أكتوبر 2026):** بوابة OpenCode المجانية ترفض الطلبات التي لا تأتي من
  عميل OpenCode الرسمي (`FreeTierError`)، وقد يبقى pool البروكسيات بارداً (`warm=0`)
  رغم سلامة المرحّل. هذا قيد من المزوّد لا خلل في المرحّل؛ الحل هو الجسر أعلاه
  (يمرّ الطلب من عميل OpenCode نفسه) أو مفتاح OpenCode صالح أو مزوّد آخر. راجع
  `docs/TROUBLESHOOTING.md`.
- **السماح لكل IP (1.1.9):** الطبقة المجانية تسمح بنحو **طلب واحد لكل IP**. كان
  المرحّل يثبت كل lane بطلب حقيقي، فيستهلك الرصيد الذي يُفترض أن يخدمه الطلب
  الحقيقي. لذلك `stage2_probe` (مضبوط على `false` في `config/settings.example.json`)
  يرقّي الـlane بعد فحص connectivity رخيص، فيكون طلب العميل هو الأول عبر ذلك IP.
  `/healthz` يعرض الآن `warm` (lanes قابلة للوصول) و`proven` (lanes نجحت فعلاً).

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

## دمج OpenCode مع الـHarness (الجسر)

المزوّد اليوم يرفض أي طلب خارج عميل OpenCode الرسمي:

```
FreeTierError: OpenCode's free tier can only be used from within OpenCode.
```

لذلك يضيف المستودع **جسراً** (`stack/bridge/opencode-bridge.mjs`): واجهة
OpenAI-compatible تقف بين الـHarness وخادم OpenCode المحلي. الطلب يمر هكذا:

```
dsh web :30141 ──OpenAI/chat──▶ opencode-bridge :8902 ──session API──▶ opencode (محلي)
                                                                          │
                                                          opencode.ai ◀──┘ (عميل رسمي)
```

الفكرة أن الطلب إلى المزوّد يصدر من عميل OpenCode نفسه، فلا يحتاج أي انتحال
لهوية ولا نسخ أي مفتاح. الجسر يحوّل كل طلب إلى جلسة OpenCode، ويحوّل جوابها
إلى `chat.completion`.

**المقايضة، بوضوح:** OpenCode ينفّذ حلقته وأدواته الخاصة داخل جلسته، فيصل
إلى الـHarness الجواب فقط. أي أن الـHarness واجهة أمام OpenCode، لا وكيل ثانٍ
فوقه.

- يُفعَّل الجسر تلقائياً عند وجود `opencode` على الجهاز، ويُعطَّل بـ`--no-bridge`.
- عنوانه `http://127.0.0.1:8902/v1`، ومفتاحه نفس `OMP_ZEN_RELAY_KEY`.
- الـHarness يستخدمه افتراضياً عبر
  [`config/dsh-cordis.patch.yml`](config/dsh-cordis.patch.yml)؛ مسار المرحّل
  (بروكسيات متغيّرة) يبقى متاحاً كمزوّد ثانٍ.

---

## مصدر البروكسيات: Webshare

قوائم البروكسيات المجانية العامة شبه معطّلة الآن. القياس على هذا الجهاز:
**189,204** عنواناً فريداً من 44 قائمة، ومن 300 عيّنة عشوائية:

| المرحلة | العدد | النسبة |
|---|---|---|
| ميت (connection refused) | 213 | 71% |
| حيّ لكنه لا يمرّر HTTPS (ولا حتى example.com) | 87 | 29% |
| يصل إلى opencode.ai | **0** | **0%** |

لذلك يدمج المرحّل مع **Webshare**: عناوين سكنية حقيقية. الخطة المجانية تعطي
10 عناوين مضمونة العمل، والخطة المدفوعة تدوّر عنواناً جديداً مع كل طلب.

```bash
scripts/set-webshare-token.sh                 # يطلب المفتاح ويُخزّنه في settings.json
scripts/set-webshare-token.sh --rotating      # مع تدوير العنوان كل طلب
scripts/set-webshare-token.sh --rotating --country us
```

المفتاح يُكتب في `zen-router/settings.json` بصلاحية `0600`، أو عبر
`WEBSHARE_TOKEN` في البيئة. الحقول:

- `webshare_token`: مفتاح API من Webshare (Dashboard → API Keys)
- `webshare_rotating`: يضيف بوابة backbone التي تعطي IP سكنياً جديداً كل اتصال
- `webshare_country`: رمز الدولة للـIP (اختياري)
- `rotating_cooldown_sec`: راحة الـlane الفاشلة قبل إعادة تجربتها (5 ثوانٍ افتراضياً)

---

## نفق ثابت (Named tunnel)

نفق Cloudflare السريع يغيّر اسم النطاق عند كل إقلاع. لعنوان ثابت:

```bash
cloudflared tunnel login                       # يفتح المتصفح
cloudflared tunnel create zen-harness
cloudflared tunnel route dns zen-harness harness.example.com
```

ثم:

```bash
./install.sh --named-tunnel zen-harness --tunnel-hostname harness.example.com
```

الـstack ينشر `https://harness.example.com/?token=…` في `~/dsh-url.txt` على كل
إقلاع، بنفس العنوان. تحتاج نطاقاً مُداراً على Cloudflare.

**بديل بلا نطاق: Tailscale Funnel** (عنوان دائم `*.ts.net`، حساب مجاني):

```bash
# 1. tailscaled كخدمة نظام (يحتاج TUN ⇒ root)
install -m 0755 ~/.local/bin/tailscaled /usr/local/bin/tailscaled
install -m 0755 ~/.local/bin/tailscale  /usr/local/bin/tailscale
#    ExecStart=/usr/local/bin/tailscaled --state=/var/lib/tailscale/tailscaled.state \
#              --socket=/run/tailscale/tailscaled.sock
#    ExecStartPost=/bin/sh -c 'sleep 2; chmod 0666 /run/tailscale/tailscaled.sock'
systemctl enable --now tailscaled

# 2. سجّل الجهاز (يفتح المتصفح)
tailscale --socket=/run/tailscale/tailscaled.sock up --hostname zen-harness

# 3. اسمح للمستخدم بإعداد الـserve config (مرة واحدة)
sudo tailscale --socket=/run/tailscale/tailscaled.sock set --operator=$USER

# 4. فعّل Funnel (مرة واحدة)
tailscale --socket=/run/tailscale/tailscaled.sock funnel --bg 30141

# 5. اجعل الـstack هو المالك
#    tunnel.mode = "named"، namedCommand = tailcale ... funnel 30141،
#    tunnel.hostname = zen-harness.<tailnet>.ts.net
```

النتيجة: `https://zen-harness.<tailnet>.ts.net/?token=…` — نفس العنوان بعد كل
إقلاع؛ يتغير رمز الدخول فقط.

> ملاحظة: الـFunnel يحتاج جهاز TUN، أي `tailscaled` يعمل كـroot. في وضع
> userspace يُمنح الشهادة لكن لا يُقدَّم أي اتصال.

---

## الإعدادات الافتراضية

- الوجهة: `https://opencode.ai/inference/openai/v1` (مفتاح `public`، الطبقة المجانية)
- المنفذ: `8901`، الاستماع: `127.0.0.1` فقط (`HOST`/`PORT` للتغيير)
- الـpool: 30–40 lane، فحص حقيقي لكل بروكسي قبل ترقيته
- `upstream_user_agent`: بصمة OpenCode — بدونها ترفض الطبقة المجانية الطلب
- `stage2_probe`: `false` — لا يُثبت الـlane بطلب حقيقي (الطبقة المجانية تسمح
  بنحو طلب واحد لكل IP، والفحص كان يستهلكه)
- `webshare_token` / `webshare_rotating`: مصدر البروكسيات الحقيقي

> **حدّ مهم (مقيس):** خطّة Webshare المجانية (10 عناوين سكنية) تمرّر HTTP
> العادي لكن **لا تمرّر نفق HTTPS (CONNECT)** — والمزوّد يطلب HTTPS. لذلك لا
> تصلح الخطّة المجانية لهذا المرحّل: تحتاج خطّة مدفوعة (Backbone/Rotation)
> تدعم CONNECT. المرحّل يطلب `mode=direct` أساساً؛ أضِف `mode=backbone` إن
> كانت خطّتك دوّارة.

كل الحقول في [`config/settings.example.json`](config/settings.example.json).

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

## Security notes

- **TLS certificate verification is enforced** (since 1.1.8). A proxy in the pool can see that you talk to the upstream, plus timing and sizes — it cannot read or rewrite the conversation, because a proxy attempting to intercept TLS is rejected instead of trusted.
- `settings.json`, `lanes.json` and `burned.json` are written `0600`; `POST /api/settings` masks secrets the same way `GET` does; proxy passwords are never echoed by `/api/pool`.
- The relay key is generated on first run and never printed in full — read it from `settings.json`.
- `probe_model` is validated against the live model list and replaced automatically if the upstream retires it.
- Binding to a non-loopback `HOST` logs a warning, and an empty `relay_api_key` is rejected by the API (use `RELAY_ALLOW_ANONYMOUS=1` if you really want an unauthenticated relay).

## Repository layout

- `zen-router/rust/` — Rust source (Cargo).
- `zen-router/bin/zen-relay` — prebuilt Linux x86_64 binary (v1.2.0).
- `run.sh` — one-command launcher (prebuilt binary or source build).
- `install.sh`, `stack/`, `config/`, `scripts/`, `docs/` — full relay + tunnel + Harness supervisor stack; see `docs/INSTALL.md`.

## المسارات والأسرار

لا تُرفع إلى Git: `zen-router/settings.json` (فيه المفتاح)، `lanes.json`،
`burned.json`، `~/.dsh/.credentials.yaml`، `~/dsh-url.txt`. المستودع يحتوي
`.gitignore` يمنعها.

---

## License

MIT — انظر [LICENSE](LICENSE).

> استخدم الأداة وفق شروط مزوّد الخدمة. الهدف توزيع الحدود المسموحة لكل IP، لا
> تجاوزها.
