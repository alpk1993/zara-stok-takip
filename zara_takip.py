"""Zara TR beden stok izleyici. Python 3.10+; Playwright + Telegram Bot API."""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

PRODUCT_URL = 'https://www.zara.com/tr/tr/kareli-regular-fit-blazer-p00706408.html?v1=545475041'
PRODUCT_NAME = 'Zara Kareli Regular Fit Takım Blazer (koyu lacivert)'
SIZES = {'50', '52'}
INTERVAL_SECONDS = 600
STATE_FILE = Path(__file__).with_name('durum.json')
DEBUG_FILE = Path(__file__).with_name('stok_teshis.json')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()

AVAILABLE = {'in_stock', 'low_on_stock', 'available', 'low_stock', 'limited_stock'}
UNAVAILABLE = {'out_of_stock', 'not_available', 'unavailable', 'sold_out', 'coming_soon'}


def telegram_send(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError('TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID eksik. Kurulum rehberine bakın.')
    body = urllib.parse.urlencode({'chat_id': CHAT_ID, 'text': message, 'disable_web_page_preview': 'true'}).encode()
    request = urllib.request.Request(f'https://api.telegram.org/bot{TOKEN}/sendMessage', data=body)
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    if not result.get('ok'):
        raise RuntimeError(f'Telegram hatası: {result}')


def normalise(value):
    if isinstance(value, bool):
        return 'in_stock' if value else 'out_of_stock'
    if not isinstance(value, str):
        return None
    v = value.strip().lower().replace('-', '_').replace(' ', '_')
    return v if v in AVAILABLE | UNAVAILABLE else None


def extract_size(raw):
    if isinstance(raw, (int, str)):
        s = str(raw).strip()
        return s if s in SIZES else None
    if isinstance(raw, dict):
        for field in ('name', 'size', 'label', 'displayName', 'display_name'):
            v = raw.get(field)
            if isinstance(v, (str, int)) and str(v).strip() in SIZES:
                return str(v).strip()
    return None


def parse_status(d):
    if not isinstance(d, dict):
        return None
    for k in ('availability', 'availabilityStatus', 'availability_status', 'stockStatus', 'stock_status'):
        if k in d:
            obj = d[k]
            if isinstance(obj, dict):
                for nested in ('status', 'availability'):
                    if nested in obj:
                        v = normalise(obj[nested])
                        if v: return v
            else:
                v = normalise(obj)
                if v: return v
    # isAvailable, alone, is not reliable: may describe overall product rather than selected size.
    return None


def collect(json_obj, statuses, sku_to_size, depth=0):
    """Read only explicit per-size status. Resolve SKU availability via discovered size SKU mapping."""
    if depth > 20:
        return
    if isinstance(json_obj, dict):
        for field in ('sizes', 'sizeList', 'size_list'):
            size_list = json_obj.get(field)
            if isinstance(size_list, list):
                for size_obj in size_list:
                    if not isinstance(size_obj, dict): continue
                    size = extract_size(size_obj)
                    if not size: continue
                    for key in ('sku', 'skuId', 'sku_id', 'id'):
                        sku = size_obj.get(key)
                        if isinstance(sku, (int, str)) and str(sku).isdigit():
                            sku_to_size[str(sku)] = size
                    status = parse_status(size_obj)
                    if status: statuses.setdefault(size, set()).add(status)
        # Some payloads contain direct size records, not arrays
        if extract_size(json_obj):
            size = extract_size(json_obj)
            status = parse_status(json_obj)
            if status: statuses.setdefault(size, set()).add(status)
        for val in json_obj.values():
            collect(val, statuses, sku_to_size, depth + 1)
    elif isinstance(json_obj, list):
        for val in json_obj:
            collect(val, statuses, sku_to_size, depth + 1)


def collect_sku_status(json_obj, statuses, sku_to_size, depth=0):
    if depth > 20: return
    if isinstance(json_obj, dict):
        sku = next((json_obj[k] for k in ('sku', 'skuId', 'sku_id', 'id') if k in json_obj and isinstance(json_obj[k], (int, str))), None)
        if sku is not None and str(sku) in sku_to_size:
            status = parse_status(json_obj)
            if status:
                statuses.setdefault(sku_to_size[str(sku)], set()).add(status)
        for val in json_obj.values():
            collect_sku_status(val, statuses, sku_to_size, depth + 1)
    elif isinstance(json_obj, list):
        for val in json_obj:
            collect_sku_status(val, statuses, sku_to_size, depth + 1)


def check_stock(browser):
    # Read size selector DOM directly; never infer size from SKU order.
    page = browser.new_page(locale='tr-TR', viewport={'width': 1360, 'height': 900})
    page.set_default_timeout(12000)
    report = {'checked_at': datetime.now().isoformat(timespec='seconds'),
              'product_url': PRODUCT_URL, 'actions': [], 'sizes_seen': [], 'errors': []}
    result = {size: 'unknown' for size in SIZES}
    try:
        response = page.goto(PRODUCT_URL, wait_until='domcontentloaded', timeout=60000)
        report['http_status'] = response.status if response else None
        report['page_title'] = page.title()
        page.wait_for_timeout(5000)
        cookie = page.evaluate('''() => {
          const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],div,span'));
          const targets = nodes.filter(e => {
            const t=(e.innerText||'').trim().toLocaleUpperCase('tr-TR');
            return t==='İSTEĞE BAĞLI ÇEREZLERİ REDDET' || t==='TÜM ÇEREZLERİ KABUL ET';
          }).sort((a,b)=>a.getBoundingClientRect().width-b.getBoundingClientRect().width);
          if(targets.length){targets[0].click(); return 'clicked';}
          return 'not_shown';
        }''')
        report['actions'].append('cookie: '+cookie)
        page.wait_for_timeout(1200)
        
    # Ürün tamamen tükenmişse beden listesi gösterilmez.
    # Yalnızca ürün sayfası doğrulandığında stok dışı kabul et.
    sold_out_buttons = page.locator("button").filter(
        has_text=re.compile(
            r"BENZER\s+ÜRÜNLER\s+TÜKENDİ", re.IGNORECASE
        )
    )

    if (
        response is not None
        and response.status == 200
        and "KARELİ REGULAR FIT TAKIM BLAZER" in page.title().upper()
        and sold_out_buttons.count() > 0
    ):
        report['actions'].append('product_sold_out')
        result = {size: 'out_of_stock' for size in SIZES}
        report['result'] = result
        DEBUG_FILE.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding='utf-8'
        )
        return result


        add = page.locator('button[data-qa-action="add-to-cart"]').first
        try:
            add.click(timeout=4000)
            report['actions'].append('size_selector: clicked')
        except Exception:
            add.evaluate('(el) => el.click()')
            report['actions'].append('size_selector: DOM_clicked')
        page.locator('ul.size-selector-sizes li').first.wait_for(timeout=10000)
        page.wait_for_timeout(700)
        options = page.evaluate('''() => [...document.querySelectorAll('ul.size-selector-sizes > li')].map(li => {
           const b=li.querySelector('button');
           return {text:(li.innerText||'').trim(), className:li.className,
               action:b?.getAttribute('data-qa-action')||'',
               buttonDisabled: !!b?.disabled,
               ariaDisabled:b?.getAttribute('aria-disabled')||'',
               buttonClass:b?.className||''};
        })''')
        report['sizes_seen'] = options
        report['page_url'] = page.url
        for opt in options:
            match = re.search(r'\bEU\s*(50|52)\s*\(', opt['text'], re.I)
            if not match:
                continue
            size = match.group(1)
            cls = opt['className'].lower()
            action = opt['action'].lower()
            similar = 'benzer ürünler' in opt['text'].lower()
            unavailable = (similar or 'unavailable' in cls or '--disabled' in cls
                           or 'out-of-stock' in action or 'out_of_stock' in action)
            if unavailable:
                result[size] = 'out_of_stock'
            elif (not opt['buttonDisabled'] and opt['ariaDisabled'] != 'true'
                  and '--disabled' not in cls and 'unavailable' not in cls
                  and action and 'size' in action):
                result[size] = 'in_stock'
        report['result'] = result
        report['note'] = 'unknown means size could not be verified; no stock alert is sent.'
        DEBUG_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
    except Exception as e:
        report['errors'].append(f'{type(e).__name__}: {e}'[:500])
        try:
            report['page_title'] = page.title()
            report['page_url'] = page.url
            report['size_element_count'] = page.locator('ul.size-selector-sizes li').count()
        except Exception:
            pass
        report['result'] = result
        DEBUG_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return result
    finally:
        page.close()


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit('Önce TELEGRAM_BOT_TOKEN ve TELEGRAM_CHAT_ID ortam değişkenlerini ayarlayın. README.md dosyasına bakın.')
    once = '--once' in sys.argv or '--test' in sys.argv
    state = json.loads(STATE_FILE.read_text(encoding='utf-8')) if STATE_FILE.exists() else {}
    if '--test' in sys.argv:
        telegram_send('✅ Zara bulut test mesajı: Telegram bağlantısı çalışıyor.\n' + PRODUCT_URL)
    # Scheduled cloud runs do not send a startup notification on every check.

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=(os.getenv('ZARA_HEADED') != '1'), args=['--no-sandbox'])
        except Exception as exc:
            sys.exit(f'Chromium başlatılamadı: {exc}. Playwright Chromium kurulumunu kontrol edin.')
        try:
            while True:
                now = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
                try:
                    results = check_stock(browser)
                    print(f'[{now}] Stok: {results}', flush=True)
                    for size in sorted(SIZES):
                        status = results[size]
                        prev = state.get(size)
                        # On first observation, alert if already in stock, too.
                        if status in AVAILABLE and prev not in AVAILABLE:
                            telegram_send(f'🚨 ZARA STOKTA!\n{PRODUCT_NAME}\nBEDEN: {size}\nDurum: {status}\n{PRODUCT_URL}')
                        # Never replace confirmed status with unknown due to temporary page failure.
                        if status != 'unknown':
                            state[size] = status
                    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
                    if all(x == 'unknown' for x in results.values()):
                        print('HATA: Stok bilgisi çözümlenemedi. stok_teshis.json raporunu indirin; bildirim gönderilmedi.', flush=True)
                        if once:
                            sys.exit(2)
                except Exception as exc:
                    print(f'[{now}] Kontrol hatası: {type(exc).__name__}: {exc}', flush=True)
                if once: break
                time.sleep(INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print('\nTakip durduruldu.')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
