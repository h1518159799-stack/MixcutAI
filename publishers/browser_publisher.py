from __future__ import annotations

from pathlib import Path
import time
import re
import json

from playwright.sync_api import sync_playwright

from browser_profile_service import find_chromium
from .base import Publisher


UPLOAD_URLS = {
    "douyin": "https://creator.douyin.com/creator-micro/content/upload",
    "xiaohongshu": "https://creator.xiaohongshu.com/publish/publish",
    "kuaishou": "https://cp.kuaishou.com/article/publish/video",
    "tiktok": "https://www.tiktok.com/tiktokstudio/upload",
}

SUCCESS_TEXTS = {
    "douyin": ("发布成功", "作品发布成功", "发布完成"),
    "xiaohongshu": ("发布成功", "笔记发布成功", "审核中"),
    "kuaishou": ("发布成功", "作品发布成功", "审核中"),
    "tiktok": ("Your video has been uploaded", "Post published", "Your post is being processed"),
}

ERROR_TEXTS = ("发布失败", "上传失败", "网络异常", "请重试", "Upload failed", "Couldn't upload")


class BrowserPublisher(Publisher):
    integration_type = "browser"

    def __init__(self, platform_code: str = "browser"):
        self.platform_code = platform_code

    def publish(self, video: str, account: dict, metadata: dict):
        video_path = Path(video)
        if not video_path.is_file():
            raise ValueError("待发布视频不存在")
        profile = Path(account.get("browser_profile_path") or "")
        profile.mkdir(parents=True, exist_ok=True)
        upload_url = UPLOAD_URLS.get(self.platform_code)
        if not upload_url:
            raise ValueError(f"{self.platform_code} 尚未配置上传页面")
        title = str(metadata.get("title") or "").strip()
        description = str(metadata.get("description") or "").strip()
        tags = " ".join("#" + str(tag).lstrip("#") for tag in metadata.get("tags") or [])
        description_text = "\n".join(item for item in (description, tags) if item).strip()
        artifact_value = str(metadata.get("artifact_dir") or "").strip()
        artifact_dir = Path(artifact_value) if artifact_value else None
        if artifact_dir:
            artifact_dir.mkdir(parents=True, exist_ok=True)
        screenshots: list[str] = []
        evidence: dict[str, str] = {}
        browser_log: list[dict] = []

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile), executable_path=str(find_chromium()), headless=True,
                viewport={"width": 1280, "height": 820}, args=["--no-first-run"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.on("console", lambda message: browser_log.append({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "type": message.type,
                    "text": message.text,
                }))

                def capture(step: str) -> None:
                    if not artifact_dir:
                        return
                    safe_step = re.sub(r"[^0-9A-Za-z_-]+", "_", step).strip("_") or "step"
                    image_path = artifact_dir / f"{safe_step}.png"
                    html_path = artifact_dir / f"{safe_step}.html"
                    state_path = artifact_dir / f"{safe_step}.json"
                    page.screenshot(path=str(image_path), full_page=True)
                    html_path.write_text(page.content(), encoding="utf-8")
                    state_path.write_text(json.dumps({
                        "step": step,
                        "url": page.url,
                        "title": page.title(),
                        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    }, ensure_ascii=False, indent=2), encoding="utf-8")
                    screenshots.append(str(image_path))
                    evidence[f"{step}_html"] = str(html_path)
                    evidence[f"{step}_state"] = str(state_path)

                baseline_count = None
                if self.platform_code == "douyin":
                    page.goto("https://creator.douyin.com/creator-micro/content/manage", wait_until="domcontentloaded", timeout=90_000)
                    page.wait_for_timeout(3000)
                    match = re.search(r"共\s*(\d+)\s*个作品", page.locator("body").inner_text())
                    baseline_count = int(match.group(1)) if match else None
                page.goto(upload_url, wait_until="domcontentloaded", timeout=90_000)
                capture("01_upload_page")
                file_input = page.locator('input[type="file"]').first
                file_input.wait_for(state="attached", timeout=90_000)
                file_input.set_input_files(str(video_path.resolve()))
                if self.platform_code == "douyin":
                    title_field = page.locator('input[placeholder*="作品标题"]').first
                    description_field = page.locator('[contenteditable="true"][data-placeholder*="作品简介"]').first
                    title_field.wait_for(state="visible", timeout=120_000)
                    title_field.fill(title[:30])
                    description_field.wait_for(state="visible", timeout=30_000)
                    description_field.fill(description_text[:950])
                    cover_path = str(metadata.get("cover_path") or "").strip()
                    if cover_path and Path(cover_path).is_file():
                        # Douyin's image input opens a crop modal.  Setting the
                        # input alone does not persist the cover: the modal's
                        # exact Save button must be clicked and disappear.
                        cover_card = page.locator('[class*="coverControl"]').first
                        cover_background = cover_card.locator('[class*="background"]').first
                        before_cover_style = cover_background.get_attribute("style") if cover_background.count() else ""
                        cover_input = page.locator('input[type="file"][accept*="image"]').first
                        cover_input.wait_for(state="attached", timeout=30_000)
                        cover_input.set_input_files(str(Path(cover_path).resolve()))
                        confirm = page.get_by_role("button", name="保存", exact=True).last
                        confirm.wait_for(state="visible", timeout=30_000)
                        # The local preview appears before the image has been
                        # uploaded to Douyin. Saving too early silently falls
                        # back to a video frame, so wait for the upload stage.
                        page.wait_for_timeout(7000)
                        confirm.click()
                        confirm.wait_for(state="hidden", timeout=30_000)
                        # Do not publish until the cover card receives a new
                        # remote image URL. This is the platform-side evidence
                        # that the selected cover was really persisted.
                        cover_deadline = time.time() + 30
                        cover_saved = False
                        while time.time() < cover_deadline:
                            current_style = cover_background.get_attribute("style") if cover_background.count() else ""
                            if current_style and "url(" in current_style and current_style != before_cover_style:
                                cover_saved = True
                                break
                            page.wait_for_timeout(1000)
                        if not cover_saved:
                            raise RuntimeError("封面上传后平台未保存，已停止发布")
                    tip = page.get_by_text("我知道了", exact=True)
                    if tip.count() and tip.is_visible():
                        tip.click()
                else:
                    title_candidates = page.locator('input[placeholder*="标题"], textarea[placeholder*="title" i]')
                    description_candidates = page.locator('textarea, [contenteditable="true"]')
                    for field, value in ((title_candidates.first, title[:150]), (description_candidates.first, description_text[:2000])):
                        try:
                            field.wait_for(state="visible", timeout=30_000)
                            field.fill(value)
                        except Exception:
                            continue
                capture("02_metadata_filled")
                publish_button = page.locator('button:visible').filter(has_text="发布").last
                publish_button.wait_for(state="visible", timeout=300_000)
                # Do not click while the platform is still processing the file.
                deadline = time.time() + 300
                while time.time() < deadline and publish_button.is_disabled():
                    page.wait_for_timeout(1000)
                if publish_button.is_disabled():
                    raise RuntimeError("视频仍在上传处理中，平台发布按钮未启用")
                publish_button.click()
                capture("03_publish_clicked")
                # Keep the browser visible until the platform itself confirms
                # publication. A click, URL change or closed dialog is not proof.
                deadline = time.time() + 600
                success_text = ""
                while time.time() < deadline:
                    for text in ERROR_TEXTS:
                        marker = page.get_by_text(text, exact=False).first
                        if marker.count() and marker.is_visible():
                            raise RuntimeError(marker.inner_text() or text)
                    for text in SUCCESS_TEXTS.get(self.platform_code, ("发布成功",)):
                        marker = page.get_by_text(text, exact=False).first
                        if marker.count() and marker.is_visible():
                            success_text = text
                            break
                    if success_text:
                        break
                    if self.platform_code == "douyin" and "/content/manage" in page.url:
                        body_text = page.locator("body").inner_text()
                        expected_title = title[:10]
                        count_match = re.search(r"共\s*(\d+)\s*个作品", body_text)
                        if baseline_count is None and expected_title in body_text:
                            success_text = "内容管理已找到新作品"
                            break
                        if baseline_count is not None and count_match and int(count_match.group(1)) > baseline_count:
                            success_text = f"作品数量由{baseline_count}增加到{int(count_match.group(1))}"
                            break
                    page.wait_for_timeout(1000)
                # An explicit platform success message or an increased work
                # count is already final evidence.  Only enter the slower
                # content-list verification when no success evidence appeared;
                # this prevents needless refreshes after a successful publish.
                if self.platform_code == "douyin" and not success_text:
                    # Final evidence gate: the newly submitted title must appear
                    # in the creator content list or the total must increase.
                    # Generic tabs such as "审核中" are never success evidence.
                    page.goto("https://creator.douyin.com/creator-micro/content/manage", wait_until="domcontentloaded", timeout=90_000)
                    deadline = time.time() + 300
                    expected_title = title[:10]
                    while time.time() < deadline:
                        marker = page.get_by_text(expected_title, exact=False).first
                        if baseline_count is None and marker.count() and marker.is_visible():
                            success_text = "内容管理已找到新作品"
                            break
                        body_text = page.locator("body").inner_text()
                        count_match = re.search(r"共\s*(\d+)\s*个作品", body_text)
                        if baseline_count is not None and count_match and int(count_match.group(1)) > baseline_count:
                            success_text = f"作品数量由{baseline_count}增加到{int(count_match.group(1))}"
                            break
                        page.wait_for_timeout(2000)
                        page.reload(wait_until="domcontentloaded", timeout=90_000)
                if not success_text:
                    raise RuntimeError("平台未确认发布成功，且内容管理中未找到新作品")
                capture("04_platform_confirmed")

                # A click or success toast is not a publish ID. Acceptance runs
                # must recover an actual platform work URL/ID from the result
                # page or the creator content list; never fabricate one.
                work_url = ""
                work_id = ""
                url_patterns = {
                    "douyin": r"https?://(?:www\.)?douyin\.com/video/(\d+)",
                    "xiaohongshu": r"https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/([0-9a-fA-F]+)",
                    "kuaishou": r"https?://(?:www\.)?kuaishou\.com/short-video/([0-9A-Za-z_-]+)",
                    "tiktok": r"https?://(?:www\.)?tiktok\.com/@[^/]+/video/(\d+)",
                }
                pattern = url_patterns.get(self.platform_code)
                candidate_urls = [page.url]
                for selector in ('a[href*="/video/"]', 'a[href*="/short-video/"]',
                                 'a[href*="/explore/"]', 'a[href*="/discovery/item/"]'):
                    links = page.locator(selector)
                    for index in range(min(links.count(), 80)):
                        href = links.nth(index).get_attribute("href")
                        if href:
                            candidate_urls.append(href)
                if pattern:
                    for candidate in candidate_urls:
                        match = re.search(pattern, candidate)
                        if match:
                            work_id, work_url = match.group(1), candidate
                            break
                if not work_id or not work_url:
                    raise RuntimeError("平台页面已显示发布完成，但未取得真实作品ID或作品链接，验收不能通过")
                if artifact_dir:
                    log_path = artifact_dir / "browser.log.json"
                    log_path.write_text(json.dumps(browser_log, ensure_ascii=False, indent=2), encoding="utf-8")
                    evidence["browser_log"] = str(log_path)
                return {
                    "id": work_id, "url": work_url, "verified": True, "signal": success_text,
                    "screenshots": screenshots, "artifacts": evidence,
                }
            except Exception:
                try:
                    capture("99_failure")
                    if artifact_dir:
                        log_path = artifact_dir / "browser.log.json"
                        log_path.write_text(json.dumps(browser_log, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
                raise
            finally:
                context.close()
