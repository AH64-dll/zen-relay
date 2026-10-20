# anu-ai-agent — Zen Relay 1.1.7

مرحّل متوافق مع OpenAI API، يوزّع الطلبات على عناوين خروج متغيرة (rotating egress IPs) لتفادي حدود المعدل المفروضة لكل IP. يحتوي هذا الريبو النسخة الكاملة من مشروع **zen-router** داخل `zen-router/`، مع ملف تنفيذي جاهز لـ **Linux x86_64** لتشغيل مباشر.

## التشغيل السريع (Linux x86_64)

```bash
git clone https://github.com/utopianumber39ray-spec/anu-ai-agent.git
cd anu-ai-agent
./run.sh
```

أول تشغيل:
- ينشئ `zen-router/settings.json` تلقائياً.
- يولّد مفتاحاً عشوائياً للمرحّل (`relay_api_key`).
- يستمع على `http://127.0.0.1:8901` فقط.
- يبدأ ببناء pool من البروكسيات المجانية (يحتاج بعض الثواني ليصبح جاهزاً).

### قراءة مفتاح المرحّل

```bash
python3 -c "import json;print(json.load(open('zen-router/settings.json'))['relay_api_key'])"
# أو
grep -o 'rly_[0-9a-f]\{32\}' zen-router/settings.json | head -1
```

### الاستخدام من أي عميل OpenAI

- **Base URL:** `http://127.0.0.1:8901/v1`
- **API Key:** المفتاح أعلاه
- **Model:** مثال `deepseek-v4.1-flash-free` (أو أي موديل متاح من الوجهة)

فحص الصحة:

```bash
curl http://127.0.0.1:8901/healthz
```

## الإعدادات الافتراضية

- الوجهة: `https://opencode.ai/inference/openai/v1`
- مفتاح الوجهة: `public` (الطبقة المجانية في OpenCode Zen — لا يحتاج حساباً)
- المنفذ: `8901`
- الاستماع: `127.0.0.1` فقط
- الـpool: 30–40 lane، مع فحص حقيقي لكل بروكسي قبل ترقيته

لا حاجة لأي مفتاح أو حساب خارجي. المرحّل يحمل بصمة OpenCode المطلوبة للطبقة المجانية.

## المتطلبات

- **Linux x86_64:** الملف `zen-router/bin/zen-relay` جاهز (glibc). شغّله مباشرة.
- **macOS / Windows / معماريات أخرى:** ثبّت Rust من <https://rustup.rs> ثم:

```bash
cd zen-router/rust
cargo build --release
cd ..
./rust/target/release/zen-relay        # على Windows: zen-relay.exe
```

أو أعد تشغيل `./run.sh` وسيبني من المصدر تلقائياً عند وجود `cargo`.

## تخصيص التشغيل

```bash
PORT=8901 HOST=127.0.0.1 ./run.sh
```

لا تفتح المرحّل على الإنترنت (`HOST=0.0.0.0`) دون جدار ناري ومفتاح قوي؛ فهو يمنح وصولاً إلى الوجهة عبر عناوينك.

لتغيير الوجهة أو المفتاح أو إعدادات الـpool: عدّل `zen-router/settings.json` ثم أعد التشغيل.

## ملاحظات مهمة

- المشروع الأصلي موجود كاملاً داخل `zen-router/` دون تعديل على ملفاته.
- أُضيف فقط `bin/zen-relay` (نسخة مبنية) و`run.sh` (المشغّل) في هذا الريبو.
- تم استثناء ملفات البناء والحالة والأسرار: `target/`، `.venv/`، `.git/`، `settings.json`، `lanes.json`، `burned.json`، `.env`، والسجلات. المرحّل يولّدها عند أول تشغيل.
- `install.sh` و`install.ps1` القديمة تخصّ نسخة Python القديمة من المشروع؛ استخدم `./run.sh` بدلاً منهما.
- استخدم الأداة وفق شروط مزوّد الخدمة. الهدف منها توزيع الحدود المسموحة لكل IP، لا تجاوزها.

---

# English

A self-contained OpenAI-compatible relay with rotating egress IPs, built from the **zen-router 1.1.7** Rust project.

## Quick start (Linux x86_64)

```bash
git clone https://github.com/utopianumber39ray-spec/anu-ai-agent.git
cd anu-ai-agent
./run.sh
```

The first run creates `zen-router/settings.json` with a random relay API key, listens on `127.0.0.1:8901`, and starts warming a proxy pool.

Read the key:

```bash
python3 -c "import json;print(json.load(open('zen-router/settings.json'))['relay_api_key'])"
```

Use any OpenAI-compatible client with:

- **Base URL:** `http://127.0.0.1:8901/v1`
- **API key:** the generated `relay_api_key`
- **Model:** e.g. `deepseek-v4.1-flash-free`

Health check: `curl http://127.0.0.1:8901/healthz`

Defaults: upstream `https://opencode.ai/inference/openai/v1`, upstream key `public`, loopback-only, no external account required.

## Other platforms

Install Rust from <https://rustup.rs>, then:

```bash
cd zen-router/rust
cargo build --release
cd ..
./rust/target/release/zen-relay
```

`./run.sh` builds from source automatically when `cargo` is available.

## Repository layout

- `zen-router/` — complete upstream project, unmodified.
- `zen-router/bin/zen-relay` — prebuilt Linux x86_64 binary (v1.1.7).
- `run.sh` — one-command launcher (prebuilt binary or source build).

Build artifacts, runtime state, and secrets are excluded; the relay regenerates them on first run.
