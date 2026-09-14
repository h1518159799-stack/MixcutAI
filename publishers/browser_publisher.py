from __future__ import annotations

from pathlib import Path
import time
import re
import json

from playwright.sync_api import sync_playwright

from browser_profile_service import find_chromium, find_system_browser
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
TIKTOK_PUBLISH_LABELS = ("发布", "Post", "Publish")


def _tiktok_visible_button_snapshot(page) -> list[dict]:
    """Return a safe DOM snapshot used for resilient TikTok button discovery."""
    return page.locator("button, [role='button']").evaluate_all("""
        nodes => nodes.map((node, index) => {
          const rect = node.getBoundingClientRect();
          const style = getComputedStyle(node);
          const text = (node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim();
          return {index, text, tag: node.tagName.toLowerCase(),
            testid: node.getAttribute('data-testid') || '',
            disabled: !!node.disabled || node.getAttribute('aria-disabled') === 'true',
            visible: !!(rect.width && rect.height) && style.visibility !== 'hidden' && style.display !== 'none',
            x: rect.x, y: rect.y, width: rect.width, height: rect.height};
        }).filter(item => item.visible)
    """)


def _tiktok_button_is_covered(page, index: int) -> bool:
    return bool(page.locator("button, [role='button']").nth(index).evaluate("""
        node => { const r = node.getBoundingClientRect();
          const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return !!hit && hit !== node && !node.contains(hit); }
    """))


def find_tiktok_publish_button(page):
    """Find a usable TikTok publish control without relying on one locale/selector."""
    nodes = _tiktok_visible_button_snapshot(page)
    labels = tuple(label.casefold() for label in TIKTOK_PUBLISH_LABELS)
    ranked = []
    for item in nodes:
        text = item["text"].casefold()
        testid = item["testid"].casefold()
        score = 0
        if any(label == text or label in text for label in labels): score += 100
        if any(key in testid for key in ("publish", "post", "submit")): score += 60
        if item["tag"] == "button": score += 20
        if not item["disabled"]: score += 10
        if score: ranked.append((score, item))
    for _score, item in sorted(ranked, key=lambda pair: (pair[0], pair[1]["y"]), reverse=True):
        locator = page.locator("button, [role='button']").nth(item["index"])
        if item["disabled"] or _tiktok_button_is_covered(page, item["index"]):
            continue
        return locator
    return None


def wait_for_tiktok_publish_button(page, artifact_dir: Path | None = None, timeout_seconds: int = 60):
    """Wait up to 60s, rescanning every 2s, and persist diagnostics on failure."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        button = find_tiktok_publish_button(page)
        if button is not None:
            return button
        page.wait_for_timeout(2000)
    if artifact_dir:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "tiktok_publish_button_diagnostics.json").write_text(json.dumps({
            "url": page.url, "title": page.title(),
            "buttons": page.locator("button, [role='button']").evaluate_all("""
                nodes => nodes.map(n => ({text: (n.innerText || n.textContent || '').trim(),
                testid: n.getAttribute('data-testid') || '', role: n.getAttribute('role') || '',
                disabled: !!n.disabled, visible: !!(n.offsetWidth || n.offsetHeight)}))
            """),
            "visible_buttons": _tiktok_visible_button_snapshot(page),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        page.screenshot(path=str(artifact_dir / "tiktok_publish_button_diagnostics.png"), full_page=True)
    raise RuntimeError("TikTok发布按钮未找到，已保存定位诊断")


def extract_douyin_work_items(payload: dict) -> list[dict]:
    result = []
    for item in payload.get("aweme_list") or []:
        work_id = str(item.get("aweme_id") or item.get("item_id") or "").strip()
        title_value = str(item.get("item_title") or item.get("desc") or "").strip()
        share_url = str(item.get("share_url") or "").strip()
        if work_id and title_value:
            result.append({"id": work_id, "title": title_value, "url": share_url,
                           "create_time": int(item.get("create_time") or 0),
                           "duration": float(item.get("duration") or 0) / 1000.0})
    return result


def match_douyin_work(items: list[dict], expected_title: str, *, excluded_ids=(), created_after: int = 0,
                       expected_duration: float = 0):
    expected = re.sub(r"\s+", " ", str(expected_title or "").strip())
    excluded = {str(value) for value in excluded_ids}
    candidates = []
    for item in items:
        item_title = re.sub(r"\s+", " ", item["title"])
        if item["id"] in excluded or int(item.get("create_time") or 0) < int(created_after or 0):
            continue
        item_duration = float(item.get("duration") or 0)
        if expected_duration and item_duration and abs(item_duration - expected_duration) > 1.5:
            continue
        if item_title == expected or item_title.startswith(expected) or expected.startswith(item_title):
            candidates.append(item)
    if not candidates:
        return None
    result = max(candidates, key=lambda value: int(value.get("create_time") or 0))
    if not result.get("url"):
        result = {**result, "url": f"https://www.douyin.com/video/{result['id']}"}
    return result


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
        state_callback = metadata.get("state_callback")
        current_stage = "prechecking"

        def set_stage(stage: str) -> None:
            nonlocal current_stage
            current_stage = stage
            if callable(state_callback):
                state_callback(stage)

        # Douyin's current creator page renders works as JavaScript cards and
        # no longer exposes public /video/ anchors in the DOM.  The page's own
        # work-list response is the authoritative source for aweme_id and the
        # share URL, so keep only the returned work metadata (never cookies or
        # request headers).
        douyin_work_items: list[dict] = []

        def remember_douyin_works(response) -> None:
            if self.platform_code != "douyin" or "/janus/douyin/creator/pc/work_list" not in response.url:
                return
            try:
                payload = response.json()
            except Exception:
                return
            douyin_work_items.extend(extract_douyin_work_items(payload))

        def find_douyin_work(expected_title: str, *, excluded_ids=(), created_after: int = 0):
            return match_douyin_work(
                douyin_work_items, expected_title, excluded_ids=excluded_ids,
                created_after=created_after,
                expected_duration=float(metadata.get("video_duration") or 0),
            )

        with sync_playwright() as playwright:
            browser_executable = find_system_browser() if self.platform_code == "tiktok" else find_chromium()
            context = playwright.chromium.launch_persistent_context(
                str(profile), executable_path=str(browser_executable), headless=True,
                viewport={"width": 1280, "height": 820}, args=["--no-first-run"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.on("console", lambda message: browser_log.append({
                    "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "type": message.type,
                    "text": message.text,
                }))
                page.on("response", remember_douyin_works)

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
                baseline_work_ids: set[str] = set()
                if self.platform_code == "douyin":
                    page.goto("https://creator.douyin.com/creator-micro/content/manage", wait_until="domcontentloaded", timeout=90_000)
                    page.wait_for_timeout(3000)
                    body_text = page.locator("body").inner_text()
                    if "登录" in body_text[:800] and not douyin_work_items:
                        raise RuntimeError("抖音账号登录状态已失效")
                    match = re.search(r"共\s*(\d+)\s*个作品", body_text)
                    baseline_count = int(match.group(1)) if match else None
                    baseline_work_ids = {item["id"] for item in douyin_work_items}
                    recovery_after = int(metadata.get("recover_existing_after") or 0)
                    if recovery_after:
                        recovered = find_douyin_work(title, created_after=max(0, recovery_after - 300))
                        if recovered:
                            capture("00_existing_publication_confirmed")
                            return {
                                "id": recovered["id"], "url": recovered["url"],
                                "verified": True, "backend_confirmed": True,
                                "signal": "内容管理后台已找到原发布作品",
                                "recovered_existing": True, "screenshots": screenshots,
                                "artifacts": evidence,
                            }
                page.goto(upload_url, wait_until="domcontentloaded", timeout=90_000)
                capture("01_upload_page")
                file_input = page.locator('input[type="file"]').first
                file_input.wait_for(state="attached", timeout=90_000)
                set_stage("uploading")
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
                if self.platform_code == "tiktok":
                    # TikTok changes labels/roles across locales and upload
                    # phases.  Wait for the upload UI to settle, then rescan
                    # the live DOM instead of keeping a stale locator.
                    upload_deadline = time.time() + 60
                    while time.time() < upload_deadline:
                        body = page.locator("body").inner_text().casefold()
                        processing = any(marker in body for marker in ("uploading", "processing", "上传中", "处理中"))
                        description_present = bool(description_text) and bool(page.locator("textarea, [contenteditable='true']").count())
                        if not processing and (description_present or not description_text):
                            break
                        page.wait_for_timeout(2000)
                    publish_button = wait_for_tiktok_publish_button(page, artifact_dir, timeout_seconds=60)
                    if publish_button.is_disabled():
                        raise RuntimeError("TikTok发布按钮仍未启用，视频可能尚未上传完成")
                else:
                    publish_button = page.locator('button:visible').filter(has_text="发布").last
                    publish_button.wait_for(state="visible", timeout=300_000)
                    # Do not click while the platform is still processing the file.
                    deadline = time.time() + 300
                    while time.time() < deadline and publish_button.is_disabled():
                        page.wait_for_timeout(1000)
                    if publish_button.is_disabled():
                        raise RuntimeError("视频仍在上传处理中，平台发布按钮未启用")
                set_stage("submitting")
                try:
                    publish_button.scroll_into_view_if_needed()
                    publish_button.click(timeout=5000)
                except Exception:
                    if self.platform_code != "tiktok":
                        raise
                    # A transient overlay can win hit-testing between scan and
                    # click. Re-scan after the overlay settles; never force-click.
                    page.wait_for_timeout(2000)
                    publish_button = wait_for_tiktok_publish_button(page, artifact_dir, timeout_seconds=60)
                    publish_button.click(timeout=5000)
                capture("03_publish_clicked")
                # Keep the browser visible until the platform itself confirms
                # publication. A click, URL change or closed dialog is not proof.
                deadline = time.time() + 600
                success_text = ""
                # Immediate publish no longer enters the retired manual
                # confirmation state; keep the task in processing until the
                # platform returns its verified work id and URL.
                set_stage("processing")
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
                if self.platform_code == "douyin":
                    # A success toast may arrive before the page loads content
                    # management.  Always query the actual work list and match
                    # the newly created title, excluding everything that was
                    # present before upload.
                    if "/content/manage" not in page.url:
                        page.goto("https://creator.douyin.com/creator-micro/content/manage", wait_until="domcontentloaded", timeout=90_000)
                    deadline = time.time() + 180
                    while time.time() < deadline:
                        page.wait_for_timeout(1500)
                        matched_work = find_douyin_work(title, excluded_ids=baseline_work_ids)
                        if matched_work:
                            work_id, work_url = matched_work["id"], matched_work["url"]
                            break
                        page.reload(wait_until="domcontentloaded", timeout=90_000)
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
                if pattern and not work_id:
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
                    "id": work_id, "url": work_url, "verified": True,
                    "backend_confirmed": self.platform_code == "douyin",
                    "signal": success_text, "stage": current_stage,
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
