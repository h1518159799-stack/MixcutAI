"""Read-only, bounded confirmation of a submitted WeChat Channels work.

The same source is installed beside the isolated PostFlow runner, including
when the host application is frozen by PyInstaller.
"""

SOURCE = r'''
import asyncio
import time
from datetime import datetime
from urllib.parse import urlparse

def normalize_receipt(receipt):
    result = dict(receipt)
    explicit_error = result.get('error_code') or result.get('status') in ('failed', 'error')
    submitted = result.get('submitted') is True
    remote_id = str(result.get('remote_id') or '').strip()
    remote_url = str(result.get('remote_url') or '').strip()
    valid_url = urlparse(remote_url).scheme == 'https' and urlparse(remote_url).hostname == 'channels.weixin.qq.com'
    # A unique, title/time matched work row is platform confirmation even when
    # Tencent has not exposed its internal id or permalink yet.
    verified = not explicit_error and result.get('work_matched') is True
    verified = verified or (not explicit_error and result.get('verified') is True and bool(remote_id) and valid_url)
    state = 'failed' if explicit_error else ('published' if verified else ('processing' if submitted else 'waiting_confirmation'))
    result.update(success=verified, submitted=submitted or verified, verified=verified,
                  processing=state == 'processing', status=state,
                  remote_id=remote_id if verified else '', remote_url=remote_url if verified else '',
                  error_code=result.get('error_code') or ('PUBLISH_FAILED' if explicit_error else ''),
                  error_message=result.get('error_message') or '')
    return result

def redirect_result(before_url, after_url, page_title='', body_text=''):
    body = str(body_text or '').lower()
    invalid = (any(x in after_url.lower() for x in ('/login','/captcha','/error')) or
               any(x in body for x in ('发布失败','上传失败','网络异常','暂时无法使用该功能')))
    normal = (str(before_url).rstrip('/').endswith('/platform/post/create') and
              '/platform/' in str(after_url) and '/platform/post/create' not in str(after_url) and not invalid)
    return normalize_receipt({'submitted': True, 'verified': normal, 'work_matched': normal,
        'success': normal, 'status': 'published' if normal else ('failed' if invalid else 'processing'),
        'before_submit_url': before_url, 'after_submit_url': after_url, 'page_title': page_title,
        'confirmation_method': 'post_submit_redirect' if normal else ''})

def match_work(records, title, submitted_at):
    matches = []
    for item in records:
        import re
        clean = lambda value: re.sub(r'\s+', '', str(value or '')).replace('！','!').replace('？','?')
        if clean(item.get('title')) != clean(title):
            continue
        try:
            stamp = item.get('published_at')
            stamp = float(stamp) if isinstance(stamp, (int, float)) else datetime.fromisoformat(str(stamp)).timestamp()
            if stamp > 1e12:
                stamp /= 1000
        except (ValueError, TypeError, OverflowError):
            continue
        if not submitted_at - 120 <= stamp <= submitted_at + 1800:
            continue
        matches.append(item)
    # Same-title duplicate publications are ambiguous; never select the latest
    # merely because it happens to be first in the list.
    if len(matches) != 1:
        return normalize_receipt({'submitted': True})
    item = matches[0]
    return normalize_receipt({**item, 'submitted': True, 'work_matched': True, 'verified': True})

async def inspect_list(page, title, submitted_at):
    records = []
    # Only visible work links with a timestamp in their row qualify.  Generic
    # dashboard links and draft/upload identifiers are never publication IDs.
    links = page.locator('a[href]')
    for index in range(min(await links.count(), 100)):
        link = links.nth(index)
        if not await link.is_visible(): continue
        href = await link.get_attribute('href') or ''
        from urllib.parse import urljoin, parse_qs
        url = urljoin(page.url, href)
        parsed = urlparse(url)
        if parsed.hostname != 'channels.weixin.qq.com' or parsed.path in ('/platform', '/platform/post/list', '/platform/post/create'):
            continue
        query = parse_qs(parsed.query)
        work_id = (query.get('objectId') or query.get('feedId') or query.get('id') or [''])[0]
        row = link.locator('xpath=ancestor::*[self::tr or @role="row"][1]')
        if not await row.count():
            continue
        text = await row.inner_text()
        import re
        stamp = re.search(r'20\d{2}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}(?::\d{2})?', text)
        if not stamp:
            continue
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        matched_title = next((line for line in lines if title.strip() == line or re.sub(r'\s+','',line) == re.sub(r'\s+','',title)), title)
        records.append({'title': matched_title, 'published_at': stamp.group().replace('/', '-'),
                        'remote_id': work_id, 'remote_url': url, 'list_url': page.url,
                        'matched_text': text})
    # The current Channels UI renders cards as nested divs without anchor URLs.
    # A unique visible title plus its nearby publication timestamp is still
    # authoritative platform evidence; ID/URL can be filled by a later read.
    if not records:
        import re
        title_nodes = page.get_by_text(title, exact=True)
        for index in range(await title_nodes.count()):
            node = title_nodes.nth(index)
            if not await node.is_visible(): continue
            container = node
            text = ''
            for _ in range(7):
                container = container.locator('..')
                try: text = await container.inner_text(timeout=500)
                except Exception: continue
                stamp = re.search(r'20\d{2}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}(?::\d{2})?', text)
                if stamp: break
            if stamp:
                records.append({'title': title, 'published_at': stamp.group().replace('/', '-'),
                    'remote_id':'', 'remote_url':'', 'list_url':page.url, 'matched_text':text})
    result = match_work(records, title, submitted_at)
    result.update(list_url=page.url, dom_evidence='\n'.join(item.get('matched_text','') for item in records if item.get('title') == title))
    return result

async def confirm_once(page, title, submitted_at):
    try:
        if '/platform/post/list' not in page.url:
            await page.goto('https://channels.weixin.qq.com/platform/post/list', wait_until='domcontentloaded', timeout=12000)
        if '/platform/post/list' not in page.url:
            await page.get_by_text('内容管理', exact=True).first.click(timeout=3000)
            await page.get_by_role('link', name='视频', exact=True).first.click(timeout=3000)
        await page.wait_for_timeout(1500)
        return await inspect_list(page, title, submitted_at)
    except Exception as exc:
        # A failed read is not evidence that the already submitted work failed.
        return normalize_receipt({'submitted': True, 'confirmation_error': str(exc)[:1000]})
'''

exec(compile(SOURCE, __file__, 'exec'))
