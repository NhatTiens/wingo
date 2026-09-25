#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local selector probe for 88idd. Does not export cookies/passwords/input values."""
from __future__ import annotations
import argparse, json, re
from pathlib import Path

TARGET = "https://www.88idd.com/home/#/lottery?tabName=Lottery&id=77"

JS = r'''
() => {
  const visible = el => {
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.visibility !== 'hidden' && s.display !== 'none' && r.width > 0 && r.height > 0;
  };
  const safe = el => {
    const attrs = {};
    for (const a of Array.from(el.attributes || [])) {
      if (a.name.toLowerCase() === 'value') continue;
      if (a.name.toLowerCase().includes('token')) continue;
      if (a.name.toLowerCase().includes('password')) continue;
      if (a.name === 'style') continue;
      attrs[a.name] = String(a.value).slice(0, 240);
    }
    const r = el.getBoundingClientRect();
    return {
      tag: el.tagName.toLowerCase(),
      text: (el.innerText || '').trim().replace(/\s+/g,' ').slice(0,180),
      placeholder: (el.getAttribute('placeholder') || '').slice(0,180),
      aria_label: (el.getAttribute('aria-label') || '').slice(0,180),
      type: (el.getAttribute('type') || '').slice(0,80),
      id: (el.id || '').slice(0,180),
      name: (el.getAttribute('name') || '').slice(0,180),
      classes: (el.className && typeof el.className === 'string' ? el.className : '').slice(0,300),
      attrs,
      box: {x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)}
    };
  };
  const controls = Array.from(document.querySelectorAll('button,input,[role="button"],select,textarea')).filter(visible).map(safe);
  const texts = Array.from(document.querySelectorAll('body *')).filter(el => visible(el) && el.children.length === 0)
    .map(el => ({tag:el.tagName.toLowerCase(), text:(el.innerText||'').trim().replace(/\s+/g,' ').slice(0,200), id:(el.id||'').slice(0,100), classes:(typeof el.className==='string'?el.className:'').slice(0,180)}))
    .filter(x => x.text && /(số dư|balance|đặt cược|xác nhận|\b0\b|\b1\b|\b2\b|\b3\b|\b4\b|\b5\b|\b6\b|\b7\b|\b8\b|\b9\b|₫|vnd)/i.test(x.text))
    .slice(0,400);
  return {url: location.href, title: document.title, controls, text_candidates:texts};
}
'''

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--auth-state',default='auth_state.json')
    ap.add_argument('--url',default=TARGET)
    ap.add_argument('--headless',action='store_true')
    ap.add_argument('--out',default='88i_selector_probe.json')
    ap.add_argument('--screenshot',default='88i_lottery_probe.png')
    args=ap.parse_args()
    auth=Path(args.auth_state)
    if not auth.exists():
        raise SystemExit(f"Thiếu {auth}. Hãy tạo session login trước.")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=args.headless)
        ctx=browser.new_context(storage_state=str(auth))
        page=ctx.new_page(); page.goto(args.url,wait_until='domcontentloaded',timeout=60000); page.wait_for_timeout(2500)
        data=page.evaluate(JS)
        Path(args.out).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        page.screenshot(path=args.screenshot,full_page=True)
        print(f"Đã tạo {args.out} và {args.screenshot}")
        print("Probe KHÔNG chứa cookies, password hay giá trị input. Hãy xem JSON để lấy selector ổn định.")
        browser.close()
if __name__=='__main__': main()
