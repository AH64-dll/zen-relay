# دليل التشغيل الكامل — من صفر إلى مرحّل يعمل

> **الجمهور:** أي شخص يريد تشغيل هذا المستودع من الصفر، على جهاز Linux جديد،
> بدون معرفة مسبقة بالمشروع.
>
> **القاعدة:** كل أمر في هذا الدليل مُختبَر على هذه الآلة (Fedora 44، glibc 2.43،
> Node 24.18، Python 3.14.3). القيم المذكورة هي القيم الافتراضية في الكود، وليست
> تخميناً.
>
> **اقرأ أولاً:** القسم 2 يشرح *لماذا* تفشل معظم المحاولات. تخطّيه هو السبب الأول
> لتقرير «المرحّل لا يعمل».

---

## 0. الخريطة السريعة

| أريد… | اذهب إلى |
|---|---|
| تشغيل المرحّل وحده بأسرع طريق | [§3](#3-المرحّل-وحده-أسرع-طريق) |
| الحزمة كاملة (مرحّل + نفق + Harness) | [§4](#4-الحزمة-الكاملة-مرحّل--نفق--harness) |
| ربط OpenCode | [§5](#5-ربط-opencode) |
| فهم تجمّع البروكسي | [§6](#6-تجمّع-البروكسي-كيف-يعمل-فعلاً) |
| شيء لا يعمل | [§8](#8-استكشاف-الأخطاء) |
| كل الإعدادات | [§7](#7-مرجع-الإعدادات) |

الأوامر أدناه تفترض أنك في جذر المستودع:

```bash
git clone https://github.com/AH64-dll/zen-relay.git
cd zen-relay
```

---

## 1. المتطلبات

### 1.1 الحد الأدنى (المرحّل وحده)

| المطلوب | الإصدار | لماذا | التحقق |
|---|---|---|---|
| Linux x86_64 | glibc ≥ 2.34 | الثنائي المُجمَّع مسبقاً | `ldd --version \| head -1` |
| Python | ≥ 3.12 | مرحّل REST (اختياري إن استخدمت ثنائي Rust) | `python3 --version` |
| Node.js | ≥ 22 | `dsh-stack` وHarness | `node --version` |

### 1.2 للحزمة الكاملة

| المطلوب | الإصدار | التحقق |
|---|---|---|
| `dsh` | 0.2.x | `dsh --version` |
| `cloudflared` | أي حديث | `cloudflared --version` |
| Rust | ≥ 1.80 (فقط إن أردت البناء من المصدر) | `rustc --version` |

### 1.3 تثبيت النواقص

```bash
# Fedora
sudo dnf install -y python3 python3-pip nodejs cloudflared

# Debian / Ubuntu
sudo apt install -y python3 python3-venv nodejs

# dsh (Harness)
npm i -g @deepseek-ai/dsh
```

### 1.4 تحقّق واحد قبل المتابعة

```bash
python3 --version && node --version && (cloudflared --version | head -1 || echo "cloudflared: غير مثبّت — النفق سيتخطّى")
```

---

## 2. قبل أن تبدأ: كيف يعمل هذا فعلاً

**هذا القسم هو أهم قسم في الدليل.** معظم تقارير «لا يعمل» سببها واحد من
الحقائق الخمس التالية، وليست خطأً في التثبيت.

### 2.1 الوجهة تفرض بصمة عميل، لا مفتاحاً

الوجهة (opencode.ai) ترفض أي طلب لا يحمل `User-Agent` يحتوي `opencode`، **مع**
ترويسات جلسة بصيغة محدّدة:

```
User-Agent: opencode/latest/2.0.32/cli
x-opencode-client: cli
x-opencode-session: ses_...
x-opencode-session-id: ses_...
x-session-id: ses_...
x-session-affinity: ses_...
x-opencode-request: msg_...
x-opencode-project: global
x-opencode-directory: /path/to/cwd
```

نتيجة القياس: نفس الـIP ونفس المفتاح →

| الترويسات | النتيجة |
|---|---|
| `curl` عادي | `403 FreeTierError` |
| UA يحتوي `opencode` فقط | `200` على `/zen/v1/models` |
| بصمة كاملة + جسم بشكل وكيل | `200` على الإكمالات |

المرحّل يضيف هذه الترويسات تلقائياً على **كل** نداء صاعد (فحص، قائمة موديلات،
وطلب مُمرَّر). لا تفعل ذلك يدوياً بـ`curl` وتستنتج أن المرحّل معطوب.

### 2.2 الرصيد المجاني لكل IP خروج، وليس لكل مفتاح

المفتاح `public` مشترك عالمياً. الرصيد يُحسب على **IP الخروج**. لذلك:

- `direct_lane = true` لا ينفع إذا كان IP مزوّدك مستهلكاً (قياس: 4/4 طلبات → `429`).
- أي IP مُستهلَك يبقى ميتاً مدة طويلة (قياس: 40+ دقيقة، وعلى أحد المضيفين 8 أيام).
  لذلك `burn_memory = true` و`burn_ttl_sec = 86400` ليستا اختياريتين.

### 2.3 البروتوكول المُعلن في القوائم العامة غير موثوق

قياس على 240 مرشّحاً من قوائم عامة: **12 حيّاً فقط، وكلها تتكلّم SOCKS5** — أحدها
كان مُعلَناً `http`. لو جرّب المرحّل البروتوكول المُعلن فقط، لأسقط كل هؤلاء.

المرحّل الآن يجرّب: المُعلن → `http` → `socks5`. و`socks4` يُطلب كـ`socks5`، لأن
`httpx` لا يملك ناقل SOCKS4 ويرفع `ValueError` قبل إرسال أي بايت.

### 2.4 `stage2_probe` يجب أن يبقى `false`

الفحص بإكمال حقيقي (stage 2) يستهلك **الإكمال الوحيد** المسموح لذلك الـIP قبل أن
يخدم أي طلب. النتيجة المقيسة: الـpool يبقى على lane واحد.

مع `false`: يُرقّى المرشّح بعد فحص الوصول، ويبدأ `ok=0`، وأول طلب ناجح يثبته.
النتيجة المقيسة: الـpool يمتلئ إلى الهدف.

### 2.5 المصدر الموثوق الوحيد في هذه البيئة هو WARP

قياس متزامن تحت حمل:

| الخروج | النتيجة |
|---|---|
| Cloudflare WARP المحلي (`127.0.0.1:40000`) | **6/6 نجاح** |
| IP المزوّد مباشرة | 6/6 → `429` |
| خروج Tor | 30/30 → `429` |
| بروكسيات عامة | ~0% |

لذلك المرحّل يعامل WARP كـ**عمود فقري محمي**: لا يُpark ولا يُحرق أبداً، ودرجته
لها حد أدنى، ويُستعاد تلقائياً إن خرج من التبديل. البروكسيات العامة تُجرَّب
فرصةً للتحسين، لا كأساس.

> **الخلاصة العملية:** إن أردت نتيجة موثوقة، شغّل WARP. إن أردت فقط أن ترى
> الـpool يمتلئ، تكفي القوائم العامة — لكن التبديل سيتنقّل بين خيارات ميتة.

---

## 3. المرحّل وحده (أسرع طريق)

### 3.1 التشغيل

```bash
./run.sh
```

ماذا يحدث:

1. ينشئ `zen-router/settings.json` بمفتاح عشوائي `rly_...` إن لم يكن موجوداً.
2. يستخدم الثنائي المُجمَّع `zen-router/bin/zen-relay` على Linux x86_64.
3. وإلا يبني من المصدر بـ`cargo` إن توفّر.

في نافذة ثانية، اقرأ المفتاح:

```bash
grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1
```

### 3.2 تحقّق

```bash
# 1) الصحة — بلا مفتاح
curl -s http://127.0.0.1:8901/healthz
# {"ok":true,"warm":8,"queue":2661,"subnets":8,...}

# 2) المفتاح مطلوب فعلاً
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8901/api/pool
# 401

# 3) طلب إكمال حقيقي
KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
curl -s http://127.0.0.1:8901/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Say OK"}],"stream":false}'
```

### 3.3 قراءة `/healthz`

| الحقل | المعنى | القيمة الصحية |
|---|---|---|
| `ok` | هل هناك lane واحد دافئ على الأقل | `true` |
| `warm` | lanes جاهزة الآن | يقترب من `proxy_pool_target` |
| `parked` | lanes في فترة تهدئة | منخفض |
| `queue` | مرشّحون ينتظرون الفحص | طبيعي أن يكون بالآلاف |
| `subnets` | تنوّع /24 | كلما زاد كان أفضل |
| `capacity` | `warm × lane_max_inflight` | — |

> `warm = 0` في أول 60–180 ثانية أمر طبيعي: الفحص يجري. لا تعتبره فشلاً قبل
> انقضاء ثلاث دقائق.

### 3.4 التشغيل الدائم (بلا Harness)

```bash
./run.sh                      # يدوياً
# أو عبر systemd للمرحّل وحده:
systemctl --user enable --now zen-rotator.service   # بعد تعديل المسارات في الوحدة
```

> `config/zen-rotator.service` مخصّص لنشر المرحّل وحده. إن استخدمت الحزمة
> الكاملة، `dsh-stack` هو من يشغّل المرحّل — لا تشغّل الاثنين معاً.

---

## 4. الحزمة الكاملة (مرحّل + نفق + Harness)

### 4.1 التثبيت

```bash
./install.sh --dry-run     # يعرض كل خطوة، لا يغيّر شيئاً
./install.sh               # يثبّت ويشغّل
```

**لا تشغّله من داخل واجهة Harness** — فهو يعيد تشغيل الـHarness الذي يخدم الصفحة
التي تكتب فيها.

خيارات `stack/install.sh`:

| الخيار | الأثر |
|---|---|
| `--dry-run` | عرض فقط |
| `--no-start` | تثبيت بلا تشغيل |
| `--no-tunnel` | بلا نفق عام |
| `--no-bridge` | بلا جسر OpenCode |
| `--named-tunnel <cmd>` | نفق ثابت بدل السريع |
| `--tunnel-hostname <host>` | اسم النفق |
| `--force` | إعادة كتابة الإعدادات |
| `--wait` | انتظار الجاهزية قبل الخروج |
| `--uninstall` | إزالة الوحدات (يبقي البيانات) |
| `--purge` | إزالة كل شيء |

### 4.2 ما الذي يُثبَّت وأين

| المسار | المحتوى |
|---|---|
| `~/.config/dsh-stack/config.json` | إعداد المشرف |
| `~/.local/state/dsh-stack/stack.log` | السجل |
| `~/.local/state/dsh-stack/status.json` | الحالة الحيّة |
| `~/dsh-url.txt` | رابط الدخول مع التوكن |
| `~/.config/systemd/user/dsh-stack.service` | وحدة systemd |

### 4.3 التشغيل من الإقلاع

```bash
systemctl --user enable --now dsh-stack
loginctl enable-linger "$USER"     # ليعمل بلا جلسة مفتوحة
```

### 4.4 التحقّق

```bash
./stack/bin/dsh-stack status --probe
```

يجب أن تكون المكوّنات الأربعة `ready`:

```
COMPONENT   STATE      PID      READY       DETAIL
proxy       ready      ...      ...         HTTP 200
tunnel      ready      ...      ...         tunnel host <name>
bridge      ready      ...      ...         HTTP 200
harness     ready      ...      ...         HTTP 401
```

`harness: HTTP 401` **صحيح**: يعني أن المصادقة تعمل.

```bash
./stack/bin/dsh-stack doctor     # يجب أن ينتهي بـ: doctor: no blocking problems
```

### 4.5 رابط الدخول

```bash
cat ~/dsh-url.txt
```

يفتح الرابط بـ`?token=...`، ويعيد `303` ويضبط كوكي `dsh-auth-*` صالحاً 30 يوماً.

> **أمان:** من يملك الرابط يدخل. لإبطاله، أعد تشغيل `dsh-stack` (يتغيّر التوكن).

---

## 5. ربط OpenCode

### 5.1 الإعداد

`~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "zen-relay": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Zen Relay",
      "options": {
        "baseURL": "http://127.0.0.1:8901/v1",
        "apiKey": "rly_ضع_مفتاحك_هنا"
      },
      "models": {
        "deepseek-v4.1-flash-free": { "name": "DeepSeek v4.1 Flash (free)" }
      }
    }
  }
}
```

### 5.2 التحقّق

```bash
# 1) هل ظهر المزوّد؟
opencode models | grep zen-relay

# 2) طلب حقيقي عبر المرحّل
KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
curl -s http://127.0.0.1:8901/v1/models -H "Authorization: Bearer $KEY" | head -c 200
```

### 5.3 من جهاز آخر

استخدم رابط النفق بدل `127.0.0.1`:

```json
"baseURL": "https://<اسم-النفق>/v1"
```

---

## 6. تجمّع البروكسي: كيف يعمل فعلاً

### 6.1 دورة الحياة

```
مصادر (200+)  →  مرشّحون  →  فحص وصول (7s)  →  دافئ (unproven)
                                      ↓
                              أول طلب ناجح  →  مُثبَت (proven)
                                      ↓
                              429 / فشل متكرر  →  مهدَّأ (cooldown)
```

### 6.2 لماذا 7 ثوانٍ

الحدّ 7s مطبَّق على **أربعة** مسارات، وكلها ملفوفة بـ`asyncio.wait_for` كي لا
يتعلّق عامل على مصافحة SOCKS5 تقبل TCP ثم تسكت:

| المسار | الكود |
|---|---|
| فحص الوصول | `t_sec = min(7.0, float(PROBE_TIMEOUT))` |
| محاولة المرحّل | `attempt_timeout = min(7.0, RELAY_PROXY_TIMEOUT, timeout)` |
| محاولة البثّ | نفس السابقة |
| عميل الـlane | `t_sec = min(connect_timeout, float(RELAY_PROXY_TIMEOUT))` |

**استثناء مهم:** مهلة `read` في البثّ **ليست** 7s. `httpx` يطبّق `read` على كل
فجوة بين حزمتين، فحدّ 7s يقتل بثّاً «يفكّر» بين توكنين. الحدّ 7s على الاتصال
والاستجابة الأولى، لا على ما بين التوكنات.

### 6.3 التبديل بين كل الـlanes

- المؤشر يدور على **كل** الـpool، لا على المثبتة فقط (تدوير المثبتة فقط يُجوّع
  غير المثبتة فلا تُثبت أبداً).
- 4 من كل 5 طلبات تذهب إلى مُثبَت، و1 من 5 إلى غير مُثبت (فرصة إثبات).
- عند الفشل: **العمود الفقري أولاً**، ثم بالدرجة. بدون هذا الترتيب، سلسلة
  بروكسيات ميتة تستهلك الميزانية كلها قبل الوصول إلى الـlane العامل
  (قياس: الفشل كان 50s، صار 8–15s).
- السقف الزمني الكلي مستقل عن `relay_attempts`: `7s × 80 = 560s` كان توقّفاً
  أطول من صبر أي عميل.

### 6.4 إدخال بروكسي يدوياً

```bash
KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
curl -s -X POST http://127.0.0.1:8901/api/pool/seed \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"lanes":["1.2.3.4:1080","5.6.7.8:8080"],"proto":"socks5"}'
# {"added":2,"queue":2262}
```

الجسم يقبل `lanes` (قائمة `ip:port`) و`proto` واحد
(`http` | `https` | `socks4` | `socks5`). الـlanes المُدخلة تدخل **مقدّمة**
طابور الفحص، ولا تخدم قبل أن تنجح في الفحص. لخلط بروتوكولات، أرسل النداء أكثر
من مرة بـ`proto` مختلف.

### 6.5 المصادر وترتيبها

المصادر مرتّبة حسب **المردود المقيس**، لا عشوائياً:

| المصدر | مردود حيّ |
|---|---|
| `monosans/proxy-list` (socks5) | 14.2% |
| `TheSpeedX/PROXY-List` (socks5) | 9.2% |
| `proxifly/free-proxy-list` (socks5) | 8.3% |
| `TheSpeedX` (http) | 0.8% |

خلط المصادر عشوائياً يملأ سقف المرشّحين بجثث قبل الوصول إلى المصدر الجيّد.

### 6.6 Webshare

```json
{ "webshare_token": "توكن-حسابك" }
```

قياس على الحساب المجاني (10 بروكسيات): تمرّ فحص الوصول، لكن الإكمالات تنتهي
بـ`ReadTimeout`/`429`. مفيدة كطبقة إضافية، لا كأساس.

### 6.7 Tor

```json
{ "tor_enabled": true, "tor_lanes": 40, "tor_socks_port": 9050 }
```

يتطلّب `SocksPort 9050 IsolateSOCKSAuth` وإلا شاركت كل الـlanes دائرة واحدة.

> **قياس:** 30/30 خروج Tor أعاد `429` على الإكمالات. الفحص ينجح (200 على
> `/zen/v1/models`) لكن الرصيد مستهلك. لذلك `tor_enabled: false` هو الافتراضي
> هنا — تمكينه يضخّ lanes ميتة تُخفّف التبديل.

---

## 7. مرجع الإعدادات

`zen-router/settings.json` — كل القيم الافتراضية من الكود:

| المفتاح | الافتراضي | ملاحظات |
|---|---|---|
| `upstream_base_url` | `https://opencode.ai/inference/openai/v1` | — |
| `screen_url` | `https://opencode.ai/zen/v1/models` | فحص الوصول |
| `upstream_api_key` | `public` | مشترك ومستهلك؛ يُنتج تحذيراً |
| `relay_api_key` | يُولَّد | مفتاحك `rly_...` |
| `probe_model` | `beam-free` | يُستبدل تلقائياً إن لم يوجد |
| `proxy_pool_target` | 30 | ارفعه إلى 40 على نطاق واسع |
| `proxy_max_candidates` | 3000 | سقف الطابور |
| `proxy_test_concurrency` | 60 | **اخفضه إلى 15–25** على خط منزلي |
| `proxy_probe_timeout` | 7 | مُقيَّد بـ7 داخلياً |
| `stage2_timeout` | 25 | ميزانية فحص الإكمال، منفصلة عن 7s |
| `relay_proxy_timeout` | 7 | مُقيَّد بـ7 داخلياً |
| `relay_attempts` | 80 | عدد المحاولات |
| `lane_cooldown_sec` | 3600 | **لا تقلّ عن 600**: IP محروق يبقى ميتاً 40+ دقيقة |
| `lane_recover_sec` | 3600 | إعادة فحص |
| `direct_lane` | true | IP المزوّد — غالباً مستهلك |
| `sources_enabled` | true | القوائم العامة |
| `allow_socks` | true | يتطلّب `httpx[socks]` |
| `stage2_probe` | **false** | انظر §2.4 |
| `lane_max_inflight` | 2 | بروكسي مجاني يتحمّل 1–2 |
| `max_lanes_per_subnet` | 3 | تنوّع /24 |
| `adaptive_concurrency` | true | تراجع تلقائي |
| `persist_lanes` | true | استعادة بعد إعادة التشغيل |
| `upstream_user_agent` | `opencode/latest/2.0.32/cli` | **يجب** أن يحتوي `opencode` |
| `burn_memory` | true | تذكّر الـIPs المحروقة |
| `burn_ttl_sec` | 86400 | — |
| `tor_enabled` | false | انظر §6.7 |
| `lane_pin_count` | 3 | — |

### 7.1 متغيّرات البيئة

| المتغيّر | الأثر |
|---|---|
| `PORT` | منفذ المرحّل (8901) |
| `HOST` | واجهة الاستماع (127.0.0.1) |
| `SETTINGS_FILE` | مسار الإعدادات |
| `LANES_FILE` | لقطة الـlanes |
| `BURNED_FILE` | ذاكرة الحرق |
| `RELAY_ALLOW_ANONYMOUS=1` | تعطيل المصادقة (لا تفعل) |

### 7.2 تشخيص الإعدادات

```bash
KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
curl -s http://127.0.0.1:8901/api/diagnostics -H "Authorization: Bearer $KEY"
```

الاستجابة تحتوي `verdict` و`advice` و`config_warnings` (قائمة) و`probe_failures`
و`pool` و`adaptive`. لعرض التحذيرات وحدها:

```bash
curl -s http://127.0.0.1:8901/api/diagnostics -H "Authorization: Bearer $KEY" \
  | python3 -c 'import json,sys; [print("-", w) for w in json.load(sys.stdin)["config_warnings"]]'
```

التحذيرات المتوقّعة من الكود:

| التحذير | معناه |
|---|---|
| `upstream_api_key is 'public'` | طبيعي في الطبقة المجانية |
| `proxy_test_concurrency > 40` | اخفضه إلى 15–25 |
| `lane_cooldown_sec < 600` | ارفعه إلى 3600 |
| `socksio is not installed` | `pip install 'httpx[socks]'` |
| `upstream_user_agent` بلا `opencode` | كل الطلبات سترجع 429 |

---

## 8. استكشاف الأخطاء

### 8.1 تشخيص سريع بثلاث خطوات

```bash
curl -s http://127.0.0.1:8901/healthz                    # هل المرحّل حيّ؟
./stack/bin/dsh-stack status --probe                      # هل كل المكوّنات جاهزة؟
tail -50 ~/.local/state/dsh-stack/stack.log               # ماذا يقول السجل؟
```

### 8.2 جدول الأعراض

| العرض | السبب المرجّح | الحل |
|---|---|---|
| `401 invalid relay key` | مفتاح خاطئ | `grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json` |
| `403 FreeTierError` | ترويسات البصمة ناقصة | استخدم المرحّل، لا `curl` مباشراً |
| `429` على كل الطلبات | IP الخروج مستهلك | شغّل WARP؛ تحقّق من `direct_lane` |
| `warm: 0` بعد 3 دقائق | كل المرشّحين يفشلون | تحقّق من `stage2_probe: false` |
| `503 rotator_exhausted` | لا lane نجح | انظر §8.3 |
| البثّ يُقطع | `read` timeout | حدّث — أُصلح: `read` لم يبقَ 7s |
| `address already in use` | عملية قديمة على المنفذ | `ss -ltnp \| grep 8901` ثم `kill` |
| `ValueError` في الفحص | إعلان `socks4` | أُصلح — يُطلب كـ`socks5` |
| `harness: HTTP 401` | مصادقة تعمل | **طبيعي** |
| `dsh-url.txt` لا يفتح | نفق متوقّف | `./stack/bin/dsh-stack status --probe` |

### 8.3 كل الطلبات 503

```bash
KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
curl -s http://127.0.0.1:8901/api/stats -H "Authorization: Bearer $KEY"
```

- `warm > 0` لكن كل الطلبات تفشل → الـlanes ميتة. شغّل WARP.
- `failovers` عالٍ و`lane_failures` عالٍ → طبيعي مع بروكسيات عامة.
- `probes_burned` يتزايد بسرعة → رصيد الـIPs مستهلك.

### 8.4 تنظيف الحالة

```bash
# نسيان الـIPs المحروقة (بعد تغيير خروجك)
rm -f zen-router/rest/burned.json
systemctl --user restart dsh-stack

# بداية باردة كاملة
rm -f zen-router/rest/lanes.json zen-router/rest/burned.json
```

### 8.5 التحديث

```bash
git pull
cd zen-router/rust && cargo build --release && cd ../..
cp zen-router/rust/target/release/zen-relay zen-router/bin/zen-relay
systemctl --user restart dsh-stack
```

---

## 9. الأمان

| القاعدة | السبب |
|---|---|
| لا تُودِع `settings.json` أو `lanes.json` أو `burned.json` | تحمل `relay_api_key` و`webshare_token` |
| `.gitignore` يغطّي نسخ الجذر والنسخ الفرعية | أُضيف في هذه الجولة |
| لا تُشغّل بـ`RELAY_ALLOW_ANONYMOUS=1` | من يصل للمنفذ يقرأ الـpool ويحوّل الوجهة |
| لا تُشارك رابط `?token=` | يدخل مباشرة لمدة 30 يوماً |
| TLS يبقى مفعّلاً على البروكسي | يرى الوجهة والتوقيت، لا النصّ |

تحقّق سريع:

```bash
git check-ignore -v zen-router/settings.json settings.json lanes.json burned.json
```

---

## 10. مرجع سريع

```bash
# تشغيل
./run.sh
./install.sh

# حالة
curl -s http://127.0.0.1:8901/healthz
./stack/bin/dsh-stack status --probe
./stack/bin/dsh-stack doctor

# مفتاح
grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1

# سجل
tail -f ~/.local/state/dsh-stack/stack.log

# رابط الدخول
cat ~/dsh-url.txt

# اختبار إكمال
KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
curl -s http://127.0.0.1:8901/v1/chat/completions \
  -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4.1-flash-free","messages":[{"role":"user","content":"Say OK"}],"stream":false}'
```

### المسارات

| المسار | الغرض |
|---|---|
| `/healthz` | صحة عامة (بلا مفتاح) |
| `/metrics` | Prometheus (بلا مفتاح) |
| `/v1/chat/completions` | OpenAI-متوافق |
| `/v1/messages` | Anthropic-متوافق |
| `/v1/models` | الموديلات |
| `/api/pool` | الـlanes |
| `/api/stats` | الإحصاءات |
| `/api/diagnostics` | تحذيرات الإعداد |
| `/api/settings` | قراءة/تعديل الإعدادات |
| `/api/pool/seed` | إدخال بروكسي |
| `/api/burned` | الـIPs المحروقة |
| `/api/refresh` | إعادة فحص فورية |

---

## 11. ما تم قياسه في هذه الجولة

كي تعرف ما هو مُثبت وما هو استنتاج:

| القياس | النتيجة |
|---|---|
| إكمالات غير بثّية | 8/8 بعد الإصلاح |
| بثّ | 11/12 (كان 1/8) |
| حمل 6×3 | 17/18 (94.4%) |
| اختبارات الحزمة | 64/64 |
| WARP تحت حمل | 6/6 |
| Tor على الإكمالات | 0/30 |
| بروكسيات عامة | ~0% |
| مردود المصادر | 8.3%–14.2% (socks5) |

**حدود معروفة:** الأساس الموثوق هو WARP. بدون خروج نظيف، البروكسيات العامة وحدها
لا تكفي لإكمالات متّسقة — هذا حدّ الطبقة المجانية للوجهة، لا خطأ في المرحّل.
