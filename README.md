# zen-relay — Zen Relay 1.1.8

مرحّل متوافق مع OpenAI API، يوزّع الطلبات على عناوين خروج متغيرة (rotating egress IPs) لتفادي حدود المعدل المفروضة لكل IP. هذا المستودع مخصّص لنسخة **Rust** فقط: المصدر داخل `zen-router/rust/`، والملف التنفيذي الجاهز في `zen-router/bin/zen-relay`.

## التشغيل السريع (Linux x86_64)

```bash
git clone https://github.com/AH64-dll/zen-relay.git
cd zen-relay
./run.sh
```

أول تشغيل:
- ينشئ `zen-router/settings.json` تلقائياً.
- يولّد مفتاحاً عشوائياً للمرحّل (`relay_api_key`).
- يستمع على `http://127.0.0.1:8901` فقط.
- يبدأ ببناء pool من البروكسيات المجانية (يحتاج بعض الثواني ليصبح جاهزاً).

### قراءة مفتاح المرحّل

```bash
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

- **Linux x86_64:** الملف `zen-router/bin/zen-relay` جاهز (يتطلب glibc 2.34 أو أحدث: Ubuntu 22.04+، Debian 12+). شغّله مباشرة.
- **macOS / Windows / معماريات أخرى:** ثبّت Rust من <https://rustup.rs> ثم:

```bash
cd zen-router/rust
cargo build --release
cd ..
./rust/target/release/zen-relay        # على Windows: zen-relay.exe
```

أو أعد تشغيل `./run.sh` وسيبني من المصدر تلقائياً عند وجود `cargo`.

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

---

# English

A self-contained OpenAI-compatible relay with rotating egress IPs, built from the **zen-relay 1.1.8** Rust source.

## Quick start (Linux x86_64)

```bash
git clone https://github.com/AH64-dll/zen-relay.git
cd zen-relay
./run.sh
```

The first run creates `zen-router/settings.json` with a random relay API key, listens on `127.0.0.1:8901`, and starts warming a proxy pool.

Read the key:

```bash
grep -o 'rly_[0-9a-f]\{32\}' zen-router/settings.json | head -1
```

Use any OpenAI-compatible client with:

- **Base URL:** `http://127.0.0.1:8901/v1`
- **API key:** the generated `relay_api_key`
- **Model:** e.g. `deepseek-v4.1-flash-free`

Health check: `curl http://127.0.0.1:8901/healthz`

Defaults: upstream `https://opencode.ai/inference/openai/v1`, upstream key `public`, loopback-only, no external account required.

## Other platforms

The prebuilt binary requires glibc 2.34+ (Ubuntu 22.04+, Debian 12+). On older systems, build from source.

Install Rust from <https://rustup.rs>, then:

```bash
cd zen-router/rust
cargo build --release
cd ..
./rust/target/release/zen-relay
```

`./run.sh` builds from source automatically when `cargo` is available.

## Security notes

- **TLS certificate verification is enforced** (since 1.1.8). A proxy in the pool can see that you talk to the upstream, plus timing and sizes — it cannot read or rewrite the conversation, because a proxy attempting to intercept TLS is rejected instead of trusted.
- `settings.json`, `lanes.json` and `burned.json` are written `0600`; `POST /api/settings` masks secrets the same way `GET` does; proxy passwords are never echoed by `/api/pool`.
- The relay key is generated on first run and never printed in full — read it from `settings.json`.
- `probe_model` is validated against the live model list and replaced automatically if the upstream retires it.
- Binding to a non-loopback `HOST` logs a warning, and an empty `relay_api_key` is rejected by the API (use `RELAY_ALLOW_ANONYMOUS=1` if you really want an unauthenticated relay).

## Repository layout

- `zen-router/rust/` — Rust source (Cargo).
- `zen-router/bin/zen-relay` — prebuilt Linux x86_64 binary (v1.1.8).
- `run.sh` — one-command launcher (prebuilt binary or source build).

Build artifacts, runtime state, and secrets are excluded; the relay regenerates them on first run.
