# -*- coding: utf-8 -*-
from datetime import datetime

from playwright.async_api import Playwright, async_playwright
import os
import asyncio

from conf import LOCAL_CHROME_PATH, LOCAL_CHROME_HEADLESS
from utils.base_social_media import set_init_script
from utils.files_times import get_absolute_path
from utils.log import tencent_logger
from browser_resolver import resolve_browser

_BROWSER_PATH, _BROWSER_SOURCE = resolve_browser()
print(f'BROWSER_SOURCE={_BROWSER_SOURCE}', flush=True)
print(f'BROWSER_PATH={_BROWSER_PATH}', flush=True)


def _env_enabled(name):
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


async def _pubshot(page, tag):
    """按需保存发布前截图；截图失败不影响发布。"""
    if not (_env_enabled("POSTFLOW_DEBUG_SHOTS") or os.environ.get("LW_SHOT_TAG")):
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


def format_str_for_short_title(origin_title: str) -> str:
    """
    格式化短标题：6-16字符
    视频号只支持：书名号《》、引号""、冒号：、加号+、问号？、百分号%、摄氏度°
    逗号用空格代替
    """
    import re
    
    # 先把逗号替换成空格
    title = origin_title.replace('，', ' ').replace(',', ' ')
    
    # 只保留：中文、字母、数字、空格 + 允许的符号（《》""：+？%°）
    allowed_pattern = r'[\u4e00-\u9fa5a-zA-Z0-9 《》""：\+？%°]'
    filtered_chars = re.findall(allowed_pattern, title)
    formatted_string = ''.join(filtered_chars).strip()
    
    # 合并连续空格
    formatted_string = re.sub(r'\s+', ' ', formatted_string)

    # 调整字符串长度
    if len(formatted_string) > 16:
        formatted_string = formatted_string[:16].rstrip()
    elif len(formatted_string) < 6:
        formatted_string += ' ' * (6 - len(formatted_string))

    return formatted_string


def format_description(desc: str, max_length: int = 500) -> str:
    """
    格式化描述文本：过滤不支持的特殊字符，限制长度
    """
    import re
    # 移除可能有问题的特殊字符（保留大部分中文标点和常用符号）
    # 移除 emoji 和特殊 unicode 字符
    cleaned = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9\s《》""''：:；;？?！!、，,。.…·\-—\(\)（）\[\]【】]', '', desc)
    
    # 合并连续空白
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    
    # 限制长度
    if len(cleaned) > max_length:
        cleaned = cleaned[:max_length-3] + '...'
    
    return cleaned


async def cookie_auth(account_file):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=LOCAL_CHROME_HEADLESS, executable_path=_BROWSER_PATH)
        context = await browser.new_context(storage_state=account_file)
        context = await set_init_script(context)
        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://channels.weixin.qq.com/platform/post/create")
        try:
            await page.wait_for_selector('div.title-name:has-text("微信小店")', timeout=5000)  # 等待5秒
            tencent_logger.error("[+] 等待5秒 cookie 失效")
            return False
        except:
            tencent_logger.success("[+] cookie 有效")
            return True


async def get_tencent_cookie(account_file):
    async with async_playwright() as playwright:
        options = {
            'args': [
                '--lang en-GB'
            ],
            'headless': LOCAL_CHROME_HEADLESS,  # Set headless option here
            'executable_path': _BROWSER_PATH,
        }
        # Make sure to run headed.
        browser = await playwright.chromium.launch(**options)
        # Setup context however you like.
        context = await browser.new_context()  # Pass any options
        # Pause the page, and start recording manually.
        context = await set_init_script(context)
        page = await context.new_page()
        await page.goto("https://channels.weixin.qq.com")
        await page.pause()
        # 点击调试器的继续，保存cookie
        await context.storage_state(path=account_file)


async def weixin_setup(account_file, handle=False):
    account_file = get_absolute_path(account_file, "tencent_uploader")
    if not os.path.exists(account_file) or not await cookie_auth(account_file):
        if not handle:
            # Todo alert message
            return False
        tencent_logger.info('[+] cookie文件不存在或已失效，即将自动打开浏览器，请扫码登录，登陆后会自动生成cookie文件')
        await get_tencent_cookie(account_file)
    return True


class TencentVideo(object):
    def __init__(self, title, file_path, tags, publish_date: datetime, account_file, category=None, is_draft=False, short_title=None, cover_path=None, require_cover=False):
        self.title = title  # 描述内容（显示在描述区）
        self.short_title = short_title  # 短标题（6-16字，用于搜索/话题等）
        self.file_path = file_path
        self.tags = tags
        self.publish_date = publish_date
        self.account_file = account_file
        self.category = category
        self.headless = LOCAL_CHROME_HEADLESS
        self.is_draft = is_draft  # 是否保存为草稿
        self.cover_path = cover_path  # 自定义封面图（可选；None=不改封面，向后兼容）
        # 封面硬要求开关：
        #   False（默认）→ 封面失败时降级为平台默认截帧封面继续发布；
        #   True → 封面失败【中止发布，绝不裸发】，在发表前抛异常让 upload() 提前退出。
        self.require_cover = require_cover
        self.local_executable_path = (
            LOCAL_CHROME_PATH
            if LOCAL_CHROME_PATH and os.path.exists(LOCAL_CHROME_PATH)
            else _BROWSER_PATH
        )

    async def set_schedule_time_tencent(self, page, publish_date):
        # 先尝试 nth(1)，失败则用 JS click 兜底
        try:
            label_element = page.locator("label").filter(has_text="定时").nth(1)
            await label_element.click(timeout=8000)
        except Exception:
            try:
                label_element = page.locator("label").filter(has_text="定时").first
                await label_element.click(timeout=8000)
            except Exception:
                await page.evaluate("""
                    () => {
                        const labels = [...document.querySelectorAll('label')];
                        const t = labels.find(l => l.textContent.includes('定时'));
                        if (t) t.click();
                    }
                """)
        tencent_logger.info(f"  [-] 设置定时发布: {publish_date}")

        await page.click('input[placeholder="请选择发表时间"]')
        await asyncio.sleep(0.5)

        str_month = str(publish_date.month) if publish_date.month > 9 else "0" + str(publish_date.month)
        current_month = str_month + "月"
        # 获取当前的月份
        page_month = await page.inner_text('span.weui-desktop-picker__panel__label:has-text("月")')

        # 检查当前月份是否与目标月份相同
        if page_month != current_month:
            await page.click('button.weui-desktop-btn__icon__right')
            tencent_logger.info(f"  [-] 切换到下个月")

        # 获取页面元素
        elements = await page.query_selector_all('table.weui-desktop-picker__table a')

        # 遍历元素并点击匹配的元素
        for element in elements:
            if 'weui-desktop-picker__disabled' in await element.evaluate('el => el.className'):
                continue
            text = await element.inner_text()
            if text.strip() == str(publish_date.day):
                await element.click()
                tencent_logger.info(f"  [-] 选择日期: {publish_date.day}日")
                break

        # 输入时间部分（小时:分钟）
        time_input = page.locator('input[placeholder="请选择时间"]')
        await time_input.click()
        await asyncio.sleep(0.3)
        
        # 格式化时间 HH:MM
        time_str = f"{publish_date.hour:02d}:{publish_date.minute:02d}"
        
        # 使用 fill 直接替换值（更可靠）
        await time_input.fill(time_str)
        tencent_logger.info(f"  [-] 设置时间: {time_str}")

        # 选择标题栏（令定时时间生效）
        await asyncio.sleep(0.3)
        await page.locator("div.input-editor").click()

    async def handle_upload_error(self, page):
        tencent_logger.info("视频出错了，重新上传中")
        await page.locator('div.media-status-content div.tag-inner:has-text("删除")').click()
        await page.get_by_role('button', name="删除", exact=True).click()
        file_input = page.locator('input[type="file"]')
        await file_input.set_input_files(self.file_path)

    async def upload(self, playwright: Playwright) -> None:
        # 使用 Chromium (这里使用系统内浏览器，用chromium 会造成h264错误
        # 浏览器内 host 映射(无 sudo,只影响本浏览器):当本机 split-DNS 解不出视频号某些
        # CDN 上传域名(如 res.wxqcloud.qq.com.cn)时,用 --host-resolver-rules 直接把这些
        # host 映到经 223.5.5.5 解出的 IP。通过环境变量 TENCENT_HOST_RESOLVER_RULES 传入,
        # 例:MAP res.wxqcloud.qq.com.cn 58.221.45.61,MAP aladin.wxqcloud.qq.com 114.237.67.57
        launch_kwargs = {"headless": self.headless, "executable_path": self.local_executable_path}
        _hrr = os.environ.get("TENCENT_HOST_RESOLVER_RULES")
        if _hrr:
            launch_kwargs["args"] = [f"--host-resolver-rules={_hrr}"]
            tencent_logger.info(f"  [-] 启用浏览器内 host 映射: {_hrr}")
        browser = await playwright.chromium.launch(**launch_kwargs)
        # 创建一个浏览器上下文，使用指定的 cookie 文件
        context = await browser.new_context(storage_state=f"{self.account_file}")
        context = await set_init_script(context)

        # 创建一个新的页面
        page = await context.new_page()
        # 访问指定的 URL
        await page.goto("https://channels.weixin.qq.com/platform/post/create")
        tencent_logger.info(f'[+]正在上传-------{self.title}.mp4')
        # 等待页面跳转到指定的 URL，没进入，则自动等待到超时
        await page.wait_for_url("https://channels.weixin.qq.com/platform/post/create")
        # 等待页面 JS 初始化完成（"页面初始化中" 提示消失）+ file input 出现
        # 之前固定 sleep 3s 不够，会卡在"未找到文件上传元素"
        try:
            await page.wait_for_selector('input[type="file"]', state='attached', timeout=30000)
            tencent_logger.info('  [-] 页面初始化完成，input[type=file] 已出现')
        except Exception as e:
            tencent_logger.error(f'  [-] 等待 file input 超时 30s：{e}')
        await asyncio.sleep(2)  # 多给点缓冲

        # 新修复：先找上传区域并点击，再找 file input
        # 尝试多种选择器定位上传区域
        upload_selectors = [
            'input[type="file"]',  # 直接找 file input
            'div.upload-area',  # 上传区域
            'div[class*="upload"]',  # 上传类名
            'button:has-text("上传视频")',  # 上传按钮
            'div:has-text("上传视频")',  # 上传文字区域
            'div.post-create-upload',  # 发布页上传区
        ]
        
        file_input = None
        for selector in upload_selectors:
            try:
                # 用 .first 规避 Playwright strict mode（视频号 div[class*="upload"] 匹配 7 个元素）
                locator = page.locator(selector)
                count = await locator.count()
                if count > 0:
                    tencent_logger.info(f'  [-] 找到上传元素：{selector} (count={count})')
                    if selector == 'input[type="file"]':
                        file_input = locator.first
                        break
                    else:
                        # 多元素时取第一个可见的，避免 strict mode violation
                        await locator.first.click()
                        await asyncio.sleep(2)
                        fi = page.locator('input[type="file"]')
                        if await fi.count() > 0:
                            file_input = fi.first
                            break
            except Exception as e:
                tencent_logger.debug(f'  [-] 选择器 {selector} 失败：{e}')
                continue
        
        # 如果还是找不到，尝试截图调试
        if not file_input or await file_input.count() == 0:
            tencent_logger.error(f'  [-] 未找到文件上传元素')
            # 截图调试
            await page.screenshot(path='/tmp/weixin_upload_debug.png')
            tencent_logger.info(f'  [-] 调试截图已保存：/tmp/weixin_upload_debug.png')
            # 打印页面 HTML 片段
            html = await page.content()
            tencent_logger.debug(f'页面 HTML 长度：{len(html)}')
        else:
            await file_input.set_input_files(self.file_path)
            tencent_logger.info(f'  [-] 视频文件已选择')
        # 填充标题和话题
        await self.add_title_tags(page)
        # 添加商品
        # await self.add_product(page)
        # 合集功能
        await self.add_collection(page)
        # 原创选择
        await self.add_original(page)
        # 检测上传状态
        await self.detect_upload_status(page)
        if self.publish_date != 0:
            await self.set_schedule_time_tencent(page, self.publish_date)
        # 添加短标题
        await self.add_short_title(page)
        # 上传自定义封面（视频上传完成后、发表之前；失败自动降级不阻断发布）
        await self.add_cover(page)

        await _pubshot(page, "weixin_pre_submit")  # 发表前整页:核对封面/定时是否设上
        await self.click_publish(page)

        await context.storage_state(path=f"{self.account_file}")  # 保存cookie
        tencent_logger.success('  [-]cookie更新完毕！')
        await asyncio.sleep(2)  # 这里延迟是为了方便眼睛直观的观看
        # 关闭浏览器上下文和浏览器实例
        await context.close()
        await browser.close()

    async def add_short_title(self, page):
        short_title_element = page.get_by_text("短标题", exact=True).locator("..").locator(
            "xpath=following-sibling::div").locator(
            'span input[type="text"]')
        if await short_title_element.count():
            # 优先使用传入的 short_title，否则从 title 生成
            if self.short_title:
                short_title = format_str_for_short_title(self.short_title)
            else:
                short_title = format_str_for_short_title(self.title)
            await short_title_element.fill(short_title)
            tencent_logger.info(f"  [-] 短标题: {short_title} (长度: {len(short_title)})")

    async def add_cover(self, page):
        """上传自定义封面（视频号发表页）。

        设计约束：
          - cover_path 为空 → 默认跳过；require_cover=True 时中止发布。
          - 上传失败 → require_cover=False 时降级为默认封面，True 时中止发布。
          - 视频号封面 DOM 随版本频繁变化，故入口/标签/输入/确认全程多选择器兜底。

        视频号封面区常见结构：
          发表页封面区 → 「更换封面 / 编辑封面」→ 弹出封面编辑器
          → 「上传封面 / 本地上传」标签 → 隐藏 input[type=file][accept*=image]
          → set_input_files → 裁剪预览 →「确定 / 完成」。
        """
        if not self.cover_path:
            if self.require_cover:
                raise ValueError("require_cover=True 时必须提供 cover_path")
            return
        if not os.path.exists(self.cover_path):
            if self.require_cover:
                raise FileNotFoundError(f"封面文件不存在: {self.cover_path}")
            tencent_logger.warning(f"  [-] 封面文件不存在，跳过自定义封面: {self.cover_path}")
            return

        tencent_logger.info(f"  [-] 开始上传自定义封面: {self.cover_path}")
        try:
            # ---- 1) 打开封面编辑器（入口文案随版本变化，多文案兜底）----
            entry_texts = ["更换封面", "编辑封面", "更改封面", "设置封面"]
            opened = False
            for t in entry_texts:
                try:
                    loc = page.get_by_text(t, exact=False)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=4000)
                        tencent_logger.info(f"  [-] 已点击封面入口: {t}")
                        opened = True
                        break
                except Exception as e:
                    tencent_logger.debug(f"  [-] 封面入口 '{t}' 点击失败: {e}")
                    continue
            # 文案兜底失败，再试 class 结构兜底
            if not opened:
                class_entry_selectors = [
                    'div.cover-set-inner',
                    'div[class*="cover"][class*="set"]',
                    'div.finder-tag-wrap:has-text("封面")',
                    'div[class*="cover"]:has-text("封面")',
                ]
                for sel in class_entry_selectors:
                    try:
                        loc = page.locator(sel)
                        if await loc.count() > 0:
                            await loc.first.click(timeout=4000)
                            tencent_logger.info(f"  [-] 已点击封面入口(class): {sel}")
                            opened = True
                            break
                    except Exception:
                        continue
            if not opened:
                raise RuntimeError("未找到封面入口(更换封面/编辑封面等)")

            # 等封面编辑弹窗渲染
            await asyncio.sleep(1.5)

            # ---- 2) （可选）切到"上传封面/本地上传"标签，令图片 file input 出现 ----
            # 只用高辨识度文案，避免误点主页面的"上传视频"
            upload_tab_texts = ["上传封面", "本地上传", "上传图片", "从相册选择"]
            for t in upload_tab_texts:
                try:
                    loc = page.get_by_text(t, exact=False)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=3000)
                        tencent_logger.info(f"  [-] 已切换到封面上传标签: {t}")
                        await asyncio.sleep(0.8)
                        break
                except Exception:
                    continue

            # ---- 3) 定位图片 file input 并注入封面文件 ----
            # 关键：视频号封面走经典隐藏 <input type=file>（与视频上传同机制），
            # set_input_files 对隐藏 input 也有效，无需真正点开系统文件框。
            img_input = None
            img_input_selectors = [
                'input[type="file"][accept*="image"]',      # 首选：明确接受图片的 input
                '.weui-desktop-dialog input[type="file"]',  # 弹窗内的 file input
                'div[class*="cover"] input[type="file"]',   # 封面容器内
                'div[class*="dialog"] input[type="file"]',  # 通用弹窗内
            ]
            for sel in img_input_selectors:
                try:
                    loc = page.locator(sel)
                    if await loc.count() > 0:
                        img_input = loc.last  # 弹窗后新渲染的通常排在最后
                        tencent_logger.info(f"  [-] 找到封面 file input: {sel}")
                        break
                except Exception:
                    continue
            # 兜底：页面所有 file input，取最后一个（视频 input 通常在最前，封面 input 后出现）
            if img_input is None:
                all_inputs = page.locator('input[type="file"]')
                cnt = await all_inputs.count()
                if cnt > 1:
                    img_input = all_inputs.last
                    tencent_logger.info(f"  [-] 兜底取最后一个 file input(共 {cnt} 个)作封面输入")
            if img_input is None:
                raise RuntimeError("未找到封面图片 file input")

            await img_input.set_input_files(self.cover_path)
            tencent_logger.info("  [-] 封面图片已注入 file input")
            await asyncio.sleep(2)  # 等封面预览/裁剪区渲染

            # ---- 4) 确认/完成（视频号封面是"上传→裁剪→确认"，之前只找窄文案确认键→裁剪没确认=封面没保存）----
            await asyncio.sleep(2)  # 等裁剪区渲染稳定
            # 诊断:裁剪阶段先截图 + dump 弹窗所有可见按钮(便于定位真实确认键,版本变了看这个)
            try:
                await page.screenshot(path='/tmp/weixin_cover_crop.png')
                dbtns = page.locator('.weui-desktop-dialog button, .weui-desktop-dialog [role="button"]')
                labels = []
                for i in range(min(await dbtns.count(), 24)):
                    b = dbtns.nth(i)
                    try:
                        if await b.is_visible():
                            txt = ((await b.inner_text()) or '').strip().replace('\n', '')
                            cls = (await b.get_attribute('class')) or ''
                            if txt or 'primary' in cls:
                                labels.append(f"[{txt}|{cls[:32]}]")
                    except Exception:
                        continue
                tencent_logger.info(f"  [-] 封面弹窗可见按钮: {' '.join(labels) or '(空)'}")
            except Exception:
                pass
            confirmed = False
            # 4a) 优先点弹窗 footer 的主按钮(视频号裁剪确认键通常就是它),取最后一个可见的
            for sel in ['.weui-desktop-dialog__ft .weui-desktop-btn_primary',
                        '.weui-desktop-dialog .weui-desktop-btn_primary',
                        '.weui-desktop-dialog button.weui-desktop-btn_primary']:
                try:
                    btn = page.locator(sel)
                    if await btn.count() > 0 and await btn.last.is_visible():
                        await btn.last.click(timeout=3000)
                        tencent_logger.info(f"  [-] 已点击封面确认(主按钮): {sel}")
                        confirmed = True
                        break
                except Exception:
                    continue
            # 4b) 兜底:按文案找(扩到 应用/使用/裁剪/下一步)
            if not confirmed:
                for t in ["确定", "完成", "确认", "保存", "应用", "使用", "裁剪", "下一步"]:
                    try:
                        btn = page.locator(f'.weui-desktop-dialog button:has-text("{t}")')
                        if await btn.count() == 0:
                            btn = page.get_by_role("button", name=t, exact=True)
                        if await btn.count() > 0 and await btn.last.is_visible():
                            await btn.last.click(timeout=3000)
                            tencent_logger.info(f"  [-] 已点击封面确认: {t}")
                            confirmed = True
                            break
                    except Exception:
                        continue
            # 4c) 二级确认:有的版本裁剪确认后回到封面选择弹窗,还需再点一次"确认/完成"应用到发表页
            if confirmed:
                await asyncio.sleep(1.5)
                for t in ["确认", "完成", "确定"]:
                    try:
                        btn = page.locator(f'.weui-desktop-dialog__ft button:has-text("{t}")')
                        if await btn.count() == 0:
                            btn = page.locator(f'.weui-desktop-dialog button:has-text("{t}")')
                        if await btn.count() > 0 and await btn.last.is_visible():
                            await btn.last.click(timeout=2500)
                            tencent_logger.info(f"  [-] 已点击封面二级确认: {t}")
                            break
                    except Exception:
                        continue
            # 判定:没点着确认键 ≠ 没保存。本版视频号封面【注入即应用】,编辑器会自动关闭,故没有独立确认键=正常。
            # 只有封面编辑弹窗【仍开着】却没点着确认,才是真没保存 → 才告警(别再假报"封面可能没保存"吓人)。
            if not confirmed:
                dialog_open = False
                try:
                    dlg = page.locator('.weui-desktop-dialog')
                    dialog_open = await dlg.count() > 0 and await dlg.last.is_visible()
                except Exception:
                    pass
                if dialog_open:
                    raise RuntimeError("封面编辑弹窗仍打开，未能确认封面")
                else:
                    tencent_logger.info("  [-] 封面编辑器已自动关闭(本版注入即应用),封面已生效")
                    confirmed = True

            await asyncio.sleep(1.5)
            if _env_enabled("POSTFLOW_DEBUG_SHOTS") or os.environ.get("LW_SHOT_TAG"):
                try:
                    await page.screenshot(path='/tmp/weixin_cover_after.png')
                    tencent_logger.info("  [-] 封面完成后截图: /tmp/weixin_cover_after.png")
                except Exception:
                    pass
            tencent_logger.success("  [-] 自定义封面上传流程完成")
        except Exception as e:
            # 先截图定位失败点（无论是否硬要求都留证）
            try:
                await page.screenshot(path='/tmp/weixin_cover_debug.png')
                tencent_logger.info("  [-] 封面失败调试截图已保存: /tmp/weixin_cover_debug.png")
            except Exception:
                pass
            if self.require_cover:
                # 硬要求：封面失败即中止发布，绝不裸发（在 click_publish 之前抛出）
                tencent_logger.error(
                    f"  [-] 封面为硬要求(require_cover=True)但上传失败 → 中止发布，绝不无封面裸发: {e}")
                raise RuntimeError(f"封面上传失败且封面为硬要求，已中止发布(未发表): {e}")
            # 非硬要求：降级为默认截帧封面继续发布（向后兼容）
            try:
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(300)
            except Exception:
                pass
            tencent_logger.warning(f"  [-] 自定义封面上传失败，降级为默认封面继续发布: {e}")

    async def click_publish(self, page):
        while True:
            try:
                if self.is_draft:
                    # 点击"保存草稿"按钮
                    draft_button = page.locator('div.form-btns button:has-text("保存草稿")')
                    if await draft_button.count():
                        await draft_button.click()
                    # 等待跳转到草稿箱页面或确认保存成功
                    await page.wait_for_url("**/post/list**", timeout=5000)  # 使用通配符匹配包含post/list的URL
                    tencent_logger.success("  [-]视频草稿保存成功")
                else:
                    # 点击"发表"按钮
                    publish_button = page.locator('div.form-btns button:has-text("发表")')
                    if await publish_button.count():
                        await publish_button.click()
                    await page.wait_for_url("https://channels.weixin.qq.com/platform/post/list", timeout=5000)
                    tencent_logger.success("  [-]视频发布成功")
                break
            except Exception as e:
                current_url = page.url
                if self.is_draft:
                    # 检查是否在草稿相关的页面
                    if "post/list" in current_url or "draft" in current_url:
                        tencent_logger.success("  [-]视频草稿保存成功")
                        break
                else:
                    # 检查是否在发布列表页面
                    if "https://channels.weixin.qq.com/platform/post/list" in current_url:
                        tencent_logger.success("  [-]视频发布成功")
                        break
                tencent_logger.exception(f"  [-] Exception: {e}")
                tencent_logger.info("  [-] 视频正在发布中...")
                await asyncio.sleep(0.5)

    async def detect_upload_status(self, page):
        # 重试上限补丁（2026-05-13）：防止 detect/handle_upload_error 无限循环
        # 之前 EP100 卡 460+ 次重试，每次 handle_upload_error 重新 set_input_files
        # 导致视频号工作台积累大量失败草稿；超过 max_error_retry 抛异常让上层 chain 跳过
        max_error_retry = 5
        error_retry_count = 0
        while True:
            # 匹配删除按钮，代表视频上传完毕，如果不存在，代表视频正在上传，则等待
            try:
                # 匹配删除按钮，代表视频上传完毕
                if "weui-desktop-btn_disabled" not in await page.get_by_role("button", name="发表").get_attribute(
                        'class'):
                    tencent_logger.info("  [-]视频上传完毕")
                    break
                else:
                    tencent_logger.info("  [-] 正在上传视频中...")
                    await asyncio.sleep(2)
                    # 出错了视频出错
                    if await page.locator('div.status-msg.error').count() and await page.locator(
                            'div.media-status-content div.tag-inner:has-text("删除")').count():
                        error_retry_count += 1
                        if error_retry_count > max_error_retry:
                            tencent_logger.error(
                                f"  [-] 上传重试 {max_error_retry} 次仍失败 — 放弃本集，抛异常让 chain 跳过下一集")
                            raise RuntimeError(
                                f"视频号上传失败超过 {max_error_retry} 次重试上限（可能是风控/UI改版/网络）")
                        tencent_logger.error(
                            f"  [-] 发现上传出错了...准备第 {error_retry_count}/{max_error_retry} 次重试")
                        await self.handle_upload_error(page)
            except RuntimeError:
                raise  # 让重试上限异常上抛
            except:
                tencent_logger.info("  [-] 正在上传视频中...")
                await asyncio.sleep(2)

    async def add_title_tags(self, page):
        # Metadata fields are optional across Channels UI revisions. Give each
        # candidate a short bounded attempt; never block publication when the
        # editor is absent or temporarily unavailable.
        editor = None
        for selector in ("div.input-editor", "textarea", "[contenteditable='true']", "[role='textbox']"):
            try:
                candidate = page.locator(selector).first
                await candidate.wait_for(state="visible", timeout=1200)
                editor = candidate
                break
            except Exception:
                continue
        if editor is None:
            tencent_logger.warning("简介/标题控件未找到，跳过填写继续发表 (description_found=false, description_skipped=true, title_found=false, title_skipped=true)")
            return
        try:
            await editor.click(timeout=1200)
            formatted_title = format_description(self.title)
            await page.keyboard.type(formatted_title)
            await page.keyboard.press("Enter")
            for tag in self.tags:
                await page.keyboard.type("#" + tag)
                await page.keyboard.press("Space")
            tencent_logger.info(f"成功添加描述: {formatted_title[:30]}... (description_found=true, title_found=true)")
            tencent_logger.info(f"成功添加hashtag: {len(self.tags)}")
        except Exception as exc:
            tencent_logger.warning(f"标题/简介填写跳过，继续发表: {type(exc).__name__}: {str(exc)[:200]} (description_skipped=true, title_skipped=true)")

    async def add_collection(self, page):
        collection_elements = page.get_by_text("添加到合集").locator("xpath=following-sibling::div").locator(
            '.option-list-wrap > div')
        if await collection_elements.count() > 1:
            await page.get_by_text("添加到合集").locator("xpath=following-sibling::div").click()
            await collection_elements.first.click()

    async def add_original(self, page):
        if await page.get_by_label("视频为原创").count():
            await page.get_by_label("视频为原创").check()
        # 检查 "我已阅读并同意 《视频号原创声明使用条款》" 元素是否存在
        label_locator = await page.locator('label:has-text("我已阅读并同意 《视频号原创声明使用条款》")').is_visible()
        if label_locator:
            await page.get_by_label("我已阅读并同意 《视频号原创声明使用条款》").check()
            await page.get_by_role("button", name="声明原创").click()
        # 2023年11月20日 wechat更新: 可能新账号或者改版账号，出现新的选择页面
        if await page.locator('div.label span:has-text("声明原创")').count() and self.category:
            # 因处罚无法勾选原创，故先判断是否可用
            if not await page.locator('div.declare-original-checkbox input.ant-checkbox-input').is_disabled():
                await page.locator('div.declare-original-checkbox input.ant-checkbox-input').click()
                if not await page.locator(
                        'div.declare-original-dialog label.ant-checkbox-wrapper.ant-checkbox-wrapper-checked:visible').count():
                    await page.locator('div.declare-original-dialog input.ant-checkbox-input:visible').click()
            if await page.locator('div.original-type-form > div.form-label:has-text("原创类型"):visible').count():
                await page.locator('div.form-content:visible').click()  # 下拉菜单
                await page.locator(
                    f'div.form-content:visible ul.weui-desktop-dropdown__list li.weui-desktop-dropdown__list-ele:has-text("{self.category}")').first.click()
                await page.wait_for_timeout(1000)
            if await page.locator('button:has-text("声明原创"):visible').count():
                await page.locator('button:has-text("声明原创"):visible').click()

    async def main(self):
        async with async_playwright() as playwright:
            await self.upload(playwright)
