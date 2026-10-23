# تجمّع البروكسي — Proxy pool

كل تفاصيل طبقة الخروج: من أين تأتي العناوين، كيف تُفحص وتُرتّب، وكيف تُحفظ
حالتها. المصدر هو `zen-router/rust/src/pool_mgr.rs` و`src/state.rs`.

## 1. كيف يُبنى الـpool

1. `_fetch_sources` يجلب قوائم نصية من ~190 مصدراً (أدناه) عبر `SOURCE_UA`.
2. يوحّد التنسيق إلى `proto://host:port` ويرفض غير الصالح.
3. `_probe_candidate` يرسل **طلب إكمال حقيقي** (توكن واحد) إلى الوجهة عبر كل
   بروكسي — وليس فحص وصول فقط — لأن `/models` ينجح حتى من IP استُنفد رصيده.
4. البروكسي الذي ينجح يدخل الـpool فوراً (لا ينتظر انتهاء الدفعة).
5. `_churn_batch` يعيد الملء بمرشحين من `/24` غير مأهولة، ويحترم
   `max_lanes_per_subnet`.
6. `_revalidate_warm` يعيد فحص الـlanes الدافئة دورياً؛ الفاشلة تُوقف مؤقتاً.

## 2. المصادر (~190)


**`http`**

- `https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text&timeout=8000`
- `https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt`
- `https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt`
- `https://www.proxy-list.download/api/v1/get?type=http`
- `https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/http/data.txt`
- `https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt`
- `https://raw.githubusercontent.com/clarketm/proxy-list/master/proxy-list-raw.txt`
- `https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/http.txt`
- `https://raw.githubusercontent.com/mmpx12/proxy-list/master/http.txt`
- `https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-http.txt`
- `https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/HTTP.txt`
- `https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/http.txt`
- `https://raw.githubusercontent.com/prxchk/proxy-list/main/http.txt`
- `https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/main/http.txt`
- `https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/http.txt`
- `https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/http_proxies.txt`

**`https`**

- `https://www.proxy-list.download/api/v1/get?type=https`
- `https://raw.githubusercontent.com/roosterkid/openproxylist/main/HTTPS_RAW.txt`

**`proto`**

- `https://raw.githubusercontent.com/sunny9577/proxy-scraper/master/proxies.txt`
- `https://api.proxyscrape.com/v3/free-proxy-list/get?request=displayproxies&proxy_format=protocolipport&format=text`

**`socks4`**

- `https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt`
- `https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks4.txt`
- `https://www.proxy-list.download/api/v1/get?type=socks4`
- `https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks4/data.txt`
- `https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS4_RAW.txt`
- `https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks4.txt`
- `https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks4.txt`
- `https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks4.txt`
- `https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS4.txt`
- `https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks4.txt`
- `https://raw.githubusercontent.com/prxchk/proxy-list/main/socks4.txt`
- `https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/main/socks4.txt`
- `https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks4.txt`
- `https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks4_proxies.txt`

**`socks5`**

- `https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt`
- `https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt`
- `https://www.proxy-list.download/api/v1/get?type=socks5`
- `https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/protocols/socks5/data.txt`
- `https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt`
- `https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt`
- `https://raw.githubusercontent.com/mmpx12/proxy-list/master/socks5.txt`
- `https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt`
- `https://raw.githubusercontent.com/jetkai/proxy-list/main/online-proxies/txt/proxies-socks5.txt`
- `https://raw.githubusercontent.com/B4RC0DE-TM/proxy-list/main/SOCKS5.txt`
- `https://raw.githubusercontent.com/MuRongPIG/Proxy-Master/main/socks5.txt`
- `https://raw.githubusercontent.com/prxchk/proxy-list/main/socks5.txt`
- `https://raw.githubusercontent.com/vmheaven/VMHeaven-Free-Proxy-Updated/main/socks5.txt`
- `https://raw.githubusercontent.com/rdavydov/proxy-list/main/proxies/socks5.txt`
- `https://raw.githubusercontent.com/Anonym0usWork1221/Free-Proxies/main/proxy_files/socks5_proxies.txt`

## 3. البروتوكولات والتنسيق

المدعوم: `http`, `https`, `socks4`, `socks5`. التنسيق الموحّد:

```
proto://host:port
```

`allow_socks=false` يستبعد socks4/socks5 كلياً. المصادر من نوع `proto` تحمل
البروتوكول داخل السطر (`socks5://1.2.3.4:1080`).

**البروتوكول المُعلن غير موثوق — يُجرَّب البديل:** ملفات القوائم العامة تخلط
البروتوكولات باستمرار؛ `http.txt` تحمل عملياً خوادم SOCKS5. القياس على 240
مرشّحاً: 12 حيّاً، **كلها تتكلّم SOCKS5**، أحدها مُعلَن `http`. لذلك يجرّب
المرحّل البروتوكول المُعلن أولاً ثم `http` ثم `socks5`، ويُسجّل على الـlane
البروتوكول الذي نجح فعلاً (لا المُعلن).

`httpx` لا يملك ناقل SOCKS4 (socksio يطبّق SOCKS5 فقط)، فطلب `socks4://` يرفع
`ValueError` قبل إرسال أي بايت. لذلك يُطلب `socks5` لأي إعلان socks، وتُسقَط
خوادم SOCKS4-only.

## 4. الفحص والترتيب والعقاب

| المرحلة | ما يحدث |
|---|---|
| فحص | طلب إكمال 1 توكن عبر البروكسي (`proxy_probe_timeout` ثانية) |
| ترتيب | `score` = دمج النجاح والزمن (`lat_ms`)؛ تُفضّل الأسرع والأنجح |
| 429 من الوجهة | الـlane يُpark ويُسجّل في `burned.json` لأنه استنفد رصيد IP |
| قطع stream بعد الإرسال | إطار `stream_interrupted` واحد ويعيد المحاولة على lane آخر |
| حدّ التزامن | `lane_max_inflight` لكل lane، `proxy_test_concurrency` للفحص |
| استرداد | إعادة فحص بعد `lane_recover_sec`، وبارد بعد `lane_cooldown_sec` |

**`stage2_probe` يجب أن يبقى `false`:** الرصيد المجاني للوجهة إكمال واحد لكل
IP خروج. فحص الإكمال (stage 2) يستهلك ذلك الإكمال الوحيد قبل أن يخدم الـlane أي
طلب، فيصل أول طلب حقيقي إلى IP مستنفد ويُطرد الـlane — وهو سبب انهيار الـpool
إلى lane واحد. مع `false` يُرقّى المرشّح بعد فحص الوصول (`/zen/v1/models`)،
ويبدأ `ok=0`؛ أول طلب ناجح يثبته.

**الدوران على كل الـlanes لا على المثبتة فقط:** تدوير المؤشر على المثبتة فقط
يُجوّع كل lane غير مثبت: لا يستقبل حركة فلا يُثبت أبداً، وينهار الـpool إلى
الـlane الذي فاز بأول سباق (عادة WARP المحلي). الترتيب: المثبتة أولاً ثم
بالـ`score`، والمؤشر يمشي على القائمة كاملة.

**بصمة العميل إلزامية:** الوجهة ترفض أي `User-Agent` لا يحتوي `opencode`.
`upstream_user_agent` افتراضياً `opencode/latest/2.0.32/cli`، والمرحّل يعيد كتابة
UA العميل إن لم يطابق.

## 5. الحالة المحفوظة

`lanes.json` — اللقطة الدافئة (تُكتب كل 30 ثانية عند `persist_lanes`):

```json
[
  {
    "addr": "184.182.240.211:4145",
    "proto": "socks5",
    "score": 0.4722,
    "lat_ms": 2318.4,
    "ok": 1,
    "fails": 1,
    "last_ok": 1792616778
  }
]
```

`burned.json` — ذاكرة العناوين المستنفدة (`burn_memory`, `burn_ttl_sec`):

```json
{
  "ips": {
    "1.20.178.212": { "at": 1792622399.88, "hits": 1, "reason": "probe-429" }
  },
  "blocked_intake": 0
}
```

عند إعادة التشغيل تُحمّل الـlanes ذات `score > 0.3` كمرشحين أولوية، فيُعاد بناء
pool صالح في ثوانٍ بدل دقائق. الملفان مستبعدان من Git.

## 6. الإعدادات المؤثرة

| المفتاح | الافتراضي | الأثر |
|---|---|---|
| `proxy_pool_target` | 40 | عدد الـlanes الدافئة المستهدفة |
| `proxy_max_candidates` | 1500 | سقف المرشحين في الطابور |
| `proxy_test_concurrency` | 30 | تزامن الفحص (يُعدّله `adaptive_concurrency`) |
| `proxy_probe_timeout` | 25 | مهلة فحص البروكسي (ثانية) |
| `relay_attempts` | 8 | عدد الـlanes التي تُجرّب قبل إرجاع الخطأ |
| `lane_max_inflight` | 2 | أقصى طلبات متزامنة على lane واحد |
| `max_lanes_per_subnet` | 3 | تنوّع الـ/24 (السعة الفعلية) |
| `lane_cooldown_sec` | 3600 | مدة إيقاف lane بعد فشل |
| `lane_recover_sec` | 3600 | إعادة فحص lane موقوف |
| `burn_memory` / `burn_ttl_sec` | true / 86400 | نسيان IP محروق بعد 24 ساعة |
| `allow_socks` | true | السماح بـsocks4/socks5 |
| `sources_enabled` | true | جلب القوائم الحرة |
| `direct_lane` | false | استخدام IP الجهاز كـlane احتياطي |
| `webshare_token` | "" | قائمة Webshare المدفوعة (اختياري) |
| `tor_enabled` / `tor_lanes` / `tor_socks_port` | false / 12 / 9150 | lanes عبر Tor المحلي |

## 7. إضافة بروكسيات يدوياً

```bash
KEY=$(grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json | head -1)
curl -s http://127.0.0.1:8901/api/pool/seed \
  -H "authorization: Bearer $KEY" -H "content-type: application/json" \
  -d '{"proto":"socks5","lanes":["1.2.3.4:1080","5.6.7.8:1080"]}'
# {"added":2,"queue":...}
```

`POST /api/refresh` يعيد الفحص فوراً. العناوين الموجودة في `burned.json` تُرفض
عند الإدخال (`blocked_intake`).

## 8. Webshare وTor

- **Webshare:** ضع `webshare_token`؛ يُجلب من
  `https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page=1&page_size=100`.
- **Tor:** شغّل Tor محلياً على `tor_socks_port` ثم `tor_enabled=true`؛ ينشئ
  `tor_lanes` قناة خروج مستقلة عبر ‎127.0.0.1:9150.

## 9. حدود معروفة

- الـpool من بروكسيات حرة: العمر قصير والجودة متقلبة؛ `warm` قد يهبط ثم يعود.
- الوجهة تفرض رصيداً لكل IP؛ لذلك 429 يعني «هذا الـIP انتهى»، لا «المفتاح خطأ».
- لا تُضف مصدراً يعيد تنسيقاً غير `host:port` إلا بتعديل المحلل في
  `pool_mgr.rs` (أو استخدم `proto` في السطر).
