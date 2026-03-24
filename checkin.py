#!/usr/bin/env python3
"""
AnyRouter.top 自动签到脚本
"""

import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

from utils.checkin_executor import DEFAULT_USER_AGENT, PLAYWRIGHT_ARGS, execute_check_in_action
from utils.config import AccountConfig, AppConfig, load_accounts_config
from utils.notify import notify

load_dotenv()

BALANCE_HASH_FILE = 'balance_hash.txt'


def load_balance_hash():
	"""加载余额哈希"""
	try:
		if os.path.exists(BALANCE_HASH_FILE):
			with open(BALANCE_HASH_FILE, 'r', encoding='utf-8') as file:
				return file.read().strip()
	except Exception:
		pass
	return None


def save_balance_hash(balance_hash):
	"""保存余额哈希"""
	try:
		with open(BALANCE_HASH_FILE, 'w', encoding='utf-8') as file:
			file.write(balance_hash)
	except Exception as e:
		print(f'Warning: Failed to save balance hash: {e}')


def generate_balance_hash(balances):
	"""生成余额哈希"""
	simple_balances = {key: value['quota'] for key, value in balances.items()} if balances else {}
	balance_json = json.dumps(simple_balances, sort_keys=True, separators=(',', ':'))
	return hashlib.sha256(balance_json.encode('utf-8')).hexdigest()[:16]


def parse_cookies(cookies_data):
	"""解析 cookies 数据"""
	if isinstance(cookies_data, dict):
		return cookies_data

	if isinstance(cookies_data, str):
		cookies_dict = {}
		for cookie in cookies_data.split(';'):
			if '=' in cookie:
				key, value = cookie.strip().split('=', 1)
				cookies_dict[key] = value
		return cookies_dict

	return {}


async def get_waf_cookies_with_playwright(account_name: str, login_url: str, required_cookies: list[str]):
	"""使用 Playwright 获取 WAF cookies"""
	print(f'[PROCESSING] {account_name}: Starting browser to get WAF cookies...')

	async with async_playwright() as playwright:
		import tempfile

		with tempfile.TemporaryDirectory() as temp_dir:
			context = await playwright.chromium.launch_persistent_context(
				user_data_dir=temp_dir,
				headless=False,
				user_agent=DEFAULT_USER_AGENT,
				viewport={'width': 1920, 'height': 1080},
				args=PLAYWRIGHT_ARGS,
			)
			page = await context.new_page()

			try:
				print(f'[PROCESSING] {account_name}: Access login page to get initial cookies...')
				await page.goto(login_url, wait_until='networkidle')

				try:
					await page.wait_for_function('document.readyState === "complete"', timeout=5000)
				except Exception:
					await page.wait_for_timeout(3000)

				cookies = await page.context.cookies()
				waf_cookies = {}
				for cookie in cookies:
					cookie_name = cookie.get('name')
					cookie_value = cookie.get('value')
					if cookie_name in required_cookies and cookie_value is not None:
						waf_cookies[cookie_name] = cookie_value

				print(f'[INFO] {account_name}: Got {len(waf_cookies)} WAF cookies')

				missing_cookies = [cookie for cookie in required_cookies if cookie not in waf_cookies]
				if missing_cookies:
					print(f'[FAILED] {account_name}: Missing WAF cookies: {missing_cookies}')
					return None

				print(f'[SUCCESS] {account_name}: Successfully got all WAF cookies')
				return waf_cookies
			except Exception as e:
				print(f'[FAILED] {account_name}: Error occurred while getting WAF cookies: {e}')
				return None
			finally:
				await context.close()


def get_user_info(client, headers, user_info_url: str, provider_config):
	"""获取用户信息"""
	try:
		response = client.get(user_info_url, headers=headers, timeout=30)

		if response.status_code == 200:
			try:
				data = response.json()
			except ValueError:
				content_type = response.headers.get('content-type', '')
				body_preview = response.text.strip().replace('\n', ' ')[:120]
				return {
					'success': False,
					'error': (
						f'Failed to get user info: invalid JSON response, '
						f'content-type={content_type}, body={body_preview}'
					),
				}

			if provider_config.user_info_mode == 'sign_status':
				success_field = provider_config.user_info_success_field or 'signedInToday'
				signed_in_today = bool(data.get(success_field))
				return {
					'success': signed_in_today,
					'display': f'[CHECK-IN] Signed in today: {"yes" if signed_in_today else "no"}',
				}

			if provider_config.user_info_mode == 'record_status':
				record_field = provider_config.user_info_success_field or 'today_record'
				record = data.get(record_field)
				signed_in_today = bool(record)
				display = f'[CHECK-IN] Today record exists: {"yes" if signed_in_today else "no"}'
				if isinstance(record, dict) and record.get('difficulty_key'):
					display += f', difficulty={record.get("difficulty_key")}'
				return {
					'success': signed_in_today,
					'display': display,
				}

			if data.get('success'):
				user_data = data.get('data', {})
				quota = round(user_data.get('quota', 0) / 500000, 2)
				used_quota = round(user_data.get('used_quota', 0) / 500000, 2)
				return {
					'success': True,
					'quota': quota,
					'used_quota': used_quota,
					'display': f':money: Current balance: ${quota}, Used: ${used_quota}',
				}

		return {'success': False, 'error': f'Failed to get user info: HTTP {response.status_code}'}
	except Exception as e:
		return {'success': False, 'error': f'Failed to get user info: {str(e)[:50]}...'}


def build_request_headers(provider_config, api_user: str) -> dict:
	"""构建通用请求头"""
	return {
		'User-Agent': DEFAULT_USER_AGENT,
		'Accept': 'application/json, text/plain, */*',
		'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
		'Accept-Encoding': 'identity',
		'Referer': provider_config.domain,
		'Origin': provider_config.domain,
		'Connection': 'keep-alive',
		'Sec-Fetch-Dest': 'empty',
		'Sec-Fetch-Mode': 'cors',
		'Sec-Fetch-Site': 'same-origin',
		provider_config.api_user_key: api_user,
	}


def fetch_user_info(client, provider_config, headers: dict):
	"""查询用户信息并打印结果"""
	if provider_config.user_info_mode == 'none' or not provider_config.user_info_path:
		return None

	user_info_url = f'{provider_config.domain}{provider_config.user_info_path}'
	user_info = get_user_info(client, headers, user_info_url, provider_config)

	if user_info and user_info.get('display'):
		print(user_info['display'])
	elif user_info and user_info.get('success'):
		print(user_info['display'])
	elif user_info:
		print(user_info.get('error', 'Unknown error'))

	return user_info


async def prepare_cookies(account_name: str, provider_config, user_cookies: dict) -> dict | None:
	"""准备请求所需 cookies"""
	waf_cookies = {}

	if provider_config.needs_waf_cookies():
		login_url = f'{provider_config.domain}{provider_config.login_path}'
		waf_cookies = await get_waf_cookies_with_playwright(account_name, login_url, provider_config.waf_cookie_names)
		if not waf_cookies:
			print(f'[FAILED] {account_name}: Unable to get WAF cookies')
			return None
	else:
		print(f'[INFO] {account_name}: Bypass WAF not required, using user cookies directly')

	return {**waf_cookies, **user_cookies}


async def check_in_account(account: AccountConfig, account_index: int, app_config: AppConfig):
	"""为单个账号执行签到"""
	account_name = account.get_display_name(account_index)
	print(f'\n[PROCESSING] Starting to process {account_name}')

	provider_config = app_config.get_provider(account.provider)
	if not provider_config:
		print(f'[FAILED] {account_name}: Provider "{account.provider}" not found in configuration')
		return False, None

	print(
		f'[INFO] {account_name}: Using provider "{account.provider}" '
		f'({provider_config.domain}, mode={provider_config.check_in_mode})'
	)

	user_cookies = parse_cookies(account.cookies)
	has_browser_local_storage = bool(account.browser_local_storage)
	if not user_cookies and not has_browser_local_storage:
		print(f'[FAILED] {account_name}: Invalid configuration format')
		return False, None

	all_cookies = await prepare_cookies(account_name, provider_config, user_cookies)
	if all_cookies is None:
		return False, None

	client = httpx.Client(http2=True, timeout=30.0)

	try:
		client.cookies.update(all_cookies)
		headers = build_request_headers(provider_config, account.api_user)

		if provider_config.user_info_mode in ('sign_status', 'record_status'):
			before_user_info = fetch_user_info(client, provider_config, headers)
			if before_user_info and before_user_info.get('success'):
				print(f'[INFO] {account_name}: Already signed in today, skip browser check-in flow')
				return True, before_user_info

		if not provider_config.needs_manual_check_in():
			user_info = fetch_user_info(client, provider_config, headers)
			success = bool(user_info and user_info.get('success'))
			if success:
				print(f'[INFO] {account_name}: Check-in completed automatically (triggered by user info request)')
			return success, user_info

		success = await execute_check_in_action(
			client,
			account_name,
			provider_config,
			headers,
			all_cookies,
			account.browser_local_storage,
		)
		user_info = fetch_user_info(client, provider_config, headers)
		if provider_config.user_info_mode != 'none' and user_info is not None:
			success = success and bool(user_info.get('success'))
		return success, user_info
	except Exception as e:
		print(f'[FAILED] {account_name}: Error occurred during check-in process - {str(e)[:50]}...')
		return False, None
	finally:
		client.close()


async def main():
	"""主函数"""
	print('[SYSTEM] AnyRouter.top multi-account auto check-in script started (using Playwright)')
	print(f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')

	app_config = AppConfig.load_from_env()
	print(f'[INFO] Loaded {len(app_config.providers)} provider configuration(s)')

	accounts = load_accounts_config()
	if not accounts:
		print('[FAILED] Unable to load account configuration, program exits')
		sys.exit(1)

	print(f'[INFO] Found {len(accounts)} account configurations')

	last_balance_hash = load_balance_hash()
	success_count = 0
	total_count = len(accounts)
	notification_content = []
	current_balances = {}
	need_notify = False
	balance_changed = False

	for i, account in enumerate(accounts):
		account_key = f'account_{i + 1}'
		try:
			success, user_info = await check_in_account(account, i, app_config)
			if success:
				success_count += 1

			should_notify_this_account = False
			if not success:
				should_notify_this_account = True
				need_notify = True
				account_name = account.get_display_name(i)
				print(f'[NOTIFY] {account_name} failed, will send notification')

			if user_info and user_info.get('success'):
				if 'quota' in user_info and 'used_quota' in user_info:
					current_balances[account_key] = {'quota': user_info['quota'], 'used': user_info['used_quota']}

			if should_notify_this_account:
				account_name = account.get_display_name(i)
				status = '[SUCCESS]' if success else '[FAIL]'
				account_result = f'{status} {account_name}'
				if user_info and user_info.get('success'):
					account_result += f'\n{user_info["display"]}'
				elif user_info:
					account_result += f'\n{user_info.get("error", "Unknown error")}'
				notification_content.append(account_result)
		except Exception as e:
			account_name = account.get_display_name(i)
			print(f'[FAILED] {account_name} processing exception: {e}')
			need_notify = True
			notification_content.append(f'[FAIL] {account_name} exception: {str(e)[:50]}...')

	current_balance_hash = generate_balance_hash(current_balances) if current_balances else None
	if current_balance_hash:
		if last_balance_hash is None:
			balance_changed = True
			need_notify = True
			print('[NOTIFY] First run detected, will send notification with current balances')
		elif current_balance_hash != last_balance_hash:
			balance_changed = True
			need_notify = True
			print('[NOTIFY] Balance changes detected, will send notification')
		else:
			print('[INFO] No balance changes detected')

	if balance_changed:
		for i, account in enumerate(accounts):
			account_key = f'account_{i + 1}'
			if account_key in current_balances:
				account_name = account.get_display_name(i)
				account_result = f'[BALANCE] {account_name}'
				account_result += (
					f'\n:money: Current balance: ${current_balances[account_key]["quota"]}, '
					f'Used: ${current_balances[account_key]["used"]}'
				)
				if not any(account_name in item for item in notification_content):
					notification_content.append(account_result)

	if current_balance_hash:
		save_balance_hash(current_balance_hash)

	if need_notify and notification_content:
		summary = [
			'[STATS] Check-in result statistics:',
			f'[SUCCESS] Success: {success_count}/{total_count}',
			f'[FAIL] Failed: {total_count - success_count}/{total_count}',
		]

		if success_count == total_count:
			summary.append('[SUCCESS] All accounts check-in successful!')
		elif success_count > 0:
			summary.append('[WARN] Some accounts check-in successful')
		else:
			summary.append('[ERROR] All accounts check-in failed')

		time_info = f'[TIME] Execution time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
		notify_content = '\n\n'.join([time_info, '\n'.join(notification_content), '\n'.join(summary)])

		print(notify_content)
		notify.push_message('AnyRouter Check-in Alert', notify_content, msg_type='text')
		print('[NOTIFY] Notification sent due to failures or balance changes')
	else:
		print('[INFO] All accounts successful and no balance changes detected, notification skipped')

	sys.exit(0 if success_count > 0 else 1)


def run_main():
	"""运行主函数"""
	try:
		asyncio.run(main())
	except KeyboardInterrupt:
		print('\n[WARNING] Program interrupted by user')
		sys.exit(1)
	except Exception as e:
		print(f'\n[FAILED] Error occurred during program execution: {e}')
		sys.exit(1)


if __name__ == '__main__':
	run_main()
