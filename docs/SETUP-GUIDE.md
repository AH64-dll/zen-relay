# الدليل الكامل — تشغيل المرحّل وربطه بـOpenCode وdsh

هذا المستند هو المسار الكامل من جهاز فارغ إلى:

```
   OpenCode CLI ─┐
   dsh (Harness) ─┼──► 127.0.0.1:8901  zen-relay ──► بروكسيات متغيّرة ──► opencode.ai
   جهاز آخر ─────┘         (loopback)        (HTTP/SOCKS، عشرات العناوين)
```

كل أمر هنا مكتوب ليُنسخ كما هو. القيم السرّية تظهر كـ`rly_...` — استبدلها بمفتاحك.

> **ما هو المرحّل؟** خدمة متوافقة مع OpenAI API فقط (`/v1/models` و
> `/v1/chat/completions`). ليس VPN ولا بروكسي شبكة، ولا يوجّه تصفّح الجهاز.
> `/v1/messages` (صيغة Anthropic) **غير مدعوم** في 1.1.7 — انظر قسم الأخطاء.

---

## 0. الخريطة السريعة

| الشيء | القيمة الافتراضية |
|---|---|
| منفذ المرحّل | `8901` على `127.0.0.1` |
| Base URL | `http://127.0.0.1:8901/v1` |
| المفتاح | `rly_` + 32 hex، يُولَّد عند أول تشغيل |
| إعدادات المرحّل | `zen-router/settings.json` (أو `SETTINGS_FILE`) |
| حالة الـpool | `zen-router/lanes.json` و`burned.json` |
| إعداد OpenCode العام | `~/.config/opencode/opencode.json` أو `opencode.jsonc` |
| إعداد OpenCode للمشروع | `./opencode.json` |
| ربط dsh | `~/.dsh/cordis.patch.yml` |
| مفتاح dsh | `~/.dsh/.credentials.yaml` أو متغيّر `OMP_ZEN_RELAY_KEY` |
| منفذ الـHarness | `30141` |
| رابط الـHarness | `~/dsh-url.txt` (مع النفق) |

المكوّنات الثلاثة مستقلة: يمكنك تشغيل المرحّل وحده، أو ربط OpenCode فقط، أو
تشغيل الحزمة كاملة (مرحّل + نفق + Harness).

---

## 1. المتطلبات

| المكوّن | الإصدار | لماذا |
|---|---|---|
| Linux x86_64 | glibc 2.34+ | الملف التنفيذي الجاهز في `zen-router/bin/zen-relay` |
| Node.js | **22+** (يُفضّل 24) | `dsh-stack` يحتاج `zstd` في `node:zlib` |
| `dsh` | 0.2.x | `npm i -g @deepseek-ai/dsh` |
| `cloudflared` | حديث | اختياري — للنفق العام فقط |
| Rust | 1.80+ | اختياري — للبناء من المصدر على غير Linux x86_64 |

تحقّق:

```bash
uname -m                                   # x86_64
ldd --version | head -1                    # glibc 2.34 أو أحدث
node -v                                    # v22+ ، وإلا ثبّت Node 24
dsh --version
cloudflared --version
```

إن كان `node` النظام قديماً، ضع Node 24 في `PATH` قبل أي أمر:

```bash
export PATH="$HOME/.local/node24/node-v24.22.0-linux-x64/bin:$PATH"
node -v        # v24.x
```

> `dsh-stack` يكتشف `node` و`dsh` و`zen-relay` و`cloudflared` من `PATH` عند
> التثبيت ويكتب المسارات المطلقة في وحدته. إن ثبّت Node لاحقاً في مكان آخر،
> أعد تشغيل `stack/install.sh` أو عدّل `~/.config/dsh-stack/config.json`.

---

## 2. تثبيت المرحّل

### 2.1 من المستودع

```bash
git clone https://github.com/utopianumber39ray-spec/zen-relay.git
cd zen-relay
./run.sh
```

`run.sh`:

1. ينتقل إلى `zen-router/`؛
2. يستخدم `bin/zen-relay` إن كان يعمل على هذا النظام (يفحص `ldd`)؛
3. وإلا يبني من المصدر عبر `cargo build --release`.

### 2.2 أول تشغيل

```text
SECURITY: no relay_api_key was configured — generated rly_xxxxxxxx... and saved it to .../zen-router/settings.json
```

يُنشأ `zen-router/settings.json` بكل المفاتيح وبمفتاح عشوائي. اقرأه:

```bash
grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1
```

ثبّت صلاحيات الملف (المفتاح سرّي، والملف يحوي `webshare_token` أيضاً):

```bash
chmod 600 zen-router/settings.json
```

> إذا لم يُولَّد مفتاح (ملف موجود بمفتاح فارغ)، يُولَّد ويُحفظ تلقائياً. لا تشغّل
> المرحّل بمفتاح فارغ إلا في شبكة معزولة؛ `RELAY_ALLOW_ANONYMOUS=1` يسمح بذلك
> صراحةً.

### 2.3 تشغيل يدوي بلا `run.sh`

```bash
cd zen-router
PORT=8901 HOST=127.0.0.1 ./bin/zen-relay
```

أو بناء من المصدر:

```bash
cd zen-router/rust
cargo build --release
cd ..
PORT=8901 HOST=127.0.0.1 ./rust/target/release/zen-relay
```

### 2.4 كل إعدادات `settings.json`

الملف يُقرأ عند الإقلاع؛ التعديل اليدوي يحتاج إعادة تشغيل. القيم أدناه هي قيم
`config/settings.example.json` (المضبوطة للإنتاج) — وهي تختلف قليلاً عن قيم
الكود الافتراضية حيث ذُكر.

| المفتاح | القيمة | المعنى |
|---|---|---|
| `upstream_base_url` | `https://opencode.ai/inference/openai/v1` | وجهة الطلبات (OpenAI-compatible) |
| `screen_url` | `https://opencode.ai/zen/v1/models` | مصدر كتالوج الموديلات في `/v1/models` |
| `upstream_api_key` | `public` | مفتاح الوجهة (يدعم قائمة مفصولة بفواصل) |
| `relay_api_key` | يُولَّد | المفتاح الذي يقدّمه العملاء؛ **هو نفسه مفتاح `/api/*`** |
| `probe_model` | `beam-free` | الموديل المستخدم لفحص البروكسي بطلب إكمال حقيقي |
| `proxy_pool_target` | `40` | عدد الـlanes الدافئة المستهدفة (افتراضي الكود 30) |
| `proxy_max_candidates` | `1500` | سقف طابور المرشحين (افتراضي الكود 3000) |
| `proxy_test_concurrency` | `30` | تزامن الفحص (افتراضي الكود 60) |
| `proxy_probe_timeout` | `25` | مهلة فحص البروكسي، ثانية |
| `relay_proxy_timeout` | `40` | مهلة محاولة الطلب عبر lane، ثانية |
| `relay_attempts` | `8` | عدد الـlanes التي تُجرَّب قبل إرجاع 503 (افتراضي الكود 6) |
| `lane_cooldown_sec` | `3600` | مدة إيقاف lane بعد فشل |
| `lane_recover_sec` | `3600` | إعادة فحص lane موقوف |
| `direct_lane` | `false` | السماح باستخدام IP الجهاز كـlane أخير (افتراضي الكود `true`) |
| `webshare_token` | `""` | قائمة Webshare المدفوعة (اختياري) |
| `sources_enabled` | `true` | جلب قوائم البروكسي الحرة |
| `allow_socks` | `true` | السماح بـsocks4/socks5 |
| `lane_max_inflight` | `2` | أقصى طلبات متزامنة على lane واحد |
| `max_lanes_per_subnet` | `3` | سقف lanes من نفس الـ`/24` |
| `adaptive_concurrency` | `true` | تعديل تزامن الفحص حسب النجاح |
| `persist_lanes` | `true` | حفظ اللقطة الدافئة إلى `lanes.json` |
| `upstream_user_agent` | `opencode/latest/2.0.32/cli` | البصمة التي يرسلها المرحّل للوجهة |
| `burn_memory` | `true` | تذكّر العناوين المستنفدة ومنع إدخالها |
| `burn_ttl_sec` | `86400` | نسيان عنوان محروق بعد 24 ساعة |
| `tor_enabled` | `false` | lanes عبر Tor المحلي |
| `tor_lanes` | `12` | عدد lanes الـTor |
| `tor_socks_port` | `9150` | منفذ SOCKS المحلي لـTor |
| `lane_pin_count` | `3` | أقصى عدد lanes مثبّتة يتناوب عليها الطلب |

**قاعدة الأسبقية:** متغيّرات البيئة تُقرأ أولاً ثم **ملف الإعدادات يعلو عليها**.
لذلك عدّل `settings.json` لا البيئة، إلا لمفاتيح التشغيل الخاصة:

| متغيّر | الأثر |
|---|---|
| `HOST` / `PORT` | عنوان الاستماع (لا يوجد لهما مفتاح في الملف) |
| `SETTINGS_FILE` | مسار ملف الإعدادات |
| `LANES_FILE` / `BURNED_FILE` | مسار ملفات الحالة |
| `RELAY_ALLOW_ANONYMOUS=1` | السماح بلا مفتاح (غير آمن) |
| `IP_RELAY_NO_BACKGROUND=1` | تعطيل مدير الـpool (تشخيص فقط) |

### 2.5 تشغيل دائم بلا Harness (systemd)

`dsh-stack` يشغّل المرحّل بنفسه؛ استخدم هذه الوحدة فقط إن لم تكن تستخدم المشرف.
وجود الوحدتين معاً يخلق صراعاً على المنفذ 8901، ومثبّت الحزمة يعطّل
`zen-rotator` تلقائياً.

```bash
sed "s|@ROOT@|$PWD|g" config/zen-rotator.service \
  > ~/.config/systemd/user/zen-rotator.service
systemctl --user daemon-reload
systemctl --user enable --now zen-rotator
systemctl --user status zen-rotator --no-pager
```

### 2.6 تدوير المفتاح

```bash
cd zen-relay
NEW="rly_$(openssl rand -hex 16)"
node -e '
  const fs=require("fs"), p="zen-router/settings.json";
  const s=JSON.parse(fs.readFileSync(p,"utf8"));
  s.relay_api_key=process.argv[1];
  fs.writeFileSync(p, JSON.stringify(s,null,2)+"\n", {mode:0o600});
' "$NEW"
echo "$NEW"
```

ثم حدّث كل عميل (OpenCode، dsh) وأعد التشغيل. المفتاح القديم يتوقف فوراً.

---

## 3. التحقق من المرحّل

### 3.1 الصحة (بلا مفتاح)

```bash
curl -s http://127.0.0.1:8901/healthz | python3 -m json.tool
```

مثال:

```json
{
  "ok": true,
  "version": "1.1.7",
  "warm": 38,
  "parked": 42,
  "queue": 1020,
  "capacity": 76,
  "subnets": 61,
  "sources_ok": 46,
  "best_latency_ms": 1142,
  "inflight": 0,
  "tiers": {"fast": 33, "medium": 2, "slow": 3}
}
```

### 3.2 المفتاح مطلوب

```bash
KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)

curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8901/v1/models
# 401

curl -s http://127.0.0.1:8901/v1/models \
  -H "authorization: Bearer $KEY" | python3 -m json.tool | head -30
# 10 موديلات مجانية: beam-free, flint-40b-free, deepseek-v4.1-flash-free, ...
```

`/v1/models` يعرض الموديلات المنتهية بـ`-free` فقط (مفلترة من `screen_url`،
مع كاش). الموديلات غير المجانية قد تعمل في الطلبات لكنها لا تظهر في الكتالوج.

### 3.3 طلب إكمال

```bash
curl -s http://127.0.0.1:8901/v1/chat/completions \
  -H "authorization: Bearer $KEY" \
  -H "content-type: application/json" \
  -d '{
    "model": "deepseek-v4.1-flash-free",
    "messages": [{"role":"user","content":"Reply with exactly: PROXY-OK"}],
    "max_tokens": 20
  }'
```

نجاح:

```json
{"id":"...","object":"chat.completion","choices":[{"message":{"role":"assistant","content":"PROXY-OK"}}],...}
```

### 3.4 Streaming

```bash
curl -N -s http://127.0.0.1:8901/v1/chat/completions \
  -H "authorization: Bearer $KEY" \
  -H "content-type: application/json" \
  -d '{
    "model": "deepseek-v4.1-flash-free",
    "stream": true,
    "messages": [{"role":"user","content":"count 1 to 5"}]
  }'
```

يظهر `data: {...}` متتابعاً ثم `data: [DONE]`.

### 3.5 جدول المسارات

| المسار | الطريقة | المصادقة | الوظيفة |
|---|---|---|---|
| `/healthz` | GET | لا | جاهزية الـpool |
| `/v1/models` | GET | نعم | كتالوج الموديلات المجانية |
| `/v1/chat/completions` | POST | نعم | الإكمال (عادي وstreaming) |
| `/api/settings` | GET/POST | نعم | قراءة/تعديل الإعدادات أثناء العمل |
| `/api/stats` | GET | نعم | عدّادات الطلبات والـlanes |
| `/api/pool` | GET | نعم | محتوى الـpool الحالي |
| `/api/pool/seed` | POST | نعم | إدخال بروكسيات يدوياً |
| `/api/refresh` | POST | نعم | إعادة فحص فورية |
| `/api/burned` | GET | نعم | العناوين المستنفدة |
| `/api/burned/clear` | POST | نعم | مسح ذاكرة الحرق |
| `/v1/messages` | — | — | **غير موجود (404)** |

> تحقّقنا عملياً: `POST /v1/messages` يعيد **404**. لا تستخدم المرحّل مع عملاء
> Anthropic SDK؛ استخدم OpenAI SDK أو OpenCode أو dsh.

---

## 4. تجمّع البروكسي (pool)

### 4.1 ماذا يحدث تلقائياً

1. جلب 49 قائمة بروكسي حرة.
2. توحيدها إلى `proto://host:port`.
3. فحص كل مرشح بـ**طلب إكمال حقيقي** (توكن واحد) عبر `probe_model` — لا يكفي
   فحص الاتصال.
4. إدخال الناجح فوراً، وإعادة الملء من `/24` غير مأهولة.
5. إعادة فحص الدافئ، وpark للفاشل، وتسجيل المحروق.

التفاصيل الكاملة (المصادر الـ49، صيغ الملفات، Webshare، Tor) في
[`PROXY-POOL.md`](PROXY-POOL.md).

### 4.2 قراءة `/healthz`

| الحقل | المعنى |
|---|---|
| `warm` | lanes جاهزة الآن |
| `parked` | lanes موقوفة مؤقتاً |
| `queue` | مرشحون في انتظار الفحص |
| `capacity` | `warm × lane_max_inflight` تقريباً |
| `subnets` | عدد الـ`/24` الممثلة (التنوّع) |
| `best_latency_ms` | أفضل زمن إكمال مسجّل |
| `sources_ok` | مصادر نجح جلبها من 49 |
| `tiers` | توزيع السرعة |

### 4.3 انتظار الجاهزية

بعد أي إقلاع، لا تختبر فوراً؛ الـpool يحتاج ثوانٍ إلى دقيقة:

```bash
for i in $(seq 1 60); do
  warm=$(curl -s http://127.0.0.1:8901/healthz | sed -n 's/.*"warm":\([0-9]*\).*/\1/p')
  echo "warm=$warm"
  [ "${warm:-0}" -ge 20 ] && break
  sleep 3
done
```

### 4.4 إدخال بروكسيات يدوياً

```bash
curl -s http://127.0.0.1:8901/api/pool/seed \
  -H "authorization: Bearer $KEY" \
  -H "content-type: application/json" \
  -d '{"proto":"socks5","lanes":["1.2.3.4:1080","5.6.7.8:1080"]}'
# {"added":2,"queue":...}
```

`proto` واحد من `http`, `https`, `socks4`, `socks5`. العناوين المحروقة تُرفض
ويظهر `blocked_intake`.

```bash
curl -s -X POST http://127.0.0.1:8901/api/refresh -H "authorization: Bearer $KEY"
curl -s http://127.0.0.1:8901/api/pool -H "authorization: Bearer $KEY" | head -c 600
```

### 4.5 ضبط الأداء

| الحالة | التعديل |
|---|---|
| `warm` منخفض و`queue` كبير | ارفع `proxy_test_concurrency` إلى 50–80 و`proxy_pool_target` إلى 50–60 |
| 429/403 كثيرة | **لا ترفع** الهدف؛ الـIP نفسه استُنفد. استخدم Webshare أو lanes نظيفة |
| زمن أول توكن مرتفع | اخفض `relay_attempts` (مثلاً 4) وارفع `max_lanes_per_subnet` قليلاً |
| أخطاء «capacity» | ارفع `lane_max_inflight` إلى 3 فقط إذا كانت الـlanes مستقرة |
| بعد حظر جماعي | `POST /api/burned/clear` ثم `POST /api/refresh` |

`max_lanes_per_subnet` هو ما يمنع «40 lane من نفس الشبكة»؛ ارفعه فقط عند نقص
التنوّع.

### 4.6 Webshare وTor

- **Webshare:** ضع `webshare_token` في `settings.json`. يُجلب من
  `proxy.webshare.io` دفعة 100، وتُعامَل كبقية المرشحين.
- **Tor:** شغّل Tor محلياً على `9150` ثم:

```json
{"tor_enabled": true, "tor_lanes": 12, "tor_socks_port": 9150}
```

Tor أبطأ لكنه أكثر ثباتاً؛ مناسب كاحتياطي لا كمسار أساسي.

---

## 5. ربط OpenCode

OpenCode (v2) يقرأ الإعداد من مصدرين:

1. العام: `~/.config/opencode/opencode.json` أو `opencode.jsonc`
2. المشروع: `./opencode.json` (يُدمج فوق العام)

للتحقق من المصادر الفعلية:

```bash
opencode debug config | head -20
```

### 5.1 الإعداد الكامل (v2)

أضف هذا إلى `~/.config/opencode/opencode.json` (أو أنشئه). إن كان الملف موجوداً،
أضف مفتاح `providers` و`model` داخل الكائن الجذري:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "zen-relay": {
      "name": "Zen Relay (local)",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8901/v1",
        "apiKey": "rly_..."
      },
      "models": {
        "deepseek-v4.1-flash-free": {
          "name": "DeepSeek V4.1 Flash (free)",
          "limit": { "context": 200000 },
          "capabilities": {
            "tools": true,
            "input": ["text"],
            "output": ["text"]
          }
        }
      }
    }
  },
  "model": "zen-relay/deepseek-v4.1-flash-free"
}
```

شرح الحقول:

| الحقل | إلزامي | المعنى |
|---|---|---|
| `providers.<id>` | نعم | معرّف تستخدمه في `model` |
| `package` | نعم | `@opencode/ai/providers/openai-compatible` |
| `settings.baseURL` | نعم | ينتهي بـ`/v1` |
| `settings.apiKey` | نعم | `rly_...` |
| `models.<id>` | نعم | يجب أن يطابق `id` من `/v1/models` حرفياً |
| `limit.context` | مستحسن | نافذة السياق |
| `capabilities.tools` | مستحسن | تفعيل الأدوات |
| `capabilities.input` | مستحسن | `text` فقط إن لم تتحقق من الصور |
| `model` | مستحسن | الموديل الافتراضي `provider/model` |

مفتاح `apiKey` نصّي في الملف — احمِ الملف:

```bash
chmod 600 ~/.config/opencode/opencode.json
```

### 5.2 إعداد المشروع (بدل العام)

ضع نفس المحتوى في `./opencode.json` داخل مجلّد المشروع. المشروع يعلو على العام،
وهذا مناسب لتجربة المرحّل دون تغيير إعدادك اليومي.

### 5.3 الإصدار الأول من OpenCode (v1)

الإصدارات القديمة تستخدم صيغة مختلفة:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "zen-relay": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Zen Relay",
      "options": {
        "baseURL": "http://127.0.0.1:8901/v1",
        "apiKey": "rly_..."
      },
      "models": {
        "deepseek-v4.1-flash-free": { "name": "DeepSeek V4.1 Flash (free)" }
      }
    }
  },
  "model": "zen-relay/deepseek-v4.1-flash-free"
}
```

### 5.4 التحقق

```bash
# 1) هل ظهر المزوّد والموديل؟
opencode models | grep zen-relay
# zen-relay/deepseek-v4.1-flash-free

# 2) هل المصادر صحيحة؟
opencode debug config | grep -c zen-relay

# 3) طلب حقيقي
opencode run "Reply with exactly: RELAY-OK" \
  --model zen-relay/deepseek-v4.1-flash-free
# RELAY-OK
```

### 5.5 من جهاز آخر

بدّل `baseURL` فقط إلى نفق HTTPS:

```json
"baseURL": "https://<name>.trycloudflare.com/v1"
```

راجع [`OTHER-DEVICES.md`](OTHER-DEVICES.md) للنفق والنطاق الثابت وCORS.

> OpenCode يرسل `User-Agent` يحوي `opencode`؛ المرحّل يمرّره كما هو (وهذا مطلوب
> من الوجهة). لا تقلد بصمة `opencode` بصيغة خاطئة في سكربتاتك — أرسل UA عادياً
> وسيضع المرحّل بصمته المضبوطة، وإلا حصلت على `426 UpgradeRequired`.

---

## 6. ربط dsh (DeepSeek Harness)

### 6.1 أين يُوضع الربط

dsh يركّب طبقات patch فوق profile. الربط في:

```
$DSH_HOME/cordis.patch.yml        # افتراضياً ~/.dsh/cordis.patch.yml
```

انسخ القالب:

```bash
mkdir -p ~/.dsh
cp config/dsh-cordis.patch.yml ~/.dsh/cordis.patch.yml
```

> إن كان لديك `cordis.patch.yml` مسبقاً **لا تستبدله**. أضف عنصري القائمة
> (`llm-pi-ai` و`agent-default-model`) إلى القائمة الموجودة، أو ادمج
> `providers` و`models` يدوياً.

### 6.2 الحد الأدنى من الربط

```yaml
- id: llm-pi-ai
  config:
    providers:
      opencode-zen:
        displayName: OpenCode Zen (local relay)
        api: openai-completions
        apiKeyEnv: OMP_ZEN_RELAY_KEY
        baseURL: http://127.0.0.1:8901/v1
        retryPolicy:
          mode: normal
          maxRetries: 8
          retryableCodes: [EMPTY_RESPONSE, RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT, QUOTA, AUTH]
          backoff:
            initialDelayMs: 800
            maxDelayMs: 20000
            jitterRatio: 0.25
        models:
          - id: deepseek-v4.1-flash-free
            name: DeepSeek V4.1 Flash (free)
            contextWindow: 200000
            input: [text]

- id: agent-default-model
  config:
    provider: opencode-zen
    model: deepseek-v4.1-flash-free
```

`apiKeyEnv` **اسم مرجع**، لا قيمة. القيمة تُحلّ من مخزن الاعتماد أو البيئة.

### 6.3 وضع المفتاح — ثلاث طرق

**أ) متغيّر بيئة في وحدة systemd (الأوضح والأضمن)**

```bash
mkdir -p ~/.config/systemd/user/dsh-stack.service.d
cat > ~/.config/systemd/user/dsh-stack.service.d/relay-key.conf <<EOF
[Service]
Environment=OMP_ZEN_RELAY_KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
EOF
chmod 600 ~/.config/systemd/user/dsh-stack.service.d/relay-key.conf
systemctl --user daemon-reload
systemctl --user restart dsh-stack
```

**ب) مخزن اعتماد dsh (دائم، يقرأه الـHarness مباشرة)**

`~/.dsh/.credentials.yaml`:

```yaml
version: 1
refs:
  OMP_ZEN_RELAY_KEY: rly_...
records: {}
```

```bash
chmod 600 ~/.dsh/.credentials.yaml
```

احتفظ بأي مفاتيح أخرى موجودة. الملف يجب أن يكون `0600` وإلا يرفض المزوّد قراءته.
أعد تشغيل الـHarness بعد التعديل (المراقب يعيد القراءة تلقائياً، لكن إعادة
التشغيل أضمن).

**ج) واجهة الـHarness**

Settings → Models → OpenCode Zen (local relay) → ضع المفتاح. الواجهة تكتبه في
`refs` باسم المرجع نفسه. استخدمها إن كانت متاحة في نسختك.

**أسبقية الحل** (من الأعلى):

```
بيئة العملية  >  ~/.dsh/.credentials.yaml  >  ./.env (مجلد التشغيل)  >  ~/.dsh/.env
```

لذلك متغيّر البيئة يعلو على الملف؛ إن وضعت المفتاح في مكانين مختلفين، البيئة هي
الفائزة.

### 6.4 إعادة التشغيل

```bash
# مع dsh-stack
systemctl --user restart dsh-stack
systemctl --user status dsh-stack --no-pager | head -15

# أو يدوياً
export OMP_ZEN_RELAY_KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
dsh web --port 30141
```

### 6.5 التحقق

```bash
# 1) هل دخل الربط في الشجرة المركّبة؟
dsh --profile web --dump-config | grep -A8 'opencode-zen:'

# 2) هل الـHarness يعمل؟
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:30141/     # 401 بلا توكن
curl -s http://127.0.0.1:8901/healthz                                 # ok:true

# 3) السجل
journalctl --user -u dsh-stack -n 50 --no-pager
tail -30 ~/.local/state/dsh-stack/stack.log
```

في الواجهة: افتح `~/dsh-url.txt` (أو العنوان المحلي مع توكن الدخول)، اختر
الموديل من قائمة النماذج، ثم أرسل رسالة قصيرة. إن ظهر رد، فالربط سليم.

### 6.6 مزامنة كتالوج الموديلات

بعد أي تغيير في المتاح من الوجهة:

```bash
curl -s http://127.0.0.1:8901/v1/models \
  -H "authorization: Bearer $KEY" \
  | python3 -c 'import json,sys; [print(m["id"]) for m in json.load(sys.stdin)["data"]]'
```

أضف الموديلات الجديدة إلى `models` في `cordis.patch.yml`:

```yaml
          - id: model-id-from-relay
            name: الاسم الظاهر
            contextWindow: 200000
            input: [text]
            compat:
              supportsStore: false
              supportsDeveloperRole: false
              supportsStrictMode: true
              maxTokensField: max_tokens
```

- `id` يجب أن يطابق `/v1/models` حرفياً.
- `contextWindow` رقم تقديري؛ لا يوجد في `/v1/models`.
- `supportsStore`/`supportsDeveloperRole` = `false` لأن الوجهة لا تدعمهما.
- `maxTokensField: max_tokens` لأن المرحّل يمرّر `max_tokens`.
- أضف `reasoningEfforts` فقط إن كان الموديل يدعم التفكير.
- أضف `requiresReasoningContentOnAssistantMessages: true` للموديلات التي تعيد
  `reasoning_content` (كما في القالب).

### 6.7 لماذا إعادة المحاولة بهذه الشراسة؟

التوزيع على IPs يجعل معظم الأخطاء **عابرة**: lane محروق ليس مفتاحاً خطأً. لذلك:

- `QUOTA` و`AUTH` قابلان لإعادة المحاولة (قد يكون الحظر على الـlane لا الحساب).
- `maxRetries: 8` مع backoff متزايد يغطي دورة إعادة بناء الـpool.
- بلا هذه السياسة ترى رسالة «Invalid API key» بسبب lane واحد محظور.

---

## 7. الحزمة الكاملة: مرحّل + نفق + Harness

### 7.1 التثبيت

> شغّل المثبّت من طرفية، لا من داخل واجهة الـHarness: هو يعيد تشغيل الواجهة التي
> قد تكون تتحدث عبرها.

```bash
cd zen-relay
./install.sh --dry-run     # يعرض كل خطوة بلا تعديل
./install.sh               # يثبّت ويشغّل
```

ما يفعله:

1. يتحقق من `node >= 22` و`dsh` و`cloudflared`.
2. ينشئ `zen-router/settings.json` إن لم يوجد ويطبع المفتاح.
3. يشغّل `stack/install.sh` الذي:
   - يكتشف المسارات ويكتب `~/.config/dsh-stack/config.json`؛
   - يعطّل الوحدات المتعارضة (`dsh-harness`, `zen-rotator`,
     `cloudflared-tunnel`) ويسجّل حالتها؛
   - يثبّت `dsh-stack.service` (`Type=notify`, `WatchdogSec=90`)؛
   - ينتظر جاهزية المرحّل والنفق والـHarness.

### 7.2 التشغيل من الإقلاع

```bash
sudo loginctl enable-linger $(id -un)
```

### 7.3 التحقق

```bash
systemctl --user is-active dsh-stack          # active
node stack/bin/dsh-stack status               # جدول المكوّنات
node stack/bin/dsh-stack doctor               # المسارات والصلاحيات
cat ~/dsh-url.txt                             # https://<name>.trycloudflare.com/?token=...
curl -s http://127.0.0.1:8901/healthz
```

### 7.4 النفق

- `cloudflared tunnel --url http://127.0.0.1:30141` — **quick tunnel**.
- الاسم يتغيّر عند كل إعادة تشغيل أو إقلاع؛ من شارك الرابط يفقد الوصول.
- للثبات استخدم نفقاً مُسمّى (انظر `OTHER-DEVICES.md`).
- الـHarness يُشغَّل بـ`--trusted-host <tunnel host>` تلقائياً.

لنشر المرحّل نفسه (لا الـHarness) على نفق:

```bash
scripts/expose-relay.sh          # يطبع base URL والمفتاح
scripts/expose-relay.sh --print-only
```

### 7.5 CORS وواجهات المتصفح

المرحّل لا يرسل CORS ولا يجيب `OPTIONS`، والمفتاح نفسه يفتح `/api/*`. استخدم
`config/Caddyfile.example` إن كنت ستخدم واجهة ويب من أصل مختلف:

```bash
caddy run --config config/Caddyfile.example   # بعد تعديل النطاق
```

يفعل ثلاثة أشياء: CORS + preflight، حجب `/api/*` عن البعيد، وTLS تلقائي.

---

## 8. التشغيل اليومي

| المهمة | الأمر |
|---|---|
| الحالة | `systemctl --user status dsh-stack` |
| إعادة تشغيل | `systemctl --user restart dsh-stack` |
| إيقاف | `systemctl --user stop dsh-stack` |
| السجل الحيّ | `journalctl --user -u dsh-stack -f` |
| سجل المشرف | `tail -f ~/.local/state/dsh-stack/stack.log` |
| رابط الـHarness | `cat ~/dsh-url.txt` |
| صحة المرحّل | `curl -s 127.0.0.1:8901/healthz` |
| تحديث الكود | `git pull && systemctl --user restart dsh-stack` |
| إعادة بناء المرحّل | `cargo build --release --manifest-path zen-router/rust/Cargo.toml` |
| إلغاء التثبيت | `./stack/install.sh --uninstall` |
| إلغاء + حذف الحالة | `./stack/install.sh --uninstall --purge` |

---

## 9. استكشاف الأخطاء

| العرض | السبب | الحل |
|---|---|---|
| `401 {"type":"unauthorized"}` | مفتاح خطأ أو غائب | استخدم `rly_...` نفسه من `settings.json`؛ تحقق من عدم وجود مسافة/سطر جديد |
| `503 rotator_exhausted` | لا lane جاهز/كل المحاولات فشلت | انتظر 10–30 ثانية؛ راقب `warm`؛ `POST /api/refresh`؛ ارفع الـpool أو أضف Webshare |
| `503 zen_lane_blocked` | كل الـlanes المقترحة محظورة من الوجهة | مؤقت: أعد المحاولة. إن تكرر: امسح `burned` وحدّث الـpool |
| `403 quota` في السجل | رصيد الـIP انتهى عند الوجهة | ليس خطأ مفتاح؛ الـlane يُحرق تلقائياً. أضف مصدراً/Webshare |
| `426 UpgradeRequired` | UA يحوي `opencode` بصيغة غير معتمدة | لا تقلّد البصمة؛ أرسل UA عادياً أو حدّث `upstream_user_agent` |
| `404` على `/v1/messages` | غير مدعوم في 1.1.7 | استخدم `/v1/chat/completions` أو عميل OpenAI |
| `Connection refused` | المرحّل غير مشغّل/منفذ مختلف | `ss -ltnp | grep 8901`؛ راجع `PORT` |
| `530` من نفق Cloudflare | quick tunnel تعطّل أو تغيّر اسمه | أعد التشغيل واقرأ `~/dsh-url.txt`؛ أو نفق مُسمّى |
| dsh: «Invalid API key» | المفتاح لم يُحلّ | `dsh --profile web --dump-config | grep apiKeyEnv`؛ ضع `OMP_ZEN_RELAY_KEY` في البيئة/المخزن |
| dsh: الموديل غير موجود | `id` لا يطابق الـrelay | طابق `id` مع `/v1/models` |
| OpenCode لا يرى المزوّد | الملف ليس في مسار يقرأه | `opencode debug config`؛ ضع `opencode.json` في المشروع أو العام |
| OpenCode: فشل بعد محاولات | pool بارد | `curl /healthz` حتى `warm ≥ 20` ثم أعد المحاولة |
| واجهة ويب: CORS | لا CORS في المرحّل | Caddy أو وسيط يعيد التوجيه من الخادم |
| `EADDRINUSE :8901` | وحدتان تشغّلان المرحّل | `systemctl --user list-units | grep -E 'zen|dsh'`؛ عطّل `zen-rotator` مع `dsh-stack` |
| Node: `zstd` غير موجود | Node < 22 | ثبّت 24 واضبط `PATH`، ثم أعد `stack/install.sh` |

### تشخيص سريع بثلاث خطوات

```bash
# 1) هل المرحّل حيّ؟
curl -s 127.0.0.1:8901/healthz

# 2) هل المفتاح يعمل؟
KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
curl -s -o /dev/null -w '%{http_code}\n' 127.0.0.1:8901/v1/models -H "authorization: Bearer $KEY"

# 3) هل الوجهة تسمح؟
curl -s 127.0.0.1:8901/api/stats -H "authorization: Bearer $KEY" | head -c 400
```

---

## 10. الأمان

- `zen-router/settings.json` يحوي المفتاح و`webshare_token` → `chmod 600`، ولا
  يُرفع إلى Git (مستبعد في `.gitignore`).
- `lanes.json` و`burned.json` حالة متغيّرة — لا تُرفع.
- `~/.dsh/.credentials.yaml` يجب أن يكون `0600`، وكذلك ملف الإعداد الذي يحوي
  `apiKey`.
- المفتاح واحد للعملاء ولـ`/api/*`: من يملكه يستطيع تغيير الإعدادات ومسح
  الحالة. لا تشاركه مع أجهزة غير موثوقة.
- عند النشر العام: احجب `/api/*` (Caddy)، واستخدم TLS، وفكّر في مفتاح منفصل
  للإدارة (غير متاح في 1.1.7 — يحتاج تعديل Rust).
- النفق يعرّض الـHarness نفسه: رابط الدخول يحوي توكن جلسة؛ لا تنشره علناً.

---

## 11. قائمة التحقق النهائية

- [ ] `node -v` ≥ 22 و`dsh --version` يعمل.
- [ ] `./run.sh` يعمل و`/healthz` يعيد `"ok":true`.
- [ ] `settings.json` موجود، مفتاحه `rly_...`، وصلاحياته `600`.
- [ ] `/v1/models` يعيد 200 مع المفتاح و401 بدونه.
- [ ] طلب إكمال يعيد نصاً (وليس 503).
- [ ] `opencode models | grep zen-relay` يظهر الموديل.
- [ ] `opencode run ... --model zen-relay/...` يعيد رداً.
- [ ] `~/.dsh/cordis.patch.yml` يحوي `opencode-zen` و`agent-default-model`.
- [ ] `dsh --profile web --dump-config | grep -A3 opencode-zen` يظهر `baseURL`.
- [ ] `OMP_ZEN_RELAY_KEY` موجود في بيئة الـHarness أو في `.credentials.yaml`.
- [ ] الـHarness يرد على رسالة اختبار بالموديل الصحيح.
- [ ] `systemctl --user is-active dsh-stack` = `active` (إن استخدمت الحزمة).
- [ ] `~/dsh-url.txt` يفتح الواجهة (مع التوكن).
- [ ] بعد إعادة إقلاع: كل ما سبق يعمل بلا تدخل.

---

## ملحق أ — كل المسارات

| المسار | المحتوى |
|---|---|
| `zen-router/bin/zen-relay` | الملف التنفيذي |
| `zen-router/rust/` | المصدر |
| `zen-router/settings.json` | الإعدادات + المفتاح (سرّي) |
| `zen-router/lanes.json` | لقطة الـpool الدافئة |
| `zen-router/burned.json` | ذاكرة العناوين المستنفدة |
| `~/.config/opencode/opencode.json(c)` | إعداد OpenCode العام |
| `./opencode.json` | إعداد OpenCode للمشروع |
| `~/.dsh/cordis.patch.yml` | ربط dsh بالمرحّل |
| `~/.dsh/.credentials.yaml` | مخزن اعتماد dsh |
| `~/.dsh/env.yml` | بيئة يديرها `dsh-env-manager` |
| `~/.config/dsh-stack/config.json` | إعداد المشرف الفعلي |
| `~/.config/systemd/user/dsh-stack.service` | وحدة systemd |
| `~/.local/state/dsh-stack/stack.log` | سجل المشرف |
| `~/dsh-url.txt` | رابط الـHarness + التوكن |

## ملحق ب — أوامر لا تُنسى

```bash
# المفتاح
KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)

# صحة
curl -s 127.0.0.1:8901/healthz

# كتالوج
curl -s 127.0.0.1:8901/v1/models -H "authorization: Bearer $KEY"

# اختبار سريع
curl -s 127.0.0.1:8901/v1/chat/completions \
  -H "authorization: Bearer $KEY" -H "content-type: application/json" \
  -d '{"model":"deepseek-v4.1-flash-free","messages":[{"role":"user","content":"hi"}],"max_tokens":5}'

# OpenCode
opencode models | grep zen-relay
opencode run "hi" --model zen-relay/deepseek-v4.1-flash-free

# dsh
dsh --profile web --dump-config | grep -A8 'opencode-zen:'
systemctl --user restart dsh-stack
cat ~/dsh-url.txt
```
