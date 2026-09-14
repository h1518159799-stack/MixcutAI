# -*- coding: utf-8 -*-
from datetime import datetime

from playwright.async_api import Playwright, async_playwright, Page
import os
import asyncio

from conf import LOCAL_CHROME_PATH, LOCAL_CHROME_HEADLESS
from utils.base_social_media import set_init_script
from utils.log import xiaohongshu_logger


def _env_enabled(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _debug_artifacts_enabled():
    return (
        _env_enabled("POSTFLOW_DEBUG_SHOTS")
        or _env_enabled("POSTFLOW_INSPECT")
        or _env_enabled("LW_INSPECT")
        or bool(os.environ.get("LW_SHOT_TAG"))
    )


async def _pubshot(page, tag):
    """按需保存发布前截图；截图失败不影响发布。"""
    if not _debug_artifacts_enabled():
        return
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        d = os.path.join(root, ".scratch", "publish-shots")
        os.makedirs(d, exist_ok=True)
        pre = os.environ.get("POSTFLOW_SHOT_TAG") or os.environ.get("LW_SHOT_TAG", "")
        name = f"{pre}_{tag}" if pre else tag
        await page.screenshot(path=os.path.join(d, f"{name}.png"), full_page=True)
    except Exception:
        pass


async def cookie_auth(account_file):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=LOCAL_CHROME_HEADLESS, executable_path=LOCAL_CHROME_PATH)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://creator.xiaohongshu.com/creator-micro/content/upload")
        try:
            await page.wait_for_url("https://creator.xiaohongshu.com/creator-micro/content/upload", timeout=5000)
        except:
            print("[+] 等待5秒 cookie 失效")
            await context.close()
            await browser.close()
            return False
        # 2024.06.17 抖音创作者中心改版
        if await page.get_by_text('手机号登录').count() or await page.get_by_text('扫码登录').count():
            print("[+] 等待5秒 cookie 失效")
            return False
        else:
            print("[+] cookie 有效")
            return True


async def xiaohongshu_setup(account_file, handle=False):
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            # Todo alert message
            return False
        xiaohongshu_logger.info('[+] cookie文件不存在或已失效，即将自动打开浏览器，请扫码登录，登陆后会自动生成cookie文件')
        await xiaohongshu_cookie_gen(account_file)
    return True


async def xiaohongshu_cookie_gen(account_file):
    async with async_playwright() as playwright:
        options = {
            'headless': LOCAL_CHROME_HEADLESS,
            'executable_path': LOCAL_CHROME_PATH,
        }
        # Make sure to run headed.
        browser = await playwright.chromium.launch(**options)
        # Setup context however you like.
        context = await browser.new_context()  # Pass any options
        context = await set_init_script(context)
        # Pause the page, and start recording manually.
        page = await context.new_page()
        await page.goto("https://creator.xiaohongshu.com/")
        await page.pause()
        # 点击调试器的继续，保存cookie
        await context.storage_state(path=account_file)


class XiaoHongShuVideo(object):
    def __init__(self, title, file_path, tags, publish_date: datetime, account_file, thumbnail_path=None, description=None):
        self.title = title  # 视频标题
        self.file_path = file_path
        self.tags = tags
        self.publish_date = publish_date
        self.account_file = account_file
        self.date_format = '%Y年%m月%d日 %H:%M'
        self.local_executable_path = LOCAL_CHROME_PATH
        self.headless = LOCAL_CHROME_HEADLESS
        self.thumbnail_path = thumbnail_path
        self.description = description  # 视频描述/正文

    async def set_schedule_time_xiaohongshu(self, page, publish_date):
        """设置定时发布时间 - 2026.02.03 重写"""
        xiaohongshu_logger.info("  [-] 正在设置定时发布时间...")
        xiaohongshu_logger.info(f"  [-] 目标时间: {publish_date}")
        
        # 0. 先关闭可能存在的弹窗（点击页面空白处）
        try:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            # 点击页面左上角空白处
            await page.mouse.click(100, 100)
            await asyncio.sleep(0.5)
        except:
            pass
        
        # 1. 点击定时发布开关
        try:
            # 定位定时发布区域的开关
            switch = page.locator('.post-time-wrapper .d-switch-simulator')
            if await switch.count() > 0:
                await switch.click(timeout=5000, force=True)
                xiaohongshu_logger.info("  [+] 定时发布开关已点击")
            else:
                # 备用选择器
                switch = page.locator('.post-time-wrapper').locator('.d-switch')
                await switch.click(timeout=5000)
                xiaohongshu_logger.info("  [+] 定时发布开关已点击 (备用)")
        except Exception as e:
            xiaohongshu_logger.warning(f"  [!] 点击定时发布开关失败: {e}")
            return
        
        # 2. 等待时间选择器出现
        await asyncio.sleep(2)
        
        # 3. 填写时间 - 只在 .post-time-wrapper 内查找输入框
        publish_date_hour = publish_date.strftime("%Y-%m-%d %H:%M")
        
        try:
            # 在定时发布区域内找时间输入框
            time_wrapper = page.locator('.post-time-wrapper')
            time_input = time_wrapper.locator('input[type="text"], input:not([type="checkbox"])')
            
            if await time_input.count() > 0:
                # 用 fill 直接设置值
                await time_input.first.click(timeout=3000)
                await asyncio.sleep(0.5)
                await time_input.first.fill(publish_date_hour)
                await page.keyboard.press("Enter")
                xiaohongshu_logger.info(f"  [+] 时间已设置: {publish_date_hour}")
            else:
                # 尝试点击区域后查找弹出的日期选择器
                date_picker = page.locator('.d-date-picker input, .el-date-editor input')
                if await date_picker.count() > 0:
                    await date_picker.first.fill(publish_date_hour)
                    await page.keyboard.press("Enter")
                    xiaohongshu_logger.info(f"  [+] 时间已设置 (date-picker): {publish_date_hour}")
                else:
                    xiaohongshu_logger.warning("  [!] 未找到时间输入框")
        except Exception as e:
            xiaohongshu_logger.warning(f"  [!] 设置时间失败: {e}")
        
        await asyncio.sleep(1)

    async def handle_upload_error(self, page):
        xiaohongshu_logger.info('视频出错了，重新上传中')
        await page.locator('div.progress-div [class^="upload-btn-input"]').set_input_files(self.file_path)

    async def upload(self, playwright: Playwright) -> None:
        # 使用 Chromium 浏览器启动一个浏览器实例
        if self.local_executable_path:
            browser = await playwright.chromium.launch(headless=self.headless, executable_path=self.local_executable_path)
        else:
            browser = await playwright.chromium.launch(headless=self.headless)
        # 创建一个浏览器上下文，使用指定的 cookie 文件
        context = await browser.new_context(
            viewport={"width": 1600, "height": 900},
            storage_state=f"{self.account_file}"
        )
        context = await set_init_script(context)

        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video")
        xiaohongshu_logger.info(f'[+]正在上传-------{self.title}.mp4')
        # 等待页面跳转到指定的 URL，没进入，则自动等待到超时
        xiaohongshu_logger.info(f'[-] 正在打开主页...')
        await page.wait_for_url("https://creator.xiaohongshu.com/publish/publish?from=homepage&target=video")
        # 点击 "上传视频" 按钮
        await page.locator("div[class^='upload-content'] input[class='upload-input']").set_input_files(self.file_path)

        # 等待视频上传完成 - 2026.02.03 修复：必须等"重新上传"按钮出现
        xiaohongshu_logger.info("  [-] 等待视频上传完成...")
        max_wait = 180  # 最多等待180秒（3分钟）
        for i in range(max_wait):
            try:
                # 只用"重新上传"按钮作为成功标识（最可靠）
                reupload_btn = page.locator('text="重新上传"')
                if await reupload_btn.count() > 0:
                    xiaohongshu_logger.info("[+] 检测到「重新上传」按钮，视频上传成功!")
                    # 额外等待5秒让页面和预览完全加载
                    xiaohongshu_logger.info("  [-] 等待页面稳定...")
                    await asyncio.sleep(5)
                    break
                
                # 兼容旧版 - 检测"上传成功"文字
                success_text = page.locator('text="上传成功"')
                if await success_text.count() > 0:
                    xiaohongshu_logger.info("[+] 检测到「上传成功」标识!")
                    await asyncio.sleep(2)
                    break
                
                print(f"  [-] 上传中... ({i+1}s)")
                await asyncio.sleep(1)
            except Exception as e:
                print(f"  [-] 检测出错: {str(e)}")
                await asyncio.sleep(1)
        else:
            xiaohongshu_logger.warning("  [!] 等待上传超时，尝试继续...")

        # 填充标题和话题 - 2026.02.03 更新选择器兼容新版页面
        await asyncio.sleep(1)
        xiaohongshu_logger.info(f'  [-] 正在填充标题和话题...')
        
        # 尝试多种标题选择器
        title_selectors = [
            'div.plugin.title-container input.d-text',  # 旧版
            'input[placeholder*="标题"]',               # placeholder 包含标题
            'textarea[placeholder*="标题"]',            # textarea 版本
            '.title-container input',                   # 通用
            'input.title-input',                        # 可能的 class
        ]
        
        title_filled = False
        for selector in title_selectors:
            try:
                el = page.locator(selector)
                if await el.count() > 0:
                    await el.first.fill(self.title[:30])
                    xiaohongshu_logger.info(f'  [+] 标题已填充 (selector: {selector})')
                    title_filled = True
                    break
            except Exception as e:
                continue
        
        if not title_filled:
            # 尝试通过 placeholder 文本定位
            try:
                # 找"填写标题"相关的输入框
                title_input = page.get_by_placeholder("填写标题", exact=False)
                if await title_input.count() > 0:
                    await title_input.first.fill(self.title[:30])
                    xiaohongshu_logger.info('  [+] 标题已填充 (placeholder)')
                    title_filled = True
            except:
                pass
        
        if not title_filled:
            xiaohongshu_logger.warning('  [!] 未找到标题输入框，跳过标题填充')
        
        # 填充正文（描述 + 话题）- 2026.02.03 添加描述内容支持
        await asyncio.sleep(0.5)
        desc_selectors = [
            '.ql-editor',                              # Quill 编辑器
            'div[contenteditable="true"]',             # contenteditable
            'textarea[placeholder*="正文"]',           # textarea
            'textarea[placeholder*="描述"]',           # 描述
        ]
        
        desc_filled = False
        for selector in desc_selectors:
            try:
                el = page.locator(selector)
                if await el.count() > 0:
                    # 先清空再填充
                    await el.first.click()
                    
                    # 1. 填充描述内容（如果有）
                    if self.description:
                        await el.first.type(self.description, delay=30)
                        await el.first.type("\n\n", delay=30)  # 换行
                        xiaohongshu_logger.info(f'  [+] 描述已填充')
                    
                    # 2. 填充话题标签
                    for tag in self.tags:
                        await el.first.type("#" + tag + " ", delay=50)
                    
                    xiaohongshu_logger.info(f'  [+] 正文和话题已填充 (selector: {selector})')
                    desc_filled = True
                    break
            except Exception as e:
                xiaohongshu_logger.info(f'  [-] 尝试 {selector} 失败: {e}')
                continue
        
        if desc_filled:
            xiaohongshu_logger.info(f'总共添加{len(self.tags)}个话题')
        else:
            xiaohongshu_logger.warning('  [!] 未找到正文输入框，跳过话题填充')

        # while True:
        #     # 判断重新上传按钮是否存在，如果不存在，代表视频正在上传，则等待
        #     try:
        #         #  新版：定位重新上传
        #         number = await page.locator('[class^="long-card"] div:has-text("重新上传")').count()
        #         if number > 0:
        #             xiaohongshu_logger.success("  [-]视频上传完毕")
        #             break
        #         else:
        #             xiaohongshu_logger.info("  [-] 正在上传视频中...")
        #             await asyncio.sleep(2)

        #             if await page.locator('div.progress-div > div:has-text("上传失败")').count():
        #                 xiaohongshu_logger.error("  [-] 发现上传出错了... 准备重试")
        #                 await self.handle_upload_error(page)
        #     except:
        #         xiaohongshu_logger.info("  [-] 正在上传视频中...")
        #         await asyncio.sleep(2)
        
        # 上传视频封面(2026-08-15 修:此调用之前被注释=小红书封面从来没设过)
        if self.thumbnail_path:
            try:
                await self.set_thumbnail(page, self.thumbnail_path)
                xiaohongshu_logger.success("  [-] 小红书自定义封面已设置")
            except Exception as e:
                xiaohongshu_logger.warning(f"  [-] 小红书封面设置失败(降级默认封面继续发): {e}")
            # 关键(2026-08-15 修卡死):封面流程无论成败,关掉可能残留的弹窗,否则挡住发布按钮=不自动点发布
            try:
                for _ in range(3):
                    modal = page.locator("div.semi-modal-content:visible, div.semi-modal:visible, div[role='dialog']:visible")
                    if await modal.count() == 0:
                        break
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(500)
                await page.mouse.click(50, 50)  # 点页面空白兜底关弹窗
                await page.wait_for_timeout(300)
            except Exception:
                pass

        # 更换可见元素
        # await self.set_location(page, "青岛市")

        # # 頭條/西瓜
        # third_part_element = '[class^="info"] > [class^="first-part"] div div.semi-switch'
        # # 定位是否有第三方平台
        # if await page.locator(third_part_element).count():
        #     # 检测是否是已选中状态
        #     if 'semi-switch-checked' not in await page.eval_on_selector(third_part_element, 'div => div.className'):
        #         await page.locator(third_part_element).locator('input.semi-switch-native-control').click()

        if self.publish_date != 0:
            await self.set_schedule_time_xiaohongshu(page, self.publish_date)

        # 自动尝试设置原创与 AI 合成内容声明；失败不阻断发布。
        await self.set_aigc_declaration(page)
        # 发布前最后一道闸:任何残留弹窗(原创声明确认框等)都会全屏遮罩挡住发布按钮=发不出去,强制关掉
        await self._dismiss_blocking_modal(page)
        await _pubshot(page, "xhs_pre_submit")  # 提交前整页:核对封面/原创+AI合成声明/定时是否都设上
        # 检查模式只验证填写结果，不点击发布。保留旧环境变量作为兼容别名。
        if _env_enabled("POSTFLOW_INSPECT") or _env_enabled("LW_INSPECT"):
            xiaohongshu_logger.info("  [i] POSTFLOW_INSPECT=1 → 只验证不发布，已保存提交前截图")
            await context.storage_state(path=self.account_file)
            await context.close()
            await browser.close()
            return

        # 判断视频是否发布成功（patch 2026-05-16 v3：多 selector fallback 链）
        # 小红书新版红按钮可能是 <div role="button"> 或 Semi Design 的 .semi-button-primary，不是原生 <button>
        max_retries = 40
        retry_count = 0
        publish_clicked = False
        publish_success = False
        btn_text = "定时发布" if self.publish_date != 0 else "发布"
        # patch 2026-05-16 v4：关键发现 — 小红书发布按钮是 Web Component <xhs-publish-btn>
        # 实测 outerHTML: <xhs-publish-btn submit-text="定时发布" submit-disabled="false">
        # 不是 <button>，所有 has-text("定时发布") 类 selector 永不匹配
        selector_candidates = [
            f'xhs-publish-btn[submit-text="{btn_text}"][submit-disabled="false"]',  # 精准 + 可点击
            f'xhs-publish-btn[submit-text="{btn_text}"]',  # 自定义元素 attribute 匹配
            'xhs-publish-btn',  # 自定义元素兜底（页面唯一）
            'button:has-text("发布笔记")',  # 2026-08 新版可能改文案
            f'button.semi-button-primary:has-text("{btn_text}")',  # 旧版 Semi Design 兜底
            f'button.red:has-text("{btn_text}")',  # 红按钮
            f'[class*="publish"][class*="btn"]:has-text("{btn_text}")',  # class 含 publish/btn
            f'[class*="submit"]:has-text("{btn_text}")',  # class 含 submit
            f'[role="button"]:has-text("{btn_text}")',  # ARIA role 兜底
            f'button:has-text("{btn_text}")',  # 原生 button 兜底
        ]

        async def try_click_publish():
            """点发布按钮。关键(2026-08-15 据实测DOM):发布键在 <xhs-publish-btn> 的【闭合 shadow DOM】
            (<template shadowrootmode=closed>)里,普通/文本 selector 穿不进闭合 shadow → 之前点不着。
            正解=用 host 元素坐标点"发布"(底部bar里居中两按钮:暂存离开|发布红键,发布≈中心右移72px)。"""
            # 1) 闭合 shadow:host 坐标点击(主)
            try:
                host = page.locator('xhs-publish-btn').first
                if await host.count() > 0 and await host.is_visible():
                    disabled = await host.get_attribute("submit-disabled")
                    loading = await host.get_attribute("submit-loading")
                    if disabled == "true" or loading == "true":
                        return None  # 未就绪/发布中,别乱点
                    box = await host.bounding_box()
                    if box and box['width'] > 200:
                        x = box['x'] + box['width'] / 2 + 72   # 红色"发布"在右
                        y = box['y'] + box['height'] / 2
                        await page.mouse.click(x, y)
                        return 'xhs-publish-btn[host-coord]'
            except Exception:
                pass
            # 2) 兜底:老版/其它形态按钮(闭合 shadow 下多半无效,留作向后兼容)
            for sel in selector_candidates:
                try:
                    locator = page.locator(sel)
                    if await locator.count() > 0:
                        target = locator.last
                        if await target.is_visible():
                            await target.click(timeout=3000)
                            return sel
                except Exception:
                    continue
            return None

        async def try_confirm_dialog():
            """点发布后小红书可能弹二次确认框(确认发布/继续/同意)——之前没处理→框挡着不跳转卡死。每轮点掉。"""
            dlg_selectors = [
                '.semi-modal-content .semi-button-primary',
                '.semi-modal-content button:has-text("确认发布")',
                '.semi-modal-content button:has-text("继续发布")',
                '.semi-modal-content button:has-text("确认")',
                '.semi-modal-content button:has-text("确定")',
                '.semi-modal-content button:has-text("发布")',
                '[role="dialog"] .semi-button-primary',
                '[role="dialog"] button:has-text("确认")',
            ]
            for sel in dlg_selectors:
                try:
                    btn = page.locator(sel)
                    if await btn.count() > 0 and await btn.last.is_visible():
                        await btn.last.click(timeout=2500)
                        xiaohongshu_logger.info(f"  [+] 已点掉二次确认弹窗: {sel}")
                        return True
                except Exception:
                    continue
            return False

        while retry_count < max_retries:
            retry_count += 1
            try:
                # 检测当前 URL 是否已跳转到 success 页面
                current_url = page.url
                if "/publish/success" in current_url or "/new/home" in current_url:
                    xiaohongshu_logger.success(f"  [-]视频发布成功（URL 已跳转: {current_url}）")
                    publish_success = True
                    break

                if not publish_clicked:
                    clicked_sel = await try_click_publish()
                    if clicked_sel:
                        publish_clicked = True
                        xiaohongshu_logger.info(f"  [+] 已点击发布按钮（selector: {clicked_sel}），等待跳转...")
                    elif retry_count == 8:
                        # 诊断:8 次还点不着发布按钮 → dump 所有含"发布"的可点元素(定位新版按钮)
                        try:
                            await page.screenshot(path='/tmp/xhs_publish_debug.png')
                            cand = page.locator('button, xhs-publish-btn, [role="button"], [class*="publish"], [class*="submit"]')
                            info = []
                            for i in range(min(await cand.count(), 30)):
                                el = cand.nth(i)
                                try:
                                    if await el.is_visible():
                                        t = (((await el.inner_text()) or '').strip().replace('\n', ''))[:12]
                                        tag = await el.evaluate("e=>e.tagName.toLowerCase()")
                                        cls = (((await el.get_attribute('class')) or ''))[:36]
                                        if '发布' in t or 'publish' in cls or 'submit' in cls or tag == 'xhs-publish-btn':
                                            info.append(f"<{tag}|{t}|{cls}>")
                                except Exception:
                                    continue
                            xiaohongshu_logger.warning(f"  [!] 发布按钮点不着,可点元素dump: {' '.join(info) or '(无)'} 截图/tmp/xhs_publish_debug.png")
                        except Exception:
                            pass
                else:
                    # 已点过，检查 URL 或按钮是否消失
                    still_exists = False
                    for sel in selector_candidates:
                        try:
                            if await page.locator(sel).count() > 0:
                                still_exists = True
                                break
                        except Exception:
                            pass
                    if not still_exists:
                        xiaohongshu_logger.success("  [-]视频发布成功（按钮已消失）")
                        publish_success = True
                        break

                # 发布后可能弹二次确认框(之前卡死在这:框不提交),每轮都尝试点掉
                if publish_clicked:
                    await try_confirm_dialog()

                # 等 URL 变化（兜底）
                await page.wait_for_url(
                    "https://creator.xiaohongshu.com/publish/success?**",
                    timeout=3000
                )
                xiaohongshu_logger.success("  [-]视频发布成功（wait_for_url）")
                publish_success = True
                break
            except Exception:
                xiaohongshu_logger.info(f"  [-] 视频正在发布中... ({retry_count}/{max_retries})")
                await asyncio.sleep(0.5)
        if not publish_success:
            xiaohongshu_logger.warning("  [!] 等待发布确认超时，视频可能已排定时（去小红书工作台手动确认）")

        await context.storage_state(path=self.account_file)  # 保存cookie
        xiaohongshu_logger.success('  [-]cookie更新完毕！')
        await asyncio.sleep(2)  # 这里延迟是为了方便眼睛直观的观看
        # 关闭浏览器上下文和浏览器实例
        await context.close()
        await browser.close()
    
    async def _dump_modal(self, page, name):
        """把当前弹窗(小红书 d-modal / semi-modal)的 outerHTML 存到 .scratch/publish-shots/,失败不阻断。
        用于在页面结构变化后定位选择器问题。"""
        if not _debug_artifacts_enabled():
            return
        try:
            html = await page.evaluate(
                "() => { const m = document.querySelector('.d-modal-mask,.d-modal,div.semi-modal-content,[role=\"dialog\"]'); return m ? m.outerHTML : ''; }")
            if html:
                d = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".scratch", "publish-shots")
                os.makedirs(d, exist_ok=True)
                pre = os.environ.get("POSTFLOW_SHOT_TAG") or os.environ.get("LW_SHOT_TAG", "")
                fn = f"{pre}_{name}.html" if pre else f"{name}.html"
                with open(os.path.join(d, fn), "w", encoding="utf-8") as f:
                    f.write(html)
        except Exception:
            pass

    async def _dismiss_blocking_modal(self, page):
        """发布前兜底:若还有弹窗(d-modal-mask 等)盖住页面 → 关掉它,否则全屏遮罩挡住发布按钮=一直发不出去。
        先点关闭 ×,再退回 Escape;返回 True=已无遮挡弹窗。(2026-08-16 原创声明框卡死教训的最后一道闸)"""
        sel = ".d-modal-mask, .d-modal, div.semi-modal-content:visible, div[role='dialog']:visible"
        for _ in range(4):
            try:
                if await page.locator(sel).count() == 0:
                    return True
            except Exception:
                return True
            closed = False
            for x in [".d-modal-mask [class*='close']", ".d-modal [class*='close']",
                      "div[role='dialog'] [class*='close']", ".semi-modal-content .semi-modal-close"]:
                try:
                    b = page.locator(x).first
                    if await b.count() > 0 and await b.is_visible():
                        await b.click(timeout=1500)
                        closed = True
                        break
                except Exception:
                    continue
            if not closed:
                try:
                    await page.keyboard.press("Escape")
                except Exception:
                    pass
            await page.wait_for_timeout(500)
        try:
            return await page.locator(sel).count() == 0
        except Exception:
            return True

    async def _confirm_original_modal(self, page):
        """处理开原创声明后弹出的「笔记完成原创声明后,将获得以下权益」确认框:
        ⚠️ 关键(2026-08-16 实测 bug 真因):该框有【我已阅读并同意《原创声明须知》】勾选框,
        不勾则「声明原创」按钮灰着(disabled)点不动,框不关=d-modal-mask 全屏遮罩挡住发布按钮→一直发不出去。
        正解=①先勾同意(只点复选框本身,别点《原创声明须知》链接=会跳转)②再点已激活的「声明原创」。
        返回 True=框已消失(确认成功)。"""
        # 1) 勾「我已阅读并同意」复选框
        checked = False
        for cs in [".d-modal-mask .d-checkbox", ".d-modal .d-checkbox",
                   ".d-modal-mask input[type='checkbox']", ".d-modal input[type='checkbox']",
                   ".d-modal-mask [class*='checkbox']", ".d-modal [class*='checkbox']"]:
            try:
                box = page.locator(cs).first
                if await box.count() > 0 and await box.is_visible():
                    await box.click(timeout=2000, force=True)
                    checked = True
                    break
            except Exception:
                continue
        if not checked:
            # 兜底:点「我已阅读并同意」文字最左端(≈复选框位置,避开《原创声明须知》链接)
            try:
                t = page.locator(".d-modal-mask, .d-modal").get_by_text("我已阅读并同意", exact=False).first
                if await t.count() > 0:
                    b = await t.bounding_box()
                    if b:
                        await page.mouse.click(b['x'] - 12, b['y'] + b['height'] / 2)
                        checked = True
            except Exception:
                pass
        await page.wait_for_timeout(500)
        # 2) 点「声明原创」(勾选后应已激活);兼容 button / [class*=btn] 两种形态
        for t in ["声明原创", "同意并声明", "确定声明", "开启原创", "确定", "确认"]:
            try:
                b = page.locator(
                    f'.d-modal-mask button:has-text("{t}"), .d-modal button:has-text("{t}"), '
                    f'.d-modal-mask [class*="btn"]:has-text("{t}"), .d-modal [class*="btn"]:has-text("{t}")').last
                if await b.count() > 0 and await b.is_visible():
                    await b.click(timeout=2500)
                    await page.wait_for_timeout(900)
                    break
            except Exception:
                continue
        # 3) 判定框是否已关(关了=成功)
        try:
            return await page.locator(".d-modal-mask, .d-modal").count() == 0
        except Exception:
            return True

    async def set_aigc_declaration(self, page):
        """处理小红书原创与 AI 合成内容声明：
        ① 原创声明=开关,点开会弹「获得原创笔记标记」确认框——须勾同意+点「声明原创」才关(见 _confirm_original_modal);
        ② 添加内容类型声明=入口,点开选「笔记含AI合成内容」。
        都不阻断发布;确认框完不成也必须关掉,决不留遮罩挡发布按钮。"""
        # ① 原创声明开关:找"原创声明"文字同卡片里的开关,off 则点开
        try:
            label = page.locator('text=原创声明').first
            if await label.count() > 0:
                await label.scroll_into_view_if_needed(timeout=2000)
                card = label.locator("xpath=ancestor::div[contains(@class,'custom-switch-card')][1]")
                sw = card.locator('.d-switch').first
                if await sw.count() == 0:
                    sw = page.locator('.original-wrapper .d-switch, .custom-switch-card .d-switch').first
                if await sw.count() > 0:
                    if await sw.locator('.d-switch-simulator.checked').count() > 0:
                        xiaohongshu_logger.info("  [+] 原创声明已开")
                    else:
                        await sw.click(timeout=2500)
                        await page.wait_for_timeout(900)
                        # 开原创弹确认框:先 dump DOM 存证,再走勾同意+声明原创;完不成则关框保发布
                        if await page.locator(".d-modal-mask, .d-modal").count() > 0:
                            await self._dump_modal(page, "xhs_yuanchuang_modal")
                            if await self._confirm_original_modal(page):
                                xiaohongshu_logger.success("  [+] 已完成原创声明(勾同意+声明原创)")
                            else:
                                await self._dismiss_blocking_modal(page)
                                xiaohongshu_logger.warning("  [!] 原创声明确认框未完成→已关闭(本条可能未带原创标,但不挡发布)")
                        else:
                            xiaohongshu_logger.success("  [+] 已打开原创声明(无确认框)")
        except Exception as e:
            xiaohongshu_logger.info(f"  [-] 原创声明跳过: {e}")
            try:
                await self._dismiss_blocking_modal(page)
            except Exception:
                pass
        # ② 内容类型声明:点"添加内容类型声明"入口 → 选「笔记含AI合成内容」(→ 需要就点确定)
        try:
            entry = page.locator('text=添加内容类型声明').first
            if await entry.count() > 0 and await entry.is_visible():
                await entry.scroll_into_view_if_needed(timeout=2000)
                await entry.click(timeout=2500)
                await page.wait_for_timeout(1000)
                opt = page.locator('text=笔记含AI合成内容').first
                if await opt.count() > 0 and await opt.is_visible():
                    await opt.click(timeout=2500)
                    await page.wait_for_timeout(400)
                    for c in ["确定", "完成", "确认"]:
                        b = page.locator(f'button:has-text("{c}")').last
                        if await b.count() > 0 and await b.is_visible():
                            await b.click(timeout=1500)
                            break
                    xiaohongshu_logger.success("  [+] 已选内容类型声明:笔记含AI合成内容")
                else:
                    await page.keyboard.press("Escape")
                    xiaohongshu_logger.info("  [-] 未见AI合成选项,已关面板")
            elif await page.locator('text=笔记含AI合成内容').count() > 0:
                xiaohongshu_logger.info("  [+] 内容类型声明已是AI合成")
            else:
                xiaohongshu_logger.info("  [-] 未见'添加内容类型声明'入口,跳过")
        except Exception as e:
            try:
                await page.keyboard.press("Escape")
            except Exception:
                pass
            xiaohongshu_logger.info(f"  [-] 内容类型声明跳过: {e}")

    async def set_thumbnail(self, page: Page, thumbnail_path: str):
        """小红书自定义封面(2026-08-16 重写):旧版按 Semi Design(semi-upload-*)写的,而 XHS 创作台用的是
        d-design 体系,选择器全对不上=封面从来没上成。改法:①滚到表单顶+hover 视频预览让封面入口浮现;
        ②入口用『含"封面"字样』宽松匹配;③file input 直接全局找 input[type=file][accept*=image](隐藏也能 set);
        ④确认按钮跨 d-modal/semi/dialog 宽松匹配。任一步开不了弹窗就 dump DOM+截图,便于一次修准。"""
        if not thumbnail_path:
            return
        _d = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".scratch", "publish-shots")
        os.makedirs(_d, exist_ok=True)
        _pre = os.environ.get("POSTFLOW_SHOT_TAG") or os.environ.get("LW_SHOT_TAG", "")
        _pp = lambda n: os.path.join(_d, (f"{_pre}_{n}" if _pre else n))

        async def _dump(name):
            try:
                with open(_pp(name + ".html"), "w", encoding="utf-8") as f:
                    f.write(await page.evaluate("() => document.body.innerHTML"))
            except Exception:
                pass

        modal_sel = ".d-modal-mask, .d-modal, div.semi-modal-content:visible, [role='dialog']"
        # 0) 定位封面区 .cover-plugin-preview(实测 DOM:「设置封面」卡片),滚到可见
        cover_box = page.locator(".cover-plugin-preview").first
        try:
            if await cover_box.count() > 0:
                await cover_box.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        # 等封面处理完:别在「封面上传中」时点——注意该 label 常驻 DOM 隐藏,须判「可见」不是「存在」,最多等 12s
        for _ in range(24):
            try:
                lbl = page.locator(".cover-plugin-preview").get_by_text("封面上传中", exact=False).first
                if await lbl.count() == 0 or not await lbl.is_visible():
                    break
            except Exception:
                break
            await page.wait_for_timeout(500)
        # 1) 关键(实测):「修改封面」是封面缩略格上的浮层 .operator.pointer,opacity/pointer-events 受 hover 门控。
        #    force 坐标点会被传到底下的封面图(pointer-events:none)→Vue @click 不触发。正解=dispatch_event('click')
        #    直接触发处理器(绕过 pointer-events),兜底再走 hover+坐标点。
        opened = False
        try:
            await cover_box.hover(timeout=2000)
            await page.wait_for_timeout(400)
        except Exception:
            pass
        op = None
        for sel in [".cover-plugin-preview .operator.pointer", ".cover-plugin-preview .operator.noCover",
                    ".cover-plugin-preview .cover .operator", ".cover-plugin-preview .operator"]:
            if await page.locator(sel).count() > 0:
                op = page.locator(sel).first
                break
        if op is not None:
            for how in ["dispatch", "mouse", "force"]:
                try:
                    if how == "dispatch":
                        await op.dispatch_event("click")
                    elif how == "mouse":
                        await op.scroll_into_view_if_needed(timeout=1500)
                        box = await op.bounding_box()
                        if not box:
                            continue
                        cx, cy = box['x'] + box['width'] / 2, box['y'] + box['height'] / 2
                        await page.mouse.move(cx, cy)
                        await page.wait_for_timeout(250)
                        await page.mouse.click(cx, cy)
                    else:
                        await op.click(timeout=2000, force=True)
                    await page.wait_for_timeout(1400)
                    if await page.locator(modal_sel).count() > 0:
                        opened = True
                        xiaohongshu_logger.info(f"  [-] 小红书封面入口已打开(方式:{how})")
                        break
                except Exception:
                    continue
        if not opened:
            await _dump("xhs_cover_section")
            try:
                await page.screenshot(path=_pp("xhs_cover_fail.png"), full_page=True)
            except Exception:
                pass
            raise RuntimeError("小红书封面入口未打开(已 dump xhs_cover_section.html + xhs_cover_fail.png)")
        # 2) 弹窗打开 → dump 结构存证;切竖封面 tab(可选)
        await _dump("xhs_cover_modal_body")
        for tab in ["设置竖封面", "竖封面", "上传封面"]:
            try:
                tb = page.locator(f'text="{tab}"').first
                if await tb.count() > 0 and await tb.is_visible():
                    await tb.click(timeout=2000)
                    await page.wait_for_timeout(800)
                    break
            except Exception:
                continue
        # 3) 注入封面图:弹窗内 file input 优先,兜底全局 image 型 input(隐藏也能 set_input_files)
        injected = False
        for sel in ["div[role='dialog'] input[type='file']",
                    ".d-modal input[type='file']", ".d-modal-mask input[type='file']",
                    "div.semi-modal-content input[type='file']",
                    "input[type='file'][accept*='image']"]:
            try:
                inp = page.locator(sel)
                n = await inp.count()
                if n > 0:
                    await inp.last.set_input_files(thumbnail_path)
                    injected = True
                    xiaohongshu_logger.info(f"  [-] 小红书封面已注入: {sel}")
                    break
            except Exception:
                continue
        if not injected:
            await _dump("xhs_cover_modal_noinput")
            raise RuntimeError("小红书封面 file input 未找到(已 dump xhs_cover_modal_noinput.html)")
        await page.wait_for_timeout(2800)  # 等裁剪预览渲染
        # 4) 确认(跨 d-button/semi 宽松匹配 完成/确定/确认/保存)
        done = False
        for t in ["完成", "确定", "保存", "确认", "下一步"]:
            for scope in ["div[role='dialog']", ".d-modal", ".d-modal-mask", "div.semi-modal-content"]:
                try:
                    btn = page.locator(f'{scope} button:has-text("{t}"), {scope} [class*="btn"]:has-text("{t}")')
                    if await btn.count() > 0 and await btn.last.is_visible():
                        await btn.last.click(timeout=2500)
                        done = True
                        xiaohongshu_logger.info(f"  [-] 小红书封面已点[{t}]: {scope}")
                        break
                except Exception:
                    continue
            if done:
                break
        if not done:
            await _dump("xhs_cover_modal_noconfirm")
            try:
                await page.screenshot(path=_pp("xhs_cover_noconfirm.png"), full_page=True)
            except Exception:
                pass
            raise RuntimeError("小红书封面确认按钮未点着(已 dump xhs_cover_modal_noconfirm.html)")
        await page.wait_for_timeout(1500)
        # 存证:滚回封面区截图,核对自定义封面是否真应用上(应是斗笠金人,非黑色首帧)
        try:
            await cover_box.scroll_into_view_if_needed(timeout=1500)
            await page.wait_for_timeout(500)
            await page.screenshot(path=_pp("xhs_cover_applied.png"))
        except Exception:
            pass

    async def set_location(self, page: Page, location: str = "青岛市"):
        print(f"开始设置位置: {location}")
        
        # 点击地点输入框
        print("等待地点输入框加载...")
        loc_ele = await page.wait_for_selector('div.d-text.d-select-placeholder.d-text-ellipsis.d-text-nowrap')
        print(f"已定位到地点输入框: {loc_ele}")
        await loc_ele.click()
        print("点击地点输入框完成")
        
        # 输入位置名称
        print(f"等待1秒后输入位置名称: {location}")
        await page.wait_for_timeout(1000)
        await page.keyboard.type(location)
        print(f"位置名称输入完成: {location}")
        
        # 等待下拉列表加载
        print("等待下拉列表加载...")
        dropdown_selector = 'div.d-popover.d-popover-default.d-dropdown.--size-min-width-large'
        await page.wait_for_timeout(3000)
        try:
            await page.wait_for_selector(dropdown_selector, timeout=3000)
            print("下拉列表已加载")
        except:
            print("下拉列表未按预期显示，可能结构已变化")
        
        # 增加等待时间以确保内容加载完成
        print("额外等待1秒确保内容渲染完成...")
        await page.wait_for_timeout(1000)
        
        # 尝试更灵活的XPath选择器
        print("尝试使用更灵活的XPath选择器...")
        flexible_xpath = (
            f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
            f'//div[contains(@class, "d-options-wrapper")]'
            f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
            f'//div[contains(@class, "name") and text()="{location}"]'
        )
        await page.wait_for_timeout(3000)
        
        # 尝试定位元素
        print(f"尝试定位包含'{location}'的选项...")
        try:
            # 先尝试使用更灵活的选择器
            location_option = await page.wait_for_selector(
                flexible_xpath,
                timeout=3000
            )
            
            if location_option:
                print(f"使用灵活选择器定位成功: {location_option}")
            else:
                # 如果灵活选择器失败，再尝试原选择器
                print("灵活选择器未找到元素，尝试原始选择器...")
                location_option = await page.wait_for_selector(
                    f'//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    f'//div[contains(@class, "d-options-wrapper")]'
                    f'//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    f'/div[1]//div[contains(@class, "name") and text()="{location}"]',
                    timeout=2000
                )
            
            # 滚动到元素并点击
            print("滚动到目标选项...")
            await location_option.scroll_into_view_if_needed()
            print("元素已滚动到视图内")
            
            # 增加元素可见性检查
            is_visible = await location_option.is_visible()
            print(f"目标选项是否可见: {is_visible}")
            
            # 点击元素
            print("准备点击目标选项...")
            await location_option.click()
            print(f"成功选择位置: {location}")
            return True
            
        except Exception as e:
            print(f"定位位置失败: {e}")
            
            # 打印更多调试信息
            print("尝试获取下拉列表中的所有选项...")
            try:
                all_options = await page.query_selector_all(
                    '//div[contains(@class, "d-popover") and contains(@class, "d-dropdown")]'
                    '//div[contains(@class, "d-options-wrapper")]'
                    '//div[contains(@class, "d-grid") and contains(@class, "d-options")]'
                    '/div'
                )
                print(f"找到 {len(all_options)} 个选项")
                
                # 打印前3个选项的文本内容
                for i, option in enumerate(all_options[:3]):
                    option_text = await option.inner_text()
                    print(f"选项 {i+1}: {option_text.strip()[:50]}...")
                    
            except Exception as e:
                print(f"获取选项列表失败: {e}")
                
            # 截图保存（取消注释使用）
            # await page.screenshot(path=f"location_error_{location}.png")
            return False

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)

