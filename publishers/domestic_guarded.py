"""Fail-closed browser publishers for unverified domestic platforms.

Douyin is deliberately not implemented here.  These adapters are isolated so
changes cannot alter its validated browser publication path.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

from browser_profile_service import find_chromium
from publish_safety import (
    GUARDED_DOMESTIC_PLATFORMS, PLATFORM_NAMES, PublishGuardError, UPLOAD_URLS,
    guarded_error, map_publish_exception,
)
from .base import Publisher


SELECTORS = {
    "kuaishou": {
        "file": ('input[type="file"][accept*="video"]', 'input[type="file"]'),
        "title": ('input[placeholder*="标题"]', 'textarea[placeholder*="标题"]'),
        "description": ('textarea[placeholder*="描述"]', '[contenteditable="true"]'),
        "cover": ('input[type="file"][accept*="image"]',),
        "submit": ('button:has-text("发布")', 'button:has-text("立即发布")'),
        "result_links": ('a[href*="/short-video/"]',),
        "result_pattern": r"kuaishou\.com/short-video/([0-9A-Za-z_-]+)",
    },
    "xiaohongshu": {
        "file": ('input[type="file"][accept*="video"]', 'input[type="file"]'),
        "title": ('input[placeholder*="标题"]',),
        "description": ('textarea[placeholder*="正文"]', '[contenteditable="true"]'),
        "cover": ('input[type="file"][accept*="image"]',),
        "submit": ('button:has-text("发布")',),
        "result_links": ('a[href*="/explore/"]', 'a[href*="/discovery/item/"]'),
        "result_pattern": r"xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-fA-F]+)",
    },
    "wechat_channels": {
        "file": ('input[type="file"][accept*="video"]', 'input[type="file"]'),
        "title": ('input[placeholder*="标题"]',),
        "description": ('textarea[placeholder*="描述"]', '[contenteditable="true"]'),
        "cover": ('input[type="file"][accept*="image"]',),
        "submit": ('button:has-text("发表")', 'button:has-text("发布")'),
        "result_links": ('a[href*="/finder/"]', 'a[href*="objectId="]'),
        "result_pattern": r"(?:objectId=|/finder/)([0-9A-Za-z_-]+)",
    },
    "bilibili": {
        "file": ('input[type="file"][accept*="video"]', 'input[type="file"]'),
        "title": ('input[placeholder*="标题"]', 'input[name="title"]'),
        "description": ('textarea[placeholder*="简介"]', '[contenteditable="true"]'),
        "cover": ('input[type="file"][accept*="image"]',),
        "submit": ('button:has-text("立即投稿")', 'button:has-text("提交稿件")'),
        "result_links": ('a[href*="/video/BV"]', 'a[href*="bvid="]'),
        "result_pattern": r"(BV[0-9A-Za-z]+)",
    },
}

BLOCKING_TEXT = ("验证码", "安全验证", "请扫码", "手机确认", "拖动滑块", "人机验证")
FAILURE_TEXT = ("上传失败", "发布失败", "投稿失败", "处理失败", "网络异常", "请重试")
SUCCESS_TEXT = ("发布成功", "投稿成功", "已发布", "审核中", "处理中")


class GuardedDomesticBrowserPublisher(Publisher):
    integration_type = "browser"

    def __init__(self, platform_code: str):
        if platform_code not in GUARDED_DOMESTIC_PLATFORMS:
            raise ValueError("GuardedDomesticBrowserPublisher不支持该平台")
        self.platform_code = platform_code

    @staticmethod
    def _visible(page, selectors: tuple[str, ...]):
        for selector in selectors:
            locator = page.locator(selector)
            count = locator.count()
            for index in range(min(count, 12)):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
        return None

    @staticmethod
    def _body_contains(page, texts: tuple[str, ...]) -> str:
        body = page.locator("body").inner_text(timeout=10_000)
        return next((text for text in texts if text in body), "")

    def publish(self, video: str, account: dict, metadata: dict):
        # Normal users cannot reach this adapter until platform capability is
        # passed. Acceptance runs must opt in explicitly.
        if not metadata.get("production_allowed") and not metadata.get("acceptance_test"):
            raise guarded_error("PLATFORM_UNTESTED", f"{PLATFORM_NAMES[self.platform_code]}尚未完成真实发布验收")
        video_path = Path(video)
        profile = Path(str(account.get("browser_profile_path") or ""))
        artifact_dir = Path(str(metadata.get("artifact_dir") or "")) if metadata.get("artifact_dir") else None
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        screenshots: list[str] = []
        evidence: dict[str, str] = {}
        browser_log: list[dict] = []
        spec = SELECTORS[self.platform_code]
        state_callback = metadata.get("state_callback")

        def update_state(state: str) -> None:
            if callable(state_callback):
                state_callback(state)

        def save_artifact(page, step: str) -> None:
            if not artifact_dir:
                return
            safe = re.sub(r"[^0-9A-Za-z_-]+", "_", step).strip("_")
            screenshot = artifact_dir / f"{safe}.png"
            html = artifact_dir / f"{safe}.html"
            state = artifact_dir / f"{safe}.json"
            page.screenshot(path=str(screenshot), full_page=True)
            html.write_text(page.content(), encoding="utf-8")
            state.write_text(json.dumps({"step": step, "url": page.url, "title": page.title(), "time": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2), encoding="utf-8")
            screenshots.append(str(screenshot)); evidence[f"{safe}_html"] = str(html); evidence[f"{safe}_state"] = str(state)

        try:
            with sync_playwright() as playwright:
                try:
                    context = playwright.chromium.launch_persistent_context(
                        str(profile), executable_path=str(find_chromium()), headless=True,
                        viewport={"width": 1280, "height": 820}, args=["--no-first-run", "--disable-background-mode"],
                    )
                except Exception as exc:
                    raise guarded_error("BROWSER_CRASHED", "无法打开独立浏览器环境") from exc
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.on("console", lambda message: browser_log.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "type": message.type, "text": message.text[:500]}))
                    page.goto(UPLOAD_URLS[self.platform_code], wait_until="domcontentloaded", timeout=90_000)
                    save_artifact(page, "01_publish_page")
                    if any(token in page.url.lower() for token in ("login", "passport", "signin")):
                        raise guarded_error("AUTH_EXPIRED")
                    blocker = self._body_contains(page, BLOCKING_TEXT)
                    if blocker:
                        code = "CAPTCHA_REQUIRED" if blocker in {"验证码", "安全验证", "拖动滑块", "人机验证"} else "MANUAL_CONFIRM_REQUIRED"
                        raise guarded_error(code, blocker)
                    file_input = self._visible(page, spec["file"])
                    title_input = self._visible(page, spec["title"])
                    description_input = self._visible(page, spec["description"])
                    submit = self._visible(page, spec["submit"])
                    missing = [label for label, value in (("视频上传", file_input), ("标题", title_input), ("简介", description_input), ("发布", submit)) if value is None]
                    if missing:
                        raise guarded_error("PAGE_CHANGED", "缺少页面控件：" + "、".join(missing))
                    # Account/page confirmation immediately before any upload.
                    if str(account.get("login_status") or "") != "logged_in":
                        raise guarded_error("AUTH_EXPIRED")
                    file_input.set_input_files(str(video_path.resolve()))
                    update_state("uploading")
                    title_input.fill(str(metadata.get("title") or ""))
                    description = str(metadata.get("description") or "")
                    tags = " ".join("#" + str(item).lstrip("#") for item in metadata.get("tags") or [])
                    description_input.fill("\n".join(item for item in (description, tags) if item))
                    cover_path = str(metadata.get("cover_path") or "")
                    if cover_path:
                        cover_input = self._visible(page, spec["cover"])
                        if cover_input is None:
                            raise guarded_error("PAGE_CHANGED", "封面上传控件不存在")
                        cover_input.set_input_files(str(Path(cover_path).resolve()))
                    save_artifact(page, "02_metadata_filled")
                    blocker = self._body_contains(page, BLOCKING_TEXT)
                    if blocker:
                        raise guarded_error("MANUAL_CONFIRM_REQUIRED", blocker)
                    # Bilibili requires category and declaration choices. Do not
                    # guess them or click coordinates.
                    if self.platform_code == "bilibili" and not metadata.get("partition_confirmed"):
                        raise guarded_error("MANUAL_CONFIRM_REQUIRED", "请人工选择投稿分区和投稿声明")
                    if not metadata.get("confirm_submit"):
                        raise guarded_error("MANUAL_CONFIRM_REQUIRED", "发布信息已填写，请人工确认后提交")
                    if submit.is_disabled():
                        raise guarded_error("SUBMIT_FAILED", "发布按钮未启用，可能仍有必填项未完成")
                    update_state("submitting")
                    submit.click()
                    update_state("processing")
                    save_artifact(page, "03_submitted")
                    deadline = time.time() + 600
                    signal = ""
                    work_id = ""; work_url = ""
                    while time.time() < deadline:
                        blocker = self._body_contains(page, BLOCKING_TEXT)
                        if blocker:
                            raise guarded_error("MANUAL_CONFIRM_REQUIRED", blocker)
                        failed = self._body_contains(page, FAILURE_TEXT)
                        if failed:
                            raise guarded_error("PROCESSING_FAILED", failed)
                        signal = self._body_contains(page, SUCCESS_TEXT)
                        if signal:
                            update_state("waiting_confirmation")
                        candidates = [page.url]
                        for selector in spec["result_links"]:
                            links = page.locator(selector)
                            for index in range(min(links.count(), 60)):
                                href = links.nth(index).get_attribute("href")
                                if href:
                                    candidates.append(href)
                        for candidate in candidates:
                            match = re.search(spec["result_pattern"], candidate)
                            if match:
                                work_id, work_url = match.group(1), candidate
                                break
                        if signal and work_id and work_url:
                            break
                        page.wait_for_timeout(1500)
                    if not signal or not work_id or not work_url:
                        raise guarded_error("PROCESSING_FAILED", "平台未返回真实作品ID或后台作品链接")
                    save_artifact(page, "04_platform_confirmed")
                    if artifact_dir:
                        log = artifact_dir / "browser.log.json"
                        log.write_text(json.dumps(browser_log, ensure_ascii=False, indent=2), encoding="utf-8")
                        evidence["browser_log"] = str(log)
                    return {"id": work_id, "url": work_url, "verified": True, "backend_confirmed": True, "signal": signal, "screenshots": screenshots, "artifacts": evidence}
                finally:
                    context.close()
        except PublishGuardError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise map_publish_exception(exc) from exc
        except Exception as exc:
            raise map_publish_exception(exc) from exc
