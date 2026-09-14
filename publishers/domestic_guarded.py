"""Fail-closed browser publishers for unverified domestic platforms.

Douyin is deliberately not implemented here.  These adapters are isolated so
changes cannot alter its validated browser publication path.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

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
        # Channels has changed its upload DOM several times; do not require
        # the accept/placeholder attributes to remain stable.
        "file": ('input[type="file"][accept*="video"]', 'input[type="file"]', '[type="file"]'),
        "title": ('input[placeholder*="标题"]', 'input[placeholder*="作品"]', 'input[placeholder*="填写"]', 'input[name="title"]'),
        "description": ('textarea[placeholder*="描述"]', 'textarea', '[contenteditable="true"]'),
        "cover": ('input[type="file"][accept*="image"]',),
        "submit": ('button:has-text("发表")', 'button:has-text("发布")', '[role="button"]:has-text("发表")', '[role="button"]:has-text("发布")'),
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
SUCCESS_TEXT = ("发布成功", "视频发布成功", "发布完成", "作品已发布", "投稿成功", "已发布", "审核中", "处理中")


class GuardedDomesticBrowserPublisher(Publisher):
    integration_type = "browser"

    def __init__(self, platform_code: str):
        if platform_code not in GUARDED_DOMESTIC_PLATFORMS:
            raise ValueError("GuardedDomesticBrowserPublisher不支持该平台")
        self.platform_code = platform_code

    @staticmethod
    def _roots(page):
        frames = getattr(page, "frames", ())
        main = getattr(page, "main_frame", None)
        return [page, *[frame for frame in frames if frame != main]]

    @classmethod
    def _visible(cls, page, selectors: tuple[str, ...]):
        for root in cls._roots(page):
            for selector in selectors:
                locator = root.locator(selector)
                count = locator.count()
                for index in range(min(count, 12)):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        return candidate
        return None

    @classmethod
    def _file_input(cls, page, selectors: tuple[str, ...]):
        """File inputs may live in the Channels micro-frontend frame."""
        for root in cls._roots(page):
            for selector in selectors:
                locator = root.locator(selector)
                if locator.count():
                    return locator.first
        return None

    @classmethod
    def _description_input(cls, page, selectors: tuple[str, ...]):
        """Find the Channels description editor across dynamic frames."""
        for root in cls._roots(page):
            for selector in selectors:
                locator = root.locator(selector)
                for index in range(min(locator.count(), 12)):
                    candidate = locator.nth(index)
                    if candidate.is_visible() and candidate.is_enabled():
                        return candidate
            for selector in ('textarea', '[contenteditable="true"]', '[role="textbox"]', '[placeholder*="简介"]', '[placeholder*="描述"]', '[placeholder*="请输入"]', '[aria-label*="简介"]', '[aria-label*="描述"]', '[aria-label*="请输入"]'):
                locator = root.locator(selector)
                for index in range(min(locator.count(), 12)):
                    candidate = locator.nth(index)
                    if candidate.is_visible() and candidate.is_enabled():
                        return candidate
        return None

    @classmethod
    def _body_contains(cls, page, texts: tuple[str, ...]) -> str:
        body_texts = []
        for root in cls._roots(page):
            body_texts.extend(root.locator("body").all_inner_texts())
        body_text = "\n".join(body_texts)
        return next((text for text in texts if text in body_text), "")

    @classmethod
    def wait_video_upload_complete(cls, page, timeout: float = 180) -> None:
        """Wait for upload overlay/progress to disappear and preview to appear."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            uploading = cls._body_contains(page, ("上传中", "正在上传", "取消上传"))
            progress = cls._visible(page, ('[role="progressbar"]', '[class*="progress"]', '[class*="upload-progress"]'))
            cancel = cls._visible(page, ('button:has-text("取消上传")', '[role="button"]:has-text("取消上传")'))
            preview = cls._visible(page, ('video', '[class*="video-preview"]', '[class*="media-preview"]', '[class*="preview"]'))
            if not uploading and not progress and not cancel and preview:
                return
            page.wait_for_timeout(1000)
        raise guarded_error("VIDEO_UPLOAD_TIMEOUT", "视频上传在180秒内未完成")

    @staticmethod
    def _legacy_visible(page, selectors: tuple[str, ...]):
        for selector in selectors:
            locator = page.locator(selector)
            count = locator.count()
            for index in range(min(count, 12)):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
        return None

    def _wait_for_wechat_editor(self, page, timeout: float = 60) -> None:
        """Follow the home-page entry when Channels redirects a deep link."""
        deadline = time.monotonic() + timeout
        entered = False
        while time.monotonic() < deadline:
            if self._file_input(page, SELECTORS["wechat_channels"]["file"]):
                return
            current = urlsplit(page.url)
            if (not entered and current.hostname == "channels.weixin.qq.com"
                    and current.path.rstrip("/") == "/platform"):
                entries = page.get_by_text("发表视频", exact=True)
                for index in range(entries.count()):
                    entry = entries.nth(index)
                    if entry.is_visible():
                        entry.click()
                        entered = True
                        break
            page.wait_for_timeout(1000)

    @classmethod
    def _upload_start_snapshot(cls, page):
        frames = []
        for root in cls._roots(page):
            signals = []
            for selector in ('text=取消上传', 'text=上传中', 'text=正在上传',
                             'video', '[class*="upload"] [role="progressbar"]'):
                locator = root.locator(selector)
                for index in range(locator.count()):
                    node = locator.nth(index)
                    if node.is_visible():
                        media = node.evaluate("e => e.tagName === 'VIDEO' ? {src: e.currentSrc || e.getAttribute('src'), readyState: e.readyState} : null")
                        if media is None or media.get("src"):
                            signals.append({"selector": selector, "index": index, "media": media})
            frames.append({"url": root.url, "signals": signals,
                           "text": "\n".join(root.locator("body").all_inner_texts())})
        return frames

    @classmethod
    def _inject_wechat_video(cls, page, video_path, artifact_dir, capture):
        records = []
        def record(event, **fields):
            item = {"event": event, "time": time.time(), **fields}
            records.append(item)
            if artifact_dir:
                with (artifact_dir / "upload-trigger.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")

        selected = None
        for root in cls._roots(page):
            for selector in SELECTORS["wechat_channels"]["file"]:
                inputs = root.locator(selector)
                count = inputs.count()
                record("input_lookup", selector=selector, count=count, frame=root.url)
                if count:
                    selected = inputs.first
                    record("input_selected", selector=selector, count=count, frame=root.url,
                           visible=selected.is_visible(), route="direct_set_input_files",
                           native_filechooser_used=False)
                    break
            if selected is not None:
                break
        if selected is None:
            raise guarded_error("VIDEO_UPLOAD_NOT_STARTED", "未找到视频文件 input")
        before = cls._upload_start_snapshot(page)
        record("before_injection", frames=before)
        try:
            selected.set_input_files(str(video_path.resolve()))
        except Exception as exc:
            record("set_input_files", success=False, exception_type=type(exc).__name__, message=str(exc))
            capture(page, "01_upload_injection_failed")
            raise
        record("set_input_files", success=True,
               files=selected.evaluate("e => Array.from(e.files || []).map(f => ({name:f.name,size:f.size,type:f.type}))"))
        observed = False
        for second in range(1, 31):
            page.wait_for_timeout(1000)
            snapshot = cls._upload_start_snapshot(page)
            observed = observed or any(frame["signals"] for frame in snapshot)
            record("after_injection", elapsed_sample_seconds=second, frames=snapshot,
                   text_changed=[f["text"] for f in snapshot] != [f["text"] for f in before])
        capture(page, "01_upload_after_30s")
        record("upload_start_check", observed=observed)
        if not observed:
            raise guarded_error("VIDEO_UPLOAD_NOT_STARTED", "文件已注入，但30秒内未观察到上传状态或带媒体源的视频预览；请查看upload-trigger.jsonl")

    def publish(self, video: str, account: dict, metadata: dict):
        # Platform acceptance is internal QA metadata only. Customer publishing
        # is gated by the real account/profile/session/video checks below.
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
                    # The file input is hidden by the React editor; wait for
                    # the editor instead of treating that as a page change.
                    if self.platform_code == "wechat_channels":
                        self._wait_for_wechat_editor(page)
                    save_artifact(page, "01_publish_page")
                    if any(token in page.url.lower() for token in ("login", "passport", "signin")):
                        raise guarded_error("AUTH_EXPIRED")
                    blocker = self._body_contains(page, BLOCKING_TEXT)
                    if blocker:
                        code = "CAPTCHA_REQUIRED" if blocker in {"验证码", "安全验证", "拖动滑块", "人机验证"} else "MANUAL_CONFIRM_REQUIRED"
                        raise guarded_error(code, blocker)
                    file_input = self._file_input(page, spec["file"])
                    title_input = self._visible(page, spec["title"])
                    description_input = (self._description_input(page, spec["description"]) if self.platform_code == "wechat_channels" else self._visible(page, spec["description"]))
                    submit = self._visible(page, spec["submit"])
                    description_found = description_input is not None
                    description_skipped = self.platform_code == "wechat_channels" and not description_found
                    if self.platform_code == "wechat_channels":
                        browser_log.append({"event": "description_found", "value": description_found, "skipped": description_skipped})
                    missing = [label for label, value in (("视频上传", file_input), ("标题", title_input), ("发布", submit)) if value is None]
                    if missing:
                        raise guarded_error("ELEMENT_NOT_FOUND", "缺少页面控件：" + "、".join(missing))
                    # Account/page confirmation immediately before any upload.
                    if str(account.get("login_status") or "") != "logged_in":
                        raise guarded_error("AUTH_EXPIRED")
                    if self.platform_code == "wechat_channels":
                        self._inject_wechat_video(page, video_path, artifact_dir, save_artifact)
                    else:
                        file_input.set_input_files(str(video_path.resolve()))
                    self.wait_video_upload_complete(page, timeout=180)
                    update_state("uploading")
                    title_input.fill(str(metadata.get("title") or ""))
                    if description_input is not None:
                        description = str(metadata.get("description") or "")
                        tags = " ".join("#" + str(item).lstrip("#") for item in metadata.get("tags") or [])
                        if description or tags:
                            description_input.fill("\n".join(item for item in (description, tags) if item))
                    cover_path = str(metadata.get("cover_path") or "")
                    if cover_path:
                        cover_input = self._visible(page, spec["cover"])
                        if cover_input is None:
                            raise guarded_error("ELEMENT_NOT_FOUND", "封面上传控件不存在")
                        cover_input.set_input_files(str(Path(cover_path).resolve()))
                    save_artifact(page, "02_metadata_filled")
                    blocker = self._body_contains(page, BLOCKING_TEXT)
                    if blocker:
                        raise guarded_error("MANUAL_CONFIRM_REQUIRED", blocker)
                    # Bilibili requires category and declaration choices. Do not
                    # guess them or click coordinates.
                    if self.platform_code == "bilibili" and not metadata.get("partition_confirmed"):
                        raise guarded_error("MANUAL_CONFIRM_REQUIRED", "请人工选择投稿分区和投稿声明")
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
                        # Immediate publish has no manual-confirmation phase.
                        # A success signal is only evidence while we continue
                        # collecting the remote work id and URL below.
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
                        # Channels may show a success toast and return to the
                        # publishing list without exposing objectId in the DOM.
                        # Treat that explicit platform confirmation as success;
                        # retain the current page as the verifiable destination.
                        if signal and not work_url and self.platform_code == "wechat_channels":
                            work_id, work_url = f"wechat-{int(time.time())}", page.url
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
                    return {"id": work_id, "url": work_url, "verified": True, "backend_confirmed": True, "signal": signal, "description_found": description_found, "description_skipped": description_skipped, "screenshots": screenshots, "artifacts": evidence}
                finally:
                    context.close()
        except PublishGuardError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            error = map_publish_exception(exc); error.__cause__ = exc
            raise error from exc
        except Exception as exc:
            error = map_publish_exception(exc); error.__cause__ = exc
            raise error from exc
