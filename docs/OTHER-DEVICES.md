# استخدام المرحّل من أجهزة أخرى — Remote use

هذا المستند يجيب على السؤال: «ما الذي ينقص كي يعمل المرحّل على جهاز آخر؟»
ويشرح الطريقتين المدعومتين، والقيود المعروفة.

> **مهم:** `zen-relay` ليس VPN ولا بروكسي شبكة. هو **خدمة متوافقة مع OpenAI API**
> (`/v1/models`, `/v1/chat/completions`, `/v1/messages`). أي جهاز يستخدمه يجب أن
> يكون عميل OpenAI API: تطبيق، سكربت، أو الـHarness. لا يمكن توجيه تصفّح الجهاز
> عبره، ولا يصلح كـ«سيرفر بروكسي» في تطبيقات VPN.

---

## 1. الطريقتان

### أ) مرحّل محلي على كل جهاز (الأنسب والأسرع)

كل جهاز يشغّل نسخته من `zen-relay` محلياً على `127.0.0.1:8901`. لا نفق، لا مفتاح
مشترك، ولا سطح هجوم إضافي — لكن كل جهاز يبني pool خاصاً به من البروكسيات.

```bash
git clone https://github.com/AH64-dll/zen-relay.git
cd zen-relay && ./run.sh
```

ثم أي عميل OpenAI على نفس الجهاز:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8901/v1
export OPENAI_API_KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
```

### ب) مرحّل واحد مشترك + نفق HTTPS (للأجهزة التي لا تشغّل لينكس)

هذا هو النمط الذي ينقص المستودع سابقاً، وأُضيف هنا:

1. **شغّل المرحّل على الخادم** (يبقى على loopback):

   ```bash
   PORT=8901 HOST=127.0.0.1 ./run.sh
   ```

2. **انشر نفقاً عاماً**:

   ```bash
   scripts/expose-relay.sh
   ```

   يطبع شيئاً مثل:

   ```
   base URL : https://<name>.trycloudflare.com/v1
   api key  : rly_...
   ```

3. **على الجهاز الآخر** استخدم العنوان والمفتاح:

   ```bash
   export OPENAI_BASE_URL=https://<name>.trycloudflare.com/v1
   export OPENAI_API_KEY=rly_...
   curl -s "$OPENAI_BASE_URL/models" -H "authorization: Bearer $OPENAI_API_KEY"
   ```

   ويمكن فحص الصحة بلا مفتاح: `curl https://<name>.trycloudflare.com/healthz`

---

## 2. أمثلة عملاء

**Python (openai>=1.0)**

```python
from openai import OpenAI
client = OpenAI(base_url="https://<name>.trycloudflare.com/v1", api_key="rly_...")
print(client.chat.completions.create(
    model="deepseek-v4.1-flash-free",
    messages=[{"role": "user", "content": "مرحبا"}],
).choices[0].message.content)
```

**Node (openai v4)**

```js
import OpenAI from "openai";
const client = new OpenAI({ baseURL: "https://<name>.trycloudflare.com/v1", apiKey: "rly_..." });
const r = await client.chat.completions.create({
  model: "deepseek-v4.1-flash-free",
  messages: [{ role: "user", content: "hello" }],
});
console.log(r.choices[0].message.content);
```

**Harness على جهاز آخر** — غيّر `baseURL` فقط في `cordis.patch.yml`:

```yaml
baseURL: https://<name>.trycloudflare.com/v1
apiKeyEnv: OMP_ZEN_RELAY_KEY
```

المفتاح في `apiKeyEnv` لا في الملف، كما في `config/dsh-cordis.patch.yml`.

**Anthropic-شكل** — المسار `/v1/messages` مدعوم أيضاً، لذا يعمل مع عملاء
Anthropic SDK بنفس المفتاح والعنوان.

---

## 3. الشبكة المحلية (LAN) بدون نفق

```bash
HOST=0.0.0.0 PORT=8901 ./run.sh
```

ثم من الجهاز الآخر: `http://<server-ip>:8901/v1`. هذا **بلا TLS**؛ اقتصر عليه
على شبكة موثوقة، وافتح المنفذ للمضيف المحدد فقط:

```bash
sudo ufw allow from 192.168.0.0/24 to any port 8901 proto tcp
```

---

## 4. القيود المعروفة (كن على علم)

| القيد | الأثر | التخفيف |
|---|---|---|
| نفق quick يتغيّر اسمه عند كل تشغيل | عملاء الأجهزة الأخرى يحتاجون العنوان الجديد | انظر قسم «نطاق ثابت» أدناه |
| مفتاح واحد فقط، وهو نفسه مفتاح الإدارة | أي جهاز يملكه يستطيع `POST /api/settings` و`/api/refresh` | لا تشاركه مع أجهزة غير موثوقة؛ أو ضع reverse proxy يحجب `/api/*` |
| لا CORS | واجهات ويب تعمل من أصل مختلف (Open WebUI مثلاً) تُحجب في المتصفح | استخدم reverse proxy يضيف CORS، أو اجعل الواجهة تتصل عبر خادمها |
| لا TLS في المرحّل نفسه | `HOST=0.0.0.0` مكشوف نصّاً | استخدم النفق أو reverse proxy بـTLS |
| لا حدّ معدّل لكل عميل | جهاز واحد يمكنه استهلاك الـpool كله | حدّد `lane_max_inflight` و`proxy_pool_target`، أو reverse proxy بحدود |

### واجهات المتصفح وCORS + حماية `/api`

المرحّل لا يرسل ترويسات CORS ولا يجيب على `OPTIONS`؛ أي واجهة ويب تعمل من أصل
مختلف ستفشل في المتصفح. كما أن المفتاح نفسه يفتح `/api/*`. الحل الجاهز في
[`config/Caddyfile.example`](../config/Caddyfile.example): يضيف CORS، يجيب
preflight، ويحجب `/api/*` عن الأجهزة البعيدة، مع TLS تلقائي.

```bash
caddy run --config config/Caddyfile.example   # بعد تعديل النطاق
```

بديل بلا Caddy: حجب `/api/*` في إعداد نفق مُسمّى، أو استخدم عميلاً غير متصفح
(SDK / Harness) فلا تحتاج CORS أصلاً.

### نطاق ثابت (بدل quick tunnel)

أنشئ نفقاً مُسمّى مرة واحدة:

```bash
cloudflared tunnel login
cloudflared tunnel create zen-relay
cloudflared tunnel route dns zen-relay relay.example.com
cloudflared tunnel run --url http://127.0.0.1:8901 zen-relay
```

بعدها `https://relay.example.com/v1` ثابت، ولا يتغيّر مع إعادة التشغيل.

---

## 5. ما لا يفعله هذا المستودع

- لا يوزّع مفتاحاً لكل جهاز، ولا يدير الحصص لكل مستخدم.
- لا يحوّل التصفّح العام للجهاز عبر الـpool (ليس VPN).
- لا يبني ملفاً تنفيذياً لويندوز/ماك؛ ابنِ من المصدر (`cargo build --release`).
