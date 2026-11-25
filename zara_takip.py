"""Zara TR stok takibi: 50/52 beden, Telegram. Python 3.10+ / Playwright."""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright

PRODUCT_URL = 'https://www.zara.com/tr/tr/kareli-regular-fit-blazer-p00706408.html?v1=545475041'
PRODUCT_NAME = 'Zara Kareli Regular Fit Takım Blazer (koyu lacivert)'
SIZES = ('50', '52')
INTERVAL_SECONDS = 600
STATE_FILE = Path(__file__).with_name('durum.json')
DEBUG_FILE = Path(__file__).with_name('stok_teshis.json')
TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '').strip()


def telegram_send(message):
    if not TOKEN or not CHAT_ID:
        raise RuntimeError('Telegram token veya chat ID eksik. ayarlar.bat dosyasini kontrol edin.')
    payload = urllib.parse.urlencode({
        'chat_id': CHAT_ID,
        'text': message,
        'disable_web_page_preview': 'true',
    }).encode('utf-8')
    request = urllib.request.Request(
        'https://api.telegram.org/bot' + TOKEN + '/sendMessage', data=payload
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        reply = json.load(response)
    if not reply.get('ok'):
        raise RuntimeError('Telegram mesaji gonderilemedi.')


def check_stock(browser):
    result = {s: 'unknown' for s in SIZES}
    report = {'checked_at': datetime.now().isoformat(timespec='seconds'),
              'product_url': PRODUCT_URL, 'actions': [], 'sizes_seen': [], 'errors': []}
    page = browser.new_page(locale='tr-TR', viewport={'width': 1360, 'height': 900})
    page.set_default_timeout(12000)
    try:
        response = page.goto(PRODUCT_URL, wait_until='domcontentloaded', timeout=60000)
        report['http_status'] = response.status if response else None
        page.wait_for_timeout(4500)
        title = page.title()
        report['page_title'] = title
        page.evaluate('''() => {
          const nodes = Array.from(document.querySelectorAll('button,a,[role="button"]'));
          const target = nodes.find(el => {
            const t = (el.innerText || '').trim().toLocaleUpperCase('tr-TR');
            return t === 'İSTEĞE BAĞLI ÇEREZLERİ REDDET';
          });
          if(target) target.click();
        }''')
        page.wait_for_timeout(900)

        # A page error must never be mistaken for an out-of-stock product.
        valid_product = (response is not None and response.status == 200
                         and 'KARELİ REGULAR FIT TAKIM BLAZER' in title.upper())
        if not valid_product:
            raise ValueError('Urun sayfasi dogrulanamadi (HTTP veya baslik).')

        # Entire product sold out: Zara replaces the add button with a
        # 'BENZER ÜRÜNLER / TÜKENDİ' button, so no size menu exists.
        entirely_sold_out = page.evaluate('''() => Array.from(document.querySelectorAll('button'))
           .some(b => {
               const t=(b.innerText||'').toLocaleUpperCase('tr-TR').replace(/\\s+/g,' ').trim();
               return t.includes('BENZER ÜRÜNLER') && t.includes('TÜKENDİ');
           })''')
        if entirely_sold_out:
            report['actions'].append('whole_product_sold_out')
            result = {s: 'out_of_stock' for s in SIZES}
            return result

        add = page.locator('button[data-qa-action="add-to-cart"]').first
        try:
            add.click(timeout=6000)
            report['actions'].append('size_selector_clicked')
        except Exception:
            if add.count():
                add.evaluate('(el) => el.click()')
                report['actions'].append('size_selector_DOM_clicked')
            else:
                raise ValueError('Ne tukendi butonu ne de beden secme butonu bulundu.')
        page.locator('ul.size-selector-sizes > li').first.wait_for(timeout=12000)
        page.wait_for_timeout(600)
        options = page.evaluate('''() => Array.from(document.querySelectorAll('ul.size-selector-sizes > li')).map(li => {
          const b=li.querySelector('button');
          return {text:(li.innerText||'').trim(), className:li.className||'',
                  action:b?.getAttribute('data-qa-action')||'',
                  disabled:!!b?.disabled, ariaDisabled:b?.getAttribute('aria-disabled')||''};
        })''')
        report['sizes_seen'] = options
        for option in options:
            match = re.search(r'\bEU\s*(50|52)\s*\(', option['text'], re.I)
            if not match:
                continue
            size = match.group(1)
            cls = option['className'].lower()
            action = option['action'].lower()
            label = option['text'].lower()
            if ('benzer ürünler' in label or 'unavailable' in cls
                    or '--disabled' in cls or 'out-of-stock' in action
                    or 'out_of_stock' in action):
                result[size] = 'out_of_stock'
            elif (not option['disabled'] and option['ariaDisabled'] != 'true'
                  and 'size' in action and '--disabled' not in cls):
                result[size] = 'in_stock'
        return result
    except Exception as exc:
        report['errors'].append(str(exc)[:500])
        return result
    finally:
        report['result'] = result
        DEBUG_FILE.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding='utf-8')
        page.close()


def main():
    if not TOKEN or not CHAT_ID:
        sys.exit('Telegram ayarlari eksik. baslat.bat ile calistirin ve ayarlar.bat dosyasini kontrol edin.')
    once = '--once' in sys.argv or '--test' in sys.argv
    if '--test' in sys.argv:
        telegram_send('✅ Zara test mesaji: Telegram baglantisi calisiyor.\n' + PRODUCT_URL)
    try:
        state = json.loads(STATE_FILE.read_text(encoding='utf-8')) if STATE_FILE.exists() else {}
    except (ValueError, OSError):
        state = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=(os.getenv('ZARA_HEADED') != '1'), args=['--no-sandbox'])
        try:
            while True:
                now = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
                try:
                    results = check_stock(browser)
                    print(f'[{now}] Stok: {results}', flush=True)
                    for size in SIZES:
                        status = results[size]
                        old = state.get(size)
                        if status == 'in_stock' and old != 'in_stock':
                            telegram_send(f'🚨 ZARA STOKTA!\n{PRODUCT_NAME}\nBEDEN: {size}\n{PRODUCT_URL}')
                        if status != 'unknown':
                            state[size] = status
                    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding='utf-8')
                    if all(v == 'unknown' for v in results.values()):
                        print('HATA: Stok bilgisi okunamadi. stok_teshis.json dosyasini kontrol edin.', flush=True)
                        if once:
                            sys.exit(2)
                except Exception as exc:
                    print(f'[{now}] Kontrol hatasi: {type(exc).__name__}: {exc}', flush=True)
                    if once:
                        sys.exit(2)
                if once:
                    break
                time.sleep(INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print('\nTakip durduruldu.')
        finally:
            browser.close()


if __name__ == '__main__':
    main()
