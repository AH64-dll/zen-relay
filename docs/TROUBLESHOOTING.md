# استكشاف الأخطاء — Troubleshooting

| العرض | الفحص | الحل |
|---|---|---|
| `401 unauthorized` من `/v1/...` | هل المفتاح مطابق لـ`relay_api_key`؟ | `grep -oE 'rly_[0-9a-f]{32}' zen-router/settings.json` وأعد إرسال `authorization: Bearer …` |
| `401` على الرابط العام للـHarness | فُتح بدون توكن | افتح الرابط الكامل من `~/dsh-url.txt` (`?token=…`) مرة واحدة لتُزرع الكوكي |
| `530` من Cloudflare | النفق مقطوع عن الأصل | `systemctl --user restart dsh-stack` (يعيد النفق ويولّد اسماً جديداً) |
| الرابط القديم يعطي `000` | اسم quick tunnel تغيّر | خذ الاسم الحالي من `~/dsh-url.txt`؛ أو انقل إلى named tunnel |
| `503 zen_lane_blocked` متكرر | الـpool بارد أو محروق | انتظر دقيقة؛ `curl /healthz` ثم `/api/pool`؛ أو `/api/refresh` |
| `warm` أكبر من صفر لكن `proven: 0` | `curl /healthz` | الـlanes قابلة للوصول لكنها لم تنجح في طلب واحد بعد؛ البوابة تعطي كل IP رصيد طلب واحد، و`stage2_probe: false` يمنع الفحص من استهلاكه |
| `403 quota` من المزوّد عبر كل الـlanes | `/api/pool` ثم `/api/burned` | كل IP في الدور استُهلك رصيده (منك أو من غيرك)؛ المرحّل يبدّل الـIP تلقائياً، لكن رصيد IP واحد = طلب واحد |
| `FreeTierError` من المزوّد أو `warm=0` دائم | اطلب `inference/openai/v1` مباشرة بلا بروكسي | المزوّد يقيّد الطبقة المجانية على عميل OpenCode الرسمي؛ استخدم الجسر (`opencode-bridge` على منفذ `8902`) فيمرّ الطلب من OpenCode نفسه، أو مفتاح OpenCode صالح أو مزوّد آخر |
| `GLIBC_2.34 not found` | توزيعة قديمة | ابنِ من المصدر: `cargo build --release` |
| `Address already in use` | عملية أخرى على 8901 | `ss -lntp \| grep 8901`؛ أوقف `zen-rotator` إن كان `dsh-stack` يدير المرحّل |
| لا يبدأ `dsh-stack` | `journalctl --user -u dsh-stack -n 80` | غالباً المرحّل غير جاهز: `proxy-not-ready` في `~/.local/state/dsh-stack/stack.log` |
| `spawn ... ENOENT` والمرحّل `stopped` | `node stack/bin/dsh-stack doctor` | `proxy.cwd` يشير إلى `~/.omp/zen-router` غير الموجود؛ أعد `./install.sh` ليضبطه على `zen-router/` داخل المستودع |
| الواجهة تعمل بلا نماذج | `curl localhost:8901/healthz` | المسار ميت؛ أعد تشغيل الحزمة، وتحقق من `cordis.patch.yml` |
| نفقان معاً / تقلّب الرابط | `pgrep -af cloudflared` | عطّل الوحدة القديمة: `systemctl --user disable --now cloudflared-tunnel` |
| أجهزة أخرى تُحجب في المتصفح | لا CORS | استخدم عميلاً غير متصفح أو reverse proxy يضيف CORS |

## أوامر نافعة

```bash
systemctl --user status dsh-stack
journalctl --user -u dsh-stack -f
tail -f ~/.local/state/dsh-stack/stack.log
node stack/bin/dsh-stack status --probe
node stack/bin/dsh-stack wait --target all
systemctl --user kill -s SIGHUP  dsh-stack   # إعادة تشغيل الـHarness فقط
systemctl --user kill -s SIGUSR2 dsh-stack   # إعادة تشغيل المرحّل فقط
```

## سجلات المرحّل

المرحّل يكتب إلى stdout، فتجدها في:

```bash
journalctl --user -u dsh-stack | grep -i relay
```

أو في `stack.log` بصيغة JSON عند تشغيله تحت المشرف.
