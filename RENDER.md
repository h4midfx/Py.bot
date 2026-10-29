# راهنمای دیپلوی روی Render (قدم‌به‌قدم)

> اطلاعات Render (نوع سرویس، قیمت، دیسک، Blueprint) از مستندات رسمی در زمان نوشتن بررسی شد. داشبورد و قیمت‌ها را در render.com/pricing دوباره ببینید.

## ۱) نوع سرویس درست: Background Worker (نه Web Service رایگان)
ربات تلگرام باید ۲۴ ساعته و بدون وقفه کار کند.
- **Web Service رایگان** بعد از ۱۵ دقیقه بدون ترافیک HTTP خاموش می‌شود و دیسک دائمی هم ندارد؛ ربات (polling) با آن کار نمی‌کند. ❌
- **Background Worker** پلن رایگان ندارد؛ کمترین پلن **Starter ≈ ۷ دلار/ماه** (۰٫۵ vCPU، ۵۱۲MB RAM). این برای ربات کافی است. ✅
- **دیسک دائمی** فقط روی سرویس‌های پولی ممکن است: ≈ **۰٫۲۵ دلار برای هر GB در ماه** (۱GB کافی است).
- جمع تقریبی: **حدود ۷٫۲۵ دلار در ماه** (پلن Workspace «Hobby» رایگان است). پرداخت با کارت بین‌المللی لازم است.

## ۲) آماده‌سازی
1. توکن ربات از @BotFather و آیدی عددی خودتان از @userinfobot.
2. فایل‌های پروژه را در یک مخزن **Private** GitHub بگذارید (حتماً شامل `render.yaml`، `requirements.txt`، `.python-version`). **توکن را در GitHub نگذارید.**

## ۳) روش ساده: Blueprint (پیشنهادی)
1. render.com ← Sign up (با GitHub).
2. **New + ← Blueprint** ← مخزن را انتخاب کنید. Render فایل `render.yaml` را می‌خواند: یک Background Worker + دیسک ۱GB در **Frankfurt**.
3. مقدار `BOT_TOKEN` و `ADMIN_IDS` را وارد کنید (در GitHub ذخیره نمی‌شوند).
4. **Apply / Deploy**. (در این مرحله کارت پرداخت لازم می‌شود.)

### روش دستی (اگر Blueprint نخواستید)
**New + ← Background Worker** ← مخزن ← Language: **Python 3** ← Build: `pip install -r requirements.txt` ← Start: `python signal_bot.py` ← Instance: **Starter** ← Region: **Frankfurt** (یا Singapore) ← **Advanced ← Add Disk**: Name `bot-data`، Mount Path `/var/data`، Size 1GB ← متغیرها (در جدول پایین).

## ۴) ریجن
- ✅ **Frankfurt** یا **Singapore**
- ❌ Oregon / Ohio / Virginia (آمریکا): بایننس فیوچرز IP آمریکا را معمولاً مسدود می‌کند (خطای 451).
- ریجن سرویس بعد از ساخت معمولاً قابل تغییر نیست؛ اگر اشتباه بود سرویس جدید بسازید و دیتابیس را با `/backup` و `/restore` منتقل کنید.
- اگر بایننس باز هم مسدود بود: متغیر `EXCHANGE=bybit`.

## ۵) متغیرهای محیطی (تب Environment)
| متغیر | مقدار |
|---|---|
| `BOT_TOKEN` | توکن BotFather (محرمانه) |
| `ADMIN_IDS` | آیدی عددی شما |
| `DB_PATH` | `/var/data/bot.db` (**حتماً روی مسیر دیسک**، وگرنه با هر دیپلوی داده‌ها پاک می‌شود) |
| `PLANS` | برای تست `test:1:1`، بعداً `week:7:150,month:30:500,quarter:90:1200` |
| `PYTHON_VERSION` | `3.12.3` |
| `EXCHANGE` / `TIMEFRAME` / `MIN_SCORE` | `binance` / `15m` / `2` |
| اختیاری | `SUPPORT_USERNAME`، `HTF`، `STOP_MODE`، `MAX_FEE_R`، `PAUSE_AFTER_LOSSES` … (فایل `.env.example`) |

تغییر متغیرها، سرویس را خودکار دوباره دیپلوی می‌کند.

## ۶) بررسی لاگ
تب **Logs**. باید ببینید:
```
bot starting | exchange=binance | plans=['test'] | ... | db=/var/data/bot.db | stop=fixed
```
اگر `db=bot.db` بود، `DB_PATH` یا دیسک درست تنظیم نشده.

## ۷) تست قبل از فروش
1. `/start` و `/health` (ادمین): «نمادهای موفق» نباید صفر باشد.
2. `/buy` ← پلن test ← پرداخت **۱ Star** ← `/status` فعال شود، `/stats` پرداخت را نشان دهد.
3. `/backup` فایل دیتابیس را بفرستد.
4. `/backtest ALL 90` و مقایسه‌ی `HTF`، `STOP_MODE`، `MAX_FEE_R`. فقط اگر نتیجه‌ی واقعی قابل قبول بود، قیمت واقعی بگذارید.

## ۸) به‌روزرسانی
هر Commit به GitHub خودکار دیپلوی می‌شود. سرویس‌های دارای دیسک موقع دیپلوی چند ثانیه قطع می‌شوند (زمان‌بندی بدون وقفه ندارند)؛ برای ربات مشکلی نیست. اگر خطای `Conflict: terminated by other getUpdates` ماندگار بود، ربات جای دیگری (Railway، کامپیوتر، سرویس دوم) هم در حال اجراست؛ فقط یک نمونه فعال باشد.

## ۹) انتقال از Railway (یا هر جای دیگر) به Render
1. در ربات قدیمی `/backup` بزنید و فایل را نگه دارید.
2. ربات قدیمی را **خاموش** کنید (دو نمونه هم‌زمان نباید polling کنند).
3. ربات جدید روی Render را بالا بیاورید (همان `BOT_TOKEN`).
4. همان فایل را با کپشن `/restore CONFIRM` برای ربات بفرستید. اشتراک‌ها و پرداخت‌ها برمی‌گردند.

## مشکلات رایج
| علامت | علت / راه‌حل |
|---|---|
| Build ناموفق (نسخه Python) | `PYTHON_VERSION` باید کامل باشد، مثل `3.12.3` |
| `fetch ... failed` / 451 / 403 | ریجن آمریکا است یا بایننس مسدود است ← Frankfurt/Singapore یا `EXCHANGE=bybit` |
| `ConfigError` | پیام می‌گوید کدام متغیر نادرست است |
| اشتراک‌ها بعد از دیپلوی پاک شد | `DB_PATH` روی دیسک نیست یا دیسک وصل نشده |
| ربات جواب نمی‌دهد و لاگ ساکت است | توکن غلط، یا سرویس Suspend شده/پرداخت مشکل دارد |
| ربات خودش ری‌استارت می‌شود | نگهبان (`WATCHDOG_SECONDS`) قفل‌شدن را دیده؛ لاگ قبل از آن را ببینید |

## امنیت
- توکن فقط در Environment باشد. اگر لو رفت: @BotFather ← `/revoke` و توکن جدید.
- مخزن Private؛ لاگ‌ها را عمومی منتشر نکنید.
