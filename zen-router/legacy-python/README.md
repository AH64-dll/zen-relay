# legacy-python — مرحّل بايثون (متوقف، للطوارئ فقط)

هذا المجلد يحتوي النسخة القديمة (بايثون) من مرحّل Zen. **ليست قيد التشغيل ولم تعد الإنتاج.**

- الإنتاج الآن: `rust/target/release/zen-relay` (رست v1.1.2) عبر خدمة `zen-rotator.service` على `127.0.0.1:8901`.
- بايثون أُوقف نهائياً بتاريخ 2026-10-19 ونُقل هنا كخيار تراجع (rollback) فقط.
- نفس قائمة المصادر الجديدة (50 قائمة) موجودة هنا أيضاً، للاحتياط.

## تشغيل يدوي (للاختبار فقط)

```bash
cd /home/ai/.omp/zen-router/legacy-python
PORT=8905 /home/ai/.omp/zen-router/.venv/bin/python -m uvicorn ip_relay:app --host 127.0.0.1 --port 8905
```

## العودة إلى بايثون كإنتاج (تراجع كامل)

```bash
cp /home/ai/.omp/zen-router/staging/zen-rotator.service.python.bak ~/.config/systemd/user/zen-rotator.service
systemctl --user daemon-reload
systemctl --user restart zen-rotator
# للعودة إلى رست:
#   systemctl --user restart zen-rotator   (النسخة الحالية من الوحدة تبدأ رست)
```

## بدء بايثون داخل المختبر (A/B)

`testbed/relay_testbed.sh python 8905` يعمل من هذا المجلد تلقائياً.

## الاختبارات القديمة

مجلد `../tests/` يستورد `ip_relay` مباشرة؛ شغّلها بـ:

```bash
cd /home/ai/.omp/zen-router
PYTHONPATH=legacy-python /home/ai/.omp/zen-router/.venv/bin/python -m pytest tests/ -q
```
