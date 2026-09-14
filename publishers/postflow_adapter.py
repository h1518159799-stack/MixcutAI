"""Isolated PostFlow proof-of-concept adapter.

It provides a subprocess boundary so the third-party PostFlow runtime cannot
write MixcutAI state directly.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any
from .postflow_confirmation import SOURCE as CONFIRMATION_SOURCE, normalize_receipt

CHROME_CANDIDATES = (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")
LAST_CHROME_SOURCE = ''
def find_system_chrome(configured: str | None = None) -> str:
    global LAST_CHROME_SOURCE
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]
    embedded = [app_dir / 'runtime' / 'browser' / 'chromium' / 'chrome.exe', app_dir / 'runtime' / 'chrome' / 'chrome.exe']
    # Playwright's bundled browser may live under the PostFlow runtime.
    pw_root = app_dir / 'runtime' / 'postflow' / 'ms-playwright'
    embedded += list(pw_root.glob('chromium-*/chrome-win/chrome.exe'))
    embedded += [app_dir / 'browser' / 'chromium' / 'chrome.exe']
    paths = [str(p) for p in embedded] + [configured, *CHROME_CANDIDATES, os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "Application", "chrome.exe")]
    checked = [str(Path(p).expanduser()) for p in paths if p]
    for index, path in enumerate(checked):
        if Path(path).is_file():
            LAST_CHROME_SOURCE = 'embedded_chromium' if index == 0 else ('playwright_bundled' if index < len(embedded) else 'system')
            os.environ['BROWSER_SOURCE'] = LAST_CHROME_SOURCE
            os.environ['BROWSER_PATH'] = path
            return path
    LAST_CHROME_SOURCE = ''
    os.environ['BROWSER_SOURCE'] = ''
    os.environ['BROWSER_PATH'] = ''
    return ''

def resolve_postflow_root(configured=None) -> Path:
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent / 'runtime' / 'postflow'
    bundled = Path(__file__).resolve().parents[1] / 'runtime' / 'postflow'
    if bundled.is_dir():
        return bundled
    return Path(configured or os.environ.get('MIXCUT_POSTFLOW_ROOT') or bundled).expanduser().resolve()

def resolve_postflow_python(postflow_root: str | os.PathLike[str], configured: str | None = None) -> str:
    """Resolve a real Python interpreter, never the frozen MixcutAI EXE."""
    root = Path(postflow_root).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        candidate = resolve_postflow_root() / 'python' / 'python.exe'
        if candidate.is_file():
            return str(candidate)
        raise FileNotFoundError('POSTFLOW_PYTHON_NOT_FOUND: checked paths: ' + str(candidate))
    explicit = configured or os.environ.get("MIXCUT_POSTFLOW_PYTHON")
    candidates = ([explicit] if explicit else []) + [str(root / "python" / "python.exe")] + ([str(root / name / "Scripts" / "python.exe") for name in (".venv", "venv", "postflow_venv")] if not getattr(sys, 'frozen', False) else [])
    is_frozen = bool(getattr(sys, "frozen", False))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    if not is_frozen and not explicit:
        return sys.executable
    checked = "; ".join(str(Path(p).resolve()) for p in candidates if p)
    raise FileNotFoundError("POSTFLOW_PYTHON_NOT_FOUND: checked paths: " + checked)


def bridge_profile_to_storage_state(
    browser_profile_path: str | os.PathLike[str],
    output_dir: str | os.PathLike[str],
    *,
    browser_executable: str | None = None,
) -> Path:
    """Export a persistent Chromium profile to an isolated Playwright state.

    The source profile is opened read-only from the PoC's perspective: no
    files are copied over it and the exported JSON is written per account.
    The caller must ensure that no other browser currently locks the profile.
    """
    source = Path(browser_profile_path).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"浏览器 Profile 不存在: {source}")
    target_dir = Path(output_dir).expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "storage_state.json"
    from playwright.sync_api import sync_playwright

    chrome = find_system_chrome(browser_executable)
    with sync_playwright() as playwright:
        launch_kwargs = dict(headless=True, args=["--no-first-run", "--disable-background-mode"])
        if chrome:
            launch_kwargs["executable_path"] = chrome
        context = playwright.chromium.launch_persistent_context(str(source), **launch_kwargs)
        try:
            context.storage_state(path=str(target))
        finally:
            context.close()
    if not target.is_file() or target.stat().st_size == 0:
        raise RuntimeError("Profile 转换未生成有效 storage_state.json")
    return target


class WechatChannelsPostFlowAdapter:
    platform_code = "wechat_channels"
    integration_type = "browser_external_poc"

    def __init__(self, postflow_root: str | os.PathLike[str], *, python_executable: str | None = None):
        self.postflow_root = resolve_postflow_root(postflow_root) if getattr(sys, 'frozen', False) else Path(postflow_root).expanduser().resolve()
        self.python_executable = resolve_postflow_python(self.postflow_root, python_executable)
        if not (self.postflow_root / "uploader" / "tencent_uploader" / "main.py").is_file():
            raise FileNotFoundError("PostFlow tencent_uploader/main.py 不存在")

    def publish(
        self,
        video_path: str,
        title: str,
        description: str = "",
        cover: str = "",
        account: dict[str, Any] | None = None,
        *,
        execute_submit: bool = False,
        artifact_dir: str | os.PathLike[str] | None = None,
    ) -> dict[str, Any]:
        """Run the isolated PoC. ``execute_submit`` is explicit and defaults off."""
        account = account or {}
        result: dict[str, Any] = {
            "success": False, "remote_id": "", "remote_url": "",
            "verified": False, "error_code": "", "error_message": "",
            "screenshot": "", "traceback": "",
        }
        if not Path(video_path).is_file():
            result.update(error_code="FILE_NOT_FOUND", error_message=f"视频不存在: {video_path}")
            return result
        work_dir = Path(artifact_dir or tempfile.mkdtemp(prefix="mixcut-postflow-poc-"))
        work_dir.mkdir(parents=True, exist_ok=True)
        account_file = account.get("postflow_account_file") or account.get("account_file")
        if not account_file and account.get("browser_profile_path"):
            try:
                account_file = str(bridge_profile_to_storage_state(account["browser_profile_path"], work_dir))
            except Exception as exc:
                result.update(error_code="PROFILE_BRIDGE_FAILED", error_message=str(exc), traceback=traceback.format_exc())
                return result
        if not account_file:
            result.update(error_code="ACCOUNT_FILE_REQUIRED", error_message="PoC 需要 browser_profile_path 或 PostFlow storage_state account_file")
            return result
        runner = work_dir / "run_wechat_poc.py"
        (work_dir / "postflow_confirmation.py").write_text(CONFIRMATION_SOURCE, encoding="utf-8")
        runner.write_text(_RUNNER, encoding="utf-8")
        payload = {
            "postflow_root": str(self.postflow_root),
            "video_path": str(Path(video_path).resolve()),
            "title": str(title or ""),
            "description": str(description or ""),
            "cover": str(cover or ""),
            "account_file": str(Path(account_file).expanduser().resolve()),
            "artifact_dir": str(work_dir.resolve()),
            "execute_submit": bool(execute_submit),
            "chrome_path": find_system_chrome(),
            "chrome_source": LAST_CHROME_SOURCE,
            "browser_mode": os.environ.get("PUBLISH_BROWSER_MODE", "background").strip().lower() or "background",
        }
        job_file = work_dir / "postflow_job.json"
        job_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            completed = subprocess.run(
                [self.python_executable, str(runner), "--job", str(job_file)],
                text=True, capture_output=True, cwd=str(self.postflow_root), timeout=900,
                encoding="utf-8", errors="replace",
                env={**{k:v for k,v in os.environ.items() if k not in ('PYTHONHOME','PYTHONPATH')}, "PYTHONNOUSERSITE": "1", "POSTFLOW_DEBUG_SHOTS": "1", "POSTFLOW_SHOT_TAG": "mixcut-poc",
                     "PUBLISH_BROWSER_MODE": os.environ.get("PUBLISH_BROWSER_MODE", "background")},
            )
            lines = [line for line in completed.stdout.splitlines() if line.strip()]
            if lines:
                result.update(json.loads(lines[-1]))
            if completed.stderr:
                result["stderr"] = completed.stderr[-4000:]
            if completed.returncode and not result.get("error_code"):
                result.update(error_code="POSTFLOW_PROCESS_FAILED", error_message=completed.stderr[-2000:])
        except Exception as exc:
            result.update(error_code=type(exc).__name__, error_message=str(exc), traceback=traceback.format_exc())
        result['confirmation_job'] = str(job_file.resolve())
        result['postflow_python_path'] = self.python_executable
        result['postflow_runner_path'] = str(runner.resolve())
        result['postflow_root'] = str(self.postflow_root)
        result['is_frozen'] = bool(getattr(sys, 'frozen', False))
        result['sys_executable'] = sys.executable
        return result


class WechatChannelsPostFlowPublisher:
    """Production-contract wrapper for the video-channel provider."""
    platform_code = "wechat_channels"
    integration_type = "browser"

    def confirm(self, evidence: dict) -> dict:
        """Recheck the persisted submission; never call upload or publish."""
        import asyncio
        from .postflow_confirmation import confirm_once
        job = json.loads(Path(evidence['confirmation_job']).read_text(encoding='utf-8'))
        async def check():
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                launch_kwargs = dict(headless=True)
                if job.get('chrome_path'):
                    launch_kwargs['executable_path'] = job['chrome_path']
                browser = await pw.chromium.launch(**launch_kwargs)
                try:
                    context = await browser.new_context(storage_state=job['account_file'])
                    page = await context.new_page()
                    return await confirm_once(page, job['title'], float(evidence['submitted_at']))
                finally:
                    await browser.close()
        try:
            result = asyncio.run(asyncio.wait_for(check(), timeout=25))
            return {**evidence, **result, 'id': result['remote_id'], 'url': result['remote_url'],
                    'backend_confirmed': result['verified']}
        except Exception as exc:
            return {**evidence, 'confirmation_error': str(exc)[:1000]}

    def __init__(self, postflow_root: str | os.PathLike[str] | None = None, *, python_executable: str | None = None):
        # Packaged clients resolve PostFlow beside the executable; env remains an override for development.
        root = resolve_postflow_root(postflow_root)
        if not getattr(sys, 'frozen', False) and not root.is_dir():
            root = None
        # Defer configuration/ filesystem validation until publish so importing
        # the publisher catalog remains safe in setup and diagnostic commands.
        self._postflow_root = root
        self._python_executable = python_executable
        self.adapter = (WechatChannelsPostFlowAdapter(root, python_executable=python_executable)
                        if root else None)

    def publish(self, video: str, account: dict, metadata: dict) -> dict[str, Any]:
        from publish_safety import PublishGuardError
        if self.adapter is None:
            raise PublishGuardError("POSTFLOW_NOT_CONFIGURED", "未配置 MIXCUT_POSTFLOW_ROOT")
        result = self.adapter.publish(
            video, str(metadata.get("title") or ""), str(metadata.get("description") or ""),
            str(metadata.get("cover_path") or ""), account,
            execute_submit=True, artifact_dir=metadata.get("artifact_dir"),
        )
        result = normalize_receipt(result)
        if result['status'] in ('processing', 'waiting_confirmation'):
            return {**result, 'id': '', 'url': '', 'backend_confirmed': False}
        if result.get('success') and result.get('verified') and result.get('status') == 'published':
            return {**result, 'id': result.get('remote_id', ''), 'url': result.get('remote_url', ''),
                    'backend_confirmed': True}
        if not (result.get("success") and result.get("verified") and result.get("remote_id") and result.get("remote_url")):
            evidence = {key: result.get(key) for key in ("screenshot", "traceback", "draftId", "clipKey", "remote_id", "remote_url")}
            raise PublishGuardError(
                str(result.get("error_code") or "PUBLISH_FAILED"),
                str(result.get("error_message") or "PostFlow未返回可验证作品凭证") + " | " + json.dumps(evidence, ensure_ascii=False),
            )
        return {**result, "id": result["remote_id"], "url": result["remote_url"], "verified": True,
                "backend_confirmed": True, "screenshot": result.get("screenshot", ""),
                "traceback": result.get("traceback", ""), "draftId": result.get("draftId", ""),
                "clipKey": result.get("clipKey", "")}


_RUNNER = r'''
import argparse, asyncio, inspect, json, sys, traceback, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from postflow_confirmation import normalize_receipt, confirm_once, redirect_result

async def safe_page_text(page):
    """Collect body text across frames without Playwright strict-mode errors."""
    texts = []
    roots = [page] + list(page.frames)
    for root in roots:
        try:
            values = await root.locator('body').all_inner_texts()
            texts.extend(v for v in values if v)
        except Exception:
            continue
    return '\n'.join(texts)

parser = argparse.ArgumentParser()
parser.add_argument("--job", required=True)
args = parser.parse_args()
payload = json.loads(Path(args.job).read_text(encoding="utf-8"))
sys.path.insert(0, payload["postflow_root"])
from uploader.tencent_uploader.main import TencentVideo

async def main():
    if payload.get("browser_mode", "background") == "background":
        from playwright.async_api import BrowserType
        _launch = BrowserType.launch
        async def _background_launch(self, **kwargs):
            args = list(kwargs.get("args") or [])
            args += ["--window-position=-32000,-32000", "--window-size=1280,900",
                     "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
                     "--disable-renderer-backgrounding"]
            kwargs["args"] = list(dict.fromkeys(args))
            return await _launch(self, **kwargs)
        BrowserType.launch = _background_launch
    app = TencentVideo(
        payload["title"], payload["video_path"], [], 0,
        payload["account_file"], cover_path=payload.get("cover") or None,
        require_cover=False,
    )
    # The PoC keeps its own browser so the result contract can be enforced.
    # It never reports success merely because navigation occurred.
    if not payload.get("execute_submit"):
        from playwright.async_api import async_playwright
        
        async with async_playwright() as pw:
            launch_kwargs = dict(headless=False, args=[] if payload.get("browser_mode") == "visible" else ["--window-position=-32000,-32000", "--window-size=1280,900", "--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"])
            if payload.get("chrome_path"): launch_kwargs["executable_path"] = payload["chrome_path"]
            browser = await pw.chromium.launch(**launch_kwargs)
            context = await browser.new_context(storage_state=payload["account_file"])
            page = await context.new_page()
            await page.goto("https://channels.weixin.qq.com/platform/post/create", wait_until="domcontentloaded")
            await page.wait_for_selector('input[type="file"]', state="attached", timeout=30000)
            await page.locator('input[type="file"]').first.set_input_files(payload["video_path"])
            await page.locator("div.input-editor").click()
            await page.keyboard.type(payload["description"] or payload["title"])
            short_title = page.get_by_text("短标题", exact=True).locator("..").locator("xpath=following-sibling::div").locator('input[type="text"]')
            if await short_title.count():
                await short_title.first.fill(payload["title"][:16])
            screenshot = Path(payload["artifact_dir"]) / "postflow_pre_submit.png"
            await page.screenshot(path=str(screenshot), full_page=True)
            print(json.dumps({"success":True,"verified":False,"error_code":"SUBMIT_NOT_EXECUTED","error_message":"已完成打开页面、复用登录态、上传和填写；未点击发表","screenshot":str(screenshot)}, ensure_ascii=False))
            await context.close(); await browser.close()
        return
    submitted_at = time.time()
    before_submit_url = ''
    confirmation = {}
    original_publish = app.click_publish
    async def publish_then_confirm(page):
        # Invoke the existing submission method unchanged, then observe its
        # result page before PostFlow closes the context.
        nonlocal before_submit_url
        before_submit_url = page.url
        await original_publish(page)
        confirmation.update(submitted=True, submitted_at=submitted_at)
        after_url = page.url
        confirmation.update(before_submit_url=before_submit_url, after_submit_url=after_url,
                            page_title=await page.title(), redirect_at=time.time(),
                            confirmation_method='post_submit_redirect')
        try:
            body_text = await safe_page_text(page)
            confirmation['confirmation_text_unavailable'] = not bool(body_text)
        except Exception:
            body_text = ''
            confirmation['confirmation_text_unavailable'] = True
        redirect = redirect_result(before_submit_url, after_url, confirmation['page_title'], body_text)
        confirmation.update(redirect)
        try:
            confirmation.update(await asyncio.wait_for(confirm_once(page, payload['title'], submitted_at), timeout=20))
        except Exception as exc:
            confirmation.update(normalize_receipt({'submitted': True, 'confirmation_error': str(exc)[:1000]}))
        screenshot = Path(payload['artifact_dir']) / 'postflow_confirmation.png'
        try:
            await page.screenshot(path=str(screenshot), full_page=True, timeout=3000)
            confirmation['screenshot'] = str(screenshot)
        except Exception:
            pass
    app.click_publish = publish_then_confirm
    returned = app.main()
    if inspect.isawaitable(returned):
        returned = await returned

    # PostFlow revisions have exposed the publication receipt in different
    # places. Normalize all known forms into MixcutAI's strict contract.
    candidates = [returned, getattr(app, "result", None), getattr(app, "publish_result", None), app]
    receipt = {}
    # Include one level of common ``data/result`` envelopes used by PostFlow
    # integrations without trusting arbitrary navigation state as success.
    expanded = []
    for candidate in candidates:
        expanded.append(candidate)
        if isinstance(candidate, dict):
            expanded.extend(candidate.get(key) for key in ("data", "result", "receipt") if candidate.get(key) is not None)
    for candidate in expanded:
        if isinstance(candidate, dict):
            receipt.update(candidate)
        else:
            for key in ("remote_id", "remote_url", "remoteId", "remoteUrl", "id", "url", "video_id", "work_id", "post_id",
                        "share_url", "work_url", "draftId", "clipKey", "screenshot"):
                value = getattr(candidate, key, None)
                if value not in (None, ""):
                    receipt.setdefault(key, value)
    remote_id = (receipt.get("remote_id") or receipt.get("remoteId") or receipt.get("id") or receipt.get("video_id") or
                 receipt.get("work_id") or receipt.get("post_id"))
    remote_url = (receipt.get("remote_url") or receipt.get("remoteUrl") or receipt.get("url") or receipt.get("share_url") or
                  receipt.get("work_url"))
    final = {**receipt, **confirmation}
    if remote_id and remote_url and receipt.get('verified') is True:
        final.update(remote_id=str(remote_id), remote_url=str(remote_url), verified=True)
    if receipt.get('error_code') or receipt.get('status') in ('failed', 'error'):
        final.update(error_code=receipt.get('error_code') or 'PUBLISH_FAILED',
                     error_message=receipt.get('error_message') or '', status='failed')
    print(json.dumps(normalize_receipt(final), ensure_ascii=False))

try:
    asyncio.run(main())
except Exception as exc:
    print(json.dumps({"success":False,"error_code":type(exc).__name__,"error_message":str(exc),"traceback":traceback.format_exc()}, ensure_ascii=False))
    raise
'''


