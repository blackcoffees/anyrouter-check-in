#!/usr/bin/env python3
"""
签到执行器
"""

import asyncio
import json
import time

from urllib.parse import urljoin, urlparse

import httpx
from playwright.async_api import Page, TimeoutError as PlaywrightTimeoutError, async_playwright

from utils.config import ProviderConfig

DEFAULT_TIMEOUT_MS = 120000
PLAYWRIGHT_ARGS = [
	'--disable-blink-features=AutomationControlled',
	'--disable-dev-shm-usage',
	'--disable-web-security',
	'--disable-features=VizDisplayCompositor',
	'--no-sandbox',
]
DEFAULT_USER_AGENT = (
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
	'(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
)
DEFAULT_MODAL_DISMISS_TEXTS = ['知道了', '关闭', '确认']


class BrowserCheckInError(RuntimeError):
	"""浏览器签到失败异常，可携带登录态失效标记"""

	def __init__(self, message: str, *, expired: bool = False):
		super().__init__(message)
		self.expired = expired


def build_check_in_url(provider_config: ProviderConfig) -> str:
	"""构建签到页面地址"""
	if not provider_config.check_in_page_path:
		return provider_config.domain

	return urljoin(f'{provider_config.domain.rstrip("/")}/', provider_config.check_in_page_path.lstrip('/'))


def resolve_browser_check_in_url(provider_config: ProviderConfig, browser_check_in_url: str | None = None) -> str:
	if isinstance(browser_check_in_url, str) and browser_check_in_url.strip():
		return browser_check_in_url.strip()

	return build_check_in_url(provider_config)


def is_successful_check_in_response(response: httpx.Response) -> bool:
	"""判断签到接口是否成功"""
	if response.status_code != 200:
		return False

	try:
		result = response.json()
		return bool(result.get('ret') == 1 or result.get('code') == 0 or result.get('success'))
	except ValueError:
		return 'success' in response.text.lower()


def execute_api_check_in(client: httpx.Client, account_name: str, provider_config: ProviderConfig, headers: dict) -> bool:
	"""执行接口签到"""
	if not provider_config.sign_in_path:
		print(f'[FAILED] {account_name}: sign_in_path is not configured')
		return False

	print(f'[NETWORK] {account_name}: Executing API check-in')

	checkin_headers = headers.copy()
	checkin_headers.update({'Content-Type': 'application/json', 'X-Requested-With': 'XMLHttpRequest'})

	sign_in_url = urljoin(f'{provider_config.domain.rstrip("/")}/', provider_config.sign_in_path.lstrip('/'))
	response = client.post(sign_in_url, headers=checkin_headers, timeout=30)

	print(f'[RESPONSE] {account_name}: Response status code {response.status_code}')

	if is_successful_check_in_response(response):
		print(f'[SUCCESS] {account_name}: Check-in successful!')
		return True

	try:
		result = response.json()
		error_msg = result.get('msg', result.get('message', 'Unknown error'))
	except ValueError:
		error_msg = 'Invalid response format'

	print(f'[FAILED] {account_name}: Check-in failed - {error_msg}')
	return False


def get_timeout_ms(check_in_config: dict) -> int:
	"""读取页面签到超时时间"""
	try:
		return int(check_in_config.get('timeout_ms', DEFAULT_TIMEOUT_MS))
	except (TypeError, ValueError):
		return DEFAULT_TIMEOUT_MS


def get_required_config_value(check_in_config: dict, key: str, account_name: str) -> str | None:
	"""读取必填的页面签到配置"""
	value = check_in_config.get(key)
	if isinstance(value, str) and value.strip():
		return value.strip()

	print(f'[FAILED] {account_name}: check_in_config.{key} is required')
	return None


def get_config_string_list(check_in_config: dict, key: str) -> list[str]:
	value = check_in_config.get(key)
	if not isinstance(value, list):
		return []

	return [str(item).strip() for item in value if str(item).strip()]


async def wait_for_page_text(page: Page, text: str, timeout_ms: int):
	"""等待页面出现指定文本"""
	await page.wait_for_function(
		'expectedText => document.body && document.body.innerText.includes(expectedText)',
		arg=text,
		timeout=timeout_ms,
	)


async def wait_for_page_any_text(page: Page, texts: list[str], timeout_ms: int):
	"""等待页面出现任一指定文本"""
	await page.wait_for_function(
		'expectedTexts => document.body && expectedTexts.some(text => document.body.innerText.includes(text))',
		arg=texts,
		timeout=timeout_ms,
	)


async def get_page_body_text(page: Page) -> str:
	"""读取页面文本，失败时返回空字符串"""
	try:
		return await page.evaluate('() => document.body ? document.body.innerText : ""')
	except Exception:
		return ''


async def log_page_console_message(message):
	"""转发页面控制台消息"""
	try:
		print(f'[PAGE-CONSOLE] {message.type.upper()}: {message.text}')
	except Exception:
		pass


async def dismiss_modal_by_text(page: Page, text: str, account_name: str) -> bool:
	"""按按钮文本关闭模态框"""
	button = page.locator(f'button:has-text("{text}")').first
	if await button.count() == 0:
		return False

	try:
		if await button.is_visible():
			print(f'[PROCESSING] {account_name}: Dismissing modal by button text "{text}"')
			await button.click()
			await page.wait_for_timeout(500)
			return True
	except Exception as e:
		print(f'[WARNING] {account_name}: Failed to dismiss modal "{text}" - {str(e)[:80]}')

	return False


async def dismiss_known_overlays(page: Page, account_name: str, check_in_config: dict):
	"""关闭已知通知弹窗或蒙层"""
	dismissed = False
	dismiss_texts = check_in_config.get('modal_dismiss_texts') or DEFAULT_MODAL_DISMISS_TEXTS
	for text in dismiss_texts:
		if await dismiss_modal_by_text(page, text, account_name):
			dismissed = True

	modal_close_selectors = check_in_config.get('modal_close_selectors') or []
	for selector in modal_close_selectors:
		locator = page.locator(selector).first
		try:
			if await locator.count() > 0 and await locator.is_visible():
				print(f'[PROCESSING] {account_name}: Dismissing modal by selector {selector}')
				await locator.click()
				await page.wait_for_timeout(500)
				dismissed = True
		except Exception as e:
			print(f'[WARNING] {account_name}: Failed to click modal selector {selector} - {str(e)[:80]}')

	if dismissed:
		print(f'[INFO] {account_name}: Overlay dismissed before check-in')


async def wait_for_success_signal(page: Page, check_in_config: dict, timeout_ms: int):
	"""等待页面签到成功信号"""
	success_selector = check_in_config.get('success_selector')
	success_text = check_in_config.get('success_text')
	success_texts = check_in_config.get('success_texts')

	if success_selector:
		print(f'[PAGE-WAIT] Waiting success selector {success_selector}')
		await page.wait_for_selector(success_selector, timeout=timeout_ms)
		return

	if success_text:
		print(f'[PAGE-WAIT] Waiting success text {success_text}')
		await wait_for_page_text(page, success_text, timeout_ms)
		return

	if isinstance(success_texts, list) and success_texts:
		print(f'[PAGE-WAIT] Waiting success texts {success_texts}')
		await wait_for_page_any_text(page, [str(text) for text in success_texts if str(text).strip()], timeout_ms)
		return

	await page.wait_for_load_state('networkidle')
	await page.wait_for_timeout(1000)


async def is_success_signal_present(page: Page, check_in_config: dict) -> bool:
	"""非阻塞检查页面是否已经处于签到成功状态"""
	success_selector = check_in_config.get('success_selector')
	success_text = check_in_config.get('success_text')
	success_texts = check_in_config.get('success_texts')

	if success_selector:
		try:
			if await page.locator(success_selector).count() > 0:
				return True
		except Exception:
			pass

	if success_text:
		try:
			return bool(
				await page.evaluate(
					'expectedText => document.body && document.body.innerText.includes(expectedText)',
					success_text,
				)
			)
		except Exception:
			return False

	if isinstance(success_texts, list) and success_texts:
		try:
			return bool(
				await page.evaluate(
					'expectedTexts => document.body && expectedTexts.some(text => document.body.innerText.includes(text))',
					[str(text) for text in success_texts if str(text).strip()],
				)
			)
		except Exception:
			return False

	return False


async def detect_browser_login_required(page: Page, provider_config: ProviderConfig) -> str | None:
	"""检测页面是否已经回到登录态"""
	login_path = (provider_config.login_path or '/login').strip() or '/login'
	normalized_login_path = login_path if login_path.startswith('/') else f'/{login_path}'
	current_path = urlparse(page.url or '').path or '/'
	if current_path == normalized_login_path:
		return f'Browser page redirected to login page: {page.url}'

	expired_texts = get_config_string_list(provider_config.check_in_config or {}, 'expired_texts')
	if not expired_texts:
		return None

	page_text = await get_page_body_text(page)
	for text in expired_texts:
		if text in page_text:
			return f'Browser page shows login state, cookies/token may be expired: {text}'

	return None


async def is_selector_actionable(page: Page, selector: str) -> bool:
	"""非阻塞检查是否存在可见且可点击的元素"""
	try:
		locator = page.locator(selector)
		count = await locator.count()
		for index in range(count):
			candidate = locator.nth(index)
			try:
				if await candidate.is_visible() and await candidate.is_enabled():
					return True
			except Exception:
				continue
	except Exception:
		return False

	return False


async def wait_for_button_or_success(page: Page, button_selector: str, check_in_config: dict, timeout_ms: int):
	"""等待页面进入可点击或已成功状态，适配慢渲染 SPA"""
	deadline = time.monotonic() + timeout_ms / 1000
	print(f'[PAGE-WAIT] Waiting button or success state for {button_selector}')

	while time.monotonic() < deadline:
		if await is_success_signal_present(page, check_in_config):
			return 'success'
		if await is_selector_actionable(page, button_selector):
			return 'button'
		await page.wait_for_timeout(500)

	raise PlaywrightTimeoutError(f'Button or success state did not appear: {button_selector}')


async def find_preferred_locator(page: Page, selector: str, timeout_ms: int, require_enabled: bool = False):
	"""查找首个可见元素，必要时要求可点击"""
	await page.wait_for_selector(selector, timeout=timeout_ms)
	locator = page.locator(selector)
	deadline = time.monotonic() + timeout_ms / 1000

	while time.monotonic() < deadline:
		count = await locator.count()
		for index in range(count):
			candidate = locator.nth(index)
			try:
				if not await candidate.is_visible():
					continue
				if require_enabled and not await candidate.is_enabled():
					continue
				return candidate
			except Exception:
				continue

		await page.wait_for_timeout(200)

	raise PlaywrightTimeoutError(f'No visible locator matched selector: {selector}')


async def click_selector(page: Page, selector: str, timeout_ms: int):
	"""点击首个可见元素，失败时回退到强制点击"""
	locator = await find_preferred_locator(page, selector, timeout_ms, require_enabled=True)
	try:
		await locator.click(timeout=timeout_ms, no_wait_after=True)
		print(f'[PAGE-ACTION] Click completed for {selector}')
	except Exception as e:
		print(f'[WARNING] Click {selector} failed once, retry with force - {str(e)[:80]}')
		await locator.click(timeout=timeout_ms, force=True, no_wait_after=True)
		print(f'[PAGE-ACTION] Force click completed for {selector}')


async def wait_for_enabled_selector(page: Page, selector: str, timeout_ms: int):
	"""等待首个可见元素进入可点击状态"""
	await find_preferred_locator(page, selector, timeout_ms, require_enabled=True)


async def click_with_optional_response(page: Page, selector: str, check_in_config: dict, timeout_ms: int):
	"""点击元素并按需等待接口响应"""
	response_url_keyword = check_in_config.get('response_url_keyword')
	if response_url_keyword:
		print(f'[PAGE-ACTION] Click {selector}, waiting for response containing {response_url_keyword}')
		async with page.expect_response(lambda response: response_url_keyword in response.url, timeout=timeout_ms):
			await click_selector(page, selector, timeout_ms)
		return

	print(f'[PAGE-ACTION] Click {selector}')
	await click_selector(page, selector, timeout_ms)


async def navigate_to_check_in_page(page: Page, check_in_url: str, account_name: str, attempts: int = 3):
	"""进入签到页，失败时重试，避免偶发 about:blank 或网关抖动"""
	last_error = 'unknown navigation error'

	for attempt in range(1, attempts + 1):
		wait_until = 'domcontentloaded' if attempt == 1 else 'commit'
		try:
			await page.goto(check_in_url, wait_until=wait_until, timeout=60000)
		except Exception as e:
			last_error = str(e)[:120] or e.__class__.__name__
			print(f'[WARNING] {account_name}: Page.goto failed on attempt {attempt}/{attempts} - {last_error}')

		await page.wait_for_timeout(3000)
		if page.url and page.url != 'about:blank':
			return

		if attempt < attempts:
			print(f'[WARNING] {account_name}: Browser stayed on about:blank, retry navigation')
			await page.wait_for_timeout(attempt * 2000)

	raise BrowserCheckInError(
		f'Failed to open browser check-in page after {attempts} attempts, '
		f'current url is {page.url or "about:blank"}, last error: {last_error}'
	)


async def run_pre_click_selectors(page: Page, account_name: str, check_in_config: dict, timeout_ms: int):
	for selector in get_config_string_list(check_in_config, 'pre_click_selectors'):
		print(f'[PAGE-ACTION] {account_name}: Run pre-click selector {selector}')
		await click_selector(page, selector, timeout_ms)


async def resolve_preferred_flow(page: Page, account_name: str, check_in_config: dict, timeout_ms: int) -> dict:
	"""尝试优先签到流程（如惊喜签到），不可用时回退并返回默认配置"""
	preferred = check_in_config.get('preferred_flow')
	if not preferred or not isinstance(preferred, dict):
		return check_in_config

	pf_button = preferred.get('button_selector')
	if not isinstance(pf_button, str) or not pf_button.strip():
		return check_in_config

	pf_button = pf_button.strip()
	click_timeout = min(timeout_ms, 10000)

	pf_pre_clicks = get_config_string_list(preferred, 'pre_click_selectors')
	try:
		for selector in pf_pre_clicks:
			print(f'[PAGE-ACTION] {account_name}: Preferred flow pre-click {selector}')
			await click_selector(page, selector, click_timeout)

		await page.wait_for_timeout(1000)

		if await is_selector_actionable(page, pf_button):
			print(f'[INFO] {account_name}: Preferred flow available, using button {pf_button}')
			effective = dict(check_in_config)
			effective['button_selector'] = pf_button
			effective.pop('pre_click_selectors', None)
			effective.pop('preferred_flow', None)
			return effective
	except Exception as e:
		print(f'[WARNING] {account_name}: Preferred flow attempt failed - {str(e)[:80]}')

	print(f'[INFO] {account_name}: Preferred flow not available, falling back to default')
	for selector in get_config_string_list(preferred, 'fallback_pre_click_selectors'):
		try:
			print(f'[PAGE-ACTION] {account_name}: Fallback pre-click {selector}')
			await click_selector(page, selector, click_timeout)
		except Exception as e:
			print(f'[WARNING] {account_name}: Fallback click {selector} failed - {str(e)[:80]}')

	return check_in_config


async def execute_page_button_check_in_on_page(page: Page, account_name: str, check_in_config: dict) -> bool:
	"""执行页面按钮签到"""
	timeout_ms = get_timeout_ms(check_in_config)

	await dismiss_known_overlays(page, account_name, check_in_config)
	active_config = await resolve_preferred_flow(page, account_name, check_in_config, timeout_ms)

	button_selector = get_required_config_value(active_config, 'button_selector', account_name)
	if not button_selector:
		return False

	await run_pre_click_selectors(page, account_name, active_config, timeout_ms)
	state = await wait_for_button_or_success(page, button_selector, active_config, timeout_ms)
	if state == 'success':
		print(f'[INFO] {account_name}: Page already shows check-in success state')
		return True

	await click_with_optional_response(page, button_selector, active_config, timeout_ms)
	await wait_for_success_signal(page, active_config, timeout_ms)
	return True


async def select_challenge_difficulty(page: Page, account_name: str, check_in_config: dict, timeout_ms: int) -> bool:
	"""选择挑战难度"""
	option_selector = check_in_config.get('difficulty_option_selector')
	if option_selector:
		await click_selector(page, option_selector, timeout_ms)
		return True

	select_selector = check_in_config.get('difficulty_select_selector')
	difficulty_value = check_in_config.get('difficulty_value')
	if select_selector and difficulty_value is not None:
		await page.wait_for_selector(select_selector, timeout=timeout_ms)
		await page.select_option(select_selector, str(difficulty_value))
		return True

	input_selector = check_in_config.get('difficulty_input_selector')
	if input_selector and difficulty_value is not None:
		await page.wait_for_selector(input_selector, timeout=timeout_ms)
		await page.fill(input_selector, str(difficulty_value))
		return True

	print(f'[FAILED] {account_name}: difficulty selector is not configured')
	return False


async def wait_for_challenge_ready(page: Page, check_in_config: dict, timeout_ms: int):
	"""等待挑战计算结束"""
	ready_selector = check_in_config.get('ready_selector')
	if ready_selector:
		await page.wait_for_selector(ready_selector, timeout=timeout_ms)
		return

	ready_text = check_in_config.get('ready_text')
	if ready_text:
		await wait_for_page_text(page, ready_text, timeout_ms)
		return

	ready_wait_ms = check_in_config.get('ready_wait_ms')
	if ready_wait_ms is not None:
		await page.wait_for_timeout(int(ready_wait_ms))
		return


async def execute_page_challenge_check_in_on_page(page: Page, account_name: str, check_in_config: dict) -> bool:
	"""执行页面挑战签到"""
	timeout_ms = get_timeout_ms(check_in_config)
	await dismiss_known_overlays(page, account_name, check_in_config)

	if not await select_challenge_difficulty(page, account_name, check_in_config, timeout_ms):
		return False

	start_button_selector = get_required_config_value(check_in_config, 'start_button_selector', account_name)
	submit_button_selector = get_required_config_value(check_in_config, 'submit_button_selector', account_name)
	if not start_button_selector or not submit_button_selector:
		return False

	print(f'[PAGE-ACTION] Selected challenge difficulty for {account_name}')
	await click_with_optional_response(page, start_button_selector, check_in_config, timeout_ms)
	print(f'[PAGE-WAIT] Waiting challenge ready signal for {account_name}')
	await wait_for_challenge_ready(page, check_in_config, timeout_ms)

	await wait_for_enabled_selector(page, submit_button_selector, timeout_ms)
	print(f'[PAGE-ACTION] Click {submit_button_selector} after challenge is ready')
	await click_selector(page, submit_button_selector, timeout_ms)
	await wait_for_success_signal(page, check_in_config, timeout_ms)
	return True


def build_browser_cookies(provider_config: ProviderConfig, cookies: dict) -> list[dict]:
	"""构建 Playwright 所需 cookies"""
	parsed = urlparse(provider_config.domain)
	base_url = f'{parsed.scheme}://{parsed.netloc}'

	return [
		{
			'name': name,
			'value': value,
			'url': base_url,
			'httpOnly': False,
		}
		for name, value in cookies.items()
	]


async def inject_browser_local_storage(context, local_storage: dict[str, str] | None):
	"""向页面注入 localStorage 初始值"""
	if not local_storage:
		return

	entries_json = json.dumps(local_storage, ensure_ascii=False)
	await context.add_init_script(
		script=f"""
			(() => {{
				const entries = {entries_json};
				for (const [key, value] of Object.entries(entries)) {{
					window.localStorage.setItem(key, value);
				}}
			}})();
		""",
	)


async def apply_browser_headers(context, browser_headers: dict[str, str] | None):
	if not browser_headers:
		return

	await context.set_extra_http_headers(browser_headers)


async def execute_browser_check_in(
	account_name: str,
	provider_config: ProviderConfig,
	cookies: dict,
	browser_local_storage: dict[str, str] | None = None,
	browser_headers: dict[str, str] | None = None,
	browser_check_in_url: str | None = None,
) -> bool:
	"""执行页面签到"""
	check_in_url = resolve_browser_check_in_url(provider_config, browser_check_in_url)
	check_in_config = provider_config.check_in_config or {}

	print(f'[PROCESSING] {account_name}: Opening browser check-in page {check_in_url}')

	async with async_playwright() as playwright:
		browser = await playwright.chromium.launch(headless=False, args=PLAYWRIGHT_ARGS)
		context = await browser.new_context(
			user_agent=DEFAULT_USER_AGENT,
			viewport={'width': 1920, 'height': 1080},
			ignore_https_errors=True,
		)

		try:
			if cookies:
				await context.add_cookies(build_browser_cookies(provider_config, cookies))
				current_cookies = await context.cookies(check_in_url)
				print(
					f'[INFO] {account_name}: Browser context loaded cookies '
					f'{[cookie.get("name") for cookie in current_cookies]}'
				)
			else:
				print(f'[INFO] {account_name}: Browser context loaded cookies []')

			await inject_browser_local_storage(context, browser_local_storage)
			if browser_local_storage:
				print(
					f'[INFO] {account_name}: Browser localStorage initialized '
					f'{list(browser_local_storage.keys())}'
				)

			await apply_browser_headers(context, browser_headers)
			if browser_headers:
				print(
					f'[INFO] {account_name}: Browser extra headers initialized '
					f'{list(browser_headers.keys())}'
				)

			page = await context.new_page()
			page.on('console', lambda message: asyncio.create_task(log_page_console_message(message)))
			await navigate_to_check_in_page(page, check_in_url, account_name)
			print(f'[INFO] {account_name}: Browser current url {page.url}')
			try:
				page_title = await asyncio.wait_for(page.title(), timeout=5)
				print(f'[INFO] {account_name}: Browser page title {page_title[:120]}')
			except Exception as e:
				print(f'[WARNING] {account_name}: Failed to read page title - {str(e)[:80]}')

			login_reason = await detect_browser_login_required(page, provider_config)
			if login_reason:
				raise BrowserCheckInError(login_reason, expired=True)

			if not browser_check_in_url and provider_config.check_in_page_path and provider_config.check_in_page_path not in page.url:
				raise BrowserCheckInError(f'Browser is not on expected check-in page, current url is {page.url}')

			if provider_config.check_in_mode == 'page_button':
				print(f'[PROCESSING] {account_name}: Entering page_button flow')
				success = await execute_page_button_check_in_on_page(page, account_name, check_in_config)
			else:
				print(f'[PROCESSING] {account_name}: Entering page_challenge flow')
				success = await execute_page_challenge_check_in_on_page(page, account_name, check_in_config)

			if success:
				print(f'[SUCCESS] {account_name}: Browser check-in flow completed')

			return success
		except BrowserCheckInError:
			raise
		except Exception as e:
			raise BrowserCheckInError(f'Browser check-in failed - {str(e)[:80]}')
		finally:
			await context.close()
			await browser.close()


async def execute_check_in_action(
	client: httpx.Client,
	account_name: str,
	provider_config: ProviderConfig,
	headers: dict,
	cookies: dict,
	browser_local_storage: dict[str, str] | None = None,
	browser_headers: dict[str, str] | None = None,
	browser_check_in_url: str | None = None,
) -> bool:
	"""按 provider 配置执行签到动作"""
	if provider_config.check_in_mode == 'api_post':
		return execute_api_check_in(client, account_name, provider_config, headers)

	if provider_config.check_in_mode in ('page_button', 'page_challenge'):
		return await execute_browser_check_in(
			account_name,
			provider_config,
			cookies,
			browser_local_storage,
			browser_headers,
			browser_check_in_url,
		)

	print(f'[INFO] {account_name}: Check-in is completed by user info request')
	return True
